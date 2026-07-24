# Telegram Bot Pair Fetcher

An HTTP webhook service for Google Cloud Run. Telegram commands and Cloud
Scheduler enqueue scraping through Cloud Tasks, while Postgres stores saved
SKUs, pinned-message state, and globally deduplicated item links.

## Configuration

Copy `.env.example` to `.env` and set:

```env
TELEGRAM_BOT_TOKEN=123456789:replace-with-your-bot-token
TELEGRAM_CHAT_ID=123456789
DATABASE_URL=postgresql://user:password@host:5432/database
TELEGRAM_WEBHOOK_SECRET=replace-with-a-random-webhook-secret
SERVICE_ROLE=webhook
CLOUD_TASKS_PROJECT_ID=replace-with-your-project-id
CLOUD_TASKS_LOCATION=replace-with-your-cloud-run-region
CLOUD_TASKS_QUEUE=tg-bot-fetch
CLOUD_TASKS_TARGET_URL=https://replace-with-your-worker-url/tasks/fetch
CLOUD_TASKS_OIDC_SERVICE_ACCOUNT=tg-bot-tasks-invoker@replace-with-your-project-id.iam.gserviceaccount.com
CLOUD_TASKS_OIDC_AUDIENCE=https://replace-with-your-worker-url
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
- Optional browser settings: `SCRAPER_ACCEPT_LANGUAGE`, `SCRAPER_SCRAPE_DELAY_MS` (global delay between scrape attempts, default `2000`), `SCRAPER_STEALTH_LOCALE`, and `SCRAPER_STEALTH_TIMEZONE`.

See [.env.example](.env.example) for a complete example.

### Scraper behavior

Marketplace/SKU combinations are scraped sequentially, with
`SCRAPER_SCRAPE_DELAY_MS` controlling the delay between attempts (default
`2000`). When a batch includes stealth marketplaces, it opens one shared
`AsyncStealthySession(max_pages=1)` for the batch and uses DOM-loaded readiness
(`load_dom=True`) for each stealth navigation. Each attempt defensively closes
the page and browser context resources it receives before moving to the next
combination.

If a stealth attempt fails, the error is logged and that marketplace/SKU
combination is skipped. The restored scraper does not automatically retry with
a fresh session, replace the shared session, fall back to standard HTTP, or emit
detailed per-attempt timing logs. Marketplaces explicitly configured with
`FETCH_MODE=standard` continue to use the standard HTTP fetcher.

## HTTP API

- `SERVICE_ROLE=webhook` exposes `/`, `/healthz`, and `/telegram/webhook`.
  Telegram updates still require the configured Telegram webhook secret.
- `SERVICE_ROLE=worker` exposes `/healthz`, `/scheduler/fetch`, and
  `/tasks/fetch`. Cloud Run IAM authenticates these routes before requests reach
  the application.
- `/scheduler/fetch` requires the Cloud Scheduler job-name and schedule-time
  headers, enqueues a deterministic batch task, and returns
  `202 {"status":"queued"}`.
- `/tasks/fetch` accepts validated `batch` and `sku` payloads. A batch task
  snapshots saved SKUs and enqueues one deterministic child task per SKU.

Every Cloud Task carries an OIDC token for
`CLOUD_TASKS_OIDC_SERVICE_ACCOUNT`, with the worker base URL as its audience.
The public webhook service has no worker routes, so requests to either worker
path return `404` even though the webhook service is unauthenticated.

A Postgres advisory lock remains the final safeguard around each SKU worker.

## Local Run

Install dependencies and the Scrapling browser:

```sh
pip install -r requirements.txt
scrapling install
SERVICE_ROLE=webhook uvicorn web:create_app --factory --host 0.0.0.0 --port 8080
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

Deploy the same tested image to a public webhook service and a private worker.
Keep runtime secrets in Secret Manager and use distinct runtime and invoker
service accounts. Follow
[the ordered GCP OIDC cutover guide](docs/gcp-oidc-cutover.md) before enabling
the two-service deployment workflow.

Keep request-based Cloud Run billing. The deployment workflow sets Cloud Run's
request timeout to 540 seconds, and every created Cloud Task has an explicit
600-second dispatch deadline. Before deploying, enable the Cloud Tasks API,
create the `tg-bot-fetch` queue in the Cloud Run region, and grant the Cloud Run
runtime service account `roles/cloudtasks.enqueuer`. Configure the queue for
one concurrent dispatch and three delivery attempts:

```sh
gcloud tasks queues update tg-bot-fetch \
  --location YOUR_REGION \
  --max-concurrent-dispatches 1 \
  --max-attempts 3

gcloud tasks queues describe tg-bot-fetch \
  --location YOUR_REGION \
  --format='yaml(rateLimits.maxConcurrentDispatches,retryConfig.maxAttempts)'
```

Cloud Tasks does not guarantee FIFO execution order; task correctness relies
on deterministic IDs, global link deduplication, and the advisory lock rather
than dispatch order. Queue creation, IAM, and runtime configuration remain
outside this application.

Set the Telegram webhook after deployment:

```sh
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"https://YOUR_SERVICE_URL/telegram/webhook\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\"}"
```

Create an eight-hour Scheduler job that authenticates to the private worker:

```sh
gcloud scheduler jobs create http tg-bot-pair-fetcher \
  --location YOUR_REGION \
  --schedule '0 */8 * * *' \
  --uri 'https://YOUR_WORKER_URL/scheduler/fetch' \
  --http-method POST \
  --oidc-service-account-email 'tg-bot-scheduler-invoker@YOUR_PROJECT_ID.iam.gserviceaccount.com' \
  --oidc-token-audience 'https://YOUR_WORKER_URL'
```

Cloud Scheduler adds the job-name and schedule-time headers used to derive a
stable task ID. If Scheduler retries the same invocation, the existing task is
treated as a successful enqueue. When upgrading an existing deployment, update
the Scheduler URI to `/scheduler/fetch` immediately after the compatible
application version is deployed.

The service has no polling loop or in-process timer, so Cloud Run can scale to
zero between webhook, Scheduler, and Cloud Tasks requests.

## Telegram Commands

- `/start`: shows command help.
- `/set <sku> <name>`: saves or updates a SKU and refreshes one pinned aggregate list.
- `/list`: lists saved SKU searches.
- `/unset <sku>`: removes a saved SKU and refreshes the pinned list.
- `/fetch`: queues all saved SKUs for immediate per-SKU searches.

Each completed SKU sends its new links labeled with the saved name, or one
SKU-specific no-results message. A failed SKU sends a concise failure message.
URLs recorded in `seen_links` are deduplicated across all manual and scheduled
runs.

## Tests

```sh
python -m unittest discover tests
```

Production and CI install the fully resolved, hashed `requirements.lock`.
Regenerate it after changing a direct pin in `requirements.txt`:

```sh
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt
```

Postgres integration tests are skipped unless `TEST_DATABASE_URL` points to a disposable test database. Those tests truncate the bot tables:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost:5432/bot_test \
  python -m unittest tests.test_state
```
