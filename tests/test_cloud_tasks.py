from __future__ import annotations

import hashlib
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from google.api_core.exceptions import AlreadyExists
    from google.cloud import tasks_v2

    from cloud_tasks import FetchTaskQueue
except ModuleNotFoundError as exc:
    AlreadyExists = None
    tasks_v2 = None
    FetchTaskQueue = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(IMPORT_ERROR is not None, f'missing optional dependency: {IMPORT_ERROR}')
class FetchTaskQueueTest(unittest.IsolatedAsyncioTestCase):
    def make_queue(self, client=None) -> FetchTaskQueue:
        if client is None:
            client = MagicMock()
            client.queue_path.return_value = (
                'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
            )
            client.create_task = AsyncMock()
        return FetchTaskQueue(
            project_id='project',
            location='asia-southeast1',
            queue='tg-bot-fetch',
            target_url='https://service.example/tasks/fetch',
            oidc_service_account='tasks@example.iam.gserviceaccount.com',
            oidc_audience='https://service.example',
            client=client,
        )

    def test_target_url_requires_https_fetch_endpoint(self) -> None:
        invalid_urls = (
            'http://service.example/tasks/fetch',
            'https://service.example/tasks/other',
            'https://service.example/tasks/fetch?redirect=/tasks/fetch',
            '/tasks/fetch',
        )
        for target_url in invalid_urls:
            with (
                self.subTest(target_url=target_url),
                self.assertRaisesRegex(
                    ValueError,
                    'must use HTTPS and end in /tasks/fetch',
                ),
            ):
                FetchTaskQueue(
                    project_id='project',
                    location='asia-southeast1',
                    queue='tg-bot-fetch',
                    target_url=target_url,
                    oidc_service_account=(
                        'tasks@example.iam.gserviceaccount.com'
                    ),
                    oidc_audience='https://service.example',
                    client=MagicMock(),
                )

    def test_oidc_audience_requires_matching_base_url(self) -> None:
        invalid_audiences = (
            'http://service.example',
            'https://other.example',
            'https://service.example/tasks/fetch',
            'https://service.example?query=1',
        )
        for audience in invalid_audiences:
            with (
                self.subTest(audience=audience),
                self.assertRaisesRegex(ValueError, 'HTTPS base URL'),
            ):
                FetchTaskQueue(
                    project_id='project',
                    location='asia-southeast1',
                    queue='tg-bot-fetch',
                    target_url='https://service.example/tasks/fetch',
                    oidc_service_account=(
                        'tasks@example.iam.gserviceaccount.com'
                    ),
                    oidc_audience=audience,
                    client=MagicMock(),
                )

    def test_from_env_requires_cloud_tasks_configuration(self) -> None:
        complete_environment = {
            'CLOUD_TASKS_PROJECT_ID': 'project',
            'CLOUD_TASKS_LOCATION': 'asia-southeast1',
            'CLOUD_TASKS_QUEUE': 'tg-bot-fetch',
            'CLOUD_TASKS_TARGET_URL': 'https://service.example/tasks/fetch',
            'CLOUD_TASKS_OIDC_SERVICE_ACCOUNT': (
                'tasks@example.iam.gserviceaccount.com'
            ),
            'CLOUD_TASKS_OIDC_AUDIENCE': 'https://service.example',
        }
        for missing_name in complete_environment:
            environment = complete_environment | {missing_name: '  '}
            with (
                self.subTest(missing_name=missing_name),
                patch.dict(os.environ, environment, clear=True),
                patch('cloud_tasks.tasks_v2.CloudTasksAsyncClient'),
                self.assertRaisesRegex(ValueError, f'{missing_name} is required'),
            ):
                FetchTaskQueue.from_env()

    def test_from_env_builds_queue_from_required_configuration(self) -> None:
        environment = {
            'CLOUD_TASKS_PROJECT_ID': 'project',
            'CLOUD_TASKS_LOCATION': 'asia-southeast1',
            'CLOUD_TASKS_QUEUE': 'tg-bot-fetch',
            'CLOUD_TASKS_TARGET_URL': 'https://service.example/tasks/fetch',
            'CLOUD_TASKS_OIDC_SERVICE_ACCOUNT': (
                'tasks@example.iam.gserviceaccount.com'
            ),
            'CLOUD_TASKS_OIDC_AUDIENCE': 'https://service.example',
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch('cloud_tasks.tasks_v2.CloudTasksAsyncClient') as client_class,
        ):
            queue = FetchTaskQueue.from_env()

        self.assertEqual(queue.project_id, 'project')
        self.assertEqual(queue.location, 'asia-southeast1')
        self.assertEqual(queue.queue, 'tg-bot-fetch')
        self.assertEqual(queue.target_url, environment['CLOUD_TASKS_TARGET_URL'])
        self.assertEqual(
            queue.oidc_service_account,
            environment['CLOUD_TASKS_OIDC_SERVICE_ACCOUNT'],
        )
        self.assertEqual(
            queue.oidc_audience,
            environment['CLOUD_TASKS_OIDC_AUDIENCE'],
        )
        self.assertIs(queue.client, client_class.return_value)

    async def test_enqueue_builds_manual_batch_http_task(self) -> None:
        queue = self.make_queue()

        await queue.enqueue_manual_fetch('123', 456)

        expected_hash = hashlib.sha256(b'123:456').hexdigest()
        parent = 'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        queue.client.queue_path.assert_called_once_with(
            'project',
            'asia-southeast1',
            'tg-bot-fetch',
        )
        queue.client.create_task.assert_awaited_once_with(
            request={
                'parent': parent,
                'task': {
                    'name': f'{parent}/tasks/telegram-fetch-{expected_hash}',
                    'http_request': {
                        'http_method': tasks_v2.HttpMethod.POST,
                        'url': 'https://service.example/tasks/fetch',
                        'headers': {
                            'Content-Type': 'application/json',
                        },
                        'oidc_token': {
                            'service_account_email': (
                                'tasks@example.iam.gserviceaccount.com'
                            ),
                            'audience': 'https://service.example',
                        },
                        'body': unittest.mock.ANY,
                    },
                    'dispatch_deadline': unittest.mock.ANY,
                },
            }
        )
        task = queue.client.create_task.await_args.kwargs['request']['task']
        self.assertEqual(task['dispatch_deadline'].seconds, 600)
        self.assertEqual(
            task['http_request']['body'],
            (
                b'{"kind":"batch","manual":true,"run_id":'
                + f'"telegram-fetch-{expected_hash}"'.encode('utf-8')
                + b'}'
            ),
        )

    async def test_duplicate_task_is_accepted(self) -> None:
        client = MagicMock()
        client.queue_path.return_value = (
            'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        )
        client.create_task = AsyncMock(side_effect=AlreadyExists('duplicate'))
        queue = self.make_queue(client)

        await queue.enqueue_manual_fetch('123', 456)

        client.create_task.assert_awaited_once()

    async def test_enqueue_builds_scheduled_http_task(self) -> None:
        queue = self.make_queue()

        await queue.enqueue_scheduled_fetch(
            'projects/project/locations/asia-southeast1/jobs/fetch',
            '2026-07-21T00:00:00Z',
        )

        identity = (
            'projects/project/locations/asia-southeast1/jobs/fetch:'
            '2026-07-21T00:00:00Z'
        )
        expected_hash = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        parent = 'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        task = queue.client.create_task.await_args.kwargs['request']['task']
        self.assertEqual(
            task['name'],
            f'{parent}/tasks/scheduler-fetch-{expected_hash}',
        )
        self.assertEqual(
            task['http_request']['body'],
            (
                b'{"kind":"batch","manual":false,"run_id":'
                + f'"scheduler-fetch-{expected_hash}"'.encode('utf-8')
                + b'}'
            ),
        )
        self.assertEqual(task['dispatch_deadline'].seconds, 600)

    async def test_enqueue_builds_deterministic_sku_task(self) -> None:
        queue = self.make_queue()

        await queue.enqueue_sku_fetch(
            run_id='run-123',
            manual=True,
            sku='833603-012',
            name='HT BROWN',
        )

        expected_hash = hashlib.sha256(
            b'run-123:833603-012'
        ).hexdigest()
        task = queue.client.create_task.await_args.kwargs['request']['task']
        self.assertTrue(
            task['name'].endswith(f'/tasks/sku-fetch-{expected_hash}')
        )
        self.assertEqual(
            task['http_request']['body'],
            (
                b'{"kind":"sku","manual":true,"run_id":"run-123",'
                b'"sku":"833603-012","name":"HT BROWN"}'
            ),
        )
        self.assertEqual(task['dispatch_deadline'].seconds, 600)

    async def test_sku_retry_uses_same_task_id_and_accepts_duplicate(self) -> None:
        client = MagicMock()
        client.queue_path.return_value = (
            'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        )
        client.create_task = AsyncMock(
            side_effect=[None, AlreadyExists('duplicate')]
        )
        queue = self.make_queue(client)

        for _attempt in range(2):
            await queue.enqueue_sku_fetch(
                run_id='run-123',
                manual=False,
                sku='833603-012',
                name='HT BROWN',
            )

        task_names = [
            call.kwargs['request']['task']['name']
            for call in client.create_task.await_args_list
        ]
        self.assertEqual(task_names[0], task_names[1])

    async def test_scheduled_retry_uses_same_task_id_and_accepts_duplicate(
        self,
    ) -> None:
        client = MagicMock()
        client.queue_path.return_value = (
            'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        )
        client.create_task = AsyncMock(
            side_effect=[None, AlreadyExists('duplicate')]
        )
        queue = self.make_queue(client)

        for _attempt in range(2):
            await queue.enqueue_scheduled_fetch('job-name', 'schedule-time')

        requests = [
            call.kwargs['request']
            for call in client.create_task.await_args_list
        ]
        self.assertEqual(requests[0]['task']['name'], requests[1]['task']['name'])

    async def test_scheduled_enqueue_failure_is_raised(self) -> None:
        client = MagicMock()
        client.queue_path.return_value = (
            'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        )
        client.create_task = AsyncMock(side_effect=RuntimeError('unavailable'))
        queue = self.make_queue(client)

        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await queue.enqueue_scheduled_fetch('job-name', 'schedule-time')

    async def test_enqueue_failure_is_raised(self) -> None:
        client = MagicMock()
        client.queue_path.return_value = (
            'projects/project/locations/asia-southeast1/queues/tg-bot-fetch'
        )
        client.create_task = AsyncMock(side_effect=RuntimeError('unavailable'))
        queue = self.make_queue(client)

        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await queue.enqueue_manual_fetch('123', 456)


if __name__ == '__main__':
    unittest.main()
