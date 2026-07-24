from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from bot import (
        TELEGRAM_MESSAGE_LIMIT,
        fetch_command,
        filter_new_links,
        format_scraped_links_for_telegram,
        run_sku_fetch,
        split_links_for_telegram,
    )
    from scraper import ScrapedLink
    from state import BotStateStore, SavedSearch, SeenLink
except ModuleNotFoundError as exc:
    TELEGRAM_MESSAGE_LIMIT = None
    fetch_command = None
    filter_new_links = None
    format_scraped_links_for_telegram = None
    run_sku_fetch = None
    split_links_for_telegram = None
    ScrapedLink = None
    BotStateStore = None
    SavedSearch = None
    SeenLink = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f'missing optional dependency: {IMPORT_ERROR}')
class BotHelperTest(unittest.TestCase):
    @staticmethod
    def make_sku_fetch_context(seen_urls=None):
        seen_urls = set(seen_urls or [])

        class FakeStore:
            @contextmanager
            def fetch_lock(self):
                yield True

            def has_seen_link(self, url: str) -> bool:
                return url in seen_urls

            def record_seen_links(self, links) -> None:
                seen_urls.update(link.url for link in links)

        bot = SimpleNamespace(send_message=AsyncMock())
        application = SimpleNamespace(
            bot=bot,
            bot_data={'state_store': FakeStore()},
        )
        return application, seen_urls

    def test_filter_new_links_removes_seen_and_in_fetch_duplicates(self) -> None:
        class FakeStore:
            def has_seen_link(self, url: str) -> bool:
                return url == 'https://market.example/item/seen'

        scraped_links = [
            ScrapedLink('https://market.example/item/seen', 'marketplace_a', 'sku-a'),
            ScrapedLink('https://market.example/item/new', 'marketplace_a', 'sku-a'),
            ScrapedLink('https://market.example/item/new', 'marketplace_a', 'sku-b'),
        ]
        new_links = filter_new_links(FakeStore(), scraped_links)

        self.assertEqual(
            [(link.url, link.query) for link in new_links],
            [('https://market.example/item/new', 'sku-a')],
        )

    def test_format_scraped_links_uses_saved_search_name(self) -> None:
        formatted_links = format_scraped_links_for_telegram(
            [ScrapedLink('https://market.example/item/123', 'marketplace_a', 'sku-a')],
            [SavedSearch(sku='sku-a', name='HT BROWN')],
        )

        self.assertEqual(
            formatted_links,
            ['HT BROWN - https://market.example/item/123'],
        )

    def test_format_scraped_links_falls_back_to_url_for_unknown_query(self) -> None:
        formatted_links = format_scraped_links_for_telegram(
            [ScrapedLink('https://market.example/item/123', 'marketplace_a', 'unknown-sku')],
            [SavedSearch(sku='sku-a', name='HT BROWN')],
        )

        self.assertEqual(formatted_links, ['https://market.example/item/123'])

    def test_formatted_links_use_existing_telegram_message_splitting(self) -> None:
        url = 'https://market.example/item/' + 'a' * 100
        name = 'x' * (TELEGRAM_MESSAGE_LIMIT - len(url) - len(' - '))
        first_line = format_scraped_links_for_telegram(
            [ScrapedLink(url, 'marketplace_a', 'sku-a')],
            [SavedSearch(sku='sku-a', name=name)],
        )[0]
        second_line = format_scraped_links_for_telegram(
            [ScrapedLink('https://market.example/item/second', 'marketplace_a', 'sku-b')],
            [SavedSearch(sku='sku-b', name='SECOND')],
        )[0]

        messages = split_links_for_telegram([first_line, second_line])

        self.assertEqual(messages, [first_line, second_line])

    def test_run_fetch_reports_already_running_when_database_lock_is_unavailable(self) -> None:
        from bot import run_fetch

        class LockedStore:
            @contextmanager
            def fetch_lock(self):
                yield False

        bot = SimpleNamespace(send_message=AsyncMock())
        application = SimpleNamespace(bot=bot, bot_data={'state_store': LockedStore()})

        import asyncio
        completed = asyncio.run(run_fetch(application, '123'))

        self.assertFalse(completed)
        bot.send_message.assert_awaited_once_with(
            chat_id='123', text='A fetch is already running.'
        )

    def test_run_sku_fetch_scrapes_only_one_sku_and_sends_named_links(self) -> None:
        application, seen_urls = self.make_sku_fetch_context()
        result = ScrapedLink(
            'https://market.example/item/new',
            'marketplace-a',
            'sku-a',
        )

        async def run_test() -> bool:
            with patch(
                'bot.scrape_link_results',
                new=AsyncMock(return_value=[result]),
            ) as scrape:
                completed = await run_sku_fetch(
                    application,
                    '123',
                    SavedSearch(sku='sku-a', name='NAME A'),
                )
            scrape.assert_awaited_once_with(
                item_queries=['sku-a'],
                raise_on_error=True,
            )
            return completed

        import asyncio
        self.assertTrue(asyncio.run(run_test()))
        application.bot.send_message.assert_awaited_once_with(
            chat_id='123',
            text='NAME A - https://market.example/item/new',
            disable_web_page_preview=True,
        )
        self.assertEqual(seen_urls, {'https://market.example/item/new'})

    def test_run_sku_fetch_reports_sku_specific_no_links(self) -> None:
        application, _seen_urls = self.make_sku_fetch_context()

        async def run_test() -> bool:
            with patch(
                'bot.scrape_link_results',
                new=AsyncMock(return_value=[]),
            ):
                return await run_sku_fetch(
                    application,
                    '123',
                    SavedSearch(sku='sku-a', name='NAME A'),
                )

        import asyncio
        self.assertTrue(asyncio.run(run_test()))
        application.bot.send_message.assert_awaited_once_with(
            chat_id='123',
            text='No new links found for NAME A (sku-a).',
        )

    def test_run_sku_fetch_reports_sku_specific_failure(self) -> None:
        application, _seen_urls = self.make_sku_fetch_context()

        async def run_test() -> bool:
            with patch(
                'bot.scrape_link_results',
                new=AsyncMock(side_effect=RuntimeError('scrape failed')),
            ):
                return await run_sku_fetch(
                    application,
                    '123',
                    SavedSearch(sku='sku-a', name='NAME A'),
                )

        import asyncio
        self.assertFalse(asyncio.run(run_test()))
        application.bot.send_message.assert_awaited_once_with(
            chat_id='123',
            text='Fetch failed for NAME A (sku-a). Check bot logs.',
        )

    def test_run_sku_fetch_deduplicates_across_retries_and_runs(self) -> None:
        application, seen_urls = self.make_sku_fetch_context()
        result = ScrapedLink(
            'https://market.example/item/new',
            'marketplace-a',
            'sku-a',
        )

        async def run_test() -> None:
            with patch(
                'bot.scrape_link_results',
                new=AsyncMock(return_value=[result]),
            ):
                for _run in range(2):
                    await run_sku_fetch(
                        application,
                        '123',
                        SavedSearch(sku='sku-a', name='NAME A'),
                    )

        import asyncio
        asyncio.run(run_test())
        self.assertEqual(seen_urls, {'https://market.example/item/new'})
        messages = [
            call.kwargs['text']
            for call in application.bot.send_message.await_args_list
        ]
        self.assertEqual(
            messages,
            [
                'NAME A - https://market.example/item/new',
                'No new links found for NAME A (sku-a).',
            ],
        )

    def test_fetch_command_acknowledges_before_enqueuing_manual_fetch(self) -> None:
        events = []

        class FakeApplication:
            def __init__(self) -> None:
                task_queue = SimpleNamespace(
                    enqueue_manual_fetch=AsyncMock(
                        side_effect=lambda chat_id, update_id: events.append(
                            ('enqueue', chat_id, update_id)
                        )
                    )
                )
                self.bot_data = {
                    'chat_id': '123',
                    'fetch_task_queue': task_queue,
                }

        async def send_message(**kwargs):
            events.append(('send', kwargs['text']))

        application = FakeApplication()
        context = SimpleNamespace(
            application=application,
            bot=SimpleNamespace(send_message=AsyncMock(side_effect=send_message)),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id='123'),
            update_id=456,
        )

        async def run_test() -> None:
            with patch('bot.run_fetch', new=AsyncMock()) as run_fetch_mock:
                await fetch_command(update, context)

            run_fetch_mock.assert_not_awaited()

        import asyncio
        asyncio.run(run_test())

        self.assertEqual(
            events,
            [('send', 'Fetch started.'), ('enqueue', '123', 456)],
        )

    def test_fetch_command_reports_enqueue_failure(self) -> None:
        class FakeApplication:
            bot_data = {
                'chat_id': '123',
                'fetch_task_queue': SimpleNamespace(
                    enqueue_manual_fetch=AsyncMock(
                        side_effect=RuntimeError('queue unavailable')
                    )
                ),
            }

        context = SimpleNamespace(
            application=FakeApplication(),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id='123'),
            update_id=456,
        )

        async def run_test() -> None:
            with patch('bot.logger') as logger_mock:
                await fetch_command(update, context)

            logger_mock.exception.assert_called_once_with(
                'Could not enqueue manual fetch task'
            )

        import asyncio
        asyncio.run(run_test())

        self.assertEqual(
            [
                call.kwargs
                for call in context.bot.send_message.await_args_list
            ],
            [
                {'chat_id': '123', 'text': 'Fetch started.'},
                {'chat_id': '123', 'text': 'Could not start fetch. Check bot logs.'},
            ],
        )


if __name__ == '__main__':
    unittest.main()
