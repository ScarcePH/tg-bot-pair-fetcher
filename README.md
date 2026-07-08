# Telegram Bot Pair Fetcher

An HTTP webhook service for Google Cloud Run. Telegram delivers commands to the service, Cloud Scheduler triggers periodic marketplace scraping, and Postgres stores saved SKUs, pinned-message state, and globally deduplicated item links.

## Configuration

Copy `.env.example` to `.env` and set:

```env
TELEGRAM_BOT_TOKEN=123456789:replace-with-your-bot-token
TELEGRAM_CHAT_ID=123456789
DATABASE_URL=postgresql://user:password@host:5432/database
TELEGRAM_WEBHOOK_SECRET=replace-with-a-random-webhook-secret
SCHEDULER_SECRET=replace-with-a-random-scheduler-secret
PORT=8080
```

The database user must be able to create the `saved_searches`, `seen_links`, and `chat_state` tables. Tables are created automatically at startup. Use a Postgres URL reachable from Cloud Run; for Cloud SQL, configure the Cloud Run connection/network path appropriate to the URL.

`TELEGRAM_WEBHOOK_SECRET` must use only characters accepted by Telegram (`A-Z`, `a-z`, `0-9`, `_`, and `-`). Generate secrets independently, for example:

```sh
openssl rand -hex 32
```

Marketplace configuration stays in environment variables. `MARKETPLACES` is comma-separated, and each key has matching `MARKETPLACE_<KEY>_*` values:

- Required: `BASE_URL`, `SEARCH_URL_TEMPLATE` (containing `{query}`), `ITEM_HOSTS`, and `ITEM_PATH_MARKERS`.
- Optional: `FETCH_MODE` (`standard` or `stealth`) and non-negative `FETCH_WAIT_MS`.
- Optional browser settings: `SCRAPER_ACCEPT_LANGUAGE`, `SCRAPER_STEALTH_LOCALE`, and `SCRAPER_STEALTH_TIMEZONE`.

See [.env.example](.env.example) for a complete example.

## HTTP API

- `GET /healthz` returns service health.
- `POST /telegram/webhook` accepts Telegram updates only when `X-Telegram-Bot-Api-Secret-Token` matches `TELEGRAM_WEBHOOK_SECRET`.
- `POST /tasks/fetch` starts a scheduled fetch only when `X-Scheduler-Secret` matches `SCHEDULER_SECRET`.

A Postgres advisory lock prevents overlapping `/fetch` and Scheduler runs across separate Cloud Run instances.

## Local Run

Install dependencies and the Scrapling browser:

```sh
pip install -r requirements.txt
scrapling install
uvicorn web:create_app --factory --host 0.0.0.0 --port 8080
```

Or run the same service in Docker:

```sh
docker compose up --build
curl http://localhost:8080/healthz
```

The standalone scraper still reads saved SKUs from Postgres:

```sh
python scraper.py
```

## Cloud Run Deployment

One direct deployment flow with `gcloud` is:

```sh
gcloud run deploy tg-bot-pair-fetcher \
  --source . \
  --region YOUR_REGION \
  --allow-unauthenticated \
  --set-env-vars TELEGRAM_BOT_TOKEN=YOUR_TOKEN,TELEGRAM_CHAT_ID=YOUR_CHAT_ID,DATABASE_URL=YOUR_DATABASE_URL,TELEGRAM_WEBHOOK_SECRET=YOUR_WEBHOOK_SECRET,SCHEDULER_SECRET=YOUR_SCHEDULER_SECRET
```

Also supply all marketplace variables, preferably through your normal Secret Manager/environment deployment configuration. Cloud Run may remain publicly reachable because both POST endpoints authenticate their own shared secret; `/healthz` is intentionally public.

Set the Telegram webhook after deployment:

```sh
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"https://YOUR_SERVICE_URL/telegram/webhook\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\"}"
```

Create an eight-hour Scheduler job:

```sh
gcloud scheduler jobs create http tg-bot-pair-fetcher \
  --location YOUR_REGION \
  --schedule '0 */8 * * *' \
  --uri 'https://YOUR_SERVICE_URL/tasks/fetch' \
  --http-method POST \
  --headers "X-Scheduler-Secret=${SCHEDULER_SECRET}"
```

The service has no polling loop or in-process timer, so Cloud Run can scale to zero between webhook and Scheduler requests.

## Telegram Commands

- `/start`: shows command help.
- `/set <sku> <name>`: saves or updates a SKU and refreshes one pinned aggregate list.
- `/list`: lists saved SKU searches.
- `/unset <sku>`: removes a saved SKU and refreshes the pinned list.
- `/fetch`: searches all saved SKUs immediately.

Scheduled runs send nothing when no new links exist. URLs recorded in `seen_links` are deduplicated across all manual and scheduled runs.

## Tests

```sh
python -m unittest discover tests
```

Postgres integration tests are skipped unless `TEST_DATABASE_URL` points to a disposable test database. Those tests truncate the bot tables:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost:5432/bot_test \
  python -m unittest tests.test_state
```
