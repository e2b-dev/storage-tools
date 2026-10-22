"""Exercise the shared command surface without account access."""
import csv
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import shlex
import shutil
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Commands(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('E2B_')}
        self.env['PYTHON_DOTENV_DISABLED'] = '1'

    def command(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'storage_tools.py'), *args],
                              cwd=self.folder, env=self.env, text=True, capture_output=True, timeout=20)

    def test_help_and_invalid_commands_preserve_exit_codes(self):
        for args in [('--help',), ('cleanup-storage-inventory', '--help'),
                     ('cleanup-paused-sandboxes', '--help'), ('cleanup-unused-snapshots', '--help'),
                     ('analyze-sandbox-reuse', '--help')]:
            result = self.command(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.command('missing').returncode, 2)
        for command in ['cleanup-storage-inventory', 'cleanup-paused-sandboxes', 'cleanup-unused-snapshots']:
            extra = ['inventory.csv'] if command == 'cleanup-storage-inventory' else []
            for flag, value in [('--unused-for-days', '-1'), ('--unused-for-days', 'nan'),
                                ('--unused-for-days', '0.5'), ('--unused-for-days', '1.5'),
                                ('--unused-for-days', '3.0'),
                                ('--unused-for-hours', '-1'), ('--unused-for-hours', 'nan'),
                                ('--unused-for-hours', 'inf'),
                                ('--rate', 'inf'), ('--rate', '0'), ('--limit', '0')]:
                result = self.command(command, *extra, flag, value)
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_age_units_cannot_be_combined(self):
        for command in ['cleanup-storage-inventory', 'cleanup-paused-sandboxes', 'cleanup-unused-snapshots']:
            extra = ['inventory.csv'] if command == 'cleanup-storage-inventory' else []
            for day_flag in (['--unused-for-days', '--days'] if command == 'cleanup-paused-sandboxes' else ['--unused-for-days']):
                result = self.command(command, *extra, day_flag, '1', '--unused-for-hours', '6')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('not allowed with argument', result.stderr)

    def test_inventory_dry_run_filters_without_credentials_or_results(self):
        source = self.folder / 'inventory.csv'
        with source.open('w') as handle:
            writer = csv.writer(handle)
            writer.writerow(['as_of_date', 'resource_type', 'resource_id', 'size_gib', 'last_used_at'])
            for sid, days in [('old', 10), ('recent', 1)]:
                writer.writerow(['2026-10-22', 'paused_sandbox', sid, '1.0',
                                 (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()])
        result = self.command('cleanup-storage-inventory', str(source), '--unused-for-days', '3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Dry run: nothing deleted', result.stdout)
        self.assertFalse(source.with_suffix('.results.csv').exists())
        self.assertEqual(list(self.folder.iterdir()), [source])

    def test_lifecycle_replay_from_another_directory(self):
        source = self.folder / 'events.json'
        source.write_text('[]')
        result = self.command('analyze-sandbox-reuse', '--events-file', str(source), '--skip-listing', '--all')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Events used       : 0', result.stdout)

    def test_standalone_cleanup_quickstart_uses_packaged_files(self):
        shutil.copytree(ROOT / 'scheduled-cleanup', self.folder / 'scheduled-cleanup', ignore=shutil.ignore_patterns('__pycache__'))
        for name in ['requirements.txt', 'LICENSE']:
            shutil.copy(ROOT / name, self.folder / name)
        quickstart = (self.folder / 'scheduled-cleanup/README.md').read_text().split('## Request budgets')[0]
        commands = [shlex.split(line) for line in quickstart.splitlines()
                    if line.startswith('.venv/bin/python ')]
        self.assertTrue(commands)
        for command in commands:
            result = subprocess.run([sys.executable, *command[1:], '--help'], cwd=self.folder,
                                    env=self.env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_recurring_dry_run_apply_and_legacy_alias(self):
        requests = []
        old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        resources = [dict(sandboxID=sid, state='paused', metadata={'env': 'test'},
                          startedAt=age, endAt=age) for sid, age in [('old', old), ('recent', recent)]]

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(('GET', self.path))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(resources[0] if self.path == '/sandboxes/old' else resources).encode())

            def do_DELETE(self):
                requests.append(('DELETE', self.path))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env.update(E2B_API_KEY='offline-test-key', E2B_API_URL=f'http://127.0.0.1:{server.server_port}')
        canonical = self.command('cleanup-paused-sandboxes', '--unused-for-days', '3', '--metadata', 'env=test')
        hours = self.command('cleanup-paused-sandboxes', '--unused-for-hours', '72', '--metadata', 'env=test')
        self.assertEqual(hours.returncode, 0, hours.stderr)
        self.assertEqual(canonical.stdout, hours.stdout)
        legacy = self.command('cleanup-paused-sandboxes', '--days', '3', '--metadata', 'env=test')
        self.assertEqual(canonical.returncode, 0, canonical.stderr)
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(canonical.stdout, legacy.stdout)
        self.assertIn('Dry run: 1 paused sandbox(es)', canonical.stdout)
        self.assertFalse(any(method == 'DELETE' for method, _ in requests))
        requests.clear()
        result = self.command('cleanup-paused-sandboxes', '--unused-for-days', '3', '--metadata', 'env=test',
                              '--apply', '--recheck', '--limit', '1', '--lock-file', str(self.folder / 'cleanup.lock'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path for method, path in requests if method == 'DELETE'], ['/sandboxes/old'])
        self.assertIn(('GET', '/sandboxes/old'), requests)
        self.assertIn('Deleted 1 of 1', result.stdout)


if __name__ == '__main__':
    unittest.main()
