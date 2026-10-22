#!/usr/bin/env python3
"""Opt-in real-snapshot retention checks; all mutations target newly created fixtures.

Run with E2B_API_KEY and the repository dependencies. Usage observations may take
up to 120 seconds per spawn to appear. Evidence is written under verification/.
The cleanup subprocess gets a local test token; a fixed-upstream proxy permits
only this run's snapshots and records every request, including an injected 429.
These checks validate API-reported use, not authoritative inactivity: the usage
metric is buffered and best effort, and concurrent reuse can race with deletion.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx
from e2b import NotFoundException, Sandbox

ROOT = Path(__file__).resolve().parents[2]
USAGE_WAIT_SECONDS = 120


def now():
    return datetime.now(timezone.utc)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('API timestamp has no timezone')
    return parsed


class GuardedAPI:
    def __init__(self, key, endpoint):
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ('http', 'https') or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('E2B_API_URL must be an API origin without credentials or a path')
        self.endpoint = endpoint.rstrip('/')
        self.client = httpx.Client(headers={'X-API-Key': key}, timeout=30, follow_redirects=False)
        self.token = secrets.token_hex(24)
        self.references, self.raw_ids = set(), set()
        self.deletable = set()
        self.events = []
        self.inject_429 = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.forward()

            def do_DELETE(self):
                self.forward()

            def forward(self):
                parsed = urlsplit(self.path)
                path, query = unquote(parsed.path), parse_qs(parsed.query, keep_blank_values=True)
                method = self.command
                allowed = False
                if method == 'GET' and path == '/snapshots':
                    allowed = (set(query) <= {'name', 'limit', 'nextToken'}
                               and len(query.get('name', [])) == 1
                               and query['name'][0] in owner.references)
                elif method == 'GET' and path.startswith('/templates/aliases/'):
                    allowed = path.removeprefix('/templates/aliases/') in owner.references and not query
                elif path.startswith('/templates/'):
                    raw = path.removeprefix('/templates/')
                    allowed = ((method == 'GET' and raw in owner.raw_ids and query == {'limit': ['1']})
                               or (method == 'DELETE' and raw in owner.deletable and not query))
                event = {'method': method, 'path': path, 'query': query, 'at': now().isoformat()}
                owner.events.append(event)
                status, body, headers = 403, b'fixture guard rejected request', {}
                try:
                    if (allowed and not parsed.scheme and not parsed.netloc and not parsed.fragment
                            and secrets.compare_digest(self.headers.get('X-API-Key', ''), owner.token)):
                        if method == 'DELETE' and path == '/templates/' + str(owner.inject_429):
                            owner.inject_429 = None
                            status, body, headers = 429, b'injected retry for fixture test', {'Retry-After': '1'}
                            event['source'] = 'injected_429'
                        else:
                            response = owner.client.request(method, owner.endpoint + self.path)
                            status, body = response.status_code, response.content
                            headers = {k: v for k, v in response.headers.items()
                                       if k.lower() in ('x-next-token', 'retry-after', 'content-type')}
                            event['source'] = 'real_api'
                    else:
                        event['source'] = 'guard_rejection'
                except Exception as error:
                    status, body = 502, b'upstream request failed'
                    event['error'] = type(error).__name__
                event['status'] = status
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def request(self, method, path, **kwargs):
        return self.client.request(method, self.endpoint + path, **kwargs)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client.close()


class Suite:
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.key = os.environ['E2B_API_KEY']
        if not self.key:
            raise ValueError('E2B_API_KEY must be nonempty')
        endpoint = os.environ.get('E2B_API_URL') or f"https://api.{os.environ.get('E2B_DOMAIN') or 'e2b.app'}"
        self.evidence = ROOT / 'verification' / ('snapshot-cleanup-' + self.run_id)
        self.evidence.mkdir(mode=0o700, parents=True)
        self.api = GuardedAPI(self.key, endpoint)
        self.sandboxes, self.snapshots = [], []
        self.checks, self.commands = [], []
        self.report = {
            'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'run_id': self.run_id, 'started_at': now().isoformat(),
            'timestamp_source': 'real API GET /templates/{rawID}?limit=1 lastSpawnedAt; SDK get_info.started_at',
            'claim_scope': 'Validates retention against observed API-reported last use. The buffered, best-effort metric does not prove authoritative inactivity or exclude concurrent reuse.',
            'tool_sha256': hashlib.sha256((ROOT / 'scheduled-cleanup/cleanup_unused_snapshots.py').read_bytes()).hexdigest(),
            'checks': self.checks, 'commands': self.commands,
            'sandboxes': self.sandboxes, 'snapshots': self.snapshots,
        }
        print(f'Evidence: {self.evidence}', flush=True)

    def redact(self, text):
        return str(text).replace(self.key, '[REDACTED]').replace(self.api.token, '[REDACTED]')

    def save(self):
        (self.evidence / 'report.json').write_text(self.redact(json.dumps(self.report, indent=2)) + '\n')

    def check(self, condition, message):
        self.checks.append({'check': message, 'passed': bool(condition)})
        print(f'{"PASS" if condition else "FAIL"}: {message}', flush=True)
        self.save()
        if not condition:
            raise AssertionError(message)

    def create(self, role, template=None):
        kwargs = {'metadata': {'snapshot_cleanup_e2e': self.run_id, 'role': role}, 'timeout': 900}
        if template is not None:
            kwargs['template'] = template
        sandbox = Sandbox.create(**kwargs)
        self.sandboxes.append({'id': sandbox.sandbox_id, 'role': role})
        self.save()
        return sandbox

    def details(self, snapshot, absent_ok=False, timeout=30):
        response = self.api.request('GET', '/templates/' + quote(snapshot['raw_id'], safe='') + '?limit=1',
                                    timeout=timeout)
        if absent_ok and response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def snapshot(self, parent, role):
        name = f'storage-e2e-{self.run_id}-{role}'
        created = parent.create_snapshot(name=name)
        snapshot = {'role': role, 'id': created.snapshot_id, 'name': name,
                    'names': created.names, 'parent_id': parent.sandbox_id}
        self.snapshots.append(snapshot)
        self.save()  # Retain the SDK identifier even if alias resolution fails.
        response = self.api.request('GET', '/templates/aliases/' + quote(created.snapshot_id, safe=''))
        response.raise_for_status()
        snapshot['raw_id'] = response.json()['templateID']
        self.api.raw_ids.add(snapshot['raw_id'])
        self.api.references.update([name, created.snapshot_id, snapshot['raw_id'], *created.names])
        self.save()
        return snapshot

    def assert_gone(self, sid):
        try:
            Sandbox.get_info(sid)
        except NotFoundException:
            return
        raise AssertionError(f'sandbox fixture {sid} still exists')

    def use(self, snapshot):
        before = now()
        child = self.create(snapshot['role'] + '-child', snapshot['id'])
        info = Sandbox.get_info(child.sandbox_id)
        after = now()
        snapshot['child_id'] = child.sandbox_id
        snapshot['child_started_at'] = info.started_at.isoformat()
        Sandbox.kill(child.sandbox_id)
        self.assert_gone(child.sandbox_id)
        deadline = time.monotonic() + USAGE_WAIT_SECONDS
        observations = []
        snapshot['usage_observations'] = observations
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{snapshot['role']}: usage did not become visible within {USAGE_WAIT_SECONDS}s")
            details = self.details(snapshot, timeout=min(30, remaining))
            value = details.get('lastSpawnedAt')
            observations.append({'observed_at': now().isoformat(), 'lastSpawnedAt': value,
                                 'spawnCount': details.get('spawnCount')})
            self.save()
            if value is not None and timestamp(value) >= before - timedelta(seconds=2):
                self.check(timestamp(value) <= after + timedelta(seconds=2),
                           f"{snapshot['role']}: recorded usage corresponds to the real child creation")
                snapshot['lastSpawnedAt'] = value
                snapshot['details_after_use'] = details
                self.save()
                return
            if time.monotonic() >= deadline:
                raise AssertionError(f"{snapshot['role']}: usage did not become visible within {USAGE_WAIT_SECONDS}s")
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def survivors(self):
        for sandbox in self.sandboxes:
            if sandbox['role'] in ('parent', 'control'):
                self.check(Sandbox.get_info(sandbox['id']).sandbox_id == sandbox['id'],
                           f"{sandbox['role']}: source/control sandbox survives")
        self.check(self.details(self.recent) is not None, 'recently reused snapshot survives')
        self.check(self.details(self.never).get('lastSpawnedAt') is None,
                   'never-used snapshot survives with null lastSpawnedAt')

    def run(self, label, snapshot, expected, apply=False, inject_retry=False):
        # Recompute relative age for every command, keeping the absolute cutoff at
        # the midpoint of actual API usage timestamps despite time spent in tests.
        midpoint = timestamp(self.old['lastSpawnedAt']) + (
            timestamp(self.recent['lastSpawnedAt']) - timestamp(self.old['lastSpawnedAt'])) / 2
        hours = ((now() - midpoint).total_seconds() - 120) / 3600
        reference = snapshot['raw_id'] if snapshot is self.recent else snapshot['name']
        self.check(reference in self.api.references, f'{label}: exact fixture selector is allowlisted')
        command = [sys.executable, str(ROOT / 'storage_tools.py'), 'cleanup-unused-snapshots',
                   '--name', reference, '--unused-for-hours', f'{hours:.12f}', '--rate', '20',
                   '--page-size', '1', '--concurrency', '1', '--limit', '1', '--retries', '2',
                   '--lock-file', str(self.evidence / 'cleanup.lock')]
        if apply:
            command.append('--apply')
        self.api.events = []
        self.api.inject_429 = self.old['raw_id'] if inject_retry else None
        env = {**os.environ, 'E2B_API_KEY': self.api.token, 'E2B_API_URL': self.api.url,
               'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        events = list(self.api.events)
        output = self.redact(result.stdout + result.stderr)
        (self.evidence / f'{label}.log').write_text(output)
        self.commands.append({'case': label, 'args': command[2:], 'cutoff': midpoint.isoformat(),
                              'at': now().isoformat(), 'returncode': result.returncode, 'requests': events})
        self.save()
        self.check(result.returncode == 0, f'{label}: command exits successfully (see {label}.log)')
        self.check(all(e.get('source') != 'guard_rejection' and 'error' not in e for e in events),
                   f'{label}: every request stays inside the fixture guard')
        matches = {line.split('\t', 1)[0] for line in result.stdout.splitlines() if '\tlast_used=' in line}
        self.check(matches == expected, f'{label}: selected exactly the expected snapshots')
        deletes = [i for i, event in enumerate(events) if event['method'] == 'DELETE']
        if not apply or not expected:
            self.check(not deletes, f'{label}: no DELETE requests')
        if inject_retry:
            self.check(len(deletes) == 2 and events[deletes[0]]['status'] == 429
                       and 200 <= events[deletes[1]]['status'] < 300,
                       f'{label}: one injected 429 followed by one successful real DELETE')
            between = events[deletes[0] + 1:deletes[1]]
            self.check(any(e['method'] == 'GET' and e['path'] == '/snapshots' for e in between)
                       and any(e['method'] == 'GET' and e['path'] == '/templates/' + self.old['raw_id']
                               for e in between), f'{label}: retry rechecks membership and current usage')
        self.survivors()

    def validate(self):
        parent = self.create('parent')
        control = self.create('control')
        self.check(control.pause(), 'out-of-scope control pauses')
        self.old, self.recent, self.never = [self.snapshot(parent, role) for role in ('old', 'recent', 'never')]
        self.api.deletable.add(self.old['raw_id'])
        self.check(len({s['raw_id'] for s in self.snapshots}) == 3, 'three distinct snapshots from the same parent')
        self.check(self.details(self.never).get('lastSpawnedAt') is None, 'never-used fixture has unknown use')
        self.use(self.old)
        # The recent spawn happens only after the old usage is visible. Retain a
        # minimum separation even if this account flushes usage immediately.
        remaining = 12 - (now() - timestamp(self.old['child_started_at'])).total_seconds()
        if remaining > 0:
            time.sleep(remaining)
        self.use(self.recent)
        self.check((timestamp(self.recent['child_started_at']) - timestamp(self.old['child_started_at'])).total_seconds() >= 10,
                   'actual child creation times are at least ten seconds apart')
        self.check((timestamp(self.recent['lastSpawnedAt']) - timestamp(self.old['lastSpawnedAt'])).total_seconds() >= 10,
                   'recorded API usage times distinguish old and recent use')
        midpoint = timestamp(self.old['lastSpawnedAt']) + (
            timestamp(self.recent['lastSpawnedAt']) - timestamp(self.old['lastSpawnedAt'])) / 2
        old_details = self.details(self.old)
        guards = [old_details[key] for key in ('createdAt', 'updatedAt', 'lastSpawnedAt')]
        guards.append(old_details['builds'][0]['createdAt'])
        self.check(all(timestamp(value) < midpoint for value in guards),
                   'old snapshot creation, update, build and usage all precede the cutoff')
        # Age real fixtures enough to express the midpoint with a nonnegative
        # requested retention plus the fixed 120-second cutoff buffer.
        remaining = (midpoint + timedelta(seconds=125) - now()).total_seconds()
        if remaining > 0:
            print('Waiting for fixture ages to exceed the retention buffer', flush=True)
            time.sleep(remaining)
        self.run('dry-old-alias', self.old, {self.old['raw_id']})
        self.check(self.details(self.old) is not None, 'dry run preserves the eligible old snapshot')
        self.run('dry-recent-id', self.recent, set())
        self.run('dry-never-alias', self.never, set())
        self.run('apply-recent-id', self.recent, set(), apply=True)
        self.run('apply-never-alias', self.never, set(), apply=True)
        self.run('apply-old-alias-retry', self.old, {self.old['raw_id']}, apply=True, inject_retry=True)
        self.check(self.details(self.old, absent_ok=True) is None, 'old unused snapshot is deleted')
        self.run('apply-old-again', self.old, set(), apply=True)
        self.check(self.details(self.old, absent_ok=True) is None, 'second apply is idempotent')
        self.report['checks_passed'] = True

    def cleanup(self):
        failures = []
        for snapshot in self.snapshots:
            try:
                if 'raw_id' in snapshot:
                    response = self.api.request('DELETE', '/templates/' + quote(snapshot['raw_id'], safe=''))
                    if response.status_code != 404:
                        response.raise_for_status()
                    if self.details(snapshot, absent_ok=True) is not None:
                        raise AssertionError('snapshot remains after cleanup')
                else:
                    Sandbox.delete_snapshot(snapshot['id'])
                    if Sandbox.delete_snapshot(snapshot['id']):
                        raise AssertionError('snapshot remained after its first cleanup')
                snapshot['cleanup_verified_gone'] = True
            except Exception as error:
                failures.append({'snapshot': snapshot['id'], 'error': self.redact(error)})
        for sandbox in self.sandboxes:
            try:
                Sandbox.kill(sandbox['id'])
                self.assert_gone(sandbox['id'])
                sandbox['cleanup_verified_gone'] = True
            except Exception as error:
                failures.append({'sandbox': sandbox['id'], 'error': self.redact(error)})
        self.report['cleanup_failures'] = failures
        self.report['cleanup_verified'] = not failures
        self.report['finished_at'] = now().isoformat()
        self.save()
        self.api.close()
        return not failures


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    os.umask(0o077)
    suite = Suite()
    passed = False
    try:
        suite.validate()
        passed = True
    except BaseException as error:
        suite.report['failure'] = {'type': type(error).__name__, 'message': suite.redact(error)}
        print(f'FAIL: {suite.redact(error)}', flush=True)
    finally:
        cleaned = suite.cleanup()
    print(f'Checks passed: {passed}; fixtures cleaned: {cleaned}; evidence: {suite.evidence}', flush=True)
    return 0 if passed and cleaned else 1


if __name__ == '__main__':
    sys.exit(main())
