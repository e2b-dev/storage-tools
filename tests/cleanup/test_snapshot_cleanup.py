"""Offline snapshot retention and race tests; every HTTP request uses MockTransport."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'cleanup'))
import cleanup_unused_snapshots as cleanup
from cleanup_journal import run_lock
from request_control import RequestControl
from snapshot_backend import SnapshotBackend, snapshot_state

NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=200)


def details(raw='raw1'):
    return dict(templateID=raw, lastSpawnedAt=OLD.isoformat(), createdAt=OLD.isoformat(),
                updatedAt=OLD.isoformat(), spawnCount=2, buildID='build1', buildCount=1,
                builds=[dict(buildID='build1', createdAt=OLD.isoformat(), updatedAt=OLD.isoformat(), status='ready')])


class Account:
    def __init__(self, count=1):
        self.refs = [f'team/snapshot{i}:default' for i in range(1, count + 1)]
        self.aliases = {ref: f'raw{i}' for i, ref in enumerate(self.refs, 1)}
        self.records = {raw: details(raw) for raw in self.aliases.values()}
        self.members = set(self.records)
        self.calls = []
        self.clock = 100.0
        self.sleeps = []
        self.sleep_hook = lambda delay: None
        self.hook = lambda request: None
        self.counts = {}

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.sleep_hook(delay)
        self.clock += delay

    def handler(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path, self.clock, dict(request.url.params)))
        self.counts[(method, path)] = self.counts.get((method, path), 0) + 1
        response = self.hook(request)
        if response is not None:
            return response
        if method == 'GET' and path == '/snapshots':
            name = request.url.params.get('name')
            start = int(request.url.params.get('nextToken', '0'))
            rows = [(i, ref) for i, ref in enumerate(self.refs) if i >= start
                    and self.aliases.get(ref) in self.members
                    and (not name or name in (ref, ref.split(':')[0], self.aliases.get(ref)))]
            limit = int(request.url.params['limit'])
            page = rows[:limit]
            headers = {'X-Next-Token': str(page[-1][0] + 1)} if len(rows) > limit else {}
            return httpx.Response(200, json=[dict(snapshotID=ref, sandboxID='old-parent') for _, ref in page], headers=headers)
        if method == 'GET' and path.startswith('/templates/aliases/'):
            ref = path.removeprefix('/templates/aliases/')
            raw = self.aliases.get(ref, ref if ref in self.records else None)
            return httpx.Response(404) if raw is None else httpx.Response(200, json={'templateID': raw, 'public': False})
        if path.startswith('/templates/'):
            raw = path.removeprefix('/templates/')
            if method == 'GET':
                if request.url.params.get('limit') != '1':
                    raise AssertionError('details must request the latest build')
                return httpx.Response(404) if raw not in self.records else httpx.Response(200, json=copy.deepcopy(self.records[raw]))
            if method == 'DELETE':
                if raw not in self.records:
                    return httpx.Response(404)
                del self.records[raw]
                self.members.discard(raw)
                return httpx.Response(204)
        raise AssertionError(f'unexpected account request: {method} {path}')


class SnapshotCleanup(unittest.TestCase):
    def run_case(self, account, *flags, apply=True):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, E2B_API_KEY='snapshot-test-key',
                                                               E2B_API_URL='https://test', E2B_DEBUG='false'):
            client = httpx.Client(base_url='https://test', transport=httpx.MockTransport(account.handler))
            api = SnapshotBackend(client=client)
            api.control = RequestControl(20, retries=1, clock=lambda: account.clock, sleep=account.sleep)
            account.control = api.control
            out, err = io.StringIO(), io.StringIO()
            argv = ['--unused-for-days', '90', '--concurrency', '1', '--lock-file', str(Path(folder) / 'run.lock'), *flags]
            if apply:
                argv.append('--apply')
            with patch.object(cleanup, 'backend', return_value=api), patch.object(cleanup, 'datetime') as clock, \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                clock.now.return_value = NOW
                code = cleanup.main(argv)
        return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue(), api=api)

    def deletes(self, account):
        return [(method, path) for method, path, _, _ in account.calls if method == 'DELETE']

    def test_dry_run_uses_snapshot_usage_not_old_parent_and_skips_never_used(self):
        account = Account(3)
        account.records['raw2']['lastSpawnedAt'] = (NOW - timedelta(days=1)).isoformat()
        account.records['raw3']['lastSpawnedAt'] = None
        result = self.run_case(account, apply=False)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn('buffered and can be delayed or lost', result.err)
        self.assertIn('raw1\tlast_used=', result.out)
        self.assertNotIn('raw2\tlast_used=', result.out)
        self.assertNotIn('raw3\tlast_used=', result.out)
        self.assertIn('Dry run: 1 snapshot(s) matched', result.out)
        self.assertEqual(self.deletes(account), [])
        self.assertEqual(set(account.records), {'raw1', 'raw2', 'raw3'})

    def test_recent_creation_update_and_build_activity_each_protect_the_snapshot(self):
        for field in ('createdAt', 'updatedAt', 'build-createdAt', 'build-updatedAt'):
            with self.subTest(field=field):
                account = Account()
                target = account.records['raw1']['builds'][0] if field.startswith('build-') else account.records['raw1']
                target[field.removeprefix('build-')] = (NOW - timedelta(hours=1)).isoformat()
                result = self.run_case(account)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(self.deletes(account), [])

    def test_output_distinguishes_observed_last_use_from_retention_timestamp(self):
        account = Account()
        latest_update = OLD + timedelta(days=1)
        account.records['raw1']['builds'][0]['updatedAt'] = latest_update.isoformat()
        result = self.run_case(account, apply=False)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn(f'raw1\tlast_used={OLD.isoformat()}\tretention_at={latest_update.isoformat()}', result.out)
        self.assertEqual(self.deletes(account), [])

    def test_apply_waits_120_seconds_after_last_candidate_observation_then_rechecks(self):
        account = Account(3)
        result = self.run_case(account)
        self.assertEqual(result.code, 0, result.err)
        initial = {}
        for method, path, when, _ in account.calls:
            if method == 'GET' and path in ('/templates/raw1', '/templates/raw2', '/templates/raw3'):
                initial.setdefault(path, when)
        ready_at = max(initial.values()) + 120
        for method, path, when, params in account.calls:
            if method == 'DELETE' or (path == '/snapshots' and params.get('name')):
                self.assertGreaterEqual(when, ready_at)
        self.assertEqual(len(self.deletes(account)), 3)
        self.assertTrue(all(account.counts[('GET', path)] == 2 for path in initial))

    def test_usage_becoming_visible_during_hold_preserves_snapshot(self):
        account = Account()
        def publish_usage(delay):
            if delay >= 120:
                account.records['raw1']['lastSpawnedAt'] = NOW.isoformat()
                account.records['raw1']['spawnCount'] += 1
        account.sleep_hook = publish_usage
        result = self.run_case(account)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.deletes(account), [])
        self.assertIn('state changed', result.out)

    def test_cancelled_hold_stops_without_any_delete(self):
        account = Account()
        def cancel_hold(delay):
            if delay >= 120:
                account.control.cancelled.set()
        account.sleep_hook = cancel_hold
        result = self.run_case(account)
        self.assertEqual(result.code, 1, result.err)
        self.assertIn('cleanup stopped', result.err)
        self.assertEqual(self.deletes(account), [])
        self.assertIn('raw1', account.records)

    def test_interrupt_during_hold_cancels_without_any_delete(self):
        account = Account()
        def interrupt_hold(delay):
            if delay >= 120:
                raise KeyboardInterrupt
        account.sleep_hook = interrupt_hold
        with self.assertRaises(KeyboardInterrupt):
            self.run_case(account)
        self.assertTrue(account.control.cancelled.is_set())
        self.assertEqual(self.deletes(account), [])

    def test_early_wakeup_does_not_shorten_monotonic_hold(self):
        account = Account()
        sleep = account.sleep
        def wake_early(delay):
            if delay >= 60:
                account.sleeps.append(delay)
                account.clock += 60
            else:
                sleep(delay)
        account.sleep = wake_early
        result = self.run_case(account)
        self.assertEqual(result.code, 0, result.err)
        observed = next(when for method, path, when, _ in account.calls
                        if method == 'GET' and path == '/templates/raw1')
        deleted = next(when for method, _, when, _ in account.calls if method == 'DELETE')
        self.assertGreaterEqual(deleted - observed, 120)
        self.assertEqual([delay for delay in account.sleeps if delay >= 60], [120, 60])

    def test_dry_run_has_no_observation_hold(self):
        account = Account(25)
        result = self.run_case(account, apply=False)
        self.assertEqual(result.code, 0, result.err)
        self.assertTrue(all(delay < 1 for delay in account.sleeps))
        self.assertEqual(self.deletes(account), [])
        self.assertIn('25 snapshot(s) matched', result.out)

    def test_unchanged_batch_waits_once_across_multiple_worker_groups(self):
        account = Account(25)
        result = self.run_case(account, '--concurrency', '3')
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self.deletes(account)), 25)
        self.assertEqual([delay for delay in account.sleeps if delay >= 120], [120])

    def test_unknown_or_in_progress_latest_build_is_preserved(self):
        for status in (None, 'building', 'waiting', 'future-status'):
            with self.subTest(status=status):
                account = Account()
                if status is None:
                    account.records['raw1']['builds'] = []
                else:
                    account.records['raw1']['builds'][0]['status'] = status
                result = self.run_case(account)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(self.deletes(account), [])

    def test_canonical_ids_deduplicate_tags_and_name_filter_selects_whole_resource(self):
        account = Account(2)
        account.refs.insert(1, 'team/snapshot1:v2')
        account.aliases['team/snapshot1:v2'] = 'raw1'
        result = self.run_case(account, '--name', 'team/snapshot1')
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.deletes(account), [('DELETE', '/templates/raw1')])
        self.assertIn('raw2', account.records)
        self.assertEqual(account.calls[0][3]['name'], 'team/snapshot1')

    def test_every_usage_and_build_signature_change_prevents_first_delete(self):
        fields = ('lastSpawnedAt', 'spawnCount', 'buildID', 'buildCount', 'createdAt', 'updatedAt',
                  'latest-buildID', 'latest-createdAt', 'latest-updatedAt', 'latest-status')
        for field in fields:
            with self.subTest(field=field):
                account = Account()

                def hook(request):
                    if request.method == 'GET' and request.url.path == '/templates/raw1' and account.counts[('GET', '/templates/raw1')] == 2:
                        target = account.records['raw1']['builds'][0] if field.startswith('latest-') else account.records['raw1']
                        key = field.removeprefix('latest-')
                        value = target[key]
                        target[key] = value + 1 if isinstance(value, int) else ('build2' if key == 'buildID' else
                                      'error' if key == 'status' else (OLD + timedelta(days=1)).isoformat())
                    return None

                account.hook = hook
                result = self.run_case(account)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(self.deletes(account), [])
                self.assertIn('state changed', result.out)

    def test_membership_recheck_never_deletes_a_normal_template(self):
        account = Account()
        account.hook = lambda request: httpx.Response(200, json=[]) if (
            request.url.path == '/snapshots' and request.url.params.get('name') == 'raw1') else None
        result = self.run_case(account)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.deletes(account), [])
        self.assertIn('no longer listed as a snapshot', result.out)

    def test_usage_change_during_429_503_and_transport_backoff_prevents_second_delete(self):
        for failure in (429, 503, 'transport'):
            with self.subTest(failure=failure):
                account = Account()

                def hook(request):
                    if request.method == 'DELETE':
                        account.records['raw1']['lastSpawnedAt'] = NOW.isoformat()
                        if failure == 'transport':
                            raise httpx.ReadError('lost response', request=request)
                        return httpx.Response(failure, headers={'Retry-After': '2'})
                    return None

                account.hook = hook
                result = self.run_case(account)
                self.assertEqual(result.code, 0, result.err)
                self.assertEqual(self.deletes(account), [('DELETE', '/templates/raw1')])
                first = next(i for i, call in enumerate(account.calls) if call[0] == 'DELETE')
                self.assertEqual(account.calls[first + 1][1], '/snapshots')
                self.assertGreaterEqual(account.calls[first + 1][2] - account.calls[first][2], 1)
                self.assertIn('raw1', account.records)

    def test_unchanged_retry_rechecks_and_keeps_all_attempts_paced(self):
        account = Account()
        account.hook = lambda request: httpx.Response(429, headers={'Retry-After': '2'}) if (
            request.method == 'DELETE' and account.counts[('DELETE', request.url.path)] == 1) else None
        result = self.run_case(account)
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(len(self.deletes(account)), 2)
        self.assertTrue(all(b[2] - a[2] >= 0.049 for a, b in zip(account.calls, account.calls[1:])))
        self.assertNotIn('raw1', account.records)

    def test_retry_lookup_failures_never_issue_a_second_delete(self):
        for failure in (401, 403, 503, 'transport'):
            with self.subTest(failure=failure):
                account = Account()

                def hook(request):
                    if request.method == 'DELETE':
                        return httpx.Response(429, headers={'Retry-After': '1'})
                    if request.url.path == '/templates/raw1' and self.deletes(account):
                        if failure == 'transport':
                            raise httpx.ReadError('lookup lost', request=request)
                        return httpx.Response(failure)
                    return None

                account.hook = hook
                result = self.run_case(account)
                self.assertEqual(result.code, 1, result.out)
                self.assertEqual(len(self.deletes(account)), 1)
                self.assertEqual(result.api.control.cancelled.is_set(), failure in (401, 403))
                self.assertIn('raw1', account.records)

    def test_in_use_400_is_reported_nonzero_and_other_snapshots_continue(self):
        account = Account(2)
        account.hook = lambda request: httpx.Response(400) if (
            request.method == 'DELETE' and request.url.path == '/templates/raw1') else None
        result = self.run_case(account)
        self.assertEqual(result.code, 1)
        self.assertIn('blocked 1', result.out)
        self.assertIn('may still be in use', result.err)
        self.assertIn('raw1', account.records)
        self.assertNotIn('raw2', account.records)

    def test_delete_404_is_idempotent_and_failed_deletes_are_bounded(self):
        for status, expected, attempts in ((404, 0, 1), (503, 1, 2)):
            with self.subTest(status=status):
                account = Account()
                account.hook = lambda request: httpx.Response(status) if request.method == 'DELETE' else None
                result = self.run_case(account)
                self.assertEqual(result.code, expected, result.err)
                self.assertEqual(len(self.deletes(account)), attempts)

    def test_bad_details_are_not_deleted_and_remaining_resources_continue(self):
        for field, value in [('lastSpawnedAt', 'yesterday'), ('spawnCount', True), ('buildCount', '1'),
                             ('createdAt', None), ('updatedAt', '2026-01-01'), ('templateID', 'other')]:
            with self.subTest(field=field):
                account = Account(2)
                account.records['raw1'][field] = value
                result = self.run_case(account)
                self.assertEqual(result.code, 1, result.out)
                self.assertEqual(self.deletes(account), [('DELETE', '/templates/raw2')])

    def test_malformed_listing_is_nonzero_without_destructive_calls(self):
        account = Account()
        account.hook = lambda request: httpx.Response(200, json=[None, {'snapshotID': '../normal'}]) if request.url.path == '/snapshots' else None
        result = self.run_case(account)
        self.assertEqual(result.code, 1)
        self.assertEqual(self.deletes(account), [])
        self.assertEqual(len(account.calls), 1)

    def test_limit_stops_discovery_and_deletion_before_later_pages(self):
        account = Account(5)
        result = self.run_case(account, '--limit', '1', '--page-size', '1')
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.deletes(account), [('DELETE', '/templates/raw1')])
        self.assertFalse(any('nextToken' in params for _, _, _, params in account.calls))

    def test_alias_path_is_encoded_and_details_must_match_canonical_id(self):
        account = Account()
        raw_paths = []
        def hook(request):
            raw_paths.append(request.url.raw_path)
            return None
        account.hook = hook
        result = self.run_case(account)
        self.assertEqual(result.code, 0, result.err)
        self.assertIn(('/templates/aliases/' + quote(account.refs[0], safe='')).encode(), raw_paths)

    def test_invalid_options_fail_before_constructing_backend(self):
        for args in (['--metadata', 'env=test'], ['--metadata'], ['--unused-for-days', '0.5'],
                     ['--unused-for-hours', 'nan'], ['--unused-for-days', '1', '--unused-for-hours', '2'],
                     ['--rate', '0'], ['--concurrency', '65'], ['--page-size', '0'], ['--limit', '0'],
                     ['--retries', '-1'], ['--name', '']):
            with self.subTest(args=args), patch.object(cleanup, 'backend', side_effect=AssertionError('API access')):
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    cleanup.main(args)
                self.assertEqual(raised.exception.code, 2)
        parser = cleanup.parser_for_cli()
        self.assertEqual(parser.parse_args([]).days, 90)
        self.assertEqual(parser.parse_args(['--unused-for-hours', '36']).days, 1.5)

    def test_shared_lock_rejects_overlap_before_requests(self):
        account = Account()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, E2B_API_KEY='snapshot-test-key'):
            path = str(Path(folder) / 'run.lock')
            with httpx.Client(base_url='https://test', transport=httpx.MockTransport(account.handler)) as client:
                api = SnapshotBackend(client=client)
                with run_lock(path), patch.object(cleanup, 'backend', return_value=api), contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cleanup.main(['--apply', '--lock-file', path]), 1)
            self.assertEqual(account.calls, [])

    def test_authentication_failure_stops_listing_before_any_other_request(self):
        account = Account(3)
        account.hook = lambda _: httpx.Response(401)
        result = self.run_case(account)
        self.assertEqual(result.code, 1)
        self.assertEqual(len(account.calls), 1)
        self.assertTrue(result.api.control.cancelled.is_set())
        self.assertEqual(self.deletes(account), [])

    def test_candidate_batches_and_limit_bound_large_discovery(self):
        class LargeAPI:
            invalid_rows = generated = deleted = 0

            def __init__(self):
                self.clock = 0
                self.holds = []
                self.deleted_at = []
                self.control = RequestControl(clock=lambda: self.clock, sleep=self.sleep)

            def sleep(self, delay):
                self.holds.append(delay)
                self.clock += delay

            def list_snapshot_refs(self, name=None):
                for i in range(1_000_000):
                    self.generated += 1
                    if self.generated - self.deleted > 1000:
                        raise AssertionError('discovery exceeded the bounded candidate batch')
                    yield f'raw{i}'

            def resolve_snapshot(self, reference):
                return reference

            def snapshot_details(self, raw):
                return snapshot_state(details(raw), raw)

            def is_snapshot(self, raw):
                return True

            def delete_snapshot(self, raw, before_retry):
                self.deleted += 1
                self.deleted_at.append(self.clock)
                return True

            def cancel(self):
                self.control.cancelled.set()

        api = LargeAPI()
        args = cleanup.parser_for_cli().parse_args(['--apply', '--limit', '1008', '--concurrency', '4'])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = cleanup.run(args, api, NOW - timedelta(days=90))
        self.assertEqual(code, 0)
        self.assertEqual((api.generated, api.deleted), (1008, 1008))
        self.assertEqual(api.holds, [120, 120])
        self.assertEqual(api.deleted_at, [120] * 1000 + [240] * 8)


if __name__ == '__main__':
    unittest.main()
