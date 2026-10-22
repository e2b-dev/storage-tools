#!/usr/bin/env python3
"""Opt-in live streaming test; creates and removes only uniquely tagged fixtures."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

from e2b import Sandbox

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scheduled-cleanup'))
import cleanup_paused_sandboxes as cleanup
from request_control import RequestControl
from sandbox_backend import APIBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--count', type=int, default=150)
    parser.add_argument('--page-size', type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 100 or not args.page_size < args.count <= 1000:
        parser.error('require 1 <= page-size <= 100 and page-size < count <= 1000')

    run_id = uuid.uuid4().hex
    metadata = {'storage_streaming_e2e': run_id}
    evidence = Path(tempfile.mkdtemp(prefix='storage-streaming-live-'))
    manifest = evidence / 'fixtures.json'
    created = []
    paused = []
    guard = threading.Lock()
    setup_control = RequestControl(rate=10)
    report = {'run_id': run_id, 'commit': None, 'requested_paused': args.count,
              'started_at': datetime.now(timezone.utc).isoformat()}
    import subprocess
    report['commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    print(f'Live fixtures: storage_streaming_e2e={run_id}', flush=True)
    print(f'Evidence: {evidence}', flush=True)

    def create(meta, pause=True):
        setup_control.wait()
        sandbox = Sandbox.create(metadata=meta, timeout=900)
        with guard:
            created.append(sandbox.sandbox_id)
            manifest.write_text(json.dumps({'metadata': metadata, 'sandbox_ids': created}, indent=2))
        if pause:
            setup_control.wait()
            if not sandbox.pause():
                raise AssertionError(f'pause failed: {sandbox.sandbox_id}')
        return sandbox.sandbox_id

    try:
        running = create(metadata, pause=False)
        protected = create({'storage_streaming_e2e': run_id + '-control'})

        def create_paused(_):
            sid = create(metadata)
            with guard:
                paused.append(sid)
                if len(paused) % 25 == 0 or len(paused) == args.count:
                    print(f'Created and paused {len(paused)}/{args.count}', flush=True)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(create_paused, range(args.count)))

        api = APIBackend(page_size=args.page_size)
        try:
            assert {row.sandbox_id for row in api.list_paused(metadata)} == set(paused)
        finally:
            api.close()
        print(f'Confirmed all {args.count} fixtures are paused. Starting real --apply cleanup.', flush=True)

        events = []
        statuses = Counter()
        limits = set()
        page_sizes = []
        owned = set(paused)
        api = APIBackend(page_size=args.page_size)

        def on_request(request):
            if request.method == 'DELETE':
                sid = request.url.path.removeprefix('/sandboxes/')
                if request.url.path != f'/sandboxes/{sid}' or sid not in owned:
                    raise AssertionError('cleanup attempted a deletion outside paused test fixtures')
            elif request.method != 'GET' or request.url.path != '/v2/sandboxes':
                raise AssertionError('default cleanup made an unexpected request')
            with guard:
                events.append({'seconds': time.monotonic(), 'method': request.method,
                               'path': request.url.path})

        def on_response(response):
            # HTTPX response hooks run before the normal request body read.
            response.read()
            with guard:
                statuses[str(response.status_code)] += 1
                if response.headers.get('RateLimit-Limit'):
                    limits.add(response.headers['RateLimit-Limit'])
                if response.request.method == 'GET' and response.status_code == 200:
                    page_sizes.append(len(response.json()))

        api.client.event_hooks['request'] = [on_request]
        api.client.event_hooks['response'] = [on_response]
        command = ['--days', '0', '--age-from', 'started', '--apply',
                   '--metadata', f'storage_streaming_e2e={run_id}',
                   '--page-size', str(args.page_size)]
        report['cleanup_args'] = command
        started = time.monotonic()
        with patch.object(cleanup, 'backend', return_value=api):
            exit_code = cleanup.main(command)
        duration = time.monotonic() - started
        assert exit_code == 0, f'cleanup exited {exit_code}'
        requests = sorted(events, key=lambda event: event['seconds'])
        deletes = [i for i, event in enumerate(requests) if event['method'] == 'DELETE']
        lists = [i for i, event in enumerate(requests) if event['method'] == 'GET']
        assert len(deletes) == args.count, f'expected {args.count} deletes; got {len(deletes)}'
        assert deletes[0] < lists[-1], 'discovery completed before deletion began'
        assert sum(i < deletes[0] for i in lists) == 1, 'more than one page fetched before first delete'
        spans = [b['seconds'] - a['seconds'] for a, b in zip(requests, requests[1:])]
        assert min(spans) >= 0.04, f'unexpected request burst: minimum gap {min(spans)}'
        assert requests[-1]['seconds'] - requests[0]['seconds'] >= (len(requests) - 1) / 20 - 0.02
        assert statuses.get('429', 0) == 0, 'live account returned HTTP 429'
        origin = requests[0]['seconds']
        for event in requests:
            event['seconds'] -= origin
        report.update(duration_seconds=duration, requests=len(requests),
                      list_requests=len(lists), delete_requests=len(deletes),
                      page_sizes=page_sizes, response_statuses=dict(statuses),
                      observed_rate_limit_headers=sorted(limits),
                      min_request_gap_seconds=min(spans),
                      first_delete_seconds=requests[deletes[0]]['seconds'],
                      last_list_seconds=requests[lists[-1]]['seconds'], events=requests)

        verification = APIBackend()
        try:
            assert list(verification.list_paused(metadata)) == [], 'paused test fixtures remain'
            assert verification.get_info(running).state == 'running', 'running control changed'
            assert verification.get_info(protected).state == 'paused', 'out-of-scope control changed'
        finally:
            verification.close()
        report['result'] = 'passed'
        print(f'PASS: {len(deletes)} deletes, {len(lists)} list requests, {duration:.2f}s; '
              f'first delete at {report["first_delete_seconds"]:.3f}s, '
              f'last page request at {report["last_list_seconds"]:.3f}s; '
              f'HTTP statuses {dict(statuses)}. Both controls preserved.', flush=True)
    except BaseException as error:
        report['result'] = 'failed'
        report['error'] = f'{type(error).__name__}: {error}'
        raise
    finally:
        # The manifest is durable before pausing; recover every created ID on failures too.
        recovery = APIBackend()
        errors = []
        try:
            for sid in created:
                try:
                    recovery.kill(sid)
                except Exception as error:
                    errors.append(f'{sid}: {error}')
            remaining = list(recovery.list_paused(metadata))
            remaining += list(recovery.list_paused({'storage_streaming_e2e': run_id + '-control'}))
            if remaining:
                errors.append(f'{len(remaining)} paused fixtures remain')
        finally:
            recovery.close()
        report['fixture_cleanup_errors'] = errors
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        (evidence / 'report.json').write_text(json.dumps(report, indent=2))
        print(f'Fixture cleanup: {"FAILED" if errors else "complete"}. Report: {evidence / "report.json"}', flush=True)
        if errors:
            raise AssertionError('; '.join(errors))


if __name__ == '__main__':
    main()
