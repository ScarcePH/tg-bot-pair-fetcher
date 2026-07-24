from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from starlette.testclient import TestClient
    from web import create_app
except ModuleNotFoundError as exc:
    TestClient = None
    create_app = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class FakeTelegramApplication:
    def __init__(self) -> None:
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.fetch_task_queue = SimpleNamespace(
            enqueue_scheduled_fetch=AsyncMock(),
            enqueue_sku_fetch=AsyncMock(),
        )
        self.state_store = SimpleNamespace(list_saved_searches=lambda: [])
        self.bot_data = {
            'chat_id': '123',
            'fetch_task_queue': self.fetch_task_queue,
            'state_store': self.state_store,
        }
        self.initialize = AsyncMock()
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.shutdown = AsyncMock()
        self.process_update = AsyncMock()


@unittest.skipIf(IMPORT_ERROR is not None, f'missing optional dependency: {IMPORT_ERROR}')
class WebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.telegram = FakeTelegramApplication()
        self.app = create_app(
            self.telegram,
            service_role='worker',
        )
        self.webhook_app = create_app(
            self.telegram,
            service_role='webhook',
            webhook_secret='webhook-secret',
        )

    def test_health_check(self) -> None:
        with TestClient(self.app) as client:
            response = client.get('/healthz')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})

    def test_webhook_exposes_root_and_health_routes(self) -> None:
        with TestClient(self.webhook_app) as client:
            root_response = client.get('/')
            health_response = client.get('/healthz')
        self.assertEqual(root_response.status_code, 200)
        self.assertEqual(root_response.json(), {'status': 'running'})
        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_response.json(), {'status': 'ok'})

    def test_webhook_rejects_wrong_secret(self) -> None:
        with TestClient(self.webhook_app) as client:
            response = client.post('/telegram/webhook', json={'update_id': 1})
        self.assertEqual(response.status_code, 401)

    def test_scheduler_requires_scheduler_headers(self) -> None:
        with TestClient(self.app) as client:
            response = client.post('/scheduler/fetch')
        self.assertEqual(response.status_code, 400)
        self.telegram.fetch_task_queue.enqueue_scheduled_fetch.assert_not_awaited()

    def test_webhook_does_not_expose_worker_routes(self) -> None:
        with TestClient(self.webhook_app) as client:
            scheduler_response = client.post('/scheduler/fetch')
            task_response = client.post('/tasks/fetch')
        self.assertEqual(scheduler_response.status_code, 404)
        self.assertEqual(task_response.status_code, 404)

    def test_worker_does_not_expose_webhook_routes(self) -> None:
        with TestClient(self.app) as client:
            root_response = client.get('/')
            webhook_response = client.post('/telegram/webhook')
        self.assertEqual(root_response.status_code, 404)
        self.assertEqual(webhook_response.status_code, 404)

    def test_legacy_service_role_fails_startup_configuration(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            'SERVICE_ROLE must be webhook or worker',
        ):
            create_app(self.telegram, service_role='frontend')

    def test_unsupported_service_role_fails_startup_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, 'SERVICE_ROLE'):
            create_app(self.telegram, service_role='combined')

    def test_webhook_service_role_requires_webhook_secret(self) -> None:
        with (
            patch(
                'web.require_env',
                side_effect=RuntimeError(
                    'Missing required environment variable: '
                    'TELEGRAM_WEBHOOK_SECRET'
                ),
            ) as require_env,
            self.assertRaisesRegex(
                RuntimeError,
                'TELEGRAM_WEBHOOK_SECRET',
            ),
        ):
            create_app(self.telegram, service_role='webhook')
        require_env.assert_called_once_with('TELEGRAM_WEBHOOK_SECRET')

    def test_scheduler_requires_job_name_header(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                '/scheduler/fetch',
                headers={
                    'X-Scheduler-Secret': 'scheduler-secret',
                    'X-CloudScheduler-ScheduleTime': '2026-07-21T00:00:00Z',
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'missing scheduler headers'})

    def test_scheduler_requires_schedule_time_header(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                '/scheduler/fetch',
                headers={
                    'X-Scheduler-Secret': 'scheduler-secret',
                    'X-CloudScheduler-JobName': 'projects/p/locations/l/jobs/fetch',
                },
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'missing scheduler headers'})

    def test_scheduler_enqueues_fetch_without_running_it(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/scheduler/fetch',
                    headers={
                        'X-Scheduler-Secret': 'scheduler-secret',
                        'X-CloudScheduler-JobName': (
                            'projects/p/locations/l/jobs/fetch'
                        ),
                        'X-CloudScheduler-ScheduleTime': (
                            '2026-07-21T00:00:00Z'
                        ),
                    },
                )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {'status': 'queued'})
        self.telegram.fetch_task_queue.enqueue_scheduled_fetch.assert_awaited_once_with(
            'projects/p/locations/l/jobs/fetch',
            '2026-07-21T00:00:00Z',
        )
        run_sku_fetch.assert_not_awaited()

    def test_scheduler_duplicate_enqueue_returns_queued(self) -> None:
        self.telegram.fetch_task_queue.enqueue_scheduled_fetch = AsyncMock()
        with TestClient(self.app) as client:
            response = client.post(
                '/scheduler/fetch',
                headers={
                    'X-Scheduler-Secret': 'scheduler-secret',
                    'X-CloudScheduler-JobName': 'job-name',
                    'X-CloudScheduler-ScheduleTime': 'schedule-time',
                },
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {'status': 'queued'})

    def test_scheduler_enqueue_failure_is_server_error(self) -> None:
        self.telegram.fetch_task_queue.enqueue_scheduled_fetch = AsyncMock(
            side_effect=RuntimeError('queue unavailable')
        )
        with (
            TestClient(self.app, raise_server_exceptions=False) as client,
            patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch,
        ):
            response = client.post(
                '/scheduler/fetch',
                headers={
                    'X-Scheduler-Secret': 'scheduler-secret',
                    'X-CloudScheduler-JobName': 'job-name',
                    'X-CloudScheduler-ScheduleTime': 'schedule-time',
                },
            )
        self.assertEqual(response.status_code, 500)
        run_sku_fetch.assert_not_awaited()

    def test_worker_relies_on_cloud_run_iam_not_shared_secret(self) -> None:
        with TestClient(self.app) as client:
            response = client.post(
                '/tasks/fetch',
                json={
                    'kind': 'batch',
                    'manual': False,
                    'run_id': 'run-123',
                },
            )
        self.assertEqual(response.status_code, 200)

    def test_batch_task_enqueues_one_sku_task_per_saved_search(self) -> None:
        from state import SavedSearch

        self.telegram.state_store.list_saved_searches = lambda: [
            SavedSearch(sku='sku-a', name='NAME A'),
            SavedSearch(sku='sku-b', name='NAME B'),
        ]

        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json={
                        'kind': 'batch',
                        'manual': True,
                        'run_id': 'run-123',
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'queued', 'tasks': 2})
        self.assertEqual(
            [
                call.kwargs
                for call in self.telegram.fetch_task_queue.enqueue_sku_fetch.await_args_list
            ],
            [
                {
                    'run_id': 'run-123',
                    'manual': True,
                    'sku': 'sku-a',
                    'name': 'NAME A',
                },
                {
                    'run_id': 'run-123',
                    'manual': True,
                    'sku': 'sku-b',
                    'name': 'NAME B',
                },
            ],
        )
        run_sku_fetch.assert_not_awaited()

    def test_retrying_batch_reuses_deterministic_child_identities(self) -> None:
        from state import SavedSearch

        self.telegram.state_store.list_saved_searches = lambda: [
            SavedSearch(sku='sku-a', name='NAME A'),
        ]
        payload = {'kind': 'batch', 'manual': False, 'run_id': 'run-123'}

        with TestClient(self.app) as client:
            for _attempt in range(2):
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json=payload,
                )

        self.assertEqual(response.status_code, 200)
        calls = self.telegram.fetch_task_queue.enqueue_sku_fetch.await_args_list
        self.assertEqual(calls[0].kwargs, calls[1].kwargs)

    def test_sku_task_dispatches_one_sku_worker(self) -> None:
        with patch(
            'web.run_sku_fetch',
            new=AsyncMock(return_value=True),
        ) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json={
                        'kind': 'sku',
                        'manual': False,
                        'run_id': 'run-123',
                        'sku': 'sku-a',
                        'name': 'NAME A',
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'completed'})
        saved_search = run_sku_fetch.await_args.args[2]
        self.assertEqual((saved_search.sku, saved_search.name), ('sku-a', 'NAME A'))

    def test_worker_rejects_empty_payload(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'invalid payload'})
        run_sku_fetch.assert_not_awaited()

    def test_worker_rejects_malformed_json(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={
                        'X-Scheduler-Secret': 'scheduler-secret',
                        'Content-Type': 'application/json',
                    },
                    content=b'{',
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'invalid payload'})
        run_sku_fetch.assert_not_awaited()

    def test_worker_rejects_missing_manual_value(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json={'kind': 'batch', 'manual': False},
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'invalid payload'})
        run_sku_fetch.assert_not_awaited()

    def test_worker_rejects_non_boolean_manual_value(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json={
                        'kind': 'batch',
                        'manual': 1,
                        'run_id': 'run-123',
                    },
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'invalid payload'})
        run_sku_fetch.assert_not_awaited()

    def test_worker_rejects_non_object_json(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json=[{'manual': True}],
                )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {'detail': 'invalid payload'})
        run_sku_fetch.assert_not_awaited()

    def test_worker_rejects_incomplete_sku_payload(self) -> None:
        with patch('web.run_sku_fetch', new=AsyncMock()) as run_sku_fetch:
            with TestClient(self.app) as client:
                response = client.post(
                    '/tasks/fetch',
                    headers={'X-Scheduler-Secret': 'scheduler-secret'},
                    json={
                        'kind': 'sku',
                        'manual': True,
                        'run_id': 'run-123',
                        'sku': 'sku-a',
                    },
                )
        self.assertEqual(response.status_code, 400)
        run_sku_fetch.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
