from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    from bot import (
        TELEGRAM_MESSAGE_LIMIT,
        build_application,
        fetch_command,
        filter_new_links,
        format_scraped_links_for_telegram,
        list_command,
        run_sku_fetch,
        set_command,
        start_command,
        unset_command,
    )
    from scraper import ScrapedLink
    from state import BotStateStore, SavedSearch, SeenLink
except ModuleNotFoundError as exc:
    TELEGRAM_MESSAGE_LIMIT = None
    build_application = None
    fetch_command = None
    filter_new_links = None
    format_scraped_links_for_telegram = None
    list_command = None
    run_sku_fetch = None
    set_command = None
    start_command = None
    unset_command = None
    ScrapedLink = None
    BotStateStore = None
    SavedSearch = None
    SeenLink = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f'missing optional dependency: {IMPORT_ERROR}')
class BotHelperTest(unittest.TestCase):
    def test_build_application_requires_result_chat_id(self) -> None:
        with (
            patch.dict(
                'os.environ',
                {
                    'TELEGRAM_BOT_TOKEN': '123:token',
                    'TELEGRAM_CHAT_ID': '123',
                },
                clear=True,
            ),
            patch('bot.load_dotenv'),
            self.assertRaisesRegex(ValueError, 'TELEGRAM_RESULT_CHAT_ID is required'),
        ):
            build_application()

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
            bot_data={
                'chat_id': 'control-chat',
                'result_chat_id': 'result-channel',
                'state_store': FakeStore(),
            },
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

    def test_format_one_listing_as_compact_html_alert(self) -> None:
        messages = format_scraped_links_for_telegram(
            SavedSearch(sku='sku-a', name='HT BROWN'),
            [ScrapedLink('https://market.example/item/123', 'marketplace_a', 'sku-a')],
        )

        self.assertEqual(
            messages,
            [
                '🔔 <b>NEW FIND</b>\n\n'
                '<b>HT BROWN</b>\n'
                '1 new listing\n\n'
                '<a href="https://market.example/item/123">View listing 1</a>'
            ],
        )

    def test_format_multiple_listings_in_one_numbered_alert(self) -> None:
        messages = format_scraped_links_for_telegram(
            SavedSearch(sku='secret-sku', name='OG VENOM'),
            [
                ScrapedLink('https://market.example/item/1', 'marketplace_a', 'secret-sku'),
                ScrapedLink('https://market.example/item/2', 'marketplace_a', 'secret-sku'),
            ],
        )

        self.assertEqual(len(messages), 1)
        self.assertIn('2 new listings', messages[0])
        self.assertIn('View listing 1</a>\n<a ', messages[0])
        self.assertIn('View listing 2</a>', messages[0])
        self.assertNotIn('secret-sku', messages[0])

    def test_format_alert_escapes_html_sensitive_name_and_url(self) -> None:
        messages = format_scraped_links_for_telegram(
            SavedSearch(sku='sku-a', name='A & <B> "Special"'),
            [
                ScrapedLink(
                    'https://market.example/item/1?a=1&label="hot"',
                    'marketplace_a',
                    'sku-a',
                )
            ],
        )

        self.assertIn('<b>A &amp; &lt;B&gt; &quot;Special&quot;</b>', messages[0])
        self.assertIn('a=1&amp;label=&quot;hot&quot;', messages[0])

    def test_large_alerts_split_with_repeated_header(self) -> None:
        links = [
            ScrapedLink(
                f'https://market.example/item/{number}/' + 'x' * 120,
                'marketplace_a',
                'sku-a',
            )
            for number in range(1, 40)
        ]
        messages = format_scraped_links_for_telegram(
            SavedSearch(sku='sku-a', name='LIMITED ITEM'),
            links,
        )

        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= TELEGRAM_MESSAGE_LIMIT for message in messages))
        self.assertTrue(
            all(message.startswith('🔔 <b>NEW FIND</b>') for message in messages)
        )
        self.assertIn('View listing 39</a>', messages[-1])

    def test_run_fetch_reports_already_running_when_database_lock_is_unavailable(self) -> None:
        from bot import run_fetch

        class LockedStore:
            @contextmanager
            def fetch_lock(self):
                yield False

        bot = SimpleNamespace(send_message=AsyncMock())
        application = SimpleNamespace(
            bot=bot,
            bot_data={'chat_id': 'control-chat', 'state_store': LockedStore()},
        )

        import asyncio
        completed = asyncio.run(run_fetch(application, '123'))

        self.assertFalse(completed)
        bot.send_message.assert_not_awaited()

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
            text=(
                '🔔 <b>NEW FIND</b>\n\n'
                '<b>NAME A</b>\n'
                '1 new listing\n\n'
                '<a href="https://market.example/item/new">View listing 1</a>'
            ),
            parse_mode='HTML',
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
        application.bot.send_message.assert_not_awaited()

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
            chat_id='control-chat',
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
            len(messages),
            1,
        )
        self.assertIn('View listing 1</a>', messages[0])

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
                    'result_chat_id': '-100456',
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
                'result_chat_id': '-100456',
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

    def test_control_commands_execute_and_reply_in_private_chat(self) -> None:
        saved_search = SavedSearch(sku='sku-a', name='NAME A')
        state_store = SimpleNamespace(
            upsert_saved_search=Mock(return_value=saved_search),
            list_saved_searches=Mock(return_value=[saved_search]),
            delete_saved_search=Mock(return_value=True),
        )
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    'chat_id': '123',
                    'result_chat_id': '-100456',
                    'state_store': state_store,
                },
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            args=['sku-a', 'NAME A'],
        )
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id='123'),
            update_id=456,
        )

        async def run_test() -> None:
            with patch(
                'bot.update_saved_searches_pin',
                new=AsyncMock(),
            ) as update_pin:
                await start_command(update, context)
                await set_command(update, context)
                await list_command(update, context)
                context.args = ['sku-a']
                await unset_command(update, context)

            self.assertEqual(update_pin.await_count, 2)

        import asyncio
        asyncio.run(run_test())

        self.assertEqual(
            [call.kwargs['chat_id'] for call in context.bot.send_message.await_args_list],
            ['123', '123', '123', '123'],
        )
        state_store.upsert_saved_search.assert_called_once_with('sku-a', 'NAME A')
        state_store.list_saved_searches.assert_called_once_with()
        state_store.delete_saved_search.assert_called_once_with('sku-a')

    def test_commands_from_result_and_unauthorized_chats_are_silently_ignored(self) -> None:
        state_store = SimpleNamespace(
            upsert_saved_search=Mock(),
            list_saved_searches=Mock(),
            delete_saved_search=Mock(),
        )
        task_queue = SimpleNamespace(enqueue_manual_fetch=AsyncMock())
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={
                    'chat_id': '123',
                    'result_chat_id': '-100456',
                    'state_store': state_store,
                    'fetch_task_queue': task_queue,
                },
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            args=['sku-a', 'NAME A'],
        )

        async def run_test() -> None:
            for chat_id in ('-100456', '999'):
                update = SimpleNamespace(
                    effective_chat=SimpleNamespace(id=chat_id),
                    update_id=456,
                )
                for command in (
                    start_command,
                    fetch_command,
                    set_command,
                    list_command,
                    unset_command,
                ):
                    await command(update, context)

        import asyncio
        asyncio.run(run_test())

        context.bot.send_message.assert_not_awaited()
        task_queue.enqueue_manual_fetch.assert_not_awaited()
        state_store.upsert_saved_search.assert_not_called()
        state_store.list_saved_searches.assert_not_called()
        state_store.delete_saved_search.assert_not_called()


if __name__ == '__main__':
    unittest.main()
