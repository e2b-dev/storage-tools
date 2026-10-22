# E2B sandbox lifecycle insights

**After a sandbox is paused, how often and how long after does anyone resume it?**
Use it to judge a retention change (say 7 days to 2 or 3).

Two equivalent read-only scripts. Nothing is created, resumed, or deleted.

**Default scope is the last 7 days and the newest 10,000 events. That is a quick look only.**
For a retention decision pass `--since-days 30 --max-events 0` (or `--all`). The report header
says `(DEFAULTS)` when the defaults are in effect.

**[All storage tools](../README.md)** · **[Storage management guide](https://docs.e2b.dev/sandbox/storage-management)**

From the repository root, use `uv run storage_tools.py analyze-sandbox-reuse` with the flags below.
The direct-script examples assume a standalone customer package with `requirements.txt` included.

## Run

```bash
# Shell (curl, jq, e2b CLI)
export E2B_API_KEY=your_key
./analyze_sandbox_reuse_cli.sh --since-days 30 --max-events 0 --windows 1,2,3,7 \
    --save-events events.json --csv sandboxes.csv

# Python (e2b>=2.50.0), same flags
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python analyze_sandbox_reuse.py --since-days 30 --max-events 0 --windows 1,2,3,7

# Re-analyse a saved file offline
./analyze_sandbox_reuse_cli.sh --events-file events.json --skip-listing --all --windows 12h,1,2,3
```

| Flag | Default | Meaning |
|---|---|---|
| `--windows` | `1,2,3,7` | Idle thresholds. Plain numbers are days; `12h`, `2d` work. |
| `--since-days N` | `7` | Only the last N days. Use 30 or more. `0` = no limit. |
| `--max-events N` | `10000` | Only the newest N events. `0` = no cap. |
| `--all` | off | `--since-days 0 --max-events 0`. |
| `--metadata k=v,...` | none | Only sandboxes matching every pair. |
| `--csv file` | none | One row per sandbox. |
| `--skip-listing` | off | Skip the paused-sandbox inventory. |
| `--save-events f` / `--events-file f` | none | Save selected events / replay a saved JSON array. |

The events feed keeps limited history, so run weekly with `--save-events` and keep the files.

JSON exports contain only the lifecycle events used by the report, after deduplication,
history limits, and `--metadata` filtering. The event cap applies before metadata filtering.
Without `--metadata`, the selected history can include every sandbox in the account.
The same rules apply when exporting a replay from `--events-file`.

JSON and CSV exports contain sandbox identifiers and metadata. Both are written atomically
with owner-only permissions (`0600`), including when replacing an existing file. Review
their contents before sharing them. Filtering selects events; it does not redact fields
within a matching event.

## How the numbers are made

1. **Return** = a `paused` event immediately followed by a `resumed` event for the same
   sandbox. Idle time is the gap between them.
2. **Unpaired resume** = a resume with no such pause in the history. Counted, but left out of
   idle-time figures. The report shows lower/upper bounds assuming none or all of them were
   late among recorded resumes. Unpaired events may reflect the cutoff, dropped events, or
   ambiguous sequences; the bounds do not cover resumes that were never recorded.
3. **Consecutive pauses** with no resume between them are ambiguous and excluded.
4. **Pause with no later event**: the paused listing confirms whether it is still paused.
   Otherwise its state is unconfirmed, because deleting a paused sandbox emits no event.
5. **Rows at or beyond your current retention are not informative.** Anything already deleted
   can never show a resume. Use the shorter rows.
6. **Counts are sandboxes and pause episodes, not users.** Auto-resumes count too.

The report prints these six points in its METHOD block.

## What the report shows

1. METHOD.
2. Sandbox returns after N idle: the user's experience of an N-day rule.
3. Pause/resume timing per episode, with "unknown" for pauses too recent to judge.
4. Late resumes as a share of all resumes, with lower/upper bounds.
5. Returns measured from creation, for a rule keyed to the create timestamp.
6. Pause episodes with no observed resume, split by current listing state.
7. Currently paused sandboxes by idle age.

## Limits

- If dropped events are rare, the observed pairs can still provide useful insight. Unpaired
  and excluded counts flag incomplete or ambiguous histories; they do not measure dropped events.
- Events are fetched in offset pages of 100 with no time filter. Fetches are not retried;
  `--save-events` is written only after success.
- Replaying a saved file uses the current clock, so open pauses may reclassify. Pairs do not change.

## What it calls

`GET https://api.e2b.app/events/sandboxes` (created, paused, resumed, killed) and the paused
listing via `e2b sandbox list --state paused` or `Sandbox.list`. Both read-only.
