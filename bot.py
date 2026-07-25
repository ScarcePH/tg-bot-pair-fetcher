from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from html import escape
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from cloud_tasks import FetchTaskQueue
from scraper import ScrapedLink, get_configured_marketplaces, scrape_link_results
from state import BotStateStore, SavedSearch, SeenLink


TELEGRAM_MESSAGE_LIMIT = 4096
MAX_LISTING_BUTTONS_PER_MESSAGE = 10

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramAlert:
    text: str
    reply_markup: InlineKeyboardMarkup


def require_env(name: str) -> str:
    value = os.getenv(name, '').strip()

    if not value:
        raise ValueError(f'{name} is required')

    return value


def format_scraped_links_for_telegram(
    saved_search: SavedSearch,
    links: list[ScrapedLink],
) -> list[TelegramAlert]:
    """Build HTML-safe subscriber alerts with native listing buttons."""
    if not links:
        return []

    listing_label = 'listing' if len(links) == 1 else 'listings'
    header = (
        '🔔 <b>NEW FIND</b>\n\n'
        f'<b>{escape(saved_search.name)}</b>\n\n'
        f'{len(links)} new {listing_label}'
    )
    if len(header) > TELEGRAM_MESSAGE_LIMIT:
        raise ValueError('A formatted alert exceeds the Telegram message limit')

    alerts: list[TelegramAlert] = []
    multiple_listings = len(links) > 1
    for offset in range(0, len(links), MAX_LISTING_BUTTONS_PER_MESSAGE):
        rows: list[list[InlineKeyboardButton]] = []
        for number, link in enumerate(
            links[offset:offset + MAX_LISTING_BUTTONS_PER_MESSAGE],
            start=offset + 1,
        ):
            label = (
                f'View listing {number} ↗'
                if multiple_listings
                else 'View listing ↗'
            )
            rows.append([InlineKeyboardButton(label, url=link.url)])

        alerts.append(
            TelegramAlert(
                text=header,
                reply_markup=InlineKeyboardMarkup(rows),
            )
        )

    return alerts


async def send_links(
    context: Any,
    chat_id: str,
    alerts: list[TelegramAlert],
) -> None:
    for alert in alerts:
        await context.bot.send_message(
            chat_id=chat_id,
            text=alert.text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=alert.reply_markup,
        )


def get_bot_data(context: Any) -> dict:
    application = getattr(context, 'application', context)
    return application.bot_data


async def run_fetch(
    context: Any,
    chat_id: str,
) -> bool:
    """Run the legacy all-SKU fetch path used by the standalone bot API."""
    state_store: BotStateStore = get_bot_data(context)['state_store']
    control_chat_id = str(get_bot_data(context)['chat_id'])
    with state_store.fetch_lock() as lock_acquired:
        if not lock_acquired:
            logger.info('Skipped fetch because another fetch is already running')
            return False

        saved_searches = state_store.list_saved_searches()
        completed = True

        for saved_search in saved_searches:
            try:
                scraped_links = await scrape_link_results(
                    item_queries=[saved_search.sku],
                    raise_on_error=True,
                )
                new_links = filter_new_links(state_store, scraped_links)
                await send_links(
                    context,
                    chat_id,
                    format_scraped_links_for_telegram(saved_search, new_links),
                )
                if new_links:
                    state_store.record_seen_links(
                        [
                            SeenLink(
                                url=link.url,
                                marketplace_key=link.marketplace_key,
                                query=link.query,
                            )
                            for link in new_links
                        ]
                    )
            except Exception:
                completed = False
                logger.exception('Fetch failed for SKU %s', saved_search.sku)
                await context.bot.send_message(
                    chat_id=control_chat_id,
                    text=(
                        f'Fetch failed for {saved_search.name} '
                        f'({saved_search.sku}). Check bot logs.'
                    ),
                )

    return completed


async def run_sku_fetch(
    context: Any,
    chat_id: str,
    saved_search: SavedSearch,
) -> bool:
    state_store: BotStateStore = get_bot_data(context)['state_store']
    control_chat_id = str(get_bot_data(context)['chat_id'])
    label = f'{saved_search.name} ({saved_search.sku})'

    with state_store.fetch_lock() as lock_acquired:
        if not lock_acquired:
            logger.info(
                'Skipped SKU fetch because another fetch is already running: %s',
                saved_search.sku,
            )
            return False

        try:
            scraped_links = await scrape_link_results(
                item_queries=[saved_search.sku],
                raise_on_error=True,
            )
            new_links = filter_new_links(state_store, scraped_links)

            if new_links:
                await send_links(
                    context,
                    chat_id,
                    format_scraped_links_for_telegram(
                        saved_search,
                        new_links,
                    ),
                )
                state_store.record_seen_links(
                    [
                        SeenLink(
                            url=link.url,
                            marketplace_key=link.marketplace_key,
                            query=link.query,
                        )
                        for link in new_links
                    ]
                )
        except Exception:
            logger.exception('Fetch failed for SKU %s', saved_search.sku)
            await context.bot.send_message(
                chat_id=control_chat_id,
                text=f'Fetch failed for {label}. Check bot logs.',
            )
            return False

    return True


def filter_new_links(state_store: BotStateStore, scraped_links: list[ScrapedLink]) -> list[ScrapedLink]:
    new_links = []
    seen_this_fetch = set()

    for link in scraped_links:
        if link.url in seen_this_fetch or state_store.has_seen_link(link.url):
            continue

        seen_this_fetch.add(link.url)
        new_links.append(link)

    return new_links


def format_saved_searches_message(saved_searches: list[SavedSearch]) -> str:
    if not saved_searches:
        return 'Saved SKUs\n\nNo saved SKUs.'

    lines = ['Saved SKUs', '']
    lines.extend(f'{search.sku} - {search.name}' for search in saved_searches)
    return '\n'.join(lines)


async def update_saved_searches_pin(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> None:
    state_store: BotStateStore = context.application.bot_data['state_store']
    text = format_saved_searches_message(state_store.list_saved_searches())
    message_id = state_store.get_pinned_saved_searches_message_id(chat_id)

    if message_id is not None:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
            return
        except TelegramError:
            logger.info('Could not update saved-SKU pinned message; creating a new one', exc_info=True)

    message = await context.bot.send_message(chat_id=chat_id, text=text)
    state_store.set_pinned_saved_searches_message_id(chat_id, message.message_id)

    try:
        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True,
        )
    except TelegramError:
        logger.info('Could not pin saved-SKU message', exc_info=True)


async def is_allowed_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_chat:
        return False

    chat_id = str(update.effective_chat.id)
    configured_chat_id = str(context.application.bot_data['chat_id'])

    return chat_id == configured_chat_id


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed_chat(update, context) or not update.effective_chat:
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text='Use /set <sku> <name> to save a SKU, /list to view saved SKUs, and /fetch to search now.',
    )


async def fetch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed_chat(update, context) or not update.effective_chat:
        return

    chat_id = str(update.effective_chat.id)
    await context.bot.send_message(chat_id=chat_id, text='Fetch started.')

    try:
        task_queue: FetchTaskQueue = context.application.bot_data[
            'fetch_task_queue'
        ]
        await task_queue.enqueue_manual_fetch(chat_id, update.update_id)
    except Exception:
        logger.exception('Could not enqueue manual fetch task')
        await context.bot.send_message(
            chat_id=chat_id,
            text='Could not start fetch. Check bot logs.',
        )


async def set_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed_chat(update, context) or not update.effective_chat:
        return

    if len(context.args) < 2:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text='Usage: /set <sku> <name>',
        )
        return

    sku = context.args[0]
    name = ' '.join(context.args[1:])
    state_store: BotStateStore = context.application.bot_data['state_store']
    saved_search = state_store.upsert_saved_search(sku, name)
    chat_id = str(update.effective_chat.id)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f'Saved {saved_search.sku} - {saved_search.name}',
    )
    await update_saved_searches_pin(context, chat_id)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed_chat(update, context) or not update.effective_chat:
        return

    state_store: BotStateStore = context.application.bot_data['state_store']
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=format_saved_searches_message(state_store.list_saved_searches()),
    )


async def unset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_allowed_chat(update, context) or not update.effective_chat:
        return

    if len(context.args) != 1:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text='Usage: /unset <sku>',
        )
        return

    sku = context.args[0]
    state_store: BotStateStore = context.application.bot_data['state_store']
    deleted = state_store.delete_saved_search(sku)
    chat_id = str(update.effective_chat.id)

    if deleted:
        await context.bot.send_message(chat_id=chat_id, text=f'Removed {sku}')
        await update_saved_searches_pin(context, chat_id)
    else:
        await context.bot.send_message(chat_id=chat_id, text=f'{sku} was not saved')


def build_application() -> Application:
    load_dotenv()

    token = require_env('TELEGRAM_BOT_TOKEN')
    chat_id = require_env('TELEGRAM_CHAT_ID')
    result_chat_id = require_env('TELEGRAM_RESULT_CHAT_ID')
    marketplaces = get_configured_marketplaces()
    state_store = BotStateStore()
    state_store.initialize()
    fetch_task_queue = FetchTaskQueue.from_env()

    application = Application.builder().token(token).updater(None).build()
    application.bot_data['chat_id'] = chat_id
    application.bot_data['result_chat_id'] = result_chat_id
    application.bot_data['state_store'] = state_store
    application.bot_data['fetch_task_queue'] = fetch_task_queue
    application.add_handler(CommandHandler('start', start_command))
    application.add_handler(CommandHandler('fetch', fetch_command))
    application.add_handler(CommandHandler('set', set_command))
    application.add_handler(CommandHandler('list', list_command))
    application.add_handler(CommandHandler('unset', unset_command))

    logger.info(
        'Configured %s marketplace(s) with Postgres state',
        len(marketplaces),
    )
    return application
