import asyncio
import os
from types import SimpleNamespace

from dotenv import load_dotenv
from telegram import Bot

from bot import format_scraped_links_for_telegram, send_links
from scraper import ScrapedLink
from state import SavedSearch


async def main() -> None:
    load_dotenv()

    bot = Bot(token=os.environ['TELEGRAM_BOT_TOKEN'])
    chat_id = os.environ['TELEGRAM_RESULT_CHAT_ID']

    saved_search = SavedSearch(
        sku='TEST-123',
        name='Nike sb stefan janoski OG venom',
        image_url='https://www.img.scarceph.com/inv/OG.PNG',
    )

    links = [
        ScrapedLink(
            url='https://example.com/listing-1',
            marketplace_key='preview',
            query='TEST-123',
        ),
        ScrapedLink(
            url='https://example.com/listing-2',
            marketplace_key='preview',
            query='TEST-123',
        ),
    ]

    alerts = format_scraped_links_for_telegram(saved_search, links)

    async with bot:
        await send_links(SimpleNamespace(bot=bot), chat_id, alerts)


if __name__ == '__main__':
    asyncio.run(main())
