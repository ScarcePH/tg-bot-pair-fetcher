from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Callable, Protocol
from urllib.parse import quote_plus, urldefrag, urljoin, urlparse

from dotenv import load_dotenv
from scrapling import Selector
from scrapling.fetchers import AsyncFetcher, AsyncStealthySession

from state import BotStateStore


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Marketplace:
    key: str
    base_url: str
    search_url: Callable[[str], str]
    item_hosts: tuple[str, ...]
    item_path_markers: tuple[str, ...]
    fetch_mode: str
    fetch_wait_ms: int


@dataclass(frozen=True)
class ScrapedLink:
    url: str
    marketplace_key: str
    query: str


class LinkSelectable(Protocol):
    def css(self, query: str):
        ...


FETCH_MODE_STANDARD = 'standard'
FETCH_MODE_STEALTH = 'stealth'
FETCH_MODES = {FETCH_MODE_STANDARD, FETCH_MODE_STEALTH}
DEFAULT_SCRAPE_DELAY_MS = 2000


def parse_csv_env(name: str) -> list[str]:
    values = [
        value.strip()
        for value in os.getenv(name, '').split(',')
        if value.strip()
    ]

    if not values:
        raise ValueError(f'{name} must contain at least one comma-separated value')

    return values


def parse_csv_value(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(',') if part.strip())


def require_env(name: str) -> str:
    value = os.getenv(name, '').strip()

    if not value:
        raise ValueError(f'{name} is required')

    return value


def get_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, '').strip()

    if not raw_value:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer') from exc

    if value < 0:
        raise ValueError(f'{name} must be at least 0')

    return value


def get_scrape_delay_ms() -> int:
    return get_int_env('SCRAPER_SCRAPE_DELAY_MS', DEFAULT_SCRAPE_DELAY_MS)


def marketplace_env_prefix(key: str) -> str:
    normalized_key = re.sub(r'[^A-Za-z0-9]+', '_', key).strip('_').upper()

    if not normalized_key:
        raise ValueError('Marketplace keys must contain at least one letter or number')

    return f'MARKETPLACE_{normalized_key}'


def build_search_url(template: str, query: str) -> str:
    try:
        return template.format(query=quote_plus(query))
    except KeyError as exc:
        raise ValueError(
            'Marketplace search URL templates may only use the {query} placeholder'
        ) from exc


def build_marketplace(key: str) -> Marketplace:
    env_prefix = marketplace_env_prefix(key)
    base_url = require_env(f'{env_prefix}_BASE_URL')
    search_url_template_env = f'{env_prefix}_SEARCH_URL_TEMPLATE'
    search_url_template = require_env(search_url_template_env)
    item_hosts = parse_csv_value(require_env(f'{env_prefix}_ITEM_HOSTS'))
    item_path_markers = parse_csv_value(require_env(f'{env_prefix}_ITEM_PATH_MARKERS'))
    fetch_mode = os.getenv(f'{env_prefix}_FETCH_MODE', FETCH_MODE_STANDARD).strip().lower()
    fetch_wait_ms = get_int_env(f'{env_prefix}_FETCH_WAIT_MS', 0)

    if '{query}' not in search_url_template:
        raise ValueError(f'{search_url_template_env} must contain {{query}}')

    if fetch_mode not in FETCH_MODES:
        supported_modes = ', '.join(sorted(FETCH_MODES))
        raise ValueError(f'{env_prefix}_FETCH_MODE must be one of: {supported_modes}')

    return Marketplace(
        key=key,
        base_url=base_url,
        search_url=lambda query, template=search_url_template: build_search_url(template, query),
        item_hosts=item_hosts,
        item_path_markers=item_path_markers,
        fetch_mode=fetch_mode,
        fetch_wait_ms=fetch_wait_ms,
    )


def get_configured_marketplaces() -> list[Marketplace]:
    keys = parse_csv_env('MARKETPLACES')
    return [build_marketplace(key) for key in keys]


def is_marketplace_item_link(url: str, marketplace: Marketplace) -> bool:
    parsed_url = urlparse(url)
    normalized_host = parsed_url.netloc.lower().removeprefix('www.')
    allowed_hosts = {host.lower().removeprefix('www.') for host in marketplace.item_hosts}

    if normalized_host not in allowed_hosts:
        return False

    return any(marker in parsed_url.path for marker in marketplace.item_path_markers)


def normalize_link(link: str, base_url: str) -> str | None:
    absolute_url = urljoin(base_url, link)
    normalized_url, _fragment = urldefrag(absolute_url)
    parsed_url = urlparse(normalized_url)

    if parsed_url.scheme not in ('http', 'https') or not parsed_url.netloc:
        return None

    return normalized_url


def extract_marketplace_links(html: str, page_url: str, marketplace: Marketplace) -> list[str]:
    page = Selector(html, url=page_url)
    return extract_marketplace_links_from_page(page, page_url, marketplace)


def extract_marketplace_links_from_page(
    page: LinkSelectable,
    page_url: str,
    marketplace: Marketplace,
) -> list[str]:
    links = []
    seen = set()

    for href in page.css('a::attr(href)').getall():
        normalized_link = normalize_link(str(href), page_url)

        if not normalized_link or not is_marketplace_item_link(normalized_link, marketplace):
            continue

        if normalized_link in seen:
            continue

        seen.add(normalized_link)
        links.append(normalized_link)

    return links


async def scrape_search_page(
    marketplace: Marketplace,
    query: str,
    stealth_session: AsyncStealthySession | None = None,
) -> list[str]:
    search_url = marketplace.search_url(query)
    logger.info('Scraping %s for "%s": %s', marketplace.key, query, search_url)
    page = None

    try:
        if marketplace.fetch_mode == FETCH_MODE_STEALTH:
            if stealth_session is None:
                raise RuntimeError(f'{marketplace.key} requires a stealth Scrapling session')

            fetch_options = build_stealth_fetch_options(marketplace.fetch_wait_ms)
            accept_language = os.getenv('SCRAPER_ACCEPT_LANGUAGE', '').strip()

            if accept_language:
                fetch_options['extra_headers'] = {'Accept-Language': accept_language}

            page = await stealth_session.fetch(search_url, **fetch_options)
        else:
            page = await AsyncFetcher.get(
                search_url,
                follow_redirects=True,
                impersonate='chrome',
                stealthy_headers=True,
                timeout=30,
            )

        if page.status >= 400:
            raise RuntimeError(f'{search_url} returned HTTP {page.status}')

        return extract_marketplace_links_from_page(page, search_url, marketplace)
    finally:
        await close_scrape_resources(page)


def build_stealth_fetch_options(fetch_wait_ms: int) -> dict[str, object]:
    return {
        'timeout': 60000,
        'wait': fetch_wait_ms,
        'load_dom': True,
    }


async def close_scrape_resources(page: object | None) -> None:
    if page is None:
        return

    seen: set[int] = set()

    for resource in (
        getattr(page, 'page', None),
        page,
        getattr(page, 'context', None),
        getattr(getattr(page, 'page', None), 'context', None),
    ):
        await close_resource(resource, seen)


async def close_resource(resource: object | None, seen: set[int]) -> None:
    if resource is None or id(resource) in seen:
        return

    seen.add(id(resource))
    close = getattr(resource, 'close', None)

    if not callable(close):
        return

    result = close()

    if inspect.isawaitable(result):
        await result


async def scrape_links(
    marketplaces: list[Marketplace] | None = None,
    item_queries: list[str] | None = None,
) -> list[str]:
    return [link.url for link in await scrape_link_results(marketplaces, item_queries)]


async def scrape_link_results(
    marketplaces: list[Marketplace] | None = None,
    item_queries: list[str] | None = None,
) -> list[ScrapedLink]:
    marketplaces = marketplaces or get_configured_marketplaces()
    item_queries = item_queries or []
    scrape_delay_ms = get_scrape_delay_ms()

    found_links: list[ScrapedLink] = []
    needs_stealth_session = any(marketplace.fetch_mode == FETCH_MODE_STEALTH for marketplace in marketplaces)
    total_attempts = len(marketplaces) * len(item_queries)
    completed_attempts = 0

    async with AsyncExitStack() as stack:
        stealth_session = None

        if needs_stealth_session:
            stealth_session = await stack.enter_async_context(
                AsyncStealthySession(
                    block_webrtc=True,
                    headless=True,
                    locale=os.getenv('SCRAPER_STEALTH_LOCALE', 'en-US').strip() or 'en-US',
                    max_pages=1,
                    timezone_id=os.getenv('SCRAPER_STEALTH_TIMEZONE', 'UTC').strip() or 'UTC',
                )
            )

        for marketplace in marketplaces:
            for query in item_queries:
                try:
                    links = await scrape_search_page(marketplace, query, stealth_session)
                    found_links.extend(
                        ScrapedLink(url=link, marketplace_key=marketplace.key, query=query)
                        for link in links
                    )
                except Exception:
                    logger.exception('Failed to scrape %s for "%s"', marketplace.key, query)
                finally:
                    completed_attempts += 1

                    if completed_attempts < total_attempts and scrape_delay_ms:
                        await asyncio.sleep(scrape_delay_ms / 1000)

    return found_links


async def main() -> None:
    load_dotenv()
    state_store = BotStateStore()
    state_store.initialize()
    item_queries = [search.sku for search in state_store.list_saved_searches()]
    links = await scrape_links(item_queries=item_queries)

    for link in links:
        print(link)


if __name__ == '__main__':
    asyncio.run(main())
