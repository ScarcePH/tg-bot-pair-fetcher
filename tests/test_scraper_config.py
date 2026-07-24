from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    import scraper
    from scraper import (
        FETCH_MODE_STANDARD,
        FETCH_MODE_STEALTH,
        Marketplace,
        build_marketplace,
        build_stealth_fetch_options,
        extract_marketplace_links,
        get_scrape_delay_ms,
        is_marketplace_item_link,
        scrape_link_results,
        scrape_search_page,
    )
except ModuleNotFoundError as exc:
    scraper = None
    FETCH_MODE_STANDARD = None
    FETCH_MODE_STEALTH = None
    Marketplace = None
    build_marketplace = None
    build_stealth_fetch_options = None
    extract_marketplace_links = None
    get_scrape_delay_ms = None
    is_marketplace_item_link = None
    scrape_link_results = None
    scrape_search_page = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f'missing optional dependency: {IMPORT_ERROR}')
class ScraperConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_env = os.environ.copy()

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.original_env)

    def test_build_marketplace_uses_key_scoped_env(self) -> None:
        os.environ.update(
            {
                'MARKETPLACE_MARKETPLACE_A_BASE_URL': 'https://market.example',
                'MARKETPLACE_MARKETPLACE_A_SEARCH_URL_TEMPLATE': 'https://market.example/search?q={query}',
                'MARKETPLACE_MARKETPLACE_A_ITEM_HOSTS': 'market.example,www.market.example',
                'MARKETPLACE_MARKETPLACE_A_ITEM_PATH_MARKERS': '/item/,/product/',
                'MARKETPLACE_MARKETPLACE_A_FETCH_MODE': 'stealth',
                'MARKETPLACE_MARKETPLACE_A_FETCH_WAIT_MS': '1500',
            }
        )

        marketplace = build_marketplace('marketplace_a')

        self.assertEqual(marketplace.key, 'marketplace_a')
        self.assertEqual(marketplace.base_url, 'https://market.example')
        self.assertEqual(marketplace.search_url('sku 123'), 'https://market.example/search?q=sku+123')
        self.assertEqual(marketplace.item_hosts, ('market.example', 'www.market.example'))
        self.assertEqual(marketplace.item_path_markers, ('/item/', '/product/'))
        self.assertEqual(marketplace.fetch_mode, 'stealth')
        self.assertEqual(marketplace.fetch_wait_ms, 1500)

    def test_item_link_rules_come_from_env_config(self) -> None:
        os.environ.update(
            {
                'MARKETPLACE_MARKETPLACE_A_BASE_URL': 'https://market.example',
                'MARKETPLACE_MARKETPLACE_A_SEARCH_URL_TEMPLATE': 'https://market.example/search?q={query}',
                'MARKETPLACE_MARKETPLACE_A_ITEM_HOSTS': 'market.example',
                'MARKETPLACE_MARKETPLACE_A_ITEM_PATH_MARKERS': '/item/',
            }
        )

        marketplace = build_marketplace('marketplace_a')

        self.assertTrue(is_marketplace_item_link('https://market.example/item/123', marketplace))
        self.assertFalse(is_marketplace_item_link('https://market.example/search?q=123', marketplace))
        self.assertFalse(is_marketplace_item_link('https://other.example/item/123', marketplace))

    def test_extracts_anchor_links_and_deduplicates_results(self) -> None:
        marketplace = self.build_test_marketplace('market_a')
        html = '''
            <a href="/item/123">one</a>
            <a href="https://market.example/item/123">duplicate</a>
            <a href="/search?q=123">search</a>
            <a href="https://other.example/item/456">other host</a>
            <a href="/item/789#details">two</a>
            <script>{"path":"/item/not-an-anchor"}</script>
        '''

        links = extract_marketplace_links(html, 'https://market.example/search?q=sku', marketplace)

        self.assertEqual(
            links,
            [
                'https://market.example/item/123',
                'https://market.example/item/789',
            ],
        )

    def test_scrape_delay_defaults_to_2000_ms(self) -> None:
        os.environ.pop('SCRAPER_SCRAPE_DELAY_MS', None)

        self.assertEqual(get_scrape_delay_ms(), 2000)

    def test_scrape_delay_rejects_invalid_values(self) -> None:
        for value in ('-1', 'not-an-int'):
            with self.subTest(value=value):
                os.environ['SCRAPER_SCRAPE_DELAY_MS'] = value

                with self.assertRaises(ValueError):
                    get_scrape_delay_ms()

    def test_scrape_loop_sleeps_between_attempts_only(self) -> None:
        marketplaces = [
            self.build_test_marketplace('market_a'),
            self.build_test_marketplace('market_b'),
        ]

        async def run_test() -> None:
            with (
                patch.object(
                    scraper,
                    'scrape_search_page',
                    new=AsyncMock(return_value=['https://market.example/item/1']),
                ),
                patch.object(scraper.asyncio, 'sleep', new=AsyncMock()) as sleep_mock,
            ):
                os.environ['SCRAPER_SCRAPE_DELAY_MS'] = '250'

                links = await scrape_link_results(marketplaces, ['sku-a', 'sku-b'])

            self.assertEqual(len(links), 4)
            self.assertEqual(sleep_mock.await_count, 3)
            sleep_mock.assert_awaited_with(0.25)

        import asyncio
        asyncio.run(run_test())

    def test_stealth_fetch_options_use_dom_loaded_without_resource_disabling(self) -> None:
        options = build_stealth_fetch_options(1500)

        self.assertNotIn('network_idle', options)
        self.assertNotIn('disable_resources', options)
        self.assertEqual(options['load_dom'], True)
        self.assertEqual(options['timeout'], 60000)
        self.assertEqual(options['wait'], 1500)

    def test_stealth_batch_reuses_one_session_across_marketplaces_and_skus(self) -> None:
        marketplaces = [
            self.build_test_marketplace('market_a', FETCH_MODE_STEALTH, 750),
            self.build_test_marketplace('market_b', FETCH_MODE_STEALTH, 750),
        ]
        pages = [
            FakeFetchPage(
                hrefs=[f'https://market.example/item/{index}'],
                page=FakeCloseableResource(),
                context=FakeCloseableResource(),
            )
            for index in range(1, 5)
        ]
        sessions = [FakeStealthSession(pages)]
        session_factory = FakeStealthSessionFactory(sessions)

        async def run_test() -> None:
            with (
                patch.object(scraper, 'AsyncStealthySession', new=session_factory),
                patch.object(scraper.asyncio, 'sleep', new=AsyncMock()),
            ):
                links = await scrape_link_results(
                    marketplaces=marketplaces,
                    item_queries=['sku-a', 'sku-b'],
                )

            self.assertEqual(
                [link.url for link in links],
                [
                    'https://market.example/item/1',
                    'https://market.example/item/2',
                    'https://market.example/item/3',
                    'https://market.example/item/4',
                ],
            )
            self.assertEqual(len(session_factory.calls), 1)
            self.assertEqual(session_factory.calls[0]['max_pages'], 1)
            self.assertEqual(sessions[0].entered, 1)
            self.assertEqual(sessions[0].exited, 1)
            self.assertEqual(sessions[0].fetch.await_count, 4)
            for fetch_call in sessions[0].fetch.await_args_list:
                self.assertEqual(fetch_call.kwargs['timeout'], 60000)
                self.assertEqual(fetch_call.kwargs['wait'], 750)
                self.assertEqual(fetch_call.kwargs['load_dom'], True)
                self.assertNotIn('disable_resources', fetch_call.kwargs)
            for page in pages:
                page.page.close.assert_awaited_once()
                page.context.close.assert_awaited_once()

        import asyncio
        asyncio.run(run_test())

    def test_failed_stealth_attempt_is_skipped_without_retry_or_fallback(self) -> None:
        marketplace = self.build_test_marketplace('market_a', FETCH_MODE_STEALTH)
        successful_page = FakeFetchPage(hrefs=['https://market.example/item/2'])
        session = FakeStealthSession(
            [RuntimeError('stealth fetch failed'), successful_page]
        )
        session_factory = FakeStealthSessionFactory([session])

        async def run_test() -> None:
            with (
                patch.object(scraper, 'AsyncStealthySession', new=session_factory),
                patch.object(scraper.AsyncFetcher, 'get', new=AsyncMock()) as standard_fetch,
                patch.object(scraper.asyncio, 'sleep', new=AsyncMock()),
                patch.object(scraper, 'logger') as logger_mock,
            ):
                links = await scrape_link_results(
                    marketplaces=[marketplace],
                    item_queries=['sku-a', 'sku-b'],
                )

            self.assertEqual(
                [link.url for link in links],
                ['https://market.example/item/2'],
            )
            self.assertEqual(len(session_factory.calls), 1)
            self.assertEqual(session.fetch.await_count, 2)
            standard_fetch.assert_not_awaited()
            logger_mock.exception.assert_called_once_with(
                'Failed to scrape %s for "%s"',
                'market_a',
                'sku-a',
            )

        import asyncio
        asyncio.run(run_test())

    def test_raise_on_error_reports_failure_after_all_attempts(self) -> None:
        marketplaces = [
            self.build_test_marketplace('market_a'),
            self.build_test_marketplace('market_b'),
        ]

        async def run_test() -> None:
            with (
                patch.object(
                    scraper,
                    'scrape_search_page',
                    new=AsyncMock(
                        side_effect=[
                            RuntimeError('first failed'),
                            ['https://market.example/item/2'],
                        ]
                    ),
                ) as scrape,
                patch.object(scraper.asyncio, 'sleep', new=AsyncMock()),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    'Scrape failed for: market_a/sku-a',
                ):
                    await scrape_link_results(
                        marketplaces=marketplaces,
                        item_queries=['sku-a'],
                        raise_on_error=True,
                    )

            self.assertEqual(scrape.await_count, 2)

        import asyncio
        asyncio.run(run_test())

    def test_standard_fetch_does_not_create_stealth_session(self) -> None:
        marketplace = self.build_test_marketplace('market_a', FETCH_MODE_STANDARD)
        page = FakeFetchPage(hrefs=['https://market.example/item/1'])

        async def run_test() -> None:
            with (
                patch.object(scraper.AsyncFetcher, 'get', new=AsyncMock(return_value=page)) as fetch_mock,
                patch.object(scraper, 'AsyncStealthySession') as stealth_session_mock,
            ):
                links = await scrape_search_page(marketplace, 'sku-a')

            self.assertEqual(links, ['https://market.example/item/1'])
            fetch_mock.assert_awaited_once()
            stealth_session_mock.assert_not_called()

        import asyncio
        asyncio.run(run_test())

    def test_standard_only_batch_never_starts_browser_and_closes_responses(self) -> None:
        marketplace = self.build_test_marketplace('market_a', FETCH_MODE_STANDARD)
        pages = [
            FakeFetchPage(
                hrefs=['https://market.example/item/1'],
                page=FakeCloseableResource(),
            ),
            FakeFetchPage(
                hrefs=['https://market.example/item/2'],
                context=FakeCloseableResource(),
            ),
        ]

        async def run_test() -> None:
            with (
                patch.object(scraper.AsyncFetcher, 'get', new=AsyncMock(side_effect=pages)),
                patch.object(scraper, 'AsyncStealthySession') as stealth_session_mock,
            ):
                links = await scrape_link_results([marketplace], ['sku-a', 'sku-b'])

            self.assertEqual(
                [link.url for link in links],
                ['https://market.example/item/1', 'https://market.example/item/2'],
            )
            stealth_session_mock.assert_not_called()
            pages[0].page.close.assert_awaited_once()
            pages[1].context.close.assert_awaited_once()

        import asyncio
        asyncio.run(run_test())


    def test_closeable_resources_are_closed_after_scrape_completion(self) -> None:
        marketplace = self.build_test_marketplace('market_a')
        page = FakeFetchPage(
            hrefs=['https://market.example/item/1'],
            page=FakeCloseableResource(),
            context=FakeCloseableResource(),
        )

        async def run_test() -> None:
            with patch.object(scraper.AsyncFetcher, 'get', new=AsyncMock(return_value=page)):
                links = await scrape_search_page(marketplace, 'sku-a')

            self.assertEqual(links, ['https://market.example/item/1'])
            page.page.close.assert_awaited_once()
            page.context.close.assert_awaited_once()

        import asyncio
        asyncio.run(run_test())

    def test_closeable_resources_are_closed_after_scrape_failure(self) -> None:
        marketplace = self.build_test_marketplace('market_a')
        page = FakeFetchPage(
            status=500,
            page=FakeCloseableResource(),
            context=FakeCloseableResource(),
        )

        async def run_test() -> None:
            with patch.object(scraper.AsyncFetcher, 'get', new=AsyncMock(return_value=page)):
                with self.assertRaises(RuntimeError):
                    await scrape_search_page(marketplace, 'sku-a')

            page.page.close.assert_awaited_once()
            page.context.close.assert_awaited_once()

        import asyncio
        asyncio.run(run_test())

    @staticmethod
    def build_test_marketplace(
        key: str,
        fetch_mode: str = FETCH_MODE_STANDARD,
        fetch_wait_ms: int = 0,
    ) -> Marketplace:
        return Marketplace(
            key=key,
            base_url='https://market.example',
            search_url=lambda query: f'https://market.example/search?q={query}',
            item_hosts=('market.example',),
            item_path_markers=('/item/',),
            fetch_mode=fetch_mode,
            fetch_wait_ms=fetch_wait_ms,
        )


class FakeCloseableResource:
    def __init__(self) -> None:
        self.close = AsyncMock()


class FakeFetchPage:
    def __init__(
        self,
        status: int = 200,
        hrefs: list[str] | None = None,
        page: FakeCloseableResource | None = None,
        context: FakeCloseableResource | None = None,
    ) -> None:
        self.status = status
        self.hrefs = hrefs or []
        self.page = page
        self.context = context

    def css(self, query: str) -> SimpleNamespace:
        return SimpleNamespace(getall=lambda: self.hrefs)


class FakeStealthSession:
    def __init__(
        self,
        fetch_result: FakeFetchPage | Exception | list[FakeFetchPage | Exception],
        *,
        name: str = 'session',
        lifecycle_events: list[str] | None = None,
    ) -> None:
        self.fetch_results = list(fetch_result) if isinstance(fetch_result, list) else [fetch_result]
        self.name = name
        self.lifecycle_events = lifecycle_events
        self.entered = 0
        self.exited = 0
        self.fetch = AsyncMock(side_effect=self.fetch_page)

    async def __aenter__(self):
        self.entered += 1
        if self.lifecycle_events is not None:
            self.lifecycle_events.append(f'enter:{self.name}')
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.exited += 1
        if self.lifecycle_events is not None:
            self.lifecycle_events.append(f'exit:{self.name}')

    async def fetch_page(self, url: str, **kwargs):
        fetch_result = self.fetch_results.pop(0)

        if isinstance(fetch_result, Exception):
            raise fetch_result

        return fetch_result


class FakeStealthSessionFactory:
    def __init__(self, sessions: list[FakeStealthSession]) -> None:
        self.sessions = sessions
        self.calls = []

    def __call__(self, **kwargs) -> FakeStealthSession:
        self.calls.append(kwargs)
        return self.sessions[len(self.calls) - 1]




if __name__ == '__main__':
    unittest.main()
