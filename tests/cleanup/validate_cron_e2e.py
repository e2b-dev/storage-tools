#!/usr/bin/env python3
"""Opt-in real-account cron checks; every destructive request is restricted to test fixtures."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from e2b import Sandbox, NotFoundException

ROOT = Path(__file__).resolve().parents[2]
PROXY_TOKEN = 'local-cron-fixture-token'


class GuardedAPI:
    def __init__(self, key, endpoint):
        self.client = httpx.Client(base_url=endpoint, headers={'X-API-Key': key}, timeout=60)
        self.owned = set()
        self.deletable = set()
        self.snapshots = set()
        self.selection = {}
        self.events = []
        self.rejected = []
        self.before = lambda method, path: None
        self.after = lambda method, path, response: None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.handle_request()

            def do_DELETE(self):
                self.handle_request()

            def handle_request(self):
                parsed = urlsplit(self.path)
                path = unquote(parsed.path)
                query = parse_qs(parsed.query)
                method = self.command
                allowed = False
                if path == '/v2/sandboxes' and method == 'GET':
                    metadata = parse_qs(query.get('metadata', [''])[0])
                    allowed = metadata == {k: [v] for k, v in owner.selection.items()}
                elif path.startswith('/sandboxes/'):
                    sid = path.removeprefix('/sandboxes/')
                    allowed = sid in (owner.deletable if method == 'DELETE' else owner.owned)
                elif path == '/snapshots' and method == 'GET':
                    allowed = query.get('sandboxID', [''])[0] in owner.owned
                elif path.startswith('/templates/') and method == 'DELETE':
                    allowed = path.removeprefix('/templates/') in owner.snapshots
                event = {'method': method, 'path': path, 'time': time.monotonic()}
                owner.events.append(event)
                try:
                    if (not allowed or parsed.scheme or parsed.netloc or parsed.fragment
                            or self.headers.get('X-API-Key') != PROXY_TOKEN):
                        owner.rejected.append(event)
                        result = (403, b'fixture guard rejected request', {})
                    else:
                        result = owner.before(method, path)
                        if result is None:
                            url = owner.client.base_url.copy_with(
                                path=path, query=parsed.query.encode('ascii'))
                            response = owner.client.request(method, url)
                            result = owner.after(method, path, response)
                            if result is None:
                                result = (response.status_code, response.content,
                                          {k: v for k, v in response.headers.items()
                                           if k.lower() in ('x-next-token', 'retry-after', 'content-type')})
                    status, body, headers = result
                    event['status'] = status
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    event['error'] = type(error).__name__
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def reset(self, selection):
        self.selection = selection
        self.events = []
        self.rejected = []
        self.before = lambda method, path: None
        self.after = lambda method, path, response: None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.client.close()


class Suite:
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.evidence = ROOT / 'verification' / ('cron-' + self.run_id)
        self.evidence.mkdir(mode=0o700, parents=True)
        self.scratch = tempfile.TemporaryDirectory(prefix='storage-cron-')
        self.folder = Path(self.scratch.name)
        key = os.environ['E2B_API_KEY']
        endpoint = os.environ.get('E2B_API_URL') or f"https://api.{os.environ.get('E2B_DOMAIN') or 'e2b.app'}"
        self.proxy = GuardedAPI(key, endpoint)
        self.env_file = self.folder / 'cleanup.env'
        self.env_file.touch(mode=0o600)
        # Only the proxy holds the real key; the wrapper loads a test token.
        self.env_file.write_text(f'E2B_API_KEY={PROXY_TOKEN}\nE2B_API_URL={self.proxy.url}\n')
        self.created = []
        self.snapshot_ids = []
        self.processes = []
        self.checks = []
        self.report = {'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                       'run_id': self.run_id, 'started_at': datetime.now(timezone.utc).isoformat(),
                       'checks': self.checks, 'injected_faults': ['429', '503', '401', 'lost DELETE response']}
        self.select('basic')
        print(f'Evidence: {self.evidence}', flush=True)

    def check(self, condition, message):
        self.checks.append({'check': message, 'passed': bool(condition)})
        print(f'{"PASS" if condition else "FAIL"}: {message}', flush=True)
        if not condition:
            raise AssertionError(message)

    def select(self, case):
        self.selection = {'storage_cron_e2e': self.run_id, 'case': case}
        self.proxy.reset(self.selection)

    def create(self, paused=True, snapshot=False, protected=False):
        metadata = {**self.selection, 'case': 'protected'} if protected else self.selection
        sandbox = Sandbox.create(metadata=metadata, timeout=900)
        sid = sandbox.sandbox_id
        self.created.append(sid)
        self.proxy.owned.add(sid)
        if not protected and paused:
            self.proxy.deletable.add(sid)
        self.save_manifest()
        if snapshot:
            snap = sandbox.create_snapshot()
            self.snapshot_ids.append(snap.snapshot_id)
            self.proxy.snapshots.add(snap.snapshot_id)
            self.save_manifest()
        if paused:
            self.check(sandbox.pause(), 'fixture pause succeeds')
        return sid

    def save_manifest(self):
        (self.evidence / 'fixtures.json').write_text(json.dumps(
            {'sandboxes': self.created, 'snapshots': self.snapshot_ids}, indent=2))

    def wrapper(self, label, extra=(), apply=True):
        metadata = ','.join(f'{key}={value}' for key, value in self.selection.items())
        age = [] if any(flag in extra for flag in ('--unused-for-days', '--unused-for-hours', '--days')) else ['--unused-for-hours', '0']
        args = [str(ROOT / 'storage_tools.py'), 'cleanup-paused-sandboxes',
                *age, '--metadata', metadata, '--recheck',
                '--rate', '15', '--limit', '10000', '--page-size', '2', '--concurrency', '1']
        if apply:
            args.append('--apply')
        args.extend(extra)
        path = self.folder / f'{label}.sh'
        path.write_text('#!/usr/bin/env bash\nset -euo pipefail\nset -a\n'
                        f'source {shlex.quote(str(self.env_file))}\nset +a\n'
                        f'exec {shlex.join([sys.executable, *args])}\n')
        return path

    def start(self, label, extra=(), apply=True):
        script = self.wrapper(label, extra, apply)
        output = (self.evidence / f'{label}.log').open('w')
        process = subprocess.Popen(['/bin/bash', str(script)], cwd='/',
                                   env={'HOME': str(self.folder), 'PATH': '/usr/bin:/bin'},
                                   stdout=output, stderr=subprocess.STDOUT)
        self.processes.append((process, output))
        return process

    def finish(self, label, process, expected=0):
        code = process.wait(timeout=120)
        text = (self.evidence / f'{label}.log').read_text()
        (self.evidence / f'{label}-requests.json').write_text(json.dumps(self.proxy.events, indent=2))
        self.check(code == expected, f'{label}: exit {expected} (observed {code})')
        self.check(not self.proxy.rejected, f'{label}: no requests outside allowed fixtures/selection')
        self.check(not any('error' in event for event in self.proxy.events), f'{label}: no unexpected proxy failures')
        return text

    def run(self, label, extra=(), apply=True, expected=0):
        return self.finish(label, self.start(label, extra, apply), expected)

    def info(self, sid):
        try:
            return Sandbox.get_info(sid)
        except NotFoundException:
            return None

    def basic(self):
        candidates = [self.create(snapshot=i == 0) for i in range(5)]
        running = self.create(paused=False)
        protected = self.create(protected=True)
        self.run('default-age', ['--unused-for-days', '3'])
        self.check(all(self.info(sid) is not None for sid in candidates), 'three-day policy preserves fresh fixtures')
        self.run('hours-age', ['--unused-for-hours', '1'])
        self.check(all(self.info(sid) is not None for sid in candidates), 'one-hour policy preserves fresh fixtures')
        self.run('dry-run', ['--delete-snapshots', '--journal', str(self.folder / 'dry-run.json')], apply=False)
        self.check(not any(e['method'] == 'DELETE' for e in self.proxy.events), 'dry run sends no DELETEs')
        self.check(not list(self.folder.glob('*.json')), 'dry run creates no recovery journal')
        self.proxy.events = []
        self.run('bounded', ['--limit', '2'])
        self.check(sum(self.info(sid) is None for sid in candidates) == 2, '--limit deletes exactly two matching fixtures')
        self.proxy.events = []
        self.run('remaining')
        self.check(all(self.info(sid) is None for sid in candidates), 'next run drains the remaining real pages')
        list_positions = [i for i, event in enumerate(self.proxy.events) if event['path'] == '/v2/sandboxes']
        delete_positions = [i for i, event in enumerate(self.proxy.events) if event['method'] == 'DELETE']
        self.check(len(list_positions) >= 2 and min(delete_positions) < max(list_positions), 'deletion begins before the final listing page')
        self.run('idempotent')
        self.check(self.info(running).state.value == 'running', 'matching running control survives')
        self.check(self.info(protected).state.value == 'paused', 'out-of-scope paused control survives')
        self.check(not any(e['path'].startswith('/templates/') for e in self.proxy.events), 'normal cron does not request named-snapshot deletion')
        self.check(any(s.snapshot_id == self.snapshot_ids[0] for s in Sandbox.list_snapshots(sandbox_id=candidates[0]).next_items()), 'named snapshot survives parent deletion by normal cron')
        spans = [b['time'] - a['time'] for a, b in zip(self.proxy.events, self.proxy.events[1:])]
        self.check(not spans or min(spans) >= 0.05, 'observed cleanup HTTP requests respect the 15/s budget')

    def concurrent(self):
        self.select('concurrent')
        candidates = [self.create() for _ in range(12)]
        self.run('concurrent', ['--concurrency', '10'])
        self.check(all(self.info(sid) is None for sid in candidates), 'ten concurrent workers delete all twelve scoped fixtures')

    def resumed_during_retry(self):
        self.select('resume-retry')
        sid = self.create()
        injected = False

        def before(method, path):
            nonlocal injected
            if method == 'DELETE' and path == f'/sandboxes/{sid}' and not injected:
                injected = True
                Sandbox.connect(sid, timeout=900)
                self.proxy.deletable.remove(sid)
                return 429, b'injected retry after resume', {'Retry-After': '0.5'}
            return None

        self.proxy.before = before
        output = self.run('resume-during-429')
        self.check(injected and 'Skipped' in output, 'retry rechecks a real fixture resumed after the first DELETE attempt')
        self.check(self.info(sid).state.value == 'running', 'resumed fixture survives retry backoff')
        self.check(sum(e['method'] == 'DELETE' for e in self.proxy.events) == 1, 'no second DELETE is sent after resume')

    def overlap_and_interrupt(self):
        self.select('interrupt')
        candidates = [self.create() for _ in range(4)]
        blocked = threading.Event()
        release = threading.Event()
        deleted = threading.Event()

        def before(method, path):
            if method == 'DELETE' and not blocked.is_set():
                blocked.set()
                if not release.wait(30):
                    return 503, b'test release timed out', {}
            return None

        def after(method, path, response):
            if method == 'DELETE' and response.status_code < 300:
                deleted.set()
            return None

        self.proxy.before, self.proxy.after = before, after
        first = self.start('interrupted', ['--rate', '1'])
        try:
            self.check(blocked.wait(20), 'first cron process reaches DELETE while holding its lock')
            count = len(self.proxy.events)
            output = self.run('overlap', expected=1)
            self.check('another cleanup is running' in output and len(self.proxy.events) == count, 'second process exits before any account request')
            release.set()
            self.check(deleted.wait(20), 'first deletion finishes before interruption')
            first.send_signal(signal.SIGINT)
            self.finish('interrupted', first, expected=130)
        finally:
            release.set()
        survivors = [sid for sid in candidates if self.info(sid) is not None]
        self.check(0 < len(survivors) < len(candidates), 'interrupt leaves completed deletes and remaining work')
        self.proxy.before = lambda method, path: None
        self.run('after-interrupt')
        self.check(all(self.info(sid) is None for sid in candidates), 'rerun acquires released lock and completes remaining work')

    def auth_and_retries(self):
        self.select('retries')
        sid = self.create()
        self.proxy.before = lambda method, path: (401, b'injected auth failure', {})
        self.run('auth-failure', expected=1)
        self.check(len(self.proxy.events) == 1 and self.info(sid) is not None, 'authentication failure stops without deletes')
        self.proxy.events = []
        attempts = 0

        def before(method, path):
            nonlocal attempts
            if method == 'DELETE':
                attempts += 1
                if attempts <= 2:
                    return 503, b'injected unavailable', {'Retry-After': '0.3'}
            return None

        self.proxy.before = before
        self.run('exhausted', ['--retries', '1'], expected=1)
        self.check(attempts == 2 and self.info(sid) is not None, 'retry exhaustion is bounded and leaves fixture for next run')
        deletes = [e for e in self.proxy.events if e['method'] == 'DELETE']
        self.check(deletes[1]['time'] - deletes[0]['time'] >= 0.3, 'Retry-After is honored before the next attempt')
        self.proxy.before = lambda method, path: None
        self.run('after-failure')
        self.check(self.info(sid) is None, 'next cron run deletes fixture after transient failures clear')

    def snapshot_recovery(self):
        self.select('snapshot-recovery')
        sid = self.create(snapshot=True)
        snapshot = self.snapshot_ids[-1]
        journal = self.folder / 'snapshots.json'
        args = ['--delete-snapshots', '--journal', str(journal), '--retries', '0']
        lost = False

        def after(method, path, response):
            nonlocal lost
            if method == 'DELETE' and path == f'/sandboxes/{sid}' and response.status_code < 300 and not lost:
                lost = True
                return 503, b'injected lost successful DELETE response', {}
            return None

        self.proxy.after = after
        self.run('lost-delete-response', args, expected=1)
        self.check(lost and self.info(sid) is None, 'real parent deletion succeeded despite injected lost response')
        self.check(sid in json.loads(journal.read_text())['pending'], 'journal retains recoverable parent and snapshot IDs')
        self.proxy.after = lambda method, path, response: None
        self.run('recover-snapshot', [*args, '--retry-pending'])
        self.check(not json.loads(journal.read_text())['pending'], 'retry-pending completes and clears the journal')
        self.check(not Sandbox.delete_snapshot(snapshot), 'named snapshot was deleted by recovery')

    def close(self):
        errors = []
        for process, output in self.processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            output.close()
        for sid in self.created:
            try:
                Sandbox.kill(sid)
            except Exception as error:
                errors.append(f'sandbox {sid}: {type(error).__name__}')
        for sid in self.snapshot_ids:
            try:
                Sandbox.delete_snapshot(sid)
            except Exception as error:
                errors.append(f'snapshot {sid}: {type(error).__name__}')
        for sid in self.created:
            try:
                if self.info(sid) is not None:
                    errors.append(f'sandbox remains: {sid}')
            except Exception as error:
                errors.append(f'verify {sid}: {type(error).__name__}')
        self.proxy.close()
        self.scratch.cleanup()
        self.report['fixture_cleanup_errors'] = errors
        self.report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (self.evidence / 'report.json').write_text(json.dumps(self.report, indent=2))
        if errors:
            raise AssertionError('; '.join(errors))
        print('Fixture cleanup verified complete', flush=True)


def main():
    suite = Suite()
    try:
        suite.basic()
        suite.concurrent()
        suite.resumed_during_retry()
        suite.overlap_and_interrupt()
        suite.auth_and_retries()
        suite.snapshot_recovery()
        suite.report['result'] = 'passed'
    except BaseException as error:
        suite.report['result'] = 'failed'
        suite.report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        suite.close()


if __name__ == '__main__':
    main()
