"""Destructive retries recheck eligibility after backoff, without account access."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scheduled-cleanup'))
import cleanup_paused_sandboxes as cleanup
from request_control import RequestControl
from sandbox_backend import APIBackend


class DeleteRetrySafety(unittest.TestCase):
    def run_case(self, failure, change, *, recheck=True, retry_lookup_failure=None):
        now = datetime.now(timezone.utc)
        original = dict(sandboxID='old', state='paused', metadata={'env': 'test'},
                        startedAt=(now - timedelta(days=5)).isoformat(),
                        endAt=(now - timedelta(days=4)).isoformat())
        current = dict(original)
        calls = []
        clock = 100.0
        backoff_pending = False
        delete_attempts = 0
        retry_lookup_attempts = 0

        def sleep(delay):
            nonlocal clock, backoff_pending, current
            clock += delay
            if backoff_pending:
                backoff_pending = False
                if change == 'running':
                    current = dict(original, state='running')
                elif change == 'recent-pause':
                    current = dict(original, endAt=now.isoformat())
                elif change == 'metadata':
                    current = dict(original, metadata={'env': 'protected'})
                elif change == 'gone':
                    current = None

        def handler(request):
            nonlocal backoff_pending, delete_attempts, retry_lookup_attempts
            calls.append((request.method, request.url.path, clock))
            if request.url.path == '/v2/sandboxes':
                return httpx.Response(200, json=[original])
            self.assertEqual(request.url.path, '/sandboxes/old')
            if request.method == 'GET':
                if delete_attempts and retry_lookup_failure is not None:
                    retry_lookup_attempts += 1
                    if retry_lookup_failure == 'transport':
                        raise httpx.ReadError('retry lookup unavailable', request=request)
                    return httpx.Response(retry_lookup_failure)
                return httpx.Response(404) if current is None else httpx.Response(200, json=current)
            delete_attempts += 1
            if delete_attempts == 1:
                backoff_pending = True
                if failure == 'transport':
                    raise httpx.ReadError('lost response', request=request)
                return httpx.Response(failure, headers={'Retry-After': '2'})
            return httpx.Response(204)

        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, E2B_API_KEY='retry-test-key', E2B_API_URL='https://test'):
            argv = ['--days', '3', '--apply', '--metadata', 'env=test', '--concurrency', '1']
            if recheck:
                argv.append('--recheck')
            args = cleanup.parser_for_cli().parse_args(argv)
            with httpx.Client(base_url='https://test', transport=httpx.MockTransport(handler)) as client:
                api = APIBackend(client=client)
                api.control = RequestControl(20, retries=1, clock=lambda: clock, sleep=sleep)
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    if retry_lookup_failure is None:
                        result = cleanup.run(args, api, now - timedelta(days=3))
                    else:
                        with patch.object(cleanup, 'backend', return_value=api):
                            result = cleanup.main(argv + ['--lock-file', str(Path(folder) / 'run.lock')])
        self.assertEqual(result, int(retry_lookup_failure is not None), err.getvalue())
        if retry_lookup_failure is not None:
            authorization_failure = retry_lookup_failure in (401, 403)
            self.assertEqual(api.control.cancelled.is_set(), authorization_failure)
            self.assertEqual(retry_lookup_attempts, 1 if authorization_failure else 2)
            self.assertIn('check API key and region' if authorization_failure else 'after 2 attempts',
                          err.getvalue())
        return calls, out.getvalue()

    def test_recheck_skips_resumes_recent_pauses_and_metadata_changes_during_backoff(self):
        for failure in (429, 503, 'transport'):
            for change in ('running', 'recent-pause', 'metadata'):
                with self.subTest(failure=failure, change=change):
                    calls, output = self.run_case(failure, change)
                    self.assertEqual(sum(method == 'DELETE' for method, _, _ in calls), 1)
                    self.assertIn('skipped 1', output)
                    self.assertEqual(calls[-1][:2], ('GET', '/sandboxes/old'))
                    self.assertGreaterEqual(calls[-1][2] - calls[-2][2], 1)


    def test_still_eligible_retry_uses_fresh_get_and_spaces_both_requests(self):
        calls, output = self.run_case(429, None)
        deletes = [i for i, (method, path, _) in enumerate(calls)
                   if method == 'DELETE' and path == '/sandboxes/old']
        self.assertEqual(len(deletes), 2)
        self.assertEqual(calls[deletes[1] - 1][:2], ('GET', '/sandboxes/old'))
        self.assertGreaterEqual(calls[deletes[1] - 1][2] - calls[deletes[0]][2], 2)
        self.assertGreaterEqual(calls[deletes[1]][2] - calls[deletes[1] - 1][2], 0.049)
        self.assertIn('Deleted 1 of 1', output)

    def test_lost_delete_response_keeps_missing_idempotency(self):
        calls, _ = self.run_case('transport', 'gone')
        self.assertEqual(sum(method == 'DELETE' and path == '/sandboxes/old'
                             for method, path, _ in calls), 1)
        self.assertFalse(any(path.startswith('/templates/') for _, path, _ in calls))

    def test_retry_lookup_failures_prevent_delete(self):
        for failure in (401, 403, 503, 'transport'):
            with self.subTest(failure=failure):
                calls, _ = self.run_case(429, None, retry_lookup_failure=failure)
                self.assertEqual([(method, path) for method, path, _ in calls if method == 'DELETE'],
                                 [('DELETE', '/sandboxes/old')])
                self.assertEqual(calls[-1][:2], ('GET', '/sandboxes/old'))

    def test_default_mode_keeps_its_existing_retry_without_individual_gets(self):
        calls, _ = self.run_case(429, None, recheck=False)
        self.assertEqual([method for method, path, _ in calls if path == '/sandboxes/old'],
                         ['DELETE', 'DELETE'])


if __name__ == '__main__':
    unittest.main()
