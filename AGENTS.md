# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.12 Telegram marketplace-fetching service. Keep responsibilities aligned with the current modules:

- `web.py`: Starlette application, webhook, health, and scheduler endpoints.
- `bot.py`: Telegram command handlers and message formatting.
- `scraper.py`: environment-driven marketplace configuration, fetching, and link extraction.
- `state.py`: Postgres persistence, deduplication, and advisory locking.
- `tests/`: `unittest` suites named `test_*.py`.
- `.env.example`, `Dockerfile`, and `docker-compose.yml`: runtime configuration and container setup.

Do not commit generated state, caches, secrets, or a local `.env`.

## Build, Test, and Development Commands

Create `.env` from `.env.example` before starting the service.

```sh
pip install -r requirements.txt   # install pinned Python dependencies
scrapling install                 # install Scrapling's browser runtime
python -m unittest discover tests # run the complete test suite
uvicorn web:create_app --factory --host 0.0.0.0 --port 8080
docker compose up --build         # run the production-like container locally
curl http://localhost:8080/healthz
```

Postgres integration tests require a disposable database and truncate application tables:

```sh
TEST_DATABASE_URL=postgresql://user:password@localhost:5432/bot_test \
  python -m unittest tests.test_state
```

## Coding Style & Naming Conventions

Follow PEP 8 with four-space indentation. Use `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for constants and environment keys. Add type hints to new public functions and keep async I/O non-blocking. Prefer small helpers with explicit error handling. No formatter or linter is currently configured, so match the surrounding code and keep imports grouped as standard library, third-party, then local.

Marketplace-specific URLs, hosts, fetch modes, and browser settings belong in environment variables and `.env.example`, not hardcoded source.

## Testing Guidelines

Use the standard-library `unittest` framework. Place tests in `tests/test_<module>.py`, name methods `test_<behavior>`, and mock Telegram, network, and browser boundaries where practical. Add regression tests for command parsing, URL filtering, configuration validation, persistence, and endpoint authentication. Run the full suite before submitting changes.

## Commit & Pull Request Guidelines

History is minimal, so use concise imperative commit subjects such as `Add webhook secret validation`. Keep each commit focused. Pull requests should explain behavior changes, list configuration or schema impacts, include test commands and results, and link relevant issues. Include request/response examples for API changes; screenshots are only necessary for user-visible Telegram formatting changes.
