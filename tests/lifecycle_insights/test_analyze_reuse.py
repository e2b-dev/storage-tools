#!/usr/bin/env python3
"""Offline checks for analyze_sandbox_reuse.py using synthetic lifecycle events."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lifecycle_insights"))

import analyze_sandbox_reuse as a  # noqa: E402

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=10)
D = timedelta(days=1)


def ev(sid, kind, at, meta=None, reason=None):
    data = {}
    if meta:
        data["sandbox_metadata"] = meta
    if reason:
        data["kill_reason"] = reason
    return {"id": f"{sid}-{kind}-{at.timestamp()}", "sandboxId": sid, "type": kind,
            "timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "eventData": data}


EVENTS = [
    # A: paused, resumed after 12h, paused again, still paused for 8 days -> two episodes
    ev("A", a.CREATED, T0), ev("A", a.PAUSED, T0 + D), ev("A", a.RESUMED, T0 + D + D / 2),
    ev("A", a.PAUSED, T0 + 2 * D),
    # B: paused, killed by request 7 days later, never resumed
    ev("B", a.CREATED, T0), ev("B", a.PAUSED, T0 + D), ev("B", a.KILLED, T0 + 8 * D, reason="request"),
    # C: paused, resumed after 2.5 days
    ev("C", a.CREATED, T0), ev("C", a.PAUSED, T0 + D), ev("C", a.RESUMED, T0 + 3.5 * D),
    # E: paused 1 day before NOW, still paused -> unknown for windows > 1d
    ev("E", a.CREATED, T0), ev("E", a.PAUSED, NOW - D),
    # F: paused then killed 12h later (unknown for every window >= 1d), different metadata
    ev("F", a.CREATED, T0, meta={"env": "prod"}), ev("F", a.PAUSED, T0 + D, meta={"env": "prod"}),
    ev("F", a.KILLED, T0 + 1.5 * D, meta={"env": "prod"}, reason="request"),
]


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def main():
    check(a.parse_ts("2026-09-17T16:59:43.082732312Z") == datetime(2026, 9, 17, 16, 59, 43, 82732, tzinfo=timezone.utc),
          "nanosecond timestamps parse")

    tl = a.build_timelines(EVENTS, None, None)
    eps = a.pause_episodes(tl, NOW)
    check(len(tl) == 5, "five sandboxes")
    check(len(eps) == 6, "six pause episodes (A has two)")
    check(tl["B"].kill_reason == "request", "kill reason captured")

    by = {(e.sandbox_id, e.paused_at): e for e in eps}
    a1, a2 = by[("A", T0 + D)], by[("A", T0 + 2 * D)]
    check(a1.resume_gap == D / 2 and a1.outcome(D) == "resumed", "A first episode resumed within 1d")
    check(a2.resumed_at is None and a2.observed == 8 * D and a2.outcome(7 * D) == "not_resumed",
          "A second episode still paused, observed 8d -> not resumed within 7d")
    b = by[("B", T0 + D)]
    check(b.outcome(7 * D) == "not_resumed" and b.outcome(3 * D) == "not_resumed", "B killed at 7d counts as not resumed for <=7d")
    c = by[("C", T0 + D)]
    check(c.outcome(2 * D) == "not_resumed" and c.outcome(3 * D) == "resumed", "C resumed at 2.5d: after 2d, within 3d")
    e = by[("E", NOW - D)]
    check(e.outcome(D) == "not_resumed" and e.outcome(2 * D) == "unknown", "E paused 1d ago: evaluable at 1d, unknown at 2d")
    f = by[("F", T0 + D)]
    check(f.outcome(D) == "unknown", "F killed after 12h: unknown for 1d window (censored)")

    # metadata filter
    tl_prod = a.build_timelines(EVENTS, {"env": "prod"}, None)
    check(set(tl_prod) == {"F"}, "metadata filter keeps only matching sandboxes")

    # since filter drops old events
    tl_recent = a.build_timelines(EVENTS, None, NOW - 1.5 * D)
    check(set(tl_recent) == {"E"}, "--since-days keeps only recent events")

    # report runs and reflects the numbers above
    lines = []
    a.report(tl, eps, [], [1, 2, 3, 7], NOW, out=lambda *x: lines.append(" ".join(str(i) for i in x)))
    text = "\n".join(lines)
    check("with observed resumes : 2 of 6" in text, "report: 2 of 6 episodes have observed resumes")
    # C resumed after 2d (gap 2.5d); no observed resume gap exceeds 3d.
    row2 = next(l for l in lines if l.strip().startswith("2d |") and " of 2" in l)
    row3 = next(l for l in lines if l.strip().startswith("3d |") and " of 2" in l)
    check("1 of 2" in row2 and "0 of 2" in row3, "resumes after window: 1 of 2 after 2d, 0 of 2 after 3d")
    check("observed kill: request" in text and "no later event" in text, "breakdown of episodes without an observed resume")
    print("\nALL OFFLINE CHECKS PASSED")


if __name__ == "__main__":
    main()


def more():
    check(a.parse_window("48h") == 2.0 and a.parse_window("2d") == 2.0 and a.parse_window("0.5") == 0.5, "window tokens parse")
    check(a.fmt_window(2.0) == "2d" and a.fmt_window(0.5) == "12h" and a.fmt_window(1.5) == "36h", "window labels")
    tl = a.build_timelines(EVENTS, None, None)
    eps = a.pause_episodes(tl, NOW)
    lines = []
    a.report(tl, eps, [], [1, 2, 3], NOW, out=lambda *x: lines.append(" ".join(str(i) for i in x)))
    # paused sandboxes: A, B, C, E, F = 5. Only C came back after >2d idle (gap 2.5d). At 3d nobody did.
    reuse2 = next(l for l in lines if l.strip().startswith("2d |") and " of " not in l)
    reuse3 = next(l for l in lines if l.strip().startswith("3d |") and " of " not in l)
    # 5 paused sandboxes (A B C E F), 2 returning (A, C); only C returned after >2d
    check([field.split() for field in reuse2.split("|")] == [["2d"], ["1"], ["20.0%"], ["50.0%"], ["5"], ["2"]],
          "sandbox-level: 1 of 5 paused (1 of 2 returning) has an observed return after 2d")
    check([field.split() for field in reuse3.split("|")] == [["3d"], ["0"], ["0.0%"], ["0.0%"], ["5"], ["2"]],
          "sandbox-level: no observed return after 3d among 5 sandboxes")
    # creation-based: A resumed 1.5d after creation, C 3.5d; F observed only 1.5d (killed) so drops out at 2d+
    start = next(i for i, l in enumerate(lines) if l.startswith("OBSERVED SANDBOX RETURNS MEASURED FROM CREATION"))
    cre = [l for l in lines[start:] if l.strip().startswith(("1d |", "2d |", "3d |"))]
    cre1 = next(l for l in cre if l.strip().startswith("1d |"))
    cre2 = next(l for l in cre if l.strip().startswith("2d |"))
    check([f.split() for f in cre1.split("|")] == [["1d"], ["2", "40.0%"], ["5"]], "creation-based: 2 of 5 resumed more than 1d after creation")
    check([f.split() for f in cre2.split("|")] == [["2d"], ["1", "25.0%"], ["4"]], "creation-based: 1 of 4 observed sandboxes resumed more than 2d after creation")
    print("ALL EXTRA CHECKS PASSED")


if __name__ == "__main__":
    more()
