#!/usr/bin/env python3
"""Offline regression coverage for the standalone lifecycle analyzers (Python and shell)."""
import csv
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "lifecycle_insights"
sys.path.insert(0, str(SCRIPTS))
import analyze_sandbox_reuse as a  # noqa: E402

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def event(kind, hours, sid="one", eid=None):
    return dict(id=eid or f"{sid}-{kind}-{hours}", sandboxId=sid, type=kind,
                timestamp=(T + timedelta(hours=hours)).isoformat().replace("+00:00", "Z"), eventData={})


class LifecycleCorrectness(unittest.TestCase):
    def test_duplicate_event_ids_do_not_create_episodes(self):
        pause = event(a.PAUSED, 0)
        eps = a.pause_episodes(a.build_timelines([pause, pause, event(a.RESUMED, 2)], None, None), T + timedelta(days=1))
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0].resume_gap, timedelta(hours=2))

    def run_both(self, data, *args):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "events.json"
            path.write_text(json.dumps(data))
            env = {k: v for k, v in os.environ.items() if not k.startswith("E2B_")}
            for name, cmd in [("python", [sys.executable, str(SCRIPTS / "analyze_sandbox_reuse.py")]),
                              ("shell", ["bash", str(SCRIPTS / "analyze_sandbox_reuse_cli.sh")])]:
                yield name, subprocess.run(cmd + ["--events-file", str(path), "--skip-listing", "--since-days", "0", "--max-events", "0", *args],
                                           env=env, text=True, capture_output=True, timeout=30)

    def test_replay_needs_no_credentials_and_dedupes_in_both_entry_points(self):
        pause = event(a.PAUSED, 0)
        for name, p in self.run_both([pause, pause, event(a.RESUMED, 2)], "--windows", "1"):
            with self.subTest(name=name):
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("pause episodes: 1 ", p.stdout)

    def test_ambiguous_pause_chain_is_excluded_until_next_resume(self):
        data = [event(a.PAUSED, 0), event(a.PAUSED, 96), event(a.RESUMED, 97),
                event(a.PAUSED, 100), event(a.RESUMED, 101)]
        outputs = {}
        for name, p in self.run_both(data, "--windows", "3"):
            with self.subTest(name=name):
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("Pause episodes with observed resumes : 1 of 1", p.stdout)
                self.assertIn("Ambiguous pause episodes excluded    : 2", p.stdout)
                self.assertIn("Resume events without a usable pair  : 1", p.stdout)
                start = p.stdout.index("RESUMES THAT CAME AFTER N DAYS IDLE")
                row = next(line for line in p.stdout[start:].splitlines() if line.strip().startswith("3d |"))
                self.assertEqual(row.split("|")[1].split(), ["0", "of", "1", "0.0%"])
                self.assertIn("p50 1.0h, p90 1.0h, p99 1.0h, max 1.0h", p.stdout)
                outputs[name] = p.stdout
        self.assertEqual(len(outputs), 2)
        if len(outputs) == 2:
            self.assertEqual(outputs["python"], outputs["shell"])

    def test_resume_without_pause_is_excluded_and_reported(self):
        for name, p in self.run_both([event(a.RESUMED, 1)], "--windows", "3"):
            with self.subTest(name=name):
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("Pause episodes with observed resumes : 0 of 0", p.stdout)
                self.assertIn("Resume events without a usable pair  : 1", p.stdout)

    def test_boundary_pair_is_excluded_instead_of_invented(self):
        now = datetime.now(timezone.utc)
        data = [dict(id=kind, sandboxId="boundary", type=kind, eventData={},
                     timestamp=(now-timedelta(days=days)).isoformat().replace("+00:00", "Z"))
                for kind, days in [(a.PAUSED, 31), (a.RESUMED, 29)]]
        for name, p in self.run_both(data, "--since-days", "30", "--windows", "3"):
            with self.subTest(name=name):
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("Pause episodes with observed resumes : 0 of 0", p.stdout)
                self.assertIn("Resume events without a usable pair  : 1", p.stdout)

    def test_csv_does_not_invent_resumes_for_ambiguous_pauses(self):
        data = [event(a.PAUSED, 0), event(a.PAUSED, 96), event(a.RESUMED, 97)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sandboxes.csv"
            for name, p in self.run_both(data, "--csv", str(path)):
                with self.subTest(name=name):
                    self.assertEqual(p.returncode, 0, p.stderr)
                    with path.open() as handle:
                        row = next(csv.DictReader(handle))
                    self.assertEqual(row["resumes"], "0")
                    self.assertEqual(row["last_resumed_at"], "")
                    self.assertEqual(row["excluded_pause_episodes"], "2")
                    self.assertNotIn("Pause -> resume gap", p.stdout)

    def test_bounds_share_recorded_resume_denominator(self):
        cases = [
            ([event(a.PAUSED, 0), event(a.RESUMED, 96), event(a.RESUMED, 97, sid="orphan")],
             [["1", "of", "1", "100.0%"], ["1", "of", "2", "50.0%"], ["2", "of", "2", "100.0%"]]),
            ([event(a.RESUMED, 97, sid="orphan")],
             [["0", "of", "0", "n/a"], ["0", "of", "1", "0.0%"], ["1", "of", "1", "100.0%"]]),
            ([], [["0", "of", "0", "n/a"]] * 3),
        ]
        for data, expected in cases:
            for name, p in self.run_both(data, "--windows", "3"):
                with self.subTest(name=name, events=len(data)):
                    self.assertEqual(p.returncode, 0, p.stderr)
                    start = p.stdout.index("RESUMES THAT CAME AFTER N DAYS IDLE")
                    row = next(line for line in p.stdout[start:].splitlines() if line.strip().startswith("3d |"))
                    self.assertEqual([part.split() for part in row.split("|")[1:]], expected)

    def test_absence_from_paused_inventory_does_not_establish_deletion(self):
        from types import SimpleNamespace
        tl = a.build_timelines([event(a.PAUSED, 0)], None, None)
        eps = a.pause_episodes(tl, T + timedelta(days=10))
        for listing in [[], [SimpleNamespace(sandbox_id="other", end_at=T)]]:
            lines = []
            a.report(tl, eps, listing, [3], T + timedelta(days=10),
                     out=lambda *x: lines.append(" ".join(str(i) for i in x)), listing_queried=True)
            text = "\n".join(lines)
            self.assertIn("not in paused listing (current state unconfirmed)", text)
            self.assertNotIn("listing skipped", text)
            self.assertNotIn("gone from listing (deleted", text)

    def test_invalid_windows_fail_before_credentials_or_network(self):
        for value in ["typo", "-1", "0", "nan", "inf", "", "1,"]:
            for name, p in self.run_both([], "--windows", value):
                with self.subTest(name=name, value=value):
                    self.assertEqual(p.returncode, 2, p.stderr)
                    self.assertIn("window", p.stderr.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
