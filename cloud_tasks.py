from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2
from google.protobuf import duration_pb2


logger = logging.getLogger(__name__)


FETCH_TASK_DISPATCH_DEADLINE_SECONDS = 600


def _require_env(name: str) -> str:
    value = os.getenv(name, '').strip()

    if not value:
        raise ValueError(f'{name} is required')

    return value


def _validate_target_url(target_url: str) -> None:
    parsed = urlparse(target_url)

    if (
        parsed.scheme != 'https'
        or not parsed.netloc
        or not target_url.endswith('/tasks/fetch')
    ):
        raise ValueError(
            'CLOUD_TASKS_TARGET_URL must use HTTPS and end in /tasks/fetch'
        )


class FetchTaskQueue:
    def __init__(
        self,
        *,
        project_id: str,
        location: str,
        queue: str,
        target_url: str,
        scheduler_secret: str,
        client: Any | None = None,
    ) -> None:
        _validate_target_url(target_url)
        self.project_id = project_id
        self.location = location
        self.queue = queue
        self.target_url = target_url
        self.scheduler_secret = scheduler_secret
        self.client = client or tasks_v2.CloudTasksAsyncClient()

    @classmethod
    def from_env(cls, *, scheduler_secret: str) -> FetchTaskQueue:
        return cls(
            project_id=_require_env('CLOUD_TASKS_PROJECT_ID'),
            location=_require_env('CLOUD_TASKS_LOCATION'),
            queue=_require_env('CLOUD_TASKS_QUEUE'),
            target_url=_require_env('CLOUD_TASKS_TARGET_URL'),
            scheduler_secret=scheduler_secret,
        )

    @staticmethod
    def telegram_task_id(chat_id: str, update_id: int) -> str:
        identity = f'{chat_id}:{update_id}'.encode('utf-8')
        return f'telegram-fetch-{hashlib.sha256(identity).hexdigest()}'

    @staticmethod
    def scheduler_task_id(job_name: str, schedule_time: str) -> str:
        identity = f'{job_name}:{schedule_time}'.encode('utf-8')
        return f'scheduler-fetch-{hashlib.sha256(identity).hexdigest()}'

    @staticmethod
    def sku_task_id(run_id: str, sku: str) -> str:
        identity = f'{run_id}:{sku}'.encode('utf-8')
        return f'sku-fetch-{hashlib.sha256(identity).hexdigest()}'

    async def enqueue_manual_fetch(self, chat_id: str, update_id: int) -> None:
        run_id = self.telegram_task_id(chat_id, update_id)
        await self._enqueue(
            task_id=run_id,
            payload={
                'kind': 'batch',
                'manual': True,
                'run_id': run_id,
            },
        )

    async def enqueue_scheduled_fetch(
        self,
        job_name: str,
        schedule_time: str,
    ) -> None:
        run_id = self.scheduler_task_id(job_name, schedule_time)
        await self._enqueue(
            task_id=run_id,
            payload={
                'kind': 'batch',
                'manual': False,
                'run_id': run_id,
            },
        )

    async def enqueue_sku_fetch(
        self,
        *,
        run_id: str,
        manual: bool,
        sku: str,
        name: str,
    ) -> None:
        await self._enqueue(
            task_id=self.sku_task_id(run_id, sku),
            payload={
                'kind': 'sku',
                'manual': manual,
                'run_id': run_id,
                'sku': sku,
                'name': name,
            },
        )

    async def _enqueue(self, *, task_id: str, payload: dict[str, object]) -> None:
        parent = self.client.queue_path(
            self.project_id,
            self.location,
            self.queue,
        )
        task_name = f'{parent}/tasks/{task_id}'
        task = {
            'name': task_name,
            'http_request': {
                'http_method': tasks_v2.HttpMethod.POST,
                'url': self.target_url,
                'headers': {
                    'Content-Type': 'application/json',
                    'X-Scheduler-Secret': self.scheduler_secret,
                },
                'body': json.dumps(
                    payload,
                    separators=(',', ':'),
                ).encode('utf-8'),
            },
            'dispatch_deadline': duration_pb2.Duration(
                seconds=FETCH_TASK_DISPATCH_DEADLINE_SECONDS,
            ),
        }

        try:
            await self.client.create_task(
                request={
                    'parent': parent,
                    'task': task,
                }
            )
        except AlreadyExists:
            logger.info('Fetch task already exists: %s', task_name)
