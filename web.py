from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from telegram import Update
from telegram.ext import Application

from bot import build_application, require_env, run_fetch


logger = logging.getLogger(__name__)


def _valid_secret(request: Request, header: str, expected: str) -> bool:
    supplied = request.headers.get(header, '')
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def create_app(
    telegram_application: Application | None = None,
    *,
    webhook_secret: str | None = None,
    scheduler_secret: str | None = None,
) -> Starlette:
    load_dotenv()
    telegram_application = telegram_application or build_application()
    webhook_secret = webhook_secret or require_env('TELEGRAM_WEBHOOK_SECRET')
    scheduler_secret = scheduler_secret or require_env('SCHEDULER_SECRET')

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
        if not _valid_secret(request, 'X-Telegram-Bot-Api-Secret-Token', webhook_secret):
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)
        try:
            payload = await request.json()
            update = Update.de_json(payload, telegram_application.bot)
        except (ValueError, TypeError):
            return JSONResponse({'detail': 'invalid update'}, status_code=400)
        await telegram_application.process_update(update)
        return JSONResponse({'ok': True})

    async def scheduled_fetch(request: Request) -> JSONResponse:
        if not _valid_secret(request, 'X-Scheduler-Secret', scheduler_secret):
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)

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
        if not _valid_secret(request, 'X-Scheduler-Secret', scheduler_secret):
            return JSONResponse({'detail': 'unauthorized'}, status_code=401)

        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({'detail': 'invalid payload'}, status_code=400)

        if (
            not isinstance(payload, dict)
            or 'manual' not in payload
            or not isinstance(payload['manual'], bool)
        ):
            return JSONResponse({'detail': 'invalid payload'}, status_code=400)

        chat_id = str(telegram_application.bot_data['chat_id'])
        completed = await run_fetch(telegram_application, chat_id)
        status = 'completed' if completed else 'already_running_or_failed'
        return JSONResponse({'status': status})

    return Starlette(
        routes=[
            Route('/', root, methods=['GET']),
            Route('/healthz', healthz, methods=['GET']),
            Route('/telegram/webhook', telegram_webhook, methods=['POST']),
            Route('/scheduler/fetch', scheduled_fetch, methods=['POST']),
            Route('/tasks/fetch', fetch_task, methods=['POST']),
        ],
        lifespan=lifespan,
    )
