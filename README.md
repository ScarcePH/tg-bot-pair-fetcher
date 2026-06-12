# Telegram Bot Pair Fetcher

An always-on Telegram bot that searches configured marketplace result pages with Scrapling every 8 hours by default. It also supports saved SKU searches and a manual `/fetch` command.

The bot sends only new item links to Telegram. Saved SKUs, the pinned saved-SKU message ID, and previously seen item links are stored in SQLite.

## Configuration

Create a local `.env` file from the example:

```sh
cp .env.example .env
```

Set these values:

```env
TELEGRAM_BOT_TOKEN=123456789:replace-with-your-bot-token
TELEGRAM_CHAT_ID=123456789
DATABASE_PATH=bot_state.sqlite3
SCRAPE_INTERVAL_HOURS=8

MARKETPLACES=marketplace_a,ebay
MARKETPLACE_MARKETPLACE_A_BASE_URL=https://market.example
MARKETPLACE_MARKETPLACE_A_SEARCH_URL_TEMPLATE=https://market.example/search?q={query}
MARKETPLACE_MARKETPLACE_A_ITEM_HOSTS=market.example,www.market.example
MARKETPLACE_MARKETPLACE_A_ITEM_PATH_MARKERS=/item/,/product/
MARKETPLACE_MARKETPLACE_A_FETCH_MODE=standard
MARKETPLACE_MARKETPLACE_A_FETCH_WAIT_MS=0

MARKETPLACE_EBAY_BASE_URL=https://www.ebay.com
MARKETPLACE_EBAY_SEARCH_URL_TEMPLATE=https://www.ebay.com/sch/i.html?_nkw={query}
MARKETPLACE_EBAY_ITEM_HOSTS=www.ebay.com,ebay.com
MARKETPLACE_EBAY_ITEM_PATH_MARKERS=/itm/
MARKETPLACE_EBAY_FETCH_MODE=stealth
MARKETPLACE_EBAY_FETCH_WAIT_MS=1500
```

`MARKETPLACES` is comma-separated. Each key must have matching `MARKETPLACE_<KEY>_*` variables, where `<KEY>` is the uppercase marketplace key with non-alphanumeric characters changed to underscores.

Required per-marketplace values:

- `MARKETPLACE_<KEY>_BASE_URL`
- `MARKETPLACE_<KEY>_SEARCH_URL_TEMPLATE`, which must include `{query}`
- `MARKETPLACE_<KEY>_ITEM_HOSTS`, comma-separated
- `MARKETPLACE_<KEY>_ITEM_PATH_MARKERS`, comma-separated

Optional per-marketplace values:

- `MARKETPLACE_<KEY>_FETCH_MODE`: `standard` or `stealth`, default `standard`
- `MARKETPLACE_<KEY>_FETCH_WAIT_MS`: non-negative integer, default `0`

Optional stealth settings:

- `SCRAPER_ACCEPT_LANGUAGE`
- `SCRAPER_STEALTH_LOCALE`, default `en-US`
- `SCRAPER_STEALTH_TIMEZONE`, default `UTC`

`DATABASE_PATH` defaults to `bot_state.sqlite3` when omitted. For Docker, use `/app/data/bot_state.sqlite3` so the compose volume keeps state across container rebuilds.

## Telegram Setup

1. Create a bot with Telegram `@BotFather`.
2. Copy the bot token into `TELEGRAM_BOT_TOKEN`.
3. Send a message to your bot from the chat where it should post results.
4. Get the chat ID. One simple option is visiting:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getUpdates
```

Copy the numeric `chat.id` value into `TELEGRAM_CHAT_ID`.

## Local Run

```sh
pip install -r requirements.txt
scrapling install
python bot.py
```

Then save searches in Telegram:

```text
/set 854321-221 HT BROWN
/set 833603-012 OG VENOM 2016-2019
```

Run the scraper without Telegram:

```sh
python scraper.py
```

The scraper reads saved SKUs from SQLite and prints one found item link per line.

## Docker Run

```sh
docker compose up --build
```

The Docker image uses a slim Python base image and installs Scrapling browser dependencies during build. `docker-compose.yml` mounts `./data` to `/app/data` for SQLite persistence.

## Commands

- `/start`: confirms the bot is reachable.
- `/set <sku> <name>`: saves or updates a SKU search and refreshes the pinned saved-SKU list.
- `/list`: shows all saved SKU searches.
- `/unset <sku>`: removes a saved SKU search and refreshes the pinned saved-SKU list.
- `/fetch`: searches all saved SKUs immediately. If a scheduled fetch is already running, the bot replies that a fetch is already running.

Scheduled runs send nothing when no saved SKUs or no new links are found. Manual `/fetch` sends a clear message when there are no saved SKUs or when every found link was already seen.

## Duplicate Filtering

Item links are normalized by the scraper before output. Fragments are removed, links must match the marketplace item URL rules, and duplicates inside one scrape are collapsed.

After a manual or scheduled fetch sends links, those URLs are written to `seen_links`. Future scheduled runs, manual `/fetch` calls, and matches from other saved SKUs all share the same dedup table, so the first send wins.
