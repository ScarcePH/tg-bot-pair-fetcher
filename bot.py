from __future__ import annotations

import logging
import os
from typing import Any

from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from scraper import ScrapedLink, get_configured_marketplaces, scrape_link_results
from state import BotStateStore, SavedSearch, SeenLink


TELEGRAM_MESSAGE_LIMIT = 4096

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    value = os.getenv(name, '').strip()

    if not value:
        raise ValueError(f'{name} is required')

    return value


def split_links_for_telegram(links: list[str]) -> list[str]:
    messages = []
    current_lines = []
    current_length = 0

    for link in links:
        line_length = len(link)
        separator_length = 1 if current_lines else 0

        if current_lines and current_length + separator_length + line_length > TELEGRAM_MESSAGE_LIMIT:
            messages.append('\n'.join(current_lines))
            current_lines = [link]
            current_length = line_length
        else:
            current_lines.append(link)
            current_length += separator_length + line_length

    if current_lines:
        messages.append('\n'.join(current_lines))

    return messages


def format_scraped_links_for_telegram(
    links: list[ScrapedLink],
    saved_searches: list[SavedSearch],
) -> list[str]:
    saved_names_by_sku = {search.sku: search.name for search in saved_searches}
    formatted_links = []

    for link in links:
        saved_name = saved_names_by_sku.get(link.query)

        if saved_name:
            formatted_links.append(f'{saved_name} - {link.url}')
        else:
            formatted_links.append(link.url)

    return formatted_links


async def send_links(context: Any, chat_id: str, links: list[str]) -> None:
    for message in split_links_for_telegram(links):
        await context.bot.send_message(
            chat_id=chat_id,
            text=message,
            disable_web_page_preview=True,
        )


def get_bot_data(context: Any) -> dict:
    application = getattr(context, 'application', context)
    return application.bot_data


async def run_fetch(
    context: Any,
    chat_id: str,
    manual: bool = False,
) -> bool:
    state_store: BotStateStore = get_bot_data(context)['state_store']
    with state_store.fetch_lock() as lock_acquired:
        if not lock_acquired:
            if manual:
                await context.bot.send_message(chat_id=chat_id, text='A fetch is already running.')
            logger.info('Skipped fetch because another fetch is already running')
            return False

        try:
            saved_searches = state_store.list_saved_searches()

            if not saved_searches:
                if manual:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text='No saved SKUs. Use /set <sku> <name> first.',
                    )
                return True

            scraped_links = await scrape_link_results(
                item_queries=[search.sku for search in saved_searches],
            )
            new_links = filter_new_links(state_store, scraped_links)

            if new_links:
                await send_links(
                    context,
                    chat_id,
                    format_scraped_links_for_telegram(new_links, saved_searches),
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
            elif manual:
                await context.bot.send_message(chat_id=chat_id, text='No new links found')
        except Exception:
            logger.exception('Fetch failed')
            if manual:
                await context.bot.send_message(chat_id=chat_id, text='Fetch failed. Check bot logs.')
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

    if chat_id != configured_chat_id:
        await context.bot.send_message(chat_id=chat_id, text='This bot is configured for another chat.')
        return False

    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
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

    fetch_coroutine = run_fetch(context.application, chat_id, manual=True)
    try:
        context.application.create_task(fetch_coroutine)
    except Exception:
        fetch_coroutine.close()
        logger.exception('Could not schedule fetch task')
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
    marketplaces = get_configured_marketplaces()
    state_store = BotStateStore()
    state_store.initialize()

    application = Application.builder().token(token).updater(None).build()
    application.bot_data['chat_id'] = chat_id
    application.bot_data['state_store'] = state_store
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
