#!/usr/bin/env python3
"""Offline regression checks for reuse-report claims and inventory completeness."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "lifecycle_insights"))
import analyze_sandbox_reuse as reuse


class ReuseReporting(unittest.TestCase):
    def test_recent_pause_reports_observed_returns_and_unknown_followup(self):
        now = datetime.now(timezone.utc)
        events = [dict(id="one", sandboxId="recent", type=reuse.PAUSED, eventData={},
                       timestamp=(now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"))]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.json"
            path.write_text(json.dumps(events))
            env = {k: v for k, v in os.environ.items() if not k.startswith("E2B_")}
            env["E2B_API_KEY"] = "offline-placeholder"
            for script, cmd in [("python", [sys.executable, str(ROOT / "lifecycle_insights" / "analyze_sandbox_reuse.py")]),
                                ("shell", ["bash", str(ROOT / "lifecycle_insights" / "analyze_sandbox_reuse_cli.sh")])]:
                with self.subTest(script=script):
                    proc = subprocess.run(cmd + ["--events-file", str(path), "--skip-listing", "--windows", "7"],
                                          capture_output=True, text=True, env=env, timeout=30)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    rows = [line for line in proc.stdout.splitlines() if line.strip().startswith("7d |")]
                    self.assertEqual([field.split() for field in rows[0].split("|")],
                                     [["7d"], ["0"], ["0.0%"], ["n/a"], ["1"], ["0"]])
                    self.assertEqual([field.split() for field in rows[1].split("|")],
                                     [["7d"], ["0", "n/a"], ["0"], ["0"], ["1"]])
                    self.assertIn("OBSERVED SANDBOX RETURNS AFTER N", proc.stdout)
                    self.assertIn("insufficient follow-up", proc.stdout)
                    self.assertNotIn("safe to delete", proc.stdout.lower())
                    self.assertNotIn("policy", proc.stdout.lower())

    def test_csv_includes_current_paused_sandbox_without_retained_events(self):
        now = datetime.now(timezone.utc)
        current = SimpleNamespace(sandbox_id="listing-only", started_at=now-timedelta(days=40),
                                  end_at=now-timedelta(days=35), metadata={"env": "test"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inventory.csv"
            reuse.write_csv(str(path), {}, [], [current])
            self.assertIn("listing-only", path.read_text(),
                          "The inventory must include old sandboxes absent from retained event history")


if __name__ == "__main__":
    unittest.main(verbosity=2)
