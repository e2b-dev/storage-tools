# Streamed paused-sandbox cleanup

Delete currently paused sandboxes by time since their latest pause or latest start/resume.
The script streams ascending API pages and starts deleting before the full inventory is read.
The default is a dry run. `--apply` deletes immediately and permanently without prompts.

Both `cleanup_paused_sandboxes.py` and `cleanup_paused_sandboxes_cli.sh` use the same HTTP client.
The shell entry point needs Python and the repository dependencies, but does not need the E2B CLI.
Both read `E2B_API_KEY`, optional `E2B_DOMAIN`, and optional `E2B_API_URL` from the environment.

**[All storage tools](../README.md)** · **[Storage management guide](https://docs.e2b.dev/sandbox/storage-management)**

## Setup and run

From the repository root (or a package root containing `cleanup/`, `requirements.txt`, and
`LICENSE`), install Python 3.11 or newer and the dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export E2B_API_KEY=your_key

# Preview sandboxes that have been paused (unused) for more than 72 hours.
.venv/bin/python cleanup/cleanup_paused_sandboxes.py --unused-for-days 3

# Delete directly; no prior dry run is required.
.venv/bin/python cleanup/cleanup_paused_sandboxes.py --unused-for-days 3 --apply

# Same operation through the shell entry point, limited to one workload.
bash cleanup/cleanup_paused_sandboxes_cli.sh --unused-for-days 3 --metadata env=dev --apply
```

The default, `--age-from paused`, measures time since the sandbox was last paused, which is the
time it was last used. This is the standard retention policy ("delete anything untouched for N
days"). `--age-from started` instead measures from the latest start/resume, including initial
creation for a sandbox that has never resumed; it can select a sandbox that was paused recently
after a long run, so use it only when you specifically want start-based retention. Running
sandboxes are excluded in both modes. The cutoff is fixed when the run starts and the comparison
is strictly older than that cutoff.

## Request budgets and streaming

Every GET and DELETE attempt, including retries, shares one process-wide pacing budget.
The default is **20 total requests per second**, with requests spaced apart rather than sent in
bursts. This keeps this cleanup process's GET traffic within a 20 GET/second limit and reserves
part of that same budget for deletes. It does not reserve capacity against other applications,
other API keys, or jobs on other machines. Set a lower `--rate` when sharing a budget, and respect
any lower account-specific delete limit.

There are no hidden SDK or CLI retries. HTTP 429, 500, 502, 503, 504 and transport failures retry
up to `--retries` times. A valid `Retry-After` is honored by all workers; otherwise retries use
exponential backoff with jitter. A requested wait longer than 60 seconds stops the run for
a later invocation instead of retrying early or holding a cron indefinitely. Authentication and
permission failures stop further requests. Exhausted per-resource failures are logged and the
run continues with other resources; listing failures stop discovery and return a nonzero exit.

The normal path holds one page and at most `--concurrency` deletes in flight. It uses the listed
state and timestamps and makes **no per-sandbox GETs** unless `--recheck` is set. The API orders
by start time, not pause time, so the default pause-based retention scans every page of paused
sandboxes (one GET per 100 sandboxes). With `--age-from started`, listing stops at the first
timestamp at or beyond the cutoff. `--limit` bounds the number of matching sandboxes processed
per run. A rerun discovers the remaining resources anew.

A listing row the tool cannot interpret (unknown state, missing or malformed timestamp) is
logged to stderr and skipped; the other sandboxes on the page are still processed and the run
exits nonzero so the skip is visible to cron monitoring.

## Cron

The cron example uses a full repository installation and its unified `storage_tools.py` command.
For a standalone cleanup package, replace that command with the absolute path to
`cleanup/cleanup_paused_sandboxes.py`.

An hourly job can delete up to 10,000 qualifying sandboxes per invocation. Save a wrapper at
`/opt/storage-tools/cleanup-cron.sh`, adapting paths and region settings to your installation:

```bash
#!/usr/bin/env bash
set -euo pipefail
set -a
source /opt/storage-tools/cleanup.env
set +a
exec /opt/storage-tools/.venv/bin/python \
  /opt/storage-tools/storage_tools.py cleanup-paused-sandboxes \
  --unused-for-days 3 --apply --recheck --rate 15 --limit 10000
```

`--recheck` re-reads each sandbox before the first delete attempt and again after backoff
before each retry. It skips a sandbox that no longer matches. Each check uses one GET.
A resume can still occur between the final check and deletion; coordinate resumes in the
application if needed.

Store `E2B_API_KEY=your_team_key` and any `E2B_DOMAIN` setting in `cleanup.env`.
Restrict it with `chmod 600 /opt/storage-tools/cleanup.env`, test the wrapper manually, and schedule:

```cron
0 * * * * /bin/bash /opt/storage-tools/cleanup-cron.sh >> /opt/storage-tools/cleanup.log 2>&1
```

Apply runs acquire a nonblocking local lock for the API key and endpoint. A second overlapping
run exits nonzero rather than starting another worker pool. `--lock-file` overrides its path.
Keep a shared lock path when using multiple installations on one host. This is a local process
lock, not a distributed lock; separate hosts or credentials need external coordination.
Monitor nonzero exits and rotate the log. An hourly job enforces retention on an hourly cadence.

## Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--unused-for-days N` | `90` | Select age strictly greater than N days; N must be a whole number. `--days` is a compatibility alias. |
| `--unused-for-hours N` | — | Select age strictly greater than N hours. Use either hours or days. |
| `--age-from paused\|started` | `paused` | Latest pause or latest start/resume timestamp. |
| `--metadata k=v,k2=v2` | none | Match every supplied pair. |
| `--apply` | off | Permanently delete matches. |
| `--rate N` | `20` | Combined GET and DELETE attempts per second, including retries. |
| `--concurrency N` | `10` | Up to N sandbox deletes in flight; 1–64. |
| `--page-size N` | `100` | Resources per list request; 1–100. |
| `--retries N` | `5` | Additional attempts after transient errors; 0–10. |
| `--limit N` | none | Process at most N matching/pending sandboxes this run. |
| `--lock-file PATH` | automatic | Local overlap lock for apply runs. |
| `--recheck` | off | Fresh state, age and metadata lookup immediately before a normal sandbox delete. |
| `--delete-snapshots` | off | Also delete named snapshots associated with selected sandboxes, regardless of snapshot age. |
| `--journal PATH` | automatic | Named-snapshot recovery journal. |
| `--retry-pending` | off | With `--delete-snapshots`, process existing journal entries only. |

## Deletion semantics

The default cron path acts on the page's observed state. A sandbox resumed after it was listed
can be killed, because the API has no conditional delete and killing a resumed sandbox also
removes its pause snapshot. `--recheck` narrows that interval to the gap between one GET and the
DELETE but cannot make checking and deletion atomic.
Use application-level resume coordination if that race is unacceptable. The legacy
`--resumes-stopped` flag remains accepted for existing commands and does not create a lock.

Missing resources are treated as already gone. Failed resources are eligible on the next run.
Exit 0 means no failures, exit 1 indicates a failed request or local operation, exit 2 indicates
invalid arguments, and exit 130 indicates interruption. Completed deletions stand after an
interruption; requests already in flight can finish.

## Optional named-snapshot cleanup

A named snapshot is a separate resource that outlives its source sandbox. It is touched only with
`--delete-snapshots`. The API identifies a named snapshot as `team-slug/name:tag` (or
`templateID:tag` when it has no name); both forms are accepted and deleted. This mode processes parents sequentially, rechecking state, age, metadata
and timestamps around snapshot enumeration. It uses the same request budget as normal cleanup.

Before killing a parent, the tool durably records its timestamps and snapshot IDs. Reruns can
finish snapshot deletions after a lost kill response, partial failure or interruption. Changed
parent eligibility cancels its pending entry. The journal is private, locked and written
atomically; it is bound to credentials, endpoint and selection. Keep those consistent and retain
the journal until no entries remain. Dry runs do not create journals. Existing pending journal
entries are loaded into memory; the normal sandbox-only path needs no recovery inventory.

Snapshots created after enumeration and snapshots whose parents were already gone before the
run are not discovered. Snapshot retention is a separate policy from sandbox retention. Capturing again under the
same name updates the same snapshot resource; deleting it also removes that newer capture.
Keep `--delete-snapshots` off for names shared or reused by other workloads.

## Verification

Run offline tests from the repository root:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

`tests/cleanup/validate_e2e.py` creates uniquely tagged fixtures in the connected account, exercises
both entry points with one-resource pages, and deletes only its own fixtures. It validates age
selection, a recent resume, dry runs, bounded apply, named-snapshot cleanup and running-sandbox
preservation. It is an opt-in live test, not a million-resource benchmark.

For cron safety checks against real fixtures, run:

```bash
.venv/bin/python tests/cleanup/validate_cron_e2e.py
```

This runs the cron wrapper with an empty environment, checks concurrent workers, overlapping
jobs, interruption, retries, resume races, and snapshot recovery. A local proxy restricts
requests to the test's fixtures and injects failures; the resources and permitted API calls
are real. Evidence stays in gitignored `verification/`, and all fixtures are cleaned up.

For a larger live streaming check, run:

```bash
.venv/bin/python tests/cleanup/validate_streaming_e2e.py --count 150 --page-size 25
```

This creates 150 paused sandboxes and two controls in the connected account, then runs real
`--apply` cleanup with a unique metadata filter and `--unused-for-days 0` so fresh fixtures qualify.
It checks that deletion begins after the first page and before the last page is requested,
records HTTP timing and status codes at the default 20 requests/second budget, and verifies
that all selected sandboxes are gone while the controls survive. A `finally` block removes
the fixtures, including on failure. The printed evidence directory contains a fixture manifest
and a JSON report. This validates real pagination and deletion, not million-resource throughput.
