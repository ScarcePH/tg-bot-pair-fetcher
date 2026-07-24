# Telegram Bot Pair Fetcher

A Python Telegram bot that saves SKU searches, scrapes configured
marketplaces, and reports new item links. It runs on Google Cloud as two Cloud
Run services built from the same image:

- `tg-bot-pair-fetcher`: public Telegram webhook (`SERVICE_ROLE=webhook`).
- `tg-bot-pair-fetcher-worker`: private Scheduler and Cloud Tasks worker
  (`SERVICE_ROLE=worker`).

Long-running scraping never runs inside the public webhook request. Telegram
commands and Cloud Scheduler create deterministic Cloud Tasks; the worker fans
each batch out into one bounded task per SKU. Postgres stores searches, pinned
message state, global link deduplication, and the advisory lock used to prevent
overlapping SKU work.

## Architecture

### Telegram `/fetch`

```text
Telegram
    │ POST /telegram/webhook + Telegram secret
    ▼
Public Cloud Run webhook (tg-bot-pair-fetcher)
    │ runtime: <webhook-runtime>
    │ replies to Telegram immediately, then creates a batch task
    │ Cloud Tasks Enqueuer on tg-bot-fetch
    ▼
Cloud Tasks queue (tg-bot-fetch)
    │ dispatches POST /tasks/fetch
    │ OIDC identity: <tasks-invoker>
    ▼
Private Cloud Run worker (tg-bot-pair-fetcher-worker)
    │ Cloud Run allows the request because <tasks-invoker>
    │ has Cloud Run Invoker on the worker service
    │
    │ batch task reads saved SKUs from Postgres
    │ worker runtime creates one deterministic child task per SKU
    ▼
Cloud Tasks queue (tg-bot-fetch)
    │ OIDC identity: <tasks-invoker>
    ▼
Private worker /tasks/fetch
    ├── acquires the Postgres advisory lock
    ├── scrapes the configured marketplaces for one SKU
    ├── records new links in Postgres
    └── sends the SKU result to Telegram
```


### Scheduled fetch

```text
Cloud Scheduler (every eight hours)
    │ POST /scheduler/fetch
    │ OIDC identity: <scheduler-invoker>
    ▼
Private Cloud Run worker
    │ validates Cloud Scheduler headers
    │ creates a deterministic batch task as <worker-runtime>
    ▼
tg-bot-fetch queue → batch task → one child task per SKU → scrape and report
```

The queue is intentionally limited to one concurrent dispatch. Cloud Tasks does
not guarantee FIFO order, so correctness comes from deterministic task IDs,
Postgres deduplication, and the advisory lock—not task ordering.

### Runtime IAM

| Account | Role | Applied to | Purpose |
| --- | --- | --- | --- |
| `<webhook-runtime>` | Cloud Tasks Enqueuer | `tg-bot-fetch` queue | Create Telegram-triggered batch tasks. |
| `<webhook-runtime>` | Service Account User | `<tasks-invoker>` | Attach the task invoker identity when creating tasks. |
| `<worker-runtime>` | Cloud Tasks Enqueuer | `tg-bot-fetch` queue | Create scheduled batch tasks and per-SKU child tasks. |
| `<worker-runtime>` | Service Account User | `<tasks-invoker>` | Attach the task invoker identity when creating tasks. |
| `<tasks-invoker>` | Cloud Run Invoker | `tg-bot-pair-fetcher-worker` | Deliver authenticated Cloud Tasks requests to the private worker. |
| `<scheduler-invoker>` | Cloud Run Invoker | `tg-bot-pair-fetcher-worker` | Invoke `/scheduler/fetch` from Cloud Scheduler. |
| Google-managed Cloud Tasks service agent | Cloud Tasks Service Agent | Project | Generate OIDC tokens and deliver authenticated task requests. |
| Google-managed Cloud Scheduler service agent | Cloud Scheduler Service Agent | Project | Generate the Scheduler OIDC token and deliver the scheduled request. |

The infrastructure/deployer identity also needs Service Account User on
runtime identities when it creates or updates Cloud Run services and Scheduler
jobs. That deployment permission is separate from the runtime request flow
above.

## HTTP routes

| Service role | Route | Access | Behavior |
| --- | --- | --- | --- |
| `webhook` | `GET /` | Public | Basic running response. |
| `webhook` | `GET /healthz` | Public | Health check. |
| `webhook` | `POST /telegram/webhook` | Public + Telegram webhook secret | Processes Telegram updates. |
| `worker` | `GET /healthz` | Private Cloud Run service | Health check. |
| `worker` | `POST /scheduler/fetch` | Scheduler OIDC | Enqueues one deterministic batch task and returns `202`. |
| `worker` | `POST /tasks/fetch` | Cloud Tasks OIDC | Handles a `batch` or one `sku` task. |

The webhook service does not register worker routes. The worker should have no
`allUsers` invoker binding; Cloud Run verifies Google-signed OIDC tokens before
requests reach the application.

## Telegram commands

- `/start` — show help.
- `/set <sku> <name>` — save or rename a SKU and refresh the pinned list.
- `/list` — list saved SKU searches.
- `/unset <sku>` — remove a SKU and refresh the pinned list.
- `/fetch` — acknowledge immediately and enqueue all saved SKUs.

Each SKU task sends new links, a no-results message, or a concise failure
message. Links in `seen_links` are deduplicated across manual and scheduled
runs.

## Configuration

Copy `.env.example` to `.env`. Both roles currently initialize the shared bot,
database, marketplace configuration, and Cloud Tasks client, so both require
the common settings below.

| Setting | Webhook | Worker | Purpose |
| --- | --- | --- | --- |
| `SERVICE_ROLE` | `webhook` | `worker` | Registers only that role's routes. |
| `TELEGRAM_BOT_TOKEN` | Required | Required | Receive commands and send results. |
| `TELEGRAM_CHAT_ID` | Required | Required | Restrict commands and select the result chat. |
| `DATABASE_URL` | Required | Required | Store searches, seen links, and chat state. |
| `TELEGRAM_WEBHOOK_SECRET` | Required | Not used | Validate Telegram webhook requests. |
| `CLOUD_TASKS_PROJECT_ID` | Required | Required | Queue project. |
| `CLOUD_TASKS_LOCATION` | Required | Required | Queue region. |
| `CLOUD_TASKS_QUEUE` | Required | Required | Queue name, normally `tg-bot-fetch`. |
| `CLOUD_TASKS_TARGET_URL` | Required | Required | Worker HTTPS URL ending in `/tasks/fetch`. |
| `CLOUD_TASKS_OIDC_SERVICE_ACCOUNT` | Required | Required | Task identity, normally `<tasks-invoker>`. |
| `CLOUD_TASKS_OIDC_AUDIENCE` | Required | Required | Worker base URL, without `/tasks/fetch`. |
| `MARKETPLACES` and `MARKETPLACE_<KEY>_*` | Required | Required | Marketplace routing and fetch configuration. |

For every comma-separated key in `MARKETPLACES`, configure:

```env
MARKETPLACES=marketplace_a
MARKETPLACE_MARKETPLACE_A_BASE_URL=https://market.example
MARKETPLACE_MARKETPLACE_A_SEARCH_URL_TEMPLATE=https://market.example/search?q={query}
MARKETPLACE_MARKETPLACE_A_ITEM_HOSTS=market.example,www.market.example
MARKETPLACE_MARKETPLACE_A_ITEM_PATH_MARKERS=/item/,/product/
MARKETPLACE_MARKETPLACE_A_FETCH_MODE=standard
MARKETPLACE_MARKETPLACE_A_FETCH_WAIT_MS=0
```

`BASE_URL`, `SEARCH_URL_TEMPLATE` (with `{query}`), `ITEM_HOSTS`, and
`ITEM_PATH_MARKERS` are required. `FETCH_MODE` (`standard` or `stealth`) and
`FETCH_WAIT_MS` are optional. Global browser settings include
`SCRAPER_ACCEPT_LANGUAGE`, `SCRAPER_SCRAPE_DELAY_MS` (default `2000`),
`SCRAPER_STEALTH_LOCALE` (default `en-US`), and
`SCRAPER_STEALTH_TIMEZONE` (default `UTC`). `PORT` defaults to `8080`.

Keep secret values in Secret Manager. `TELEGRAM_WEBHOOK_SECRET` may contain only
letters, numbers, `_`, and `-`. The application no longer reads
`SCHEDULER_SECRET`; Scheduler and Tasks use IAM/OIDC.

The database account must be able to create `saved_searches`, `seen_links`, and
`chat_state`; startup creates them automatically.

## Run locally

```sh
pip install -r requirements.txt
scrapling install
python -m unittest discover tests
SERVICE_ROLE=webhook uvicorn web:create_app --factory --host 0.0.0.0 --port 8080
```

Or use the production-like container:

```sh
docker compose up --build
curl http://localhost:8080/healthz
```

The standalone scraper reads saved SKUs from Postgres:

```sh
python scraper.py
```

## Deploy and operate

The GitHub Actions workflow tests and audits the app, builds one image, and
deploys that image to both Cloud Run roles using keyless Workload Identity
Federation. Shared infrastructure—IAM, service accounts, Cloud Run settings,
the queue, Scheduler, secrets metadata.

Production assumptions:

- Worker timeout: 540 seconds.
- Cloud Task dispatch deadline: 600 seconds.
- Worker concurrency and maximum instances: 1.
- Queue maximum concurrent dispatches: 1.
- Queue delivery attempts: 3.
- Scheduler target: `POST https://WORKER_URL/scheduler/fetch` with worker base
  URL as the OIDC audience.
- Task target: `POST https://WORKER_URL/tasks/fetch` with worker base URL as
  the OIDC audience.

Set the Telegram webhook after the public service is available:

```sh
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -H 'Content-Type: application/json' \
  -d "{\"url\":\"https://WEBHOOK_URL/telegram/webhook\",\"secret_token\":\"${TELEGRAM_WEBHOOK_SECRET}\"}"
```

Useful queue verification:

```sh
gcloud tasks queues describe tg-bot-fetch \
  --location YOUR_REGION \
  --format='yaml(rateLimits.maxConcurrentDispatches,retryConfig.maxAttempts)'
```

Cloud Run can scale to zero between Telegram, Scheduler, and Cloud Tasks
requests because the service has no polling loop or in-process timer.

## Tests

```sh
python -m unittest discover tests
```

Postgres integration tests require a disposable database and truncate the bot
tables:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost:5432/bot_test \
  python -m unittest tests.test_state
```

Production and CI install the hashed lockfile. Regenerate it after changing a
direct dependency:

```sh
pip-compile --generate-hashes --output-file=requirements.lock requirements.txt
```
