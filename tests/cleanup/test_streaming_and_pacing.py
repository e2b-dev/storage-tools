"""HTTP-level pacing, retries and bounded discovery without account access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scheduled-cleanup'))
import cleanup_paused_sandboxes as cleanup
from cleanup_journal import run_lock
from request_control import AuthorizationError, BackendError, Missing, RequestControl, retry_after
from sandbox_backend import APIBackend, normalize


class Clock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


def resource(sid='old', days=4):
    now = datetime.now(timezone.utc)
    return {'sandboxID': sid, 'state': 'paused', 'metadata': {},
            'startedAt': (now - timedelta(days=days)).isoformat(),
            'endAt': (now - timedelta(days=days)).isoformat()}


class Pacing(unittest.TestCase):
    def test_all_gets_deletes_and_retries_share_twenty_per_second_without_bursts(self):
        controller = RequestControl(20)
        starts = []

        def handler(request):
            starts.append((time.monotonic(), request.method))
            return httpx.Response(200, json=[])

        with httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler)) as client:
            with ThreadPoolExecutor(max_workers=10) as pool:
                list(pool.map(lambda i: controller.request(client, 'GET' if i % 2 else 'DELETE', '/test'), range(40)))
        self.assertEqual(len(starts), 40)
        self.assertTrue(all(b[0] - a[0] >= 0.04 for a, b in zip(starts, starts[1:])))
        self.assertGreaterEqual(starts[-1][0] - starts[0][0], 1.94)

    def test_rate_limit_retries_honor_retry_after_and_consume_the_budget(self):
        clock = Clock()
        controller = RequestControl(20, clock=clock.time, sleep=clock.sleep)
        starts = []

        def handler(request):
            starts.append(clock.time())
            if len(starts) == 1:
                return httpx.Response(429, headers={'Retry-After': '2'})
            return httpx.Response(204)

        with httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler)) as client:
            self.assertEqual(controller.request(client, 'DELETE', '/sandboxes/old').status_code, 204)
        self.assertEqual(controller.attempts, 2)
        self.assertEqual(controller.retried, 1)
        self.assertGreaterEqual(starts[1] - starts[0], 2)

    def test_retries_are_bounded_and_transport_errors_back_off(self):
        for transport_error in (False, True):
            with self.subTest(transport_error=transport_error):
                clock = Clock()
                controller = RequestControl(20, retries=2, clock=clock.time, sleep=clock.sleep)

                def handler(request):
                    if transport_error:
                        raise httpx.ReadError('connection dropped', request=request)
                    return httpx.Response(503)

                with httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(BackendError):
                        controller.request(client, 'DELETE', '/sandboxes/old')
                self.assertEqual(controller.attempts, 3)
                self.assertGreaterEqual(clock.time(), 103)

    def test_unauthorized_requests_stop_subsequent_calls_and_404_is_not_retried(self):
        for status, exception in [(401, AuthorizationError), (403, AuthorizationError), (404, Missing)]:
            controller = RequestControl()
            with httpx.Client(base_url='https://test', transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
                with self.assertRaises(exception):
                    controller.request(client, 'GET', '/test')
                self.assertEqual(controller.attempts, 1)
                if status != 404:
                    with self.assertRaisesRegex(BackendError, 'stopped'):
                        controller.request(client, 'GET', '/test')
                    self.assertEqual(controller.attempts, 1)

    def test_long_retry_after_fails_instead_of_retrying_early_or_hanging_cron(self):
        controller = RequestControl()
        with httpx.Client(base_url='https://test', transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={'Retry-After': '3600'}))) as client:
            with self.assertRaisesRegex(BackendError, 'later run'):
                controller.request(client, 'GET', '/test')
        self.assertEqual(controller.attempts, 1)
        self.assertTrue(controller.cancelled.is_set())
        self.assertIsNone(retry_after('not a date'))
        self.assertEqual(retry_after('Wed, 01 Jan 2020 00:00:00 GMT'), 0)

    def test_exhausted_429_still_defers_other_requests(self):
        clock = Clock()
        controller = RequestControl(retries=0, clock=clock.time, sleep=clock.sleep)
        with httpx.Client(base_url='https://test', transport=httpx.MockTransport(
                lambda _: httpx.Response(429, headers={'Retry-After': '2'}))) as client:
            with self.assertRaises(BackendError):
                controller.request(client, 'GET', '/test')
        controller.wait()
        self.assertGreaterEqual(clock.time(), 102)

    def test_new_shared_cooldown_delays_a_worker_already_waiting_for_a_slot(self):
        clock = Clock()
        waiting, release = threading.Event(), threading.Event()

        def sleep(delay):
            if not waiting.is_set():
                waiting.set()
                if not release.wait(2):
                    raise AssertionError('test did not release waiting worker')
            clock.sleep(delay)

        controller = RequestControl(clock=clock.time, sleep=sleep)
        controller.wait()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(controller.wait)
            try:
                self.assertTrue(waiting.wait(2))
                controller.defer(2)
            finally:
                release.set()
            future.result(timeout=2)
        self.assertGreaterEqual(clock.time(), 102)
        self.assertEqual(controller.attempts, 2)

    def test_cancellation_wakes_workers_during_long_pacing_waits(self):
        controller = RequestControl(rate=0.1)
        controller.wait()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(controller.wait)
            controller.cancelled.set()
            with self.assertRaisesRegex(BackendError, 'stopped'):
                future.result(timeout=1)


class Streaming(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, E2B_API_KEY='offline-key')
        self.env.start()
        self.addCleanup(self.env.stop)

    def api(self, handler, page_size=1):
        client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        api = APIBackend(client=client, page_size=page_size)
        clock = Clock()
        api.control = RequestControl(clock=clock.time, sleep=clock.sleep)
        return api

    def test_pages_are_lazy_and_deletes_happen_before_the_next_page(self):
        calls = []

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.method == 'DELETE':
                return httpx.Response(204)
            self.assertEqual(request.url.params['limit'], '1')
            if 'nextToken' not in request.url.params:
                return httpx.Response(200, json=[resource('one')], headers={'X-Next-Token': 'page2'})
            return httpx.Response(200, json=[resource('two')])

        api = self.api(handler)
        iterator = api.list_paused()
        self.assertEqual(calls, [])
        first = next(iterator)
        self.assertTrue(api.kill(first.sandbox_id))
        self.assertEqual(next(iterator).sandbox_id, 'two')
        self.assertEqual(calls[:3], [('GET', '/v2/sandboxes'), ('DELETE', '/sandboxes/one'), ('GET', '/v2/sandboxes')])

    def test_start_cutoff_stops_without_fetching_the_rest_of_the_inventory(self):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=[resource('new', days=1)], headers={'X-Next-Token': 'never-read'})

        args = cleanup.parser_for_cli().parse_args(['--days', '3', '--age-from', 'started'])
        self.assertEqual(list(cleanup.selected_rows(args, self.api(handler), datetime.now(timezone.utc) - timedelta(days=3))), [])
        self.assertEqual(len(calls), 1)

    def test_pause_age_does_not_stop_at_a_recent_start(self):
        items = [resource('recent-start-old-pause', days=1), resource('other', days=1)]
        items[0]['endAt'] = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
        api = self.api(lambda _: httpx.Response(200, json=items))
        args = cleanup.parser_for_cli().parse_args(['--days', '3'])
        self.assertEqual([s.sandbox_id for s in cleanup.selected_rows(args, api, datetime.now(timezone.utc) - timedelta(days=3))], ['recent-start-old-pause'])

    def test_cursor_that_repeats_is_reported(self):
        api = self.api(lambda _: httpx.Response(200, json=[resource()], headers={'X-Next-Token': 'same'}))
        rows = api.list_paused()
        next(rows)
        with self.assertRaisesRegex(BackendError, 'did not advance'):
            next(rows)


    def test_unreadable_listing_rows_are_logged_and_skipped_without_aborting_the_run(self):
        import contextlib
        import io
        now = datetime.now(timezone.utc)
        page = [resource('good-one'), {'sandboxID': 'bad-state', 'state': 'hibernating', 'metadata': {},
                                       'startedAt': resource()['startedAt'], 'endAt': resource()['endAt']},
                {'sandboxID': 'bad-time', 'state': 'paused', 'startedAt': 'yesterday', 'endAt': resource()['endAt']},
                'not-an-object', resource('good-two')]
        killed = []

        def handler(request):
            if request.method == 'DELETE':
                killed.append(request.url.path)
                return httpx.Response(204)
            return httpx.Response(200, json=page)

        api = self.api(handler)
        args = cleanup.parser_for_cli().parse_args(['--days', '3', '--apply', '--concurrency', '1'])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cleanup.run_sandboxes(args, api, now - timedelta(days=3))
        self.assertEqual(killed, ['/sandboxes/good-one', '/sandboxes/good-two'])
        self.assertEqual(api.invalid_rows, 3)
        self.assertEqual(result, 1, 'unreadable rows must surface through the exit code for cron monitoring')
        self.assertIn("Skipped unreadable listing row 'bad-state'", err.getvalue())
        self.assertIn("Skipped unreadable listing row 'bad-time'", err.getvalue())
        self.assertIn("Skipped unreadable listing row None", err.getvalue())
        self.assertIn('Skipped 3 unreadable listing row(s)', err.getvalue())
        self.assertIn('Deleted 2 of 2', out.getvalue())

    def test_metadata_encoding_matches_the_sdk_and_invalid_pages_fail(self):
        from urllib.parse import quote, urlencode
        queries = []

        def handler(request):
            queries.append(request.url.params['metadata'])
            return httpx.Response(200, json={'unexpected': 'shape'})

        api = self.api(handler)
        with self.assertRaisesRegex(BackendError, 'invalid listing'):
            list(api.list_paused({'team/name': 'dev & staging'}))
        self.assertEqual(queries, [urlencode({quote('team/name'): quote('dev & staging')})])

    def test_invalid_cron_settings_fail_before_opening_an_api_client(self):
        for args in [['--rate', '0'], ['--rate', 'nan'], ['--page-size', '101'],
                     ['--concurrency', '0'], ['--retries', '-1'], ['--limit', '0']]:
            with self.subTest(args=args), patch.object(cleanup, 'backend', side_effect=AssertionError('account access')):
                import contextlib
                import io
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    cleanup.main(args)
                self.assertEqual(raised.exception.code, 2)

    def test_cron_deletes_before_materializing_a_million_candidates_and_obeys_limit(self):
        gate = threading.Event()

        class MillionRows:
            generated = 0
            killed = 0

            def list_paused(self, metadata):
                for i in range(1_000_000):
                    self.generated += 1
                    if self.generated == 5 and not gate.is_set():
                        raise AssertionError('discovery ran ahead of the bounded worker queue')
                    yield normalize(resource(f'old{i}'))

            def kill(self, sid):
                self.killed += 1
                gate.set()
                return True

            def get_info(self, sid):
                raise AssertionError('default cron must not make individual GETs')

            def cancel(self):
                pass

        api = MillionRows()
        args = cleanup.parser_for_cli().parse_args(['--days', '3', '--age-from', 'started', '--apply', '--limit', '8', '--concurrency', '4'])
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            result = cleanup.run_sandboxes(args, api, datetime.now(timezone.utc) - timedelta(days=3))
        self.assertEqual(result, 0)
        self.assertEqual((api.generated, api.killed), (8, 8))

    def test_overlap_lock_is_held_for_the_whole_apply_run(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'run.lock'
            with run_lock(path):
                with self.assertRaisesRegex(ValueError, 'another cleanup'):
                    with run_lock(path):
                        self.fail('overlapping writer acquired lock')
            with run_lock(path):
                pass


if __name__ == '__main__':
    unittest.main()
