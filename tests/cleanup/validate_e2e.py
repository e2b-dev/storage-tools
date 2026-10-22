#!/usr/bin/env python3
"""Live end-to-end validation of streamed, paced cleanup.

Creates real sandboxes tagged with a unique metadata key, runs both scripts
against them, and asserts the observable behaviour. Every --apply call is
scoped by --metadata to the fixtures created here, so nothing else is touched.

Requires E2B_API_KEY and the Python SDK. Both entry points use the shared HTTP client.
"""

import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from e2b import Sandbox, SandboxState
from e2b.exceptions import NotFoundException

ROOT = Path(__file__).resolve().parents[2]
PYTHON_SCRIPT = [sys.executable, str(ROOT / "storage_tools.py"), "cleanup-paused-sandboxes"]
SHELL_SCRIPT = [str(ROOT / "cleanup" / "cleanup_paused_sandboxes_cli.sh")]
MIN_OLD_AGE_S = 60  # how long the "old" sandbox runs before being paused
SANDBOX_TIMEOUT_S = 900  # running timeout; paused sandboxes and named snapshots need explicit cleanup

MATCH_LINE = re.compile(r"^(\S+)\tstarted=(\S+)\tpaused=(\S+)$", re.M)


def now() -> datetime:
    return datetime.now(timezone.utc)


def check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        raise AssertionError(message)


def run(cmd: list[str], *extra: str) -> subprocess.CompletedProcess:
    full = cmd + list(extra)
    if "--apply" in full and "--metadata" not in full:
        raise RuntimeError("refusing to run --apply without --metadata")
    proc = subprocess.run(full, capture_output=True, text=True, timeout=180)
    print(f"  $ {' '.join(Path(full[0]).name if i == 0 else a for i, a in enumerate(full))}")
    for line in (proc.stdout + proc.stderr).rstrip().splitlines():
        print(f"    | {line}")
    return proc


def matched_ids(stdout: str) -> set[str]:
    return {m.group(1) for m in MATCH_LINE.finditer(stdout)}


def validate_live_pacing(sandbox_id, metadata):
    sys.path.insert(0, str(ROOT / 'cleanup'))
    from sandbox_backend import APIBackend
    api = APIBackend(rate=20, page_size=1)
    starts, limits = [], []
    api.client.event_hooks['request'] = [lambda _: starts.append(time.monotonic())]
    api.client.event_hooks['response'] = [lambda response: limits.append(response.headers.get('RateLimit-Limit'))]
    try:
        list(api.list_paused(metadata))
        with ThreadPoolExecutor(max_workers=8) as pool:
            infos = list(pool.map(lambda _: api.get_info(sandbox_id), range(25)))
        check(all(info.sandbox_id == sandbox_id for info in infos), 'paced concurrent GETs return the fixture')
        check(starts[-1] - starts[0] >= (len(starts) - 1) / 20 - 0.02,
              'live HTTP attempts respect the default aggregate pacing budget')
        check(api.control.attempts >= 28, 'one-row list pages and individual GETs share the request controller')
        print(f'  paced {api.control.attempts} attempts over {starts[-1] - starts[0]:.2f}s; '
              f'list RateLimit-Limit values: {sorted({value for value in limits if value})}')
    finally:
        api.close()


def validate(script_name: str, script: list[str]) -> None:
    run_id = uuid.uuid4().hex[:8]
    meta_key = "e2e_run"
    metadata = {meta_key: run_id}
    metadata_arg = f"{meta_key}={run_id}"
    print(f"\n=== {script_name}: fixtures tagged {metadata_arg}")

    created: list[Sandbox] = []
    snapshot = None
    script = script + ["--page-size", "1", "--rate", "5", "--concurrency", "2"]
    try:
        old = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(old)
        old_started = now()
        resumed = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(resumed)
        check(resumed.pause(), "resume fixture paused before waiting")
        print(f"  created old      {old.sandbox_id}")

        running = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(running)
        print(f"  created running  {running.sandbox_id}")

        # Let "old" accumulate run time so started-based and paused-based ages diverge.
        wait = MIN_OLD_AGE_S - (now() - old_started).total_seconds()
        if wait > 0:
            print(f"  waiting {wait:.0f}s so the old sandbox has a meaningful run time")
            time.sleep(wait)

        Sandbox.connect(resumed.sandbox_id, timeout=SANDBOX_TIMEOUT_S)
        check(Sandbox.pause(resumed.sandbox_id), "old fixture resumed and paused again")

        fresh = Sandbox.create(metadata=metadata, timeout=SANDBOX_TIMEOUT_S)
        created.append(fresh)
        print(f"  created fresh    {fresh.sandbox_id}")
        snapshot = fresh.create_snapshot()
        print(f"  named snapshot   {snapshot.snapshot_id} (from fresh)")

        protected = Sandbox.create(metadata={meta_key: run_id + '-control'}, timeout=SANDBOX_TIMEOUT_S)
        created.append(protected)
        check(protected.pause(), 'out-of-scope control is paused')

        check(old.pause(), "old.pause() returned True")
        check(fresh.pause(), "fresh.pause() returned True")

        old_info = Sandbox.get_info(old.sandbox_id)
        fresh_info = Sandbox.get_info(fresh.sandbox_id)
        check(old_info.state == SandboxState.PAUSED, "old is paused")
        check(fresh_info.state == SandboxState.PAUSED, "fresh is paused")
        check(old_info.end_at > old_info.started_at, "paused sandbox end_at (pause time) is after started_at")

        validate_live_pacing(old.sandbox_id, metadata)

        paused_ids = {old.sandbox_id, fresh.sandbox_id, resumed.sandbox_id}
        fixture_ids = paused_ids | {running.sandbox_id}

        # Threshold that separates old (long run) from fresh (short run) when measured
        # from start, but excludes both when measured from pause.
        age_started_old = (now() - old_info.started_at).total_seconds()
        age_started_fresh = (now() - fresh_info.started_at).total_seconds()
        check(age_started_old - age_started_fresh > 20, "old ran at least 20s longer than fresh")
        threshold_s = (age_started_old + age_started_fresh) / 2
        threshold_hours = f"{threshold_s / 3600:.8f}"
        print(f"  threshold {threshold_s:.0f}s (old started {age_started_old:.0f}s ago, fresh {age_started_fresh:.0f}s ago)")

        print("\n-- dry run, nothing old enough")
        proc = run(script, "--unused-for-days", "1000", "--metadata", metadata_arg)
        check(proc.returncode == 0, "exit 0")
        check(matched_ids(proc.stdout) == set(), "no matches")
        check("Dry run: 0" in proc.stdout, "dry-run summary reports 0")

        print("\n-- dry run, everything paused is old enough (--days 0)")
        proc = run(script, "--unused-for-days", "0", "--metadata", metadata_arg)
        ids = matched_ids(proc.stdout)
        check(proc.returncode == 0, "exit 0")
        check(ids == paused_ids, f"matches exactly the three paused fixtures (got {sorted(ids)})")
        check(running.sandbox_id not in ids, "running sandbox is not listed")
        check(ids <= fixture_ids, "metadata filter kept out everything else in the account")
        check("Dry run: 3" in proc.stdout, "dry-run summary reports 3")

        print("\n-- --age-from started: only the long-running one qualifies")
        proc = run(script, "--unused-for-hours", threshold_hours, "--age-from", "started", "--metadata", metadata_arg)
        check(proc.returncode == 0, "exit 0")
        check(matched_ids(proc.stdout) == {old.sandbox_id}, "only old matches")

        print("\n-- --age-from paused (default): both were paused seconds ago, none qualifies")
        proc = run(script, "--unused-for-hours", threshold_hours, "--metadata", metadata_arg)
        check(proc.returncode == 0, "exit 0")
        check(matched_ids(proc.stdout) == set(), "no matches")

        print("\n-- cron apply without a resume assertion, limited to one sandbox")
        proc = run(script, "--unused-for-days", "0", "--metadata", metadata_arg, "--apply", "--limit", "1")
        check(proc.returncode == 0, "bounded cron exits 0")
        check("Deleted 1 of 1" in proc.stdout, "bounded cron deletes one fixture")
        check(Sandbox.get_info(fresh.sandbox_id).state == SandboxState.PAUSED, "later page remains for next cron")
        check(Sandbox.get_info(resumed.sandbox_id).state == SandboxState.PAUSED, "recently resumed fixture remains")
        snap_page = Sandbox.list_snapshots(sandbox_id=fresh.sandbox_id)
        check(any(s.snapshot_id == snapshot.snapshot_id for s in snap_page.next_items()), "default cron preserves named snapshots")

        print("\n-- apply deletes remaining paused sandboxes and preserves named snapshots")
        proc = run(script, "--unused-for-days", "0", "--metadata", metadata_arg, "--apply")
        check(proc.returncode == 0, "exit 0")
        check("Deleted 2 of 2" in proc.stdout, "summary reports 2 of 2 deleted")

        for sbx in (old, fresh, resumed):
            try:
                Sandbox.get_info(sbx.sandbox_id)
                check(False, f"{sbx.sandbox_id} still exists")
            except NotFoundException:
                check(True, f"paused sandbox {sbx.sandbox_id} is gone")
        check(Sandbox.get_info(running.sandbox_id).state == SandboxState.RUNNING, "running sandbox untouched")
        check(Sandbox.get_info(protected.sandbox_id).state == SandboxState.PAUSED, 'out-of-scope paused control untouched')
        remaining = Sandbox.list_snapshots(sandbox_id=fresh.sandbox_id)
        leftover = remaining.next_items() if remaining.has_next else []
        check(any(item.snapshot_id == snapshot.snapshot_id for item in leftover),
              "named snapshot remains after its parent sandbox is deleted")

        print("\n-- apply again: nothing left to do")
        proc = run(script, "--unused-for-days", "0", "--metadata", metadata_arg, "--apply")
        check(proc.returncode == 0, "exit 0")
        check("Deleted 0 of 0" in proc.stdout, "summary reports 0 of 0")
    finally:
        print("\n-- cleanup")
        cleanup_failed = False
        if snapshot is not None:
            try:
                Sandbox.delete_snapshot(snapshot.snapshot_id)
            except Exception as error:
                cleanup_failed = True
                print(f"  failed to delete named snapshot {snapshot.snapshot_id}: {error}")
        for sbx in created:
            try:
                Sandbox.kill(sbx.sandbox_id)
            except Exception as error:
                cleanup_failed = True
                print(f"  failed to delete sandbox {sbx.sandbox_id}: {error}")
        if cleanup_failed:
            raise AssertionError(f"fixture cleanup failed for {metadata_arg}; see resource IDs above")
        print(f"  fixture cleanup complete for {metadata_arg}")


def main() -> int:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    targets = {"python": PYTHON_SCRIPT, "shell": SHELL_SCRIPT}
    if which != "all":
        targets = {which: targets[which]}
    failures = 0
    for name, script in targets.items():
        try:
            validate(name, script)
            print(f"\n=== {name}: ALL CHECKS PASSED")
        except AssertionError as error:
            failures += 1
            print(f"\n=== {name}: FAILED: {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
