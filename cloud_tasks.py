from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any
from urllib.parse import urlparse

from google.api_core.exceptions import AlreadyExists
from google.cloud import tasks_v2


logger = logging.getLogger(__name__)


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


class ManualFetchTaskQueue:
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
    def from_env(cls, *, scheduler_secret: str) -> ManualFetchTaskQueue:
        return cls(
            project_id=_require_env('CLOUD_TASKS_PROJECT_ID'),
            location=_require_env('CLOUD_TASKS_LOCATION'),
            queue=_require_env('CLOUD_TASKS_QUEUE'),
            target_url=_require_env('CLOUD_TASKS_TARGET_URL'),
            scheduler_secret=scheduler_secret,
        )

    @staticmethod
    def task_id(chat_id: str, update_id: int) -> str:
        identity = f'{chat_id}:{update_id}'.encode('utf-8')
        return f'telegram-fetch-{hashlib.sha256(identity).hexdigest()}'

    async def enqueue_manual_fetch(self, chat_id: str, update_id: int) -> None:
        parent = self.client.queue_path(
            self.project_id,
            self.location,
            self.queue,
        )
        task_name = f'{parent}/tasks/{self.task_id(chat_id, update_id)}'
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
                    {'manual': True},
                    separators=(',', ':'),
                ).encode('utf-8'),
            },
        }

        try:
            await self.client.create_task(
                request={
                    'parent': parent,
                    'task': task,
                }
            )
        except AlreadyExists:
            logger.info('Manual fetch task already exists: %s', task_name)
