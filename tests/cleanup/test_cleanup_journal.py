"""Process locking and preservation of obsolete recovery files; no account access."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'cleanup'))
from cleanup_journal import run_lock
import cleanup_paused_sandboxes as cleanup


class CleanupState(unittest.TestCase):
    def test_dry_run_does_not_create_a_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.lock'
            with run_lock(path, enabled=False):
                self.assertFalse(path.exists())

    def test_lock_is_private_and_released_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.lock'
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                with run_lock(path):
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    raise RuntimeError('interrupted')
            with run_lock(path):
                pass

    def test_default_lock_coordinates_same_credentials_and_endpoint(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, E2B_API_KEY='lock-test-key', E2B_API_URL='https://test/'), \
                patch('cleanup_journal.tempfile.gettempdir', return_value=folder):
            with run_lock():
                with patch.dict(os.environ, E2B_API_URL='https://test'):
                    with self.assertRaisesRegex(ValueError, 'another cleanup'):
                        with run_lock():
                            self.fail('same endpoint acquired overlapping lock')
                with patch.dict(os.environ, E2B_API_KEY='other-test-key'):
                    with run_lock():
                        pass

    def test_removed_flags_preserve_existing_journal_and_fail_before_api(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.cleanup-journal-legacy.json'
            content = b'{"pending":{"old":{"snapshots":["team/snapshot:default"]}}}'
            path.write_bytes(content)
            before = path.stat()
            for flags in (['--delete-snapshots'], ['--retry-pending'], ['--journal', str(path)],
                          ['--journal=' + str(path)], ['--journal']):
                with self.subTest(flags=flags), patch.object(
                        cleanup, 'backend', side_effect=AssertionError('account access')):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
                        cleanup.main(['--apply', *flags])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn('cleanup-unused-snapshots', err.getvalue())
                    self.assertEqual(path.read_bytes(), content)
                    self.assertEqual(path.stat().st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == '__main__':
    unittest.main()
