#!/usr/bin/env python3
"""Understand whether and when users return to paused E2B sandboxes.

Reads the sandbox lifecycle events API (created / paused / resumed / killed) and
the current paused-sandbox listing, then reports for each observation window
(default 1, 2, 3, 7 days):

  * what share of pause episodes were resumed within the window
  * what share of observed resumes happened after the window
  * how many currently paused sandboxes are already older than the window

Read-only: it never creates, resumes, or deletes anything.
Counts describe sandboxes and pause episodes, not unique users. Results cover
the available event history; absence of a resume does not establish future use.
"""

import argparse
import csv
import json
import math
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

CREATED = "sandbox.lifecycle.created"
PAUSED = "sandbox.lifecycle.paused"
RESUMED = "sandbox.lifecycle.resumed"
KILLED = "sandbox.lifecycle.killed"
EVENT_TYPES = (CREATED, PAUSED, RESUMED, KILLED)
PAGE = 100
DEFAULT_SINCE_DAYS = 7.0
DEFAULT_MAX_EVENTS = 10_000


# --------------------------------------------------------------------------- input


def parse_ts(value: str) -> datetime:
    # API timestamps carry nanoseconds; Python parses at most microseconds.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    head, _, tail = value.partition(".")
    if tail:
        frac, _, tz = tail.partition("+")
        value = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def parse_window(token: str) -> float:
    """'48h' -> 2.0 days, '2d' -> 2.0, '0.5' -> 0.5 (days)."""
    token = token.strip().lower()
    if token.endswith("h"):
        return float(token[:-1]) / 24
    if token.endswith("d"):
        return float(token[:-1])
    return float(token)


def fmt_window(days: float) -> str:
    hours = days * 24
    return f"{hours:g}h" if days < 1 or hours % 24 else f"{days:g}d"


def metadata_arg(value: str) -> dict[str, str]:
    try:
        return dict(item.split("=", 1) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("use key=value,key2=value2") from error


def metadata_matches(event_metadata: Optional[dict], wanted: Optional[dict]) -> bool:
    if not wanted:
        return True
    if not event_metadata:
        return False
    return all(event_metadata.get(k) == v for k, v in wanted.items())


def fetch_events(
    api_key: str, domain: str, since: Optional[datetime], max_events: Optional[int], log=print
) -> list[dict]:
    """Newest-first pages of lifecycle events, stopping once older than `since` or past `max_events`."""
    base = f"https://api.{domain}/events/sandboxes"
    events: dict[str, dict] = {}
    offset = 0
    while True:
        query = [("limit", PAGE), ("offset", offset)] + [("types", t) for t in EVENT_TYPES]
        request = urllib.request.Request(
            f"{base}?{urllib.parse.urlencode(query)}", headers={"X-API-Key": api_key}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            page = json.load(response)
        for event in page:
            events[event["id"]] = event  # de-duplicate if pages shift under us
        if page:
            oldest = parse_ts(page[-1]["timestamp"])
            log(f"  fetched {len(events)} events, back to {oldest:%Y-%m-%d %H:%M}Z", file=sys.stderr)
            if since and oldest < since:
                break
        if max_events and len(events) >= max_events:
            break
        if len(page) < PAGE:
            break
        offset += PAGE
    return list(events.values())


def fetch_paused_listing(metadata: Optional[dict]) -> list:
    from e2b import Sandbox, SandboxQuery, SandboxState

    items = []
    paginator = Sandbox.list(
        query=SandboxQuery(state=[SandboxState.PAUSED], metadata=metadata), limit=100, order="asc"
    )
    while paginator.has_next:
        items.extend(paginator.next_items())
    return items


# --------------------------------------------------------------------------- model


@dataclass
class Timeline:
    sandbox_id: str
    created_at: Optional[datetime] = None
    killed_at: Optional[datetime] = None
    kill_reason: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    events: list[tuple[datetime, str]] = field(default_factory=list)  # (time, type) sorted


@dataclass
class PauseEpisode:
    sandbox_id: str
    paused_at: datetime
    resumed_at: Optional[datetime]  # next resume after this pause, if any
    ended_at: Optional[datetime]  # kill after this pause with no resume in between
    observed_until: datetime  # resume, kill, or now
    excluded: bool = False  # ambiguous consecutive pauses; no measured return time

    @property
    def resume_gap(self) -> Optional[timedelta]:
        return self.resumed_at - self.paused_at if self.resumed_at else None

    @property
    def observed(self) -> timedelta:
        return self.observed_until - self.paused_at

    def outcome(self, window: timedelta) -> str:
        """'resumed' / 'not_resumed' / 'unknown' for 'was it resumed within window?'."""
        if self.resumed_at and self.resume_gap <= window:
            return "resumed"
        if self.observed >= window:
            return "not_resumed"
        return "unknown"  # still paused (or killed) before the window elapsed


def build_timelines(events: Iterable[dict], metadata: Optional[dict], since: Optional[datetime]) -> dict[str, Timeline]:
    timelines: dict[str, Timeline] = {}
    seen_ids: set = set()
    for event in events:
        event_id = event.get("id")
        if event_id is not None:
            if event_id in seen_ids:
                continue  # duplicate delivery of the same event
            seen_ids.add(event_id)
        if event.get("type") not in EVENT_TYPES:
            continue  # updated/checkpointed etc. must not sit between a pause and its resume
        ts = parse_ts(event["timestamp"])
        if since and ts < since:
            continue
        data = event.get("eventData") or {}
        if not metadata_matches(data.get("sandbox_metadata"), metadata):
            continue
        tl = timelines.setdefault(event["sandboxId"], Timeline(event["sandboxId"]))
        if data.get("sandbox_metadata"):
            tl.metadata = data["sandbox_metadata"]
        kind = event["type"]
        tl.events.append((ts, kind))
        if kind == CREATED:
            tl.created_at = ts
        elif kind == KILLED:
            tl.killed_at = ts
            tl.kill_reason = data.get("kill_reason")
    for tl in timelines.values():
        tl.events.sort()
    return timelines


def pause_episodes(timelines: dict[str, Timeline], now: datetime) -> list[PauseEpisode]:
    episodes = []
    for tl in timelines.values():
        # Only adjacent lifecycle transitions form a pair. A chain of consecutive
        # pauses is ambiguous: retries, duplicate events, or missing transitions
        # cannot be distinguished. Exclude the entire chain through its next resume.
        for index, (ts, kind) in enumerate(tl.events):
            if kind != PAUSED:
                continue
            previous_kind = tl.events[index - 1][1] if index else None
            later_ts, later_kind = tl.events[index + 1] if index + 1 < len(tl.events) else (now, None)
            excluded = previous_kind == PAUSED or later_kind in (PAUSED, CREATED)
            resumed_at = later_ts if later_kind == RESUMED and not excluded else None
            ended_at = later_ts if later_kind == KILLED else None
            episodes.append(PauseEpisode(tl.sandbox_id, ts, resumed_at, ended_at, later_ts, excluded))
    return episodes


# --------------------------------------------------------------------------- report


def fmt_td(td: timedelta) -> str:
    seconds = int(td.total_seconds())
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def pct(numerator: int, denominator: int) -> str:
    return f"{100 * numerator / denominator:5.1f}%" if denominator else "   n/a"


def report(
    timelines: dict[str, Timeline],
    episodes: list[PauseEpisode],
    paused_now: list,
    windows: list[float],
    now: datetime,
    out=print,
    notes: Optional[list[str]] = None,
    listing_queried: Optional[bool] = None,
) -> None:
    excluded = sum(e.excluded for e in episodes)
    episodes = [e for e in episodes if not e.excluded]
    all_events = [ts for tl in timelines.values() for ts, _ in tl.events]
    out("=" * 72)
    out("SANDBOX PAUSE / RESUME ANALYSIS")
    out("=" * 72)
    if all_events:
        out(f"Events window : {min(all_events):%Y-%m-%d} .. {max(all_events):%Y-%m-%d} ({(now - min(all_events)).days} days)")
    out(f"Sandboxes seen: {len(timelines)}   pause episodes: {len(episodes)}   currently paused: {len(paused_now)}")
    for note in notes or []:
        out(note)
    if all_events and windows and (now - min(all_events)) < timedelta(days=max(windows)):
        out(f"Note: history spans {fmt_td(now - min(all_events))}, shorter than the {fmt_window(max(windows))} window;")
        out("      outcomes for that window will be mostly 'unknown'. Widen --since-days or use --all.")

    resumed = [e for e in episodes if e.resumed_at]
    gaps = [e.resume_gap.total_seconds() for e in resumed]
    resumed_by_sandbox = defaultdict(list)
    for episode in resumed:
        resumed_by_sandbox[episode.sandbox_id].append(episode)
    ever_resumed = set(resumed_by_sandbox)
    paused_sandboxes = {e.sandbox_id for e in episodes}
    out()
    out(f"Pause episodes with observed resumes : {len(resumed)} of {len(episodes)} ({pct(len(resumed), len(episodes))})")
    out(f"Sandboxes with observed returns      : {len(ever_resumed)} of {len(paused_sandboxes)} ({pct(len(ever_resumed), len(paused_sandboxes))})")
    unpaired = sum(kind == RESUMED for tl in timelines.values() for _, kind in tl.events) - len(resumed)
    out(f"Ambiguous pause episodes excluded    : {excluded}")
    out(f"Resume events without a usable pair  : {unpaired}")
    if gaps:
        out(
            "Pause -> resume gap                   : "
            f"p50 {fmt_td(timedelta(seconds=percentile(gaps, .5)))}, "
            f"p90 {fmt_td(timedelta(seconds=percentile(gaps, .9)))}, "
            f"p99 {fmt_td(timedelta(seconds=percentile(gaps, .99)))}, "
            f"max {fmt_td(timedelta(seconds=max(gaps)))}"
        )
    out()
    out("METHOD (deliberate choices)")
    out("  1. A return is a paused event immediately followed by a resumed event for the same")
    out("     sandbox, both inside the selected history. Idle time = resumed - paused.")
    out("  2. Resumes with no such pause are counted as 'unpaired' and left OUT of the gap")
    out("     figures. The pause may predate the cutoff, an event may be missing, or the sequence")
    out("     may be ambiguous. The bounds below cover unknown gaps among recorded resumes only.")
    out("  3. Consecutive pauses are ambiguous (e.g. missing or duplicate events) and excluded.")
    out("  4. No later event does not establish current state. A paused-listing match confirms")
    out("     it is currently paused; absence could mean deletion or an unobserved resume.")
    out("  5. Anything the current retention already deleted cannot show a resume, so rows at or")
    out("     beyond the current retention are not informative.")
    out("  6. Counts are sandboxes and pause episodes, not users. Auto-resumes are included.")

    out()
    out("OBSERVED SANDBOX RETURNS AFTER N")
    out("(paused = sandboxes with at least one non-ambiguous pause episode in the available history;")
    out(" returning = those with at least one observed resume)")
    out(f"{'window':>8} | {'resumed after N':>15} | {'% of paused':>11} | {'% of returning':>14} | {'paused':>6} | {'returning':>9}")
    out("-" * 80)
    for days in windows:
        window = timedelta(days=days)
        returned_after = {e.sandbox_id for e in resumed if e.resume_gap > window}
        total = len(paused_sandboxes)
        out(
            f"{fmt_window(days):>8} | {len(returned_after):>15} | {pct(len(returned_after), total):>11} | "
            f"{pct(len(returned_after), len(ever_resumed)):>14} | {total:>6} | {len(ever_resumed):>9}"
        )
    out()
    out("  Counts describe sandboxes, not unique users. Only observed pause/resume pairs count.")
    out("  Missing history and insufficient follow-up can hide returns; no observed return")
    out("  does not mean a user will never come back.")

    out()
    out("OBSERVED PAUSE / RESUME TIMING")
    out("(evaluable = resumed within the window, or observed for at least its full duration)")
    out(f"{'window':>8} | {'resumed within':>16} | {'not within window':>17} | {'evaluable':>9} | {'unknown':>7}")
    out("-" * 72)
    for days in windows:
        window = timedelta(days=days)
        outcomes = Counter(e.outcome(window) for e in episodes)
        evaluable = outcomes["resumed"] + outcomes["not_resumed"]
        out(
            f"{fmt_window(days):>8} | {outcomes['resumed']:>7} {pct(outcomes['resumed'], evaluable):>8} | "
            f"{outcomes['not_resumed']:>17} | {evaluable:>9} | {outcomes['unknown']:>7}"
        )
    out()
    out("  resumed within : share of evaluable pause episodes that were resumed inside the window")
    out("  unknown        : insufficient follow-up to classify a pause for this window")

    out()
    out("RESUMES THAT CAME AFTER N DAYS IDLE, AS A SHARE OF RECORDED RESUMES")
    out("(observed = paired only; bounds = none or all of the unpaired recorded resumes were late)")
    out(f"{'window':>8} | {'observed (paired only)':>24} | {'lower bound':>24} | {'upper bound':>24}")
    out("-" * 89)
    recorded_resumes = len(resumed) + unpaired
    for days in windows:
        window = timedelta(days=days)
        resumed_after = sum(1 for e in resumed if e.resume_gap > window)
        out(
            f"{fmt_window(days):>8} | {resumed_after:>6} of {len(resumed):<5} {pct(resumed_after, len(resumed)):>7} | "
            f"{resumed_after:>6} of {recorded_resumes:<5} {pct(resumed_after, recorded_resumes):>7} | "
            f"{resumed_after + unpaired:>6} of {recorded_resumes:<5} {pct(resumed_after + unpaired, recorded_resumes):>7}"
        )
    out()
    out("  Bounds assume measured pairs are accurate; they do not cover missing resume events.")

    out()
    out("OBSERVED SANDBOX RETURNS MEASURED FROM CREATION")
    out("(sandboxes with a created event and a pause; only those observed for at least N after")
    out(" creation count. 'resumed after N' = any resume more than N after the sandbox was created)")
    out(f"{'window':>8} | {'resumed more than N after creation':>34} | {'observed sandboxes':>18}")
    out("-" * 68)
    created = [tl for tl in timelines.values() if tl.created_at and tl.sandbox_id in paused_sandboxes]
    for days in windows:
        window = timedelta(days=days)
        observed = [tl for tl in created if ((tl.killed_at or now) - tl.created_at) >= window]
        returned = [
            tl for tl in observed
            if any(e.resumed_at - tl.created_at > window for e in resumed_by_sandbox[tl.sandbox_id])
        ]
        out(
            f"{fmt_window(days):>8} | {len(returned):>12} {pct(len(returned), len(observed)):>9}             | "
            f"{len(observed):>18}"
        )
    out()
    out("  The two tables measure different clocks. 'Returns after N' above counts idle time since")
    out("  the last pause, which is what a user experiences. This table counts time since creation,")
    out("  which matches a rule keyed to the create timestamp. A sandbox created 10 days ago, paused")
    out("  yesterday and resumed today is a return after 7d here but not in the table above.")

    out()
    out("PAUSE EPISODES WITH NO OBSERVED RESUME")
    never = [e for e in episodes if not e.resumed_at]
    listed = {s.sandbox_id for s in paused_now}
    have_listing = bool(paused_now) if listing_queried is None else listing_queried

    def never_label(e: PauseEpisode) -> str:
        if e.ended_at:
            return "observed kill: " + (timelines[e.sandbox_id].kill_reason or "unknown")
        if not have_listing:
            return "no later event (listing skipped; current state unconfirmed)"
        if e.sandbox_id in listed:
            return "no later event, still in paused listing"
        return "no later event, not in paused listing (current state unconfirmed)"

    reasons = Counter(never_label(e) for e in never)
    for label, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0])):
        out(f"  {label:<70} {count:>6}")
    killed_after_pause = [e for e in never if e.ended_at]
    if killed_after_pause:
        ages = [(e.ended_at - e.paused_at).total_seconds() for e in killed_after_pause]
        out(
            f"  pause duration before observed kill: p50 {fmt_td(timedelta(seconds=percentile(ages, .5)))}, "
            f"p90 {fmt_td(timedelta(seconds=percentile(ages, .9)))}, max {fmt_td(timedelta(seconds=max(ages)))}"
        )

    out()
    out("CURRENTLY PAUSED SANDBOXES BY TIME SINCE PAUSE")
    edges = sorted(set(windows))
    buckets = Counter()
    for sbx in paused_now:
        age_days = (now - sbx.end_at).total_seconds() / 86400
        label = next((f"< {fmt_window(d)}" for d in edges if age_days < d), f">= {fmt_window(edges[-1])}")
        buckets[label] += 1
    order = [f"< {fmt_window(d)}" for d in edges] + [f">= {fmt_window(edges[-1])}"]
    for label in order:
        out(f"  {label:<8} {buckets[label]:>6}")
    for days in edges:
        older = sum(1 for s in paused_now if (now - s.end_at) >= timedelta(days=days))
        out(f"  {older} of {len(paused_now)} sandboxes have been paused for at least {fmt_window(days)}")
    out("=" * 72)


@contextmanager
def private_output(path: str, *, newline=None):
    fd, temporary = tempfile.mkstemp(prefix=".sandbox-export-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", newline=newline) as handle:
            yield handle
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_csv(path: str, timelines: dict[str, Timeline], episodes: list[PauseEpisode], paused_now: list) -> None:
    listing = {s.sandbox_id: s for s in paused_now}
    by_sandbox: dict[str, list[PauseEpisode]] = defaultdict(list)
    for e in episodes:
        by_sandbox[e.sandbox_id].append(e)
    with private_output(path, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "sandbox_id", "created_at", "first_paused_at", "last_paused_at", "pauses", "resumes",
                "last_resumed_at", "killed_at", "kill_reason", "currently_paused",
                "current_started_at", "current_paused_at", "metadata", "excluded_pause_episodes",
            ]
        )
        for sid in sorted(set(timelines) | set(listing)):
            tl = timelines.get(sid) or Timeline(sid)
            all_eps = by_sandbox.get(sid, [])
            eps = [e for e in all_eps if not e.excluded]
            resumes = [e.resumed_at for e in eps if e.resumed_at]
            current = listing.get(sid)
            metadata = tl.metadata or (getattr(current, "metadata", None) or {})
            writer.writerow(
                [
                    sid,
                    tl.created_at.isoformat() if tl.created_at else "",
                    min(e.paused_at for e in eps).isoformat() if eps else "",
                    max(e.paused_at for e in eps).isoformat() if eps else "",
                    len(eps),
                    len(resumes),
                    max(resumes).isoformat() if resumes else "",
                    tl.killed_at.isoformat() if tl.killed_at else "",
                    tl.kill_reason or "",
                    "yes" if current else "no",
                    current.started_at.isoformat() if current else "",
                    current.end_at.isoformat() if current else "",
                    json.dumps(metadata, sort_keys=True) if metadata else "",
                    sum(e.excluded for e in all_eps),
                ]
            )


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--windows", default="1,2,3,7", help="observation windows, e.g. 12h,1,2,3,7 (plain numbers are days; default 1,2,3,7)")
    parser.add_argument("--since-days", type=float, default=None,
                        help=f"only use events from the last N days (default {DEFAULT_SINCE_DAYS:g}; 0 = no time limit)")
    parser.add_argument("--max-events", type=int, default=None,
                        help=f"use at most the newest N events (default {DEFAULT_MAX_EVENTS}; 0 = no cap)")
    parser.add_argument("--all", action="store_true", help="use all available history (same as --since-days 0 --max-events 0)")
    parser.add_argument("--metadata", type=metadata_arg, help="only sandboxes whose metadata matches key=value,key2=value2")
    parser.add_argument("--csv", help="write one row per sandbox to this CSV file")
    parser.add_argument("--skip-listing", action="store_true", help="do not query the current paused-sandbox listing")
    parser.add_argument("--events-file", help="analyse a saved JSON array of events instead of calling the API")
    parser.add_argument("--save-events", help="save events selected by history limits and metadata to a private JSON file")
    args = parser.parse_args()

    try:
        windows = sorted({parse_window(w) for w in args.windows.split(",")})
    except ValueError:
        windows = []
    if not windows or any(not math.isfinite(w) or w <= 0 for w in windows):
        parser.error("--windows must be positive durations like 12h,1,2,3,7")
    domain = os.environ.get("E2B_DOMAIN", "e2b.app")
    now = datetime.now(timezone.utc)
    using_defaults = args.since_days is None and args.max_events is None and not args.all
    if args.since_days is None:
        args.since_days = DEFAULT_SINCE_DAYS
    if args.max_events is None:
        args.max_events = DEFAULT_MAX_EVENTS
    if args.all:
        args.since_days, args.max_events = 0, 0
    if args.since_days < 0 or args.max_events < 0:
        parser.error("--since-days and --max-events must be non-negative")
    since = now - timedelta(days=args.since_days) if args.since_days else None
    max_events = args.max_events or None

    if args.events_file:
        with open(args.events_file) as handle:
            events = json.load(handle)
    else:
        api_key = os.environ.get("E2B_API_KEY")
        if not api_key:
            parser.error("E2B_API_KEY is not set (needed to fetch events; not needed with --events-file)")
        print("Fetching lifecycle events...", file=sys.stderr)
        events = fetch_events(api_key, domain, since, max_events)
    # Apply the same limits regardless of source: de-duplicate, newest first, then cap.
    events = list({e["id"]: e for e in events if e.get("type") in EVENT_TYPES}.values())
    events.sort(key=lambda e: e["timestamp"], reverse=True)
    if since:
        events = [e for e in events if parse_ts(e["timestamp"]) >= since]
    cap_reached = bool(max_events) and len(events) >= max_events
    if max_events:
        events = events[:max_events]
    events = [e for e in events if metadata_matches((e.get("eventData") or {}).get("sandbox_metadata"), args.metadata)]
    if args.save_events:
        with private_output(args.save_events) as handle:
            json.dump(events, handle)

    requested = "all available history" if not since and not max_events else ", ".join(
        part for part in [
            f"last {args.since_days:g} days" if since else "",
            f"newest {max_events} events" if max_events else "",
        ] if part
    )
    notes = [f"History requested : {requested}" + (" (DEFAULTS)" if using_defaults else "")]
    if using_defaults:
        notes.append("DEFAULTS IN EFFECT: quick look only. For a retention decision run with")
        notes.append("  --since-days 30 --max-events 0 (or --all).")
    if events:
        notes.append(f"Events used       : {len(events)} (oldest {parse_ts(events[-1]['timestamp']):%Y-%m-%d %H:%M}Z)")
    else:
        notes.append("Events used       : 0")
    if cap_reached:
        notes.append("Note: the event cap was reached, so older events were left out. Raise --max-events or use --all.")

    timelines = build_timelines(events, args.metadata, since)
    episodes = pause_episodes(timelines, now)

    paused_now = []
    if not args.skip_listing:
        print("Listing currently paused sandboxes...", file=sys.stderr)
        paused_now = fetch_paused_listing(args.metadata)

    report(timelines, episodes, paused_now, windows, now, notes=notes, listing_queried=not args.skip_listing)
    if args.csv:
        write_csv(args.csv, timelines, episodes, paused_now)
        print(f"Per-sandbox detail written to {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
