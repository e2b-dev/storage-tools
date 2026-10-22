#!/usr/bin/env python3
"""Live ground-truth check for the lifecycle analyzers.

Creates ONE sandbox tagged with unique metadata, pauses it, waits, resumes it, pauses it
again, then runs both analyzers filtered to that metadata and asserts the report shows the
known history: 2 pause episodes, 1 resume with a gap of roughly the wait time, a return
after a 1-minute window and none after a 10-minute window. Kills the sandbox at the end.
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
import uuid
import os
from pathlib import Path

from e2b import Sandbox

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "lifecycle_insights"
WAIT_S = 75
sys.path.insert(0, str(SCRIPTS))
import analyze_sandbox_reuse as a  # noqa: E402

ONE_MIN_DAYS = "0.00069444"
TEN_MIN_DAYS = "0.0069444"
ONE_MIN = a.fmt_window(float(ONE_MIN_DAYS))
TEN_MIN = a.fmt_window(float(TEN_MIN_DAYS))


def check(cond, msg):
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        raise AssertionError(msg)


def events_for(sid):
    req = urllib.request.Request(
        f"https://api.e2b.app/events/sandboxes/{sid}?limit=100", headers={"X-API-Key": os.environ["E2B_API_KEY"]}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def row(stdout, section, label):
    lines = stdout.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(section))
    for l in lines[start + 1:]:
        if l.strip().startswith(label + " |"):
            return [f.split() for f in l.split("|")]
        if l.startswith(("OBSERVED", "PAUSE EPISODES", "CURRENTLY")) and not l.startswith(section):
            break
    raise AssertionError(f"row {label!r} not found under {section!r}")


def main():
    run_id = uuid.uuid4().hex[:8]
    meta = {"lifecycle_e2e": run_id}
    meta_arg = f"lifecycle_e2e={run_id}"
    sbx = Sandbox.create(metadata=meta, timeout=900)
    sid = sbx.sandbox_id
    print(f"created {sid} tagged {meta_arg}")
    try:
        check(sbx.pause(), "first pause")
        t_pause = time.time()
        print(f"  waiting {WAIT_S}s before resuming")
        time.sleep(WAIT_S)
        sbx = Sandbox.connect(sid)  # resumes a paused sandbox
        gap = time.time() - t_pause
        print(f"  resumed after {gap:.0f}s")
        time.sleep(3)
        check(sbx.pause(), "second pause")

        print("  waiting for lifecycle events to appear")
        deadline = time.time() + 180
        while True:
            types = sorted(e["type"].rsplit(".", 1)[1] for e in events_for(sid))
            if types.count("paused") >= 2 and types.count("resumed") >= 1:
                break
            check(time.time() < deadline, f"events arrived (have {types})")
            time.sleep(5)
        print(f"  events: {types}")

        outputs = {}
        for name, cmd in [("python", [sys.executable, str(ROOT / "storage_tools.py"), "analyze-sandbox-reuse"]),
                          ("shell", ["bash", str(SCRIPTS / "analyze_sandbox_reuse_cli.sh")])]:
            proc = subprocess.run(cmd + ["--metadata", meta_arg, "--windows", f"{ONE_MIN_DAYS},{TEN_MIN_DAYS}"],
                                  capture_output=True, text=True, timeout=300)
            print(f"\n--- {name} report")
            print("\n".join("    " + l for l in proc.stdout.splitlines()[:12]))
            check(proc.returncode == 0, f"{name}: exit 0 ({proc.stderr.strip().splitlines()[-1:] })")
            out = proc.stdout
            outputs[name] = out
            check("Sandboxes seen: 1 " in out, f"{name}: exactly our sandbox after metadata filter")
            check("pause episodes: 2 " in out, f"{name}: two pause episodes")
            check("currently paused: 1" in out, f"{name}: inventory shows it paused now")
            check("observed resumes : 1 of 2" in out, f"{name}: one of two episodes resumed")
            m = re.search(r"Pause -> resume gap\s+: p50 (\S+),", out)
            check(m and m.group(1) in {"1m", "2m"} , f"{name}: p50 gap reported as {m.group(1) if m else None} (~{gap:.0f}s)")
            check(row(out, "OBSERVED SANDBOX RETURNS AFTER N", ONE_MIN) == [[ONE_MIN], ["1"], ["100.0%"], ["100.0%"], ["1"], ["1"]],
                  f"{name}: returned after a 1-minute window: 1 of 1")
            check(row(out, "OBSERVED SANDBOX RETURNS AFTER N", TEN_MIN) == [[TEN_MIN], ["0"], ["0.0%"], ["0.0%"], ["1"], ["1"]],
                  f"{name}: no return after a 10-minute window")
            check(row(out, "RESUMES THAT CAME AFTER N DAYS IDLE", ONE_MIN)[1] == ["1", "of", "1", "100.0%"],
                  f"{name}: the one resume happened after the 1-minute window")
            check(row(out, "RESUMES THAT CAME AFTER N DAYS IDLE", TEN_MIN)[1] == ["0", "of", "1", "0.0%"],
                  f"{name}: no resume happened after the 10-minute window")
        strip = lambda s: re.sub(r"\s+", " ", "\n".join(l for l in s.splitlines() if "History" not in l and "Events used" not in l and "Note:" not in l))
        check(strip(outputs["python"]) == strip(outputs["shell"]), "python and shell reports identical")
        print("\nALL LIVE LIFECYCLE CHECKS PASSED")
    finally:
        print(f"\nkilling {sid}: {Sandbox.kill(sid)}")


if __name__ == "__main__":
    main()
