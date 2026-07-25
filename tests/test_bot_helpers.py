from __future__ import annotations

import unittest
from contextlib import contextmanager
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

try:
    from bot import (
        build_application,
        convert_product_image_to_sticker,
        download_product_image,
        fetch_command,
        filter_new_links,
        format_scraped_links_for_telegram,
        format_saved_searches_message,
        list_command,
        MAX_IMAGE_DOWNLOAD_BYTES,
        run_sku_fetch,
        send_links,
        set_command,
        start_command,
        unset_command,
    )
    from scraper import ScrapedLink
    from state import BotStateStore, SavedSearch, SeenLink
except ModuleNotFoundError as exc:
    build_application = None
    convert_product_image_to_sticker = None
    download_product_image = None
    fetch_command = None
    filter_new_links = None
    format_scraped_links_for_telegram = None
    format_saved_searches_message = None
    list_command = None
    MAX_IMAGE_DOWNLOAD_BYTES = None
    run_sku_fetch = None
    send_links = None
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
    @staticmethod
    def encode_image(image: Image.Image, image_format: str = 'PNG', **kwargs) -> bytes:
        output = BytesIO()
        image.save(output, format=image_format, **kwargs)
        return output.getvalue()

    def assert_valid_padded_sticker(self, sticker_data: bytes) -> tuple[int, int, int, int]:
        self.assertLessEqual(len(sticker_data), 512 * 1024)
        with Image.open(BytesIO(sticker_data)) as sticker:
            self.assertEqual(sticker.format, 'WEBP')
            self.assertEqual(sticker.size, (512, 512))
            self.assertEqual(sticker.mode, 'RGBA')
            alpha = sticker.getchannel('A')
            self.assertEqual(alpha.getpixel((0, 0)), 0)
            bounds = alpha.getbbox()

        self.assertIsNotNone(bounds)
        left, top, right, bottom = bounds
        self.assertLessEqual(right - left, 358)
        self.assertLessEqual(bottom - top, 358)
        self.assertLessEqual(abs((left + right) - 512), 1)
        self.assertLessEqual(abs((top + bottom) - 512), 1)
        return bounds

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

        bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_sticker=AsyncMock(),
        )
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

    def test_sticker_conversion_handles_portrait_and_landscape_images(self) -> None:
        for size in [(120, 360), (360, 120)]:
            with self.subTest(size=size):
                image = Image.new('RGBA', size, (40, 80, 120, 255))
                image.putpixel((0, 0), (0, 0, 0, 0))
                sticker_data = convert_product_image_to_sticker(
                    self.encode_image(image)
                )
                bounds = self.assert_valid_padded_sticker(sticker_data)
                self.assertEqual(max(bounds[2] - bounds[0], bounds[3] - bounds[1]), 358)

    def test_sticker_conversion_supports_palette_transparency(self) -> None:
        image = Image.new('P', (100, 100), 0)
        image.putpalette([0, 0, 0, 230, 40, 60] + [0, 0, 0] * 254)
        for x in range(25, 75):
            for y in range(10, 90):
                image.putpixel((x, y), 1)

        sticker_data = convert_product_image_to_sticker(
            self.encode_image(image, transparency=0)
        )
        self.assert_valid_padded_sticker(sticker_data)

    def test_sticker_conversion_crops_existing_transparent_padding(self) -> None:
        image = Image.new('RGBA', (800, 800), (0, 0, 0, 0))
        for x in range(350, 450):
            for y in range(300, 500):
                image.putpixel((x, y), (20, 140, 80, 255))

        bounds = self.assert_valid_padded_sticker(
            convert_product_image_to_sticker(self.encode_image(image))
        )
        self.assertEqual(bounds[3] - bounds[1], 358)
        self.assertGreater(bounds[2] - bounds[0], 170)

    def test_sticker_conversion_rejects_invalid_sources(self) -> None:
        transparent = Image.new('RGBA', (20, 20), (0, 0, 0, 0))
        opaque = Image.new('RGB', (20, 20), (255, 255, 255))

        cases = [
            ('fully transparent', self.encode_image(transparent), 'fully transparent'),
            ('non-transparent', self.encode_image(opaque), 'no transparency'),
            ('malformed', b'not an image', 'could not be decoded'),
        ]
        for label, image_data, message in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, message):
                convert_product_image_to_sticker(image_data)

        with (
            patch('bot.MAX_IMAGE_PIXELS', 100),
            self.assertRaisesRegex(ValueError, 'dimensions exceed'),
        ):
            convert_product_image_to_sticker(self.encode_image(opaque))

    def test_image_download_rejects_oversized_response(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={'content-length': str(MAX_IMAGE_DOWNLOAD_BYTES + 1)},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run_test() -> None:
            with patch('bot.httpx.AsyncClient', return_value=client):
                await download_product_image('https://images.example/item.png')

        import asyncio
        with self.assertRaisesRegex(ValueError, '10 MB'):
            asyncio.run(run_test())

    def test_image_download_rejects_non_https_redirect(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={'location': 'http://unsafe.example/a.png'})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        async def run_test() -> None:
            with patch('bot.httpx.AsyncClient', return_value=client):
                await download_product_image('https://images.example/item.png')

        import asyncio
        with self.assertRaisesRegex(ValueError, 'non-HTTPS'):
            asyncio.run(run_test())

    def test_format_one_listing_as_compact_html_alert(self) -> None:
        alerts = format_scraped_links_for_telegram(
            SavedSearch(
                sku='sku-a',
                name='HT BROWN',
                image_url='https://images.example/brown.jpg',
            ),
            [ScrapedLink('https://market.example/item/123', 'marketplace_a', 'sku-a')],
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(
            alerts[0].text,
            '🔥<b>HT BROWN</b>\n',
        )
        self.assertEqual(alerts[0].image_url, 'https://images.example/brown.jpg')
        button = alerts[0].reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.text, 'View Listing')
        self.assertEqual(button.url, 'https://market.example/item/123')

    def test_format_multiple_listings_in_one_numbered_alert(self) -> None:
        alerts = format_scraped_links_for_telegram(
            SavedSearch(sku='secret-sku', name='OG VENOM'),
            [
                ScrapedLink('https://market.example/item/1', 'marketplace_a', 'secret-sku'),
                ScrapedLink('https://market.example/item/2', 'marketplace_a', 'secret-sku'),
            ],
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].text, '🔥<b>OG VENOM</b>\n')
        self.assertNotIn('secret-sku', alerts[0].text)
        rows = alerts[0].reply_markup.inline_keyboard
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [(row[0].text, row[0].url) for row in rows],
            [
                ('View Listing 1', 'https://market.example/item/1'),
                ('View Listing 2', 'https://market.example/item/2'),
            ],
        )

    def test_format_alert_escapes_name_and_preserves_exact_url(self) -> None:
        alerts = format_scraped_links_for_telegram(
            SavedSearch(sku='sku-a', name='A & <B> "Special"'),
            [
                ScrapedLink(
                    'https://market.example/item/1?a=1&label="hot"',
                    'marketplace_a',
                    'sku-a',
                )
            ],
        )

        self.assertIn(
            '<b>A &amp; &lt;B&gt; &quot;Special&quot;</b>',
            alerts[0].text,
        )
        self.assertEqual(
            alerts[0].reply_markup.inline_keyboard[0][0].url,
            'https://market.example/item/1?a=1&label="hot"',
        )

    def test_large_alerts_split_with_repeated_header(self) -> None:
        links = [
            ScrapedLink(
                f'https://market.example/item/{number}',
                'marketplace_a',
                'sku-a',
            )
            for number in range(1, 24)
        ]
        alerts = format_scraped_links_for_telegram(
            SavedSearch(sku='sku-a', name='LIMITED ITEM'),
            links,
        )

        self.assertEqual(len(alerts), 3)
        self.assertTrue(
            all(alert.text == '🔥<b>LIMITED ITEM</b>\n' for alert in alerts)
        )
        self.assertEqual(
            [len(alert.reply_markup.inline_keyboard) for alert in alerts],
            [10, 10, 3],
        )
        self.assertEqual(
            alerts[-1].reply_markup.inline_keyboard[-1][0].text,
            'View Listing 23',
        )
        self.assertEqual(
            alerts[-1].reply_markup.inline_keyboard[-1][0].url,
            'https://market.example/item/23',
        )

    def test_multiple_alert_chunks_prepare_image_only_once(self) -> None:
        links = [
            ScrapedLink(
                f'https://market.example/item/{number}',
                'marketplace_a',
                'sku-a',
            )
            for number in range(11)
        ]
        alerts = format_scraped_links_for_telegram(
            SavedSearch(
                sku='sku-a',
                name='NAME A',
                image_url='https://images.example/a.png',
            ),
            links,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_sticker=AsyncMock(),
            )
        )

        async def run_test() -> None:
            with patch(
                'bot.prepare_product_sticker',
                new=AsyncMock(return_value=b'webp'),
            ) as prepare:
                await send_links(context, 'result-chat', alerts)
            prepare.assert_awaited_once_with('https://images.example/a.png')

        import asyncio
        asyncio.run(run_test())
        self.assertEqual(context.bot.send_message.await_count, 2)
        self.assertEqual(context.bot.send_sticker.await_count, 2)

    def test_failed_image_preparation_sends_only_compact_text_alert(self) -> None:
        alerts = format_scraped_links_for_telegram(
            SavedSearch(
                sku='sku-a',
                name='NAME A',
                image_url='https://images.example/a.png',
            ),
            [ScrapedLink('https://market.example/1', 'marketplace_a', 'sku-a')],
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_message=AsyncMock(),
                send_sticker=AsyncMock(),
            )
        )

        async def run_test() -> None:
            with patch(
                'bot.prepare_product_sticker',
                new=AsyncMock(side_effect=httpx.ConnectError('download failed')),
            ):
                await send_links(context, 'result-chat', alerts)

        import asyncio
        asyncio.run(run_test())
        context.bot.send_sticker.assert_not_awaited()
        context.bot.send_message.assert_awaited_once_with(
            chat_id='result-chat',
            text='🔥<b>NAME A</b>\n',
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=alerts[0].reply_markup,
        )

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
            text='🔥<b>NAME A</b>\n',
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=application.bot.send_message.await_args.kwargs['reply_markup'],
        )
        reply_markup = application.bot.send_message.await_args.kwargs['reply_markup']
        button = reply_markup.inline_keyboard[0][0]
        self.assertEqual(button.text, 'View Listing')
        self.assertEqual(button.url, 'https://market.example/item/new')
        self.assertEqual(seen_urls, {'https://market.example/item/new'})

    def test_run_sku_fetch_sends_sticker_then_name_with_buttons(self) -> None:
        application, seen_urls = self.make_sku_fetch_context()
        events = []
        application.bot.send_message.side_effect = (
            lambda **_kwargs: events.append('message')
        )
        application.bot.send_sticker.side_effect = (
            lambda **_kwargs: events.append('sticker')
        )
        result = ScrapedLink(
            'https://market.example/item/new',
            'marketplace-a',
            'sku-a',
        )

        async def run_test() -> bool:
            with (
                patch(
                    'bot.scrape_link_results',
                    new=AsyncMock(return_value=[result]),
                ),
                patch(
                    'bot.prepare_product_sticker',
                    new=AsyncMock(return_value=b'sticker-data'),
                ),
            ):
                return await run_sku_fetch(
                    application,
                    '123',
                    SavedSearch(
                        sku='sku-a',
                        name='A & <B>',
                        image_url='https://images.example/item.jpg',
                    ),
                )

        import asyncio
        self.assertTrue(asyncio.run(run_test()))
        application.bot.send_message.assert_awaited_once_with(
            chat_id='123',
            text='🔥<b>A &amp; &lt;B&gt;</b>\n',
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=application.bot.send_message.await_args.kwargs[
                'reply_markup'
            ],
        )
        application.bot.send_sticker.assert_awaited_once_with(
            chat_id='123',
            sticker=application.bot.send_sticker.await_args.kwargs['sticker'],
        )
        sticker = application.bot.send_sticker.await_args.kwargs['sticker']
        self.assertEqual(sticker.name, 'product.webp')
        self.assertEqual(sticker.getvalue(), b'sticker-data')
        button = application.bot.send_message.await_args.kwargs[
            'reply_markup'
        ].inline_keyboard[0][0]
        self.assertEqual(button.url, 'https://market.example/item/new')
        self.assertEqual(events, ['sticker', 'message'])
        self.assertEqual(seen_urls, {'https://market.example/item/new'})

    def test_sticker_failure_falls_back_to_text_before_recording_seen(self) -> None:
        from telegram.error import TelegramError

        application, seen_urls = self.make_sku_fetch_context()
        events = []

        async def send_message(**kwargs):
            events.append(('message', 'reply_markup' in kwargs))

        async def send_sticker(**_kwargs):
            events.append(('sticker', True))
            raise TelegramError('bad sticker')

        application.bot.send_message.side_effect = send_message
        application.bot.send_sticker.side_effect = send_sticker
        result = ScrapedLink(
            'https://market.example/item/new',
            'marketplace-a',
            'sku-a',
        )

        async def run_test() -> bool:
            with (
                patch(
                    'bot.scrape_link_results',
                    new=AsyncMock(return_value=[result]),
                ),
                patch(
                    'bot.prepare_product_sticker',
                    new=AsyncMock(return_value=b'sticker-data'),
                ),
            ):
                return await run_sku_fetch(
                    application,
                    '123',
                    SavedSearch(
                        sku='sku-a',
                        name='NAME A',
                        image_url='https://images.example/item.jpg',
                    ),
                )

        import asyncio
        self.assertTrue(asyncio.run(run_test()))
        application.bot.send_sticker.assert_awaited_once()
        self.assertEqual(events, [('sticker', True), ('message', True)])
        application.bot.send_message.assert_awaited_with(
            chat_id='123',
            text='🔥<b>NAME A</b>\n',
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=application.bot.send_message.await_args.kwargs['reply_markup'],
        )
        self.assertEqual(seen_urls, {'https://market.example/item/new'})

    def test_failed_sticker_and_text_delivery_does_not_record_seen(self) -> None:
        from telegram.error import TelegramError

        application, seen_urls = self.make_sku_fetch_context()
        application.bot.send_sticker.side_effect = TelegramError('bad sticker')
        application.bot.send_message.side_effect = [
            TelegramError('text delivery failed'),
            None,
        ]
        result = ScrapedLink(
            'https://market.example/item/new',
            'marketplace-a',
            'sku-a',
        )

        async def run_test() -> bool:
            with (
                patch(
                    'bot.scrape_link_results',
                    new=AsyncMock(return_value=[result]),
                ),
                patch(
                    'bot.prepare_product_sticker',
                    new=AsyncMock(return_value=b'sticker-data'),
                ),
            ):
                return await run_sku_fetch(
                    application,
                    '123',
                    SavedSearch(
                        sku='sku-a',
                        name='NAME A',
                        image_url='https://images.example/item.jpg',
                    ),
                )

        import asyncio
        self.assertFalse(asyncio.run(run_test()))
        self.assertEqual(seen_urls, set())
        self.assertEqual(application.bot.send_message.await_count, 2)
        self.assertEqual(
            application.bot.send_message.await_args_list[-1].kwargs,
            {
                'chat_id': 'control-chat',
                'text': 'Fetch failed for NAME A (sku-a). Check bot logs.',
            },
        )

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
        sent_messages = [
            call.kwargs
            for call in application.bot.send_message.await_args_list
        ]
        self.assertEqual(
            len(sent_messages),
            1,
        )
        self.assertEqual(
            sent_messages[0]['reply_markup'].inline_keyboard[0][0].url,
            'https://market.example/item/new',
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
        saved_search = SavedSearch(
            sku='sku-a',
            name='NAME A',
            image_url='https://images.example/item.jpg',
        )
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
            args=['sku-a', 'https://images.example/item.jpg', 'NAME', 'A'],
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
        state_store.upsert_saved_search.assert_called_once_with(
            'sku-a',
            'NAME A',
            'https://images.example/item.jpg',
        )
        state_store.list_saved_searches.assert_called_once_with()
        state_store.delete_saved_search.assert_called_once_with('sku-a')

    def test_set_rejects_invalid_image_urls(self) -> None:
        state_store = SimpleNamespace(upsert_saved_search=Mock())
        context = SimpleNamespace(
            application=SimpleNamespace(
                bot_data={'chat_id': '123', 'state_store': state_store},
            ),
            bot=SimpleNamespace(send_message=AsyncMock()),
            args=[],
        )
        update = SimpleNamespace(effective_chat=SimpleNamespace(id='123'))

        async def run_test() -> None:
            invalid_args = [
                ['sku-a', 'http://images.example/item.jpg', 'Name'],
                ['sku-a', 'not-a-url', 'Name'],
                ['sku-a', 'https:///item.jpg', 'Name'],
                ['sku-a', 'https://user:pass@images.example/item.jpg', 'Name'],
            ]
            for args in invalid_args:
                context.args = args
                await set_command(update, context)

        import asyncio
        asyncio.run(run_test())
        self.assertEqual(context.bot.send_message.await_count, 4)
        state_store.upsert_saved_search.assert_not_called()

    def test_set_requires_image_url_and_name(self) -> None:
        context = SimpleNamespace(
            application=SimpleNamespace(bot_data={'chat_id': '123'}),
            bot=SimpleNamespace(send_message=AsyncMock()),
            args=['sku-a', 'https://images.example/item.jpg'],
        )
        update = SimpleNamespace(effective_chat=SimpleNamespace(id='123'))

        import asyncio
        asyncio.run(set_command(update, context))
        context.bot.send_message.assert_awaited_once_with(
            chat_id='123',
            text='Usage: /set <sku> <image_url> <name>',
        )

    def test_saved_search_list_includes_image_url_when_present(self) -> None:
        self.assertEqual(
            format_saved_searches_message(
                [
                    SavedSearch('sku-a', 'NAME A', 'https://images.example/a.jpg'),
                    SavedSearch('sku-b', 'NAME B'),
                ]
            ),
            (
                'Saved SKUs\n\n'
                'sku-a - NAME A\n'
                'Image: https://images.example/a.jpg\n'
                'sku-b - NAME B'
            ),
        )

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
