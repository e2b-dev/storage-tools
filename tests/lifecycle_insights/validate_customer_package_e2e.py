#!/usr/bin/env python3
"""Live verification of the customer ZIP; creates and cleans up one tagged sandbox.

Run with the v2 virtualenv and E2B_API_KEY. Evidence contains account data and
must not be included in the customer package.
"""
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor

from e2b import Sandbox, NotFoundException

ROOT = Path(__file__).resolve().parents[2]
RUN = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]
EVIDENCE = ROOT / 'verification' / 'live-customer-package' / RUN  # gitignored: contains account data
WINDOWS = '0.0006944444444444445,0.006944444444444444'
MINUTE = '0.0166667h'
TEN_MINUTES = '0.166667h'


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print('[PASS]', message, flush=True)


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def row(report, section, label):
    lines = report.split(section, 1)[1].splitlines()
    return next([part.split() for part in line.split('|')]
                for line in lines if line.strip().startswith(label + ' |'))


def fetch_fixture_events(sid):
    domain = os.environ.get('E2B_DOMAIN', 'e2b.app')
    req = urllib.request.Request(
        f'https://api.{domain}/events/sandboxes/{sid}?limit=100',
        headers={'X-API-Key': os.environ['E2B_API_KEY']})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def wait_events(sid, pauses, resumes):
    deadline = time.monotonic() + 180
    while True:
        events = fetch_fixture_events(sid)
        types = [e['type'].rsplit('.', 1)[-1] for e in events]
        if types.count('paused') >= pauses and types.count('resumed') >= resumes:
            return events
        if time.monotonic() >= deadline:
            raise AssertionError(f'Lifecycle events did not arrive: {types}')
        print('Waiting for event delivery:', types, flush=True)
        time.sleep(5)


def run_script(name, phase, args):
    folder = EVIDENCE / 'package' / 'lifecycle_insights'
    command = ([sys.executable, str(folder / 'analyze_sandbox_reuse.py')]
               if name == 'python' else ['bash', str(folder / 'analyze_sandbox_reuse_cli.sh')])
    print(f'Running {phase}: {name}', flush=True)
    started = time.monotonic()
    out_path = EVIDENCE / f'{phase}-{name}.txt'
    err_path = EVIDENCE / f'{phase}-{name}.stderr.txt'
    with out_path.open('w') as out, err_path.open('w') as err:
        result = subprocess.run(command + args, stdout=out, stderr=err, timeout=1200)
    check(result.returncode == 0, f'{phase} {name} exited successfully; see {err_path.name}')
    print(f'{phase} {name}: {time.monotonic() - started:.1f}s', flush=True)
    return out_path.read_text()


def both(phase, args_for):
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = {name: pool.submit(run_script, name, phase, args_for(name)) for name in ('python', 'shell')}
        return {name: future.result() for name, future in jobs.items()}


def assert_known_return(out, name, pauses, current_paused):
    check('Sandboxes seen: 1 ' in out, f'{name}: metadata selects exactly the fixture')
    check(f'pause episodes: {pauses} ' in out, f'{name}: {pauses} pause episodes')
    check(f'currently paused: {current_paused}' in out, f'{name}: current paused inventory = {current_paused}')
    check(f'observed resumes : 1 of {pauses}' in out, f'{name}: exactly one observed return')
    check('Ambiguous pause episodes excluded    : 0' in out, f'{name}: no ambiguous fixture events')
    check('Resume events without a usable pair  : 0' in out, f'{name}: no unpaired fixture resumes')
    section = 'RESUMES THAT CAME AFTER N DAYS IDLE'
    for label, count, pct in ((MINUTE, '1', '100.0%'), (TEN_MINUTES, '0', '0.0%')):
        fields = row(out, section, label)
        check(fields[1:] == [[count, 'of', '1', pct]] * 3,
              f'{name}: observed and both bounds correct at {label}')


def main():
    EVIDENCE.mkdir(parents=True)
    archive = Path(os.environ.get('LIFECYCLE_ZIP', ROOT / 'sandbox_lifecycle_insights.zip'))
    if not archive.exists():  # build the customer package from the repo when no prebuilt zip is given
        archive = EVIDENCE / 'sandbox_lifecycle_insights.zip'
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
            for name in ('analyze_sandbox_reuse.py', 'analyze_sandbox_reuse_cli.sh', 'README.md'):
                z.write(ROOT / 'lifecycle_insights' / name, f'lifecycle_insights/{name}')
            z.write(ROOT / 'requirements.txt', 'lifecycle_insights/requirements.txt')
            z.write(ROOT / 'LICENSE', 'lifecycle_insights/LICENSE')
    data = archive.read_bytes()
    manifest = {'zip_sha256': hashlib.sha256(data).hexdigest(), 'run': RUN,
                'started_at': datetime.now(timezone.utc).isoformat()}
    (EVIDENCE / 'tested-package.zip').write_bytes(data)
    with zipfile.ZipFile(EVIDENCE / 'tested-package.zip') as z:
        check(z.testzip() is None, 'ZIP integrity')
        z.extractall(EVIDENCE / 'package')
    meta = {'lifecycle_e2e': RUN}
    meta_arg = f'lifecycle_e2e={RUN}'
    print('Evidence:', EVIDENCE, flush=True)
    sbx = None
    try:
        sbx = Sandbox.create(metadata=meta, timeout=900)
        sid = sbx.sandbox_id
        manifest['sandbox_id'] = sid
        manifest['metadata'] = meta
        (EVIDENCE / 'manifest.json').write_text(json.dumps(manifest, indent=2))
        print('Created test sandbox:', sid, flush=True)
        check(sbx.pause(), 'first pause succeeded')
        paused_clock = time.monotonic()
        for _ in range(5):
            time.sleep(15)
            print(f'Controlled pause: {time.monotonic() - paused_clock:.0f}s', flush=True)
        sbx = Sandbox.connect(sid, timeout=900)
        info = Sandbox.get_info(sid)
        check(info.state.value == 'running', 'fixture is running after resume')
        events = wait_events(sid, 1, 1)
        (EVIDENCE / 'fixture-running-events.json').write_text(json.dumps(events, indent=2))
        pause = next(e for e in events if e['type'].endswith('.paused'))
        resume = next(e for e in events if e['type'].endswith('.resumed'))
        gap = (timestamp(resume['timestamp']) - timestamp(pause['timestamp'])).total_seconds()
        manifest['measured_pause_gap_seconds'] = gap
        check(60 < gap < 180, f'event timestamps confirm controlled pause: {gap:.2f}s')
        running = both('running-fixture', lambda name: [
            '--since-days', '1', '--max-events', '0', '--metadata', meta_arg,
            '--windows', WINDOWS, '--csv', str(EVIDENCE / f'running-{name}.csv')])
        for name, report in running.items():
            assert_known_return(report, name, 1, 0)
            with (EVIDENCE / f'running-{name}.csv').open() as f:
                item = next(csv.DictReader(f))
            check(item['sandbox_id'] == sid and item['currently_paused'] == 'no',
                  f'{name}: running fixture included in CSV')

        check(sbx.pause(), 'second pause succeeded')
        events = wait_events(sid, 2, 1)
        (EVIDENCE / 'fixture-paused-events.json').write_text(json.dumps(events, indent=2))
        account = both('account-30d', lambda name: [
            '--since-days', '30', '--max-events', '0', '--windows', '1,2,3,7',
            '--save-events', str(EVIDENCE / f'account-{name}-events.json'),
            '--csv', str(EVIDENCE / f'account-{name}.csv')])
        manifest['account_downloads'] = {}
        for name, report in account.items():
            raw = json.loads((EVIDENCE / f'account-{name}-events.json').read_text())
            with (EVIDENCE / f'account-{name}.csv').open() as f:
                rows = list(csv.DictReader(f))
            item = next(r for r in rows if r['sandbox_id'] == sid)
            check((item['pauses'], item['resumes'], item['currently_paused']) == ('2', '1', 'yes'),
                  f'{name}: full-account CSV has two pauses, one return, currently paused')
            pages = len((EVIDENCE / f'account-30d-{name}.stderr.txt').read_text().split('fetched ')) - 1
            check(pages > 1, f'{name}: live API pagination exercised ({pages} pages)')
            check('event cap was reached' not in report, f'{name}: no event cap truncation')
            manifest['account_downloads'][name] = {'downloaded_events': len(raw), 'pages': pages,
                                                    'csv_sandboxes': len(rows)}

        # Replay the identical captured data in each implementation. Live fetches
        # can differ as new account events arrive; replay removes that variable.
        captured = str(EVIDENCE / 'account-python-events.json')
        replay = both('same-capture-replay', lambda name: [
            '--events-file', captured, '--skip-listing', '--all', '--windows', '1,2,3,7'])
        normalize = lambda value: re.sub(r'\s+', ' ', value).strip()
        check(normalize(replay['python']) == normalize(replay['shell']),
              'full-account reports agree on the same captured events')
        fixture = both('paused-fixture', lambda name: [
            '--events-file', captured, '--all', '--metadata', meta_arg, '--windows', WINDOWS])
        for name, report in fixture.items():
            assert_known_return(report, name, 2, 1)
            fields = row(report, 'OBSERVED PAUSE / RESUME TIMING', TEN_MINUTES)
            check(fields[4] == ['1'], f'{name}: recent second pause has unknown ten-minute outcome')
        check(normalize(fixture['python']) == normalize(fixture['shell']),
              'fixture reports agree across SDK and CLI inventory')
        manifest['checks_passed'] = True
    finally:
        if sbx is not None:
            sid = sbx.sandbox_id
            print('Cleaning up only test sandbox:', sid, flush=True)
            Sandbox.kill(sid)
            try:
                Sandbox.get_info(sid)
            except NotFoundException:
                manifest['cleanup_verified_missing'] = True
                print('[PASS] Test sandbox confirmed absent after cleanup', flush=True)
            else:
                manifest['cleanup_verified_missing'] = False
                raise AssertionError('Test sandbox still exists after cleanup')
        manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
        manifest['source_zip_unchanged'] = hashlib.sha256(archive.read_bytes()).hexdigest() == manifest['zip_sha256']
        (EVIDENCE / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    print('ALL LIVE CUSTOMER PACKAGE CHECKS PASSED', flush=True)
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == '__main__':
    main()
