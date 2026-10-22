#!/usr/bin/env python3
"""Offline acceptance tests for both cleanup scripts; no account calls are made.

Run with ../.venv/bin/python test_cleanup_safety.py, or from the workspace root:
v2/.venv/bin/python v2/tests/test_cleanup_safety.py

Failures identify unmet cleanup requirements. The Python entry point uses an in-memory API double. The shell entry point
uses a local HTTP server; neither can access an E2B account.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scheduled-cleanup"))


def sandbox(sid, paused_days=100, started_days=120, state="paused"):
    now = datetime.now(timezone.utc)
    return dict(sandboxId=sid, state=state, metadata={"env": "test"},
                startedAt=(now - timedelta(days=started_days)).isoformat().replace("+00:00", "Z"),
                endAt=(now - timedelta(days=paused_days)).isoformat().replace("+00:00", "Z"))


class Backend:
    def __init__(self, config):
        self.config = config
        self.calls = []

    def call(self, operation, sid=None):
        self.calls.append([operation, sid])
        cfg = self.config
        if operation == "list":
            if cfg.get("list_fail"):
                raise RuntimeError("503 listing unavailable")
            return sorted(cfg.get("sandboxes", []), key=lambda s: s["startedAt"])
        if operation == "info":
            if sid in cfg.get("info_fail", []):
                raise RuntimeError("503 service unavailable")
            if sid in cfg.get("network_fail", []):
                raise ConnectionError("connection reset")
            if sid in cfg.get("gone", []):
                raise KeyError(sid)
            item = next((s for s in cfg["sandboxes"] if s["sandboxId"] == sid), None)
            if item is None:
                raise KeyError(sid)
            sequence = cfg.get("info_sequence", {}).get(sid)
            return sequence.pop(0) if sequence else cfg.get("current", {}).get(sid, item)
        if operation == "kill":
            if sid in cfg.get("kill_fail", []):
                raise RuntimeError("503 kill failed")
            if sid in cfg.get("gone_at_kill", []):
                return False
            if sid in cfg.get("resume_at_kill", []):
                self.calls.append(["killed_running_sandbox", sid])
            cfg["sandboxes"] = [s for s in cfg["sandboxes"] if s["sandboxId"] != sid]
            if sid in cfg.get("lost_kill_response", []):
                raise ConnectionError("kill response lost")
            return True
        raise AssertionError(operation)


class FakeAPI:
    def __init__(self, state):
        self.state = state

    def list_paused(self, metadata=None):
        from sandbox_backend import normalize
        for item in self.state.call('list'):
            yield normalize(item)

    def call(self, op, sid):
        from sandbox_backend import BackendError, Missing
        try:
            return self.state.call(op, sid)
        except KeyError as error:
            raise Missing('not found') from error
        except (RuntimeError, ConnectionError) as error:
            raise BackendError(str(error)) from error

    def get_info(self, sid):
        from sandbox_backend import normalize
        return normalize(self.call('info', sid))

    def kill(self, sid, *, before_retry=None):
        return self.call('kill', sid)

    def close(self):
        pass

    def cancel(self):
        pass


def run_python(backend, args):
    args = test_args(backend, args)
    stdout, stderr = io.StringIO(), io.StringIO()
    rc = 0
    from sandbox_backend import backend as real_backend

    def factory(name, **kwargs):
        if os.environ.get('E2B_DEBUG', '').lower() == 'true':
            return real_backend(name, **kwargs)
        return FakeAPI(backend)

    with patch.dict(os.environ, {"E2B_API_KEY": "offline-test-key"}), \
            patch('sandbox_backend.backend', factory), \
            patch.object(sys, 'argv', ['cleanup'] + args), \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            runpy.run_path(str(ROOT / 'scheduled-cleanup' / 'cleanup_paused_sandboxes.py'), run_name='__main__')
        except SystemExit as error:
            rc = error.code
    return SimpleNamespace(returncode=rc, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def run_shell(backend, args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    import threading
    args = test_args(backend, args)
    mutex = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.handle_request()

        def do_DELETE(self):
            self.handle_request()

        def handle_request(self):
            query = parse_qs(urlparse(self.path).query)
            path = urlparse(self.path).path
            status, headers, data = 200, {}, None
            with mutex:
                try:
                    if path == '/v2/sandboxes':
                        items = backend.call('list')
                        # Cursor uses an ID, so deleting previous pages cannot shift it.
                        ids = sorted(items, key=lambda s: (s['startedAt'], s['sandboxId']))
                        token = query.get('nextToken', [None])[0]
                        if token:
                            ids = [s for s in ids if s['startedAt'] > token]
                        limit = int(query['limit'][0])
                        data = ids[:limit]
                        if len(ids) > limit:
                            headers['X-Next-Token'] = data[-1]['startedAt']
                    elif path.startswith('/sandboxes/'):
                        sid = path.rsplit('/', 1)[1]
                        if self.command == 'GET':
                            data = backend.call('info', sid)
                        else:
                            status = 204 if backend.call('kill', sid) else 404
                    elif path == '/snapshots' or path.startswith('/templates/'):
                        backend.calls.append(['forbidden_snapshot_request', path])
                        status = 403
                    else:
                        status = 404
                except KeyError:
                    status = 404
                except (RuntimeError, ConnectionError):
                    status = 503
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if data is not None:
                self.wfile.write(json.dumps(data).encode())

    server = ThreadingHTTPServer(('127.0.0.1', getattr(backend, 'port', 0)), Handler)
    backend.port = server.server_port
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith('E2B_')}
        env.update(E2B_API_KEY='offline-test-key', E2B_API_URL=f'http://127.0.0.1:{server.server_port}',
                   E2B_TOOLS_PYTHON=sys.executable)
        return subprocess.run(['bash', str(ROOT / 'scheduled-cleanup' / 'cleanup_paused_sandboxes_cli.sh'),
                               *args, '--rate', '10000', '--retries', '0'],
                              capture_output=True, text=True, env=env, timeout=30)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_args(backend, args):
    args = list(args)
    if not any(flag in args for flag in ('--unused-for-days', '--unused-for-hours', '--days')):
        args += ['--unused-for-days', '90']
    if not hasattr(backend, "work_temp"):
        backend.work_temp = tempfile.TemporaryDirectory()
    args += ["--lock-file", str(Path(backend.work_temp.name) / "run.lock")]
    if "--apply" in args and not backend.config.get("omit_recheck"):
        args += ["--recheck"]
    return args


class CleanupSafety(unittest.TestCase):
    def each(self, config, args, assertion):
        for name, runner in [("python", run_python), ("shell", run_shell)]:
            with self.subTest(script=name):
                backend = Backend(json.loads(json.dumps(config)))
                proc = runner(backend, args)
                try:
                    assertion(backend, proc)
                finally:
                    backend.work_temp.cleanup()

    def test_dry_run_selects_pause_age_and_never_mutates(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("old\t", p.stdout)
            self.assertNotIn("recent-pause\t", p.stdout)
            self.assertNotIn("running\t", p.stdout)
            self.assertFalse(any(c[0] in ("kill", "delete_snapshot") for c in b.calls))
        self.each(dict(sandboxes=[sandbox("old"), sandbox("recent-pause", 1),
                                  sandbox("running", state="running")]), [], verify)

    def test_hours_delete_only_sandboxes_older_than_cutoff(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual([c for c in b.calls if c[0] == "kill"], [["kill", "old"]])
        self.each(dict(sandboxes=[sandbox("old", 1.5 / 24), sandbox("recent", 0.5 / 24)]),
                  ["--unused-for-hours", "1", "--apply"], verify)


    def test_hours_and_days_select_equivalent_cutoffs(self):
        from cleanup_paused_sandboxes import parser_for_cli
        parser = parser_for_cli()
        for hours, days in [('72', 3), ('36', 1.5), ('2.4', 0.1)]:
            self.assertEqual(parser.parse_args(['--unused-for-hours', hours]).days, days)
        self.assertEqual(parser.parse_args(['--unused-for-days', '3']).days,
                         parser.parse_args(['--days', '3']).days)
        self.assertEqual(parser.parse_args(['--unused-for-days', '90']).days, 90)

    def test_apply_deletes_sandbox_and_preserves_named_snapshots(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(b.config['sandboxes'], [])
            self.assertEqual(b.config['snapshots']['old'], ['team/snapshot:default'])
            self.assertFalse(any(op in ('snapshots', 'delete_snapshot') for op, _ in b.calls))
        self.each(dict(sandboxes=[sandbox('old')], snapshots={'old': ['team/snapshot:default']}),
                  ['--apply'], verify)

    def test_dry_run_preserves_snapshots_and_writes_no_state(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(b.config['snapshots']['old'], ['snap'])
            self.assertEqual(list(Path(b.work_temp.name).iterdir()), [])
            self.assertFalse(any(op in ('snapshots', 'delete_snapshot') for op, _ in b.calls))
        self.each(dict(sandboxes=[sandbox('old')], snapshots={'old': ['snap']}), [], verify)

    def test_removed_snapshot_flags_fail_before_account_access(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 2, p.stderr)
            self.assertIn('cleanup-unused-snapshots', p.stderr)
            self.assertFalse(b.calls)
        for args in (['--delete-snapshots'], ['--retry-pending'], ['--journal', 'legacy.json'],
                     ['--journal=legacy.json']):
            self.each(dict(sandboxes=[sandbox('old')]), ['--apply', *args], verify)

    def test_lost_kill_response_rerun_keeps_named_snapshots(self):
        for name, runner in [('python', run_python), ('shell', run_shell)]:
            with self.subTest(script=name):
                b = Backend(dict(sandboxes=[sandbox('old')], snapshots={'old': ['snap']},
                                 lost_kill_response=['old']))
                first = runner(b, ['--apply'])
                self.addCleanup(b.work_temp.cleanup)
                self.assertEqual(first.returncode, 1, first.stderr)
                self.assertEqual(b.config['sandboxes'], [])
                second = runner(b, ['--apply'])
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(b.config['snapshots']['old'], ['snap'])
                self.assertFalse(any(op in ('snapshots', 'delete_snapshot') for op, _ in b.calls))

    def test_started_age_mode(self):
        self.each(dict(sandboxes=[sandbox("old-start-recent-pause", 1)]), ["--age-from", "started"],
                  lambda b, p: self.assertIn("old-start-recent-pause\t", p.stdout))

    def test_more_than_one_thousand_candidates(self):
        self.each(dict(sandboxes=[sandbox(f"s{i}") for i in range(1001)]), [],
                  lambda b, p: self.assertEqual(sum("\tstarted=" in s for s in p.stdout.splitlines()), 1001))


    def test_recheck_skips_resumed_and_disappeared_candidates(self):
        self.each(dict(sandboxes=[sandbox("resumed"), sandbox("gone")], gone=["gone"],
                       current={"resumed": sandbox("resumed", state="running")}), ["--apply"],
                  lambda b, p: self.assertFalse(any(c[0] == "kill" for c in b.calls)))

    def test_kill_failure_continues_and_exits_nonzero(self):
        def verify(b, p):
            self.assertNotEqual(p.returncode, 0)
            self.assertIn(["kill", "second"], b.calls)
        self.each(dict(sandboxes=[sandbox("first"), sandbox("second")], kill_fail=["first"]),
                  ["--apply"], verify)

    def test_lookup_service_failure_is_not_success(self):
        self.each(dict(sandboxes=[sandbox("old")], info_fail=["old"]), ["--apply"],
                  lambda b, p: self.assertNotEqual(p.returncode, 0, p.stdout))

    def test_network_failure_does_not_abort_remaining_sandboxes(self):
        self.each(dict(sandboxes=[sandbox("first"), sandbox("second")], network_fail=["first"]),
                  ["--apply"], lambda b, p: self.assertIn(["kill", "second"], b.calls, p.stderr))


    def test_changed_metadata_is_rechecked(self):
        changed = sandbox("old")
        changed["metadata"] = {"env": "prod"}
        self.each(dict(sandboxes=[sandbox("old")], current={"old": changed}),
                  ["--metadata", "env=test", "--apply"],
                  lambda b, p: self.assertFalse(any(c[0] == "kill" for c in b.calls)))


    def test_invalid_days_fail_before_account_access(self):
        for value in ("-1", "nan", "inf", "garbage", "0.5", "1.5", "3.0"):
            self.each(dict(sandboxes=[]), ["--days", value],
                      lambda b, p: (self.assertEqual(p.returncode, 2), self.assertFalse(b.calls)))


    def test_sdk_debug_mode_rejected_before_account_access(self):
        b = Backend(dict(sandboxes=[sandbox("old")], snapshots={"old": ["snap"]}))
        with patch.dict(os.environ, E2B_DEBUG="true"):
            result = run_python(b, ["--apply"])
        self.addCleanup(b.work_temp.cleanup)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('E2B_DEBUG', result.stderr)
        self.assertFalse(b.calls)

    def test_apply_without_resume_assertion_uses_listed_state(self):
        def verify(b, p):
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(['kill', 'old'], b.calls)
            self.assertFalse(any(op == 'info' for op, _ in b.calls))
        self.each(dict(sandboxes=[sandbox('old')], omit_recheck=True), ['--apply'], verify)


if __name__ == "__main__":
    unittest.main(verbosity=2)
