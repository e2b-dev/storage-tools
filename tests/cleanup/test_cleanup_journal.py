"""Durability, scoping, and lock checks using temporary local files only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "cleanup"))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test helpers
from cleanup_journal import Journal
from test_cleanup_safety import Backend, sandbox, run_python

SCOPE = dict(days=90, age_from='paused', metadata={})


class CleanupJournal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.json'
        self.env = patch.dict(os.environ, E2B_API_KEY='test-journal-key', E2B_DOMAIN='e2b.app', E2B_API_URL='')
        self.env.start()
        self.addCleanup(self.env.stop)

    def seed(self):
        with Journal(self.path, SCOPE, True) as journal:
            journal.save()

    def test_identity_endpoint_and_scope_mismatch_rejected(self):
        self.seed()
        for env in ({'E2B_API_KEY':'different'}, {'E2B_DOMAIN':'different'}, {'E2B_API_URL':'https://different'}):
            with self.subTest(env=env), patch.dict(os.environ, env):
                with self.assertRaisesRegex(ValueError, 'different credentials'):
                    with Journal(self.path, SCOPE, True):
                        self.fail('must reject mismatch')
        with self.assertRaisesRegex(ValueError, 'selection'):
            with Journal(self.path, dict(SCOPE, days=30), True):
                self.fail('must reject mismatch')

    def test_equivalent_age_options_use_same_default_journal(self):
        for left, right in [(90, 90.0), (0.0, -0.0)]:
            with self.subTest(left=left, right=right):
                a = Journal(None, dict(SCOPE, days=left), False)
                b = Journal(None, dict(SCOPE, days=right), False)
                self.assertEqual(a.path, b.path)

    def test_second_writer_rejected_and_lock_released(self):
        with Journal(self.path, SCOPE, True):
            with self.assertRaisesRegex(ValueError, 'another cleanup'):
                with Journal(self.path, SCOPE, True):
                    self.fail('second writer acquired lock')
        with Journal(self.path, SCOPE, True) as journal:
            journal.save()

    def test_read_only_context_does_not_rewrite_existing_file(self):
        self.seed()
        before = self.path.stat()
        content = self.path.read_bytes()
        with Journal(self.path, SCOPE, False) as journal:
            self.assertEqual(journal.pending, {})
            with self.assertRaisesRegex(ValueError, 'dry-run'):
                journal.save()
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(self.path.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_failed_atomic_replace_keeps_previous_file(self):
        self.seed()
        content = self.path.read_bytes()
        with Journal(self.path, SCOPE, True) as journal:
            with patch('cleanup_journal.os.replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    journal.save()
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, {'journal.json','journal.json.lock'})

    def test_corrupt_journal_rejected_and_lock_released(self):
        self.path.write_text('{')
        with self.assertRaises(ValueError):
            with Journal(self.path, SCOPE, True):
                self.fail('must reject invalid JSON')
        self.path.unlink()
        self.seed()

    def test_interruption_after_snapshot_delete_recovers_idempotently(self):
        b = Backend(dict(sandboxes=[sandbox('old')], snapshots={'old':['snap']}))
        original_save = Journal.save
        saves = 0
        def interrupted_save(journal):
            nonlocal saves
            saves += 1
            if saves == 3:
                raise OSError('interrupted before recording snapshot success')
            original_save(journal)
        with patch.object(Journal, 'save', interrupted_save):
            first = run_python(b, ['--apply','--delete-snapshots'])
        self.addCleanup(b.journal_temp.cleanup)
        self.assertEqual(first.returncode, 1, first.stderr)
        self.assertEqual(b.config['snapshots']['old'], [])
        path = Path(b.journal_temp.name) / 'journal.json'
        self.assertEqual(json.loads(path.read_text())['pending']['old']['snapshots'], ['snap'])
        result = run_python(b, ['--apply','--delete-snapshots'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('already gone', result.stdout)
        self.assertEqual(json.loads(path.read_text())['pending'], {})


if __name__ == '__main__':
    unittest.main(verbosity=2)
