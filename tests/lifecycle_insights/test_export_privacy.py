"""Offline privacy regressions for Python and shell exports."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "lifecycle_insights"
sys.path.insert(0, str(SCRIPTS))
import analyze_sandbox_reuse as reuse


def event(eid, days=1, metadata=None, kind=reuse.PAUSED):
    return {
        "id": eid, "sandboxId": eid, "type": kind,
        "timestamp": (datetime.now(timezone.utc) - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
        "eventData": {"sandbox_metadata": metadata} if metadata is not None else {},
    }


class ExportPrivacy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / "source.json"
        self.output = self.folder / "saved events.json"
        self.csv = self.folder / "sandboxes.csv"
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("E2B_")}
        self.env.update(E2B_API_KEY="offline-placeholder", EXPORT_FIXTURE=str(self.source))
        curl = self.folder / "curl"
        curl.write_text('#!/bin/sh\ncat "$EXPORT_FIXTURE"\n')
        curl.chmod(0o700)
        self.env["PATH"] = str(self.folder) + os.pathsep + os.environ["PATH"]

    def run_export(self, name, data, *args, fetched=False):
        self.source.write_text(json.dumps(data))
        if name == "python":
            command = [sys.executable, str(SCRIPTS / "analyze_sandbox_reuse.py")]
            if fetched:
                command = [sys.executable, "-c",
                           "import os, sys, urllib.request; "
                           f"sys.path.insert(0, {str(SCRIPTS)!r}); "
                           "import analyze_sandbox_reuse as a; "
                           "urllib.request.urlopen = lambda *args, **kwargs: open(os.environ['EXPORT_FIXTURE']); "
                           "sys.exit(a.main())"]
        else:
            command = ["bash", str(SCRIPTS / "analyze_sandbox_reuse_cli.sh")]
        source_args = [] if fetched else ["--events-file", str(self.source)]
        return subprocess.run(command + source_args + [
            "--skip-listing", "--since-days", "0", "--max-events", "0",
            "--save-events", str(self.output), "--csv", str(self.csv), *args,
        ], text=True, capture_output=True, env=self.env, timeout=30, umask=0o022)

    def test_metadata_filters_replayed_and_fetched_exports(self):
        selected = event("selected", metadata={"env": "test", "group": "a=b"})
        data = [selected, event("unrelated", metadata={"env": "prod", "group": "a=b"}),
                event("partial", metadata={"env": "test"}), event("missing"),
                event("empty", metadata={})]
        for name in ("python", "shell"):
            for fetched in (False, True):
                with self.subTest(name=name, fetched=fetched):
                    proc = self.run_export(name, data, "--metadata", "env=test,group=a=b", fetched=fetched)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(json.loads(self.output.read_text()), [selected])
                    self.assertIn("Sandboxes seen: 1 ", proc.stdout)
                    self.assertIn("Events used       : 1 ", proc.stdout)
                    self.assertNotIn("unrelated", self.csv.read_text())

    def test_no_matches_exports_empty_array(self):
        for name in ("python", "shell"):
            with self.subTest(name=name):
                proc = self.run_export(name, [event("unrelated")], "--metadata", "env=test")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(self.output.read_text()), [])

    def test_export_uses_history_limits_types_and_deduplication(self):
        selected = event("selected", days=2)
        data = [event("old", days=40), selected, selected, event("capped", days=3),
                event("unrelated-type", days=0, kind="sandbox.lifecycle.updated")]
        for name in ("python", "shell"):
            with self.subTest(name=name):
                proc = self.run_export(name, data, "--since-days", "30", "--max-events", "1")
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(json.loads(self.output.read_text()), [selected])

    def test_without_metadata_exports_all_selected_accounts_events(self):
        data = [event("first", metadata={"env": "test"}), event("second", metadata={"env": "prod"})]
        for name in ("python", "shell"):
            with self.subTest(name=name):
                proc = self.run_export(name, data)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual({e["id"] for e in json.loads(self.output.read_text())}, {"first", "second"})

    def test_new_and_existing_exports_are_private(self):
        for name in ("python", "shell"):
            for existing in (False, True):
                with self.subTest(name=name, existing=existing):
                    for path in (self.output, self.csv):
                        path.unlink(missing_ok=True)
                        if existing:
                            path.write_text("old content")
                            path.chmod(0o644)
                    proc = self.run_export(name, [event("selected")])
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    for path in (self.output, self.csv):
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    self.assertEqual(list(self.folder.glob(".sandbox-export-*")), [])

    def test_exports_replace_symlinks_without_writing_their_targets(self):
        target = self.folder / "shared.txt"
        target.write_text("leave this alone")
        target.chmod(0o644)
        for name in ("python", "shell"):
            with self.subTest(name=name):
                for path in (self.output, self.csv):
                    path.unlink(missing_ok=True)
                    path.symlink_to(target)
                proc = self.run_export(name, [event("selected")])
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(target.read_text(), "leave this alone")
                for path in (self.output, self.csv):
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_failed_publish_cleans_up_private_temporary_files(self):
        self.output.mkdir()
        for name in ("python", "shell"):
            with self.subTest(name=name):
                proc = self.run_export(name, [event("selected")])
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(list(self.output.iterdir()), [])
                self.assertEqual(list(self.folder.glob(".sandbox-export-*")), [])

    def test_python_write_and_replace_failures_preserve_previous_export(self):
        self.output.write_text("previous export")
        for stage in ("write", "replace"):
            with self.subTest(stage=stage):
                with patch.object(reuse.os, "replace", side_effect=OSError("replace failed")):
                    with self.assertRaises(OSError):
                        with reuse.private_output(str(self.output)) as handle:
                            handle.write("new private data")
                            if stage == "write":
                                raise OSError("write failed")
                self.assertEqual(self.output.read_text(), "previous export")
                self.assertEqual(list(self.folder.glob(".sandbox-export-*")), [])


if __name__ == "__main__":
    unittest.main()
