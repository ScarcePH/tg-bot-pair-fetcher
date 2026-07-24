from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from telegram import Update
from telegram.ext import Application

from bot import build_application, require_env, run_sku_fetch
from state import BotStateStore, SavedSearch


logger = logging.getLogger(__name__)
SERVICE_ROLES = frozenset({'webhook', 'worker'})


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_fetch_task_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False

    common_fields_valid = (
        isinstance(payload.get('manual'), bool)
        and _is_nonempty_string(payload.get('run_id'))
    )
    if not common_fields_valid:
        return False

    if payload.get('kind') == 'batch':
        return set(payload) == {'kind', 'manual', 'run_id'}

    if payload.get('kind') == 'sku':
        return (
            set(payload) == {'kind', 'manual', 'run_id', 'sku', 'name'}
            and _is_nonempty_string(payload.get('sku'))
            and _is_nonempty_string(payload.get('name'))
        )

    return False


def _get_service_role(service_role: str | None) -> str:
    role = (service_role or os.getenv('SERVICE_ROLE', '')).strip().lower()
    if role not in SERVICE_ROLES:
        raise ValueError('SERVICE_ROLE must be webhook or worker')
    return role


def create_app(
    telegram_application: Application | None = None,
    *,
    service_role: str | None = None,
    webhook_secret: str | None = None,
) -> Starlette:
    load_dotenv()
    service_role = _get_service_role(service_role)
    telegram_application = telegram_application or build_application()
    if service_role == 'webhook':
        webhook_secret = webhook_secret or require_env('TELEGRAM_WEBHOOK_SECRET')

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        await telegram_application.initialize()
        await telegram_application.start()
        try:
            yield
        finally:
            await telegram_application.stop()
            await telegram_application.shutdown()

    async def root(_request: Request) -> JSONResponse:
        return JSONResponse({'status': 'running'})

    async def healthz(_request: Request) -> JSONResponse:
        return JSONResponse({'status': 'ok'})

    async def telegram_webhook(request: Request) -> JSONResponse:
        supplied_secret = request.headers.get(
            'X-Telegram-Bot-Api-Secret-Token',
            '',
        )
        if not supplied_secret or not hmac.compare_digest(
            supplied_secret,
            webhook_secret,
        ):
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)
        try:
            payload = await request.json()
            update = Update.de_json(payload, telegram_application.bot)
        except (ValueError, TypeError):
            return JSONResponse({'detail': 'invalid update'}, status_code=400)
        await telegram_application.process_update(update)
        return JSONResponse({'ok': True})

    async def scheduled_fetch(request: Request) -> JSONResponse:
        job_name = request.headers.get('X-CloudScheduler-JobName', '').strip()
        schedule_time = request.headers.get(
            'X-CloudScheduler-ScheduleTime',
            '',
        ).strip()
        if not job_name or not schedule_time:
            return JSONResponse(
                {'detail': 'missing scheduler headers'},
                status_code=400,
            )

        task_queue = telegram_application.bot_data['fetch_task_queue']
        await task_queue.enqueue_scheduled_fetch(job_name, schedule_time)
        return JSONResponse({'status': 'queued'}, status_code=202)

    async def fetch_task(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({'detail': 'invalid payload'}, status_code=400)

        if not _valid_fetch_task_payload(payload):
            return JSONResponse({'detail': 'invalid payload'}, status_code=400)

        task_queue = telegram_application.bot_data['fetch_task_queue']
        chat_id = str(telegram_application.bot_data['chat_id'])

        if payload['kind'] == 'batch':
            state_store: BotStateStore = telegram_application.bot_data[
                'state_store'
            ]
            saved_searches = state_store.list_saved_searches()

            if not saved_searches:
                await telegram_application.bot.send_message(
                    chat_id=chat_id,
                    text='No saved SKUs. Use /set <sku> <name> first.',
                )

            for saved_search in saved_searches:
                await task_queue.enqueue_sku_fetch(
                    run_id=payload['run_id'],
                    manual=payload['manual'],
                    sku=saved_search.sku,
                    name=saved_search.name,
                )

            return JSONResponse(
                {'status': 'queued', 'tasks': len(saved_searches)},
            )

        completed = await run_sku_fetch(
            telegram_application,
            chat_id,
            SavedSearch(sku=payload['sku'], name=payload['name']),
        )
        status = 'completed' if completed else 'already_running_or_failed'
        return JSONResponse({'status': status})

    if service_role == 'webhook':
        routes = [
            Route('/', root, methods=['GET']),
            Route('/healthz', healthz, methods=['GET']),
            Route('/telegram/webhook', telegram_webhook, methods=['POST']),
        ]
    else:
        routes = [
            Route('/healthz', healthz, methods=['GET']),
            Route('/scheduler/fetch', scheduled_fetch, methods=['POST']),
            Route('/tasks/fetch', fetch_task, methods=['POST']),
        ]

    return Starlette(routes=routes, lifespan=lifespan)
