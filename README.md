# E2B storage tools

Clean up paused sandboxes and unused snapshots, process a downloaded inventory, and analyze
sandbox reuse before choosing a retention period.
Cleanup commands preview by default and require `--apply` for permanent deletion.

**[Storage management guide](https://docs.e2b.dev/sandbox/storage-management)**

## Install

Use Python 3.11 or newer on Linux or macOS. Recurring cleanup uses POSIX file locks.

```bash
git clone https://github.com/e2b-dev/storage-tools.git
cd storage-tools
uv sync --locked
export E2B_API_KEY=your_team_key
```

Alternatively, create a virtual environment with `python3 -m venv .venv` and install
`requirements.txt` with `.venv/bin/pip install -r requirements.txt`. In that installation,
replace `uv run` below with `.venv/bin/python`.

Use the API key of the team you intend to manage. Set `E2B_DOMAIN` for a nondefault region.
The inventory command also supports its existing `.env` file. For cron, explicitly load the
credentials in the wrapper described in the [recurring cleanup guide](cleanup/README.md#cron).

## Commands

Run all tools through `storage_tools.py`. Each command accepts `--help`.

```bash
# Preview resources in a dashboard inventory that were last used more than 90 days ago.
uv run storage_tools.py cleanup-storage-inventory inventory.csv --unused-for-days 90

# Preview live sandboxes that have been paused for more than 3 days.
uv run storage_tools.py cleanup-paused-sandboxes --unused-for-days 3 --metadata env=dev

# Preview a snapshot last used more than 30 days ago.
uv run storage_tools.py cleanup-unused-snapshots --name my-snapshot --unused-for-days 30

# Analyze reuse before selecting a retention window; read-only.
uv run storage_tools.py analyze-sandbox-reuse --since-days 30 --max-events 0 --windows 1,2,3,7
```

| Command | Source and purpose | Detailed guide |
| --- | --- | --- |
| `cleanup-storage-inventory` | Dashboard CSV; bulk cleanup of paused sandboxes and snapshots, with per-row results and resume support | [Inventory cleanup](cleanup-storage-inventory/README.md) |
| `cleanup-unused-snapshots` | Live snapshot listing; cleanup by recorded last use, with identity and usage rechecks | [Snapshot cleanup](cleanup/SNAPSHOTS.md) |
| `cleanup-paused-sandboxes` | Live API listing; streaming retention cleanup of paused sandboxes, suitable for cron | [Recurring cleanup](cleanup/README.md) |
| `analyze-sandbox-reuse` | Lifecycle events and paused listing; estimate how a retention window affects observed resumes | [Lifecycle insights](lifecycle_insights/README.md) |

### Resource types and age

| Resource | Age starts at | Selection |
| --- | --- | --- |
| Paused sandbox | Its most recent pause | `cleanup-paused-sandboxes`, or inventory `--type paused_sandbox` |
| Snapshot | Its last recorded use (`lastSpawnedAt`); this also includes resumes of derived sandboxes | `cleanup-unused-snapshots` |

Snapshots do not resume. A sandbox's pause age does not establish its snapshots' last use.
Inventory cleanup selects both types unless you pass `--type`.

### Shared flags

The live cleanup commands share these flags:

| Flag | Meaning |
| --- | --- |
| `--unused-for-days N` | Select resources last used **more than** N days ago; N must be a whole number. Paused sandboxes use their latest pause; snapshots use recorded last use. |
| `--unused-for-hours N` | Select resources last used **more than** N hours ago. Use either hours or days. |
| `--apply` | Permanently delete selected resources. Without it, preview only. |
| `--rate N` | API request budget per second, default **20**. |
| `--concurrency N` | Resources in progress at once, default **10**. |
| `--limit N` | Bound selected resources per invocation; omitted means no cap. |

Live cleanup defaults to 90 days. Inventory cleanup has no age filter unless supplied.
In paused-sandbox cleanup, `--age-from started` switches the threshold to the latest
start/resume. Both live commands count matching candidates toward `--limit`, including skipped and failed candidates.
Inventory cleanup supports `--type` and `--interactive`. Paused-sandbox cleanup supports `--metadata`.
Snapshot cleanup supports `--name` instead: the snapshot API exposes no workload metadata.
Both live commands share `--retries`, `--page-size`, and `--lock-file`; snapshot rechecks are always enabled. Lifecycle analysis also uses `--metadata`; its `--since-days`
limits event history, a different concept from a deletion threshold.

The live tools space every HTTP attempt, including retries, within their budget and bound
transient retries. The inventory tool uses the SDK and queues rate-limited rows again; its rate
limiter allows bursts. Budget for other applications and concurrent jobs separately.

Paused-sandbox deletion can race with a concurrent resume: the API has no atomic paused-only delete.
Inventory cleanup always checks current sandbox state; paused-sandbox cleanup adds that lookup with
`--recheck`. Coordinate resumes in your application when this race is unacceptable.

Snapshot cleanup adds two minutes to the requested retention age, without waiting or extra
API requests. Usage updates can still be delayed or lost. See [snapshot cleanup](cleanup/SNAPSHOTS.md#scheduling-and-failure-handling)
before scheduling deletion.

### Existing entry points and recovery state

The original Python and shell scripts remain usable. Paused-sandbox cleanup accepts `--days` as an
alias for `--unused-for-days`. It deletes paused sandboxes only; snapshot deletion and its
`--journal` / `--retry-pending` flags are no longer supported. Existing journal files are left
untouched. Do not replay their snapshot IDs using parent age; evaluate snapshots independently
with the snapshot command and their own recorded-use timestamps.

Preserve inventory `*.results.csv` files alongside their CSVs. Stop the old scheduled job before
enabling its replacement, and share `--lock-file` when coordinating installations on one host.

The cleanup shell entry point uses the same Python HTTP client. The lifecycle shell entry
point is an equivalent read-only implementation that additionally needs `curl`, `jq`, and
the E2B CLI.

## Tests

```bash
uv run python -m unittest discover -s tests -v
uv run python -m unittest discover -s cleanup-storage-inventory/tests -v
uv run python tests/lifecycle_insights/test_analyze_reuse.py
shellcheck lifecycle_insights/*.sh cleanup/*.sh
```

`tests/cleanup/` and `tests/lifecycle_insights/` include the imported offline suites and opt-in
`validate_*.py` live harnesses. Live harnesses create tagged fixtures and clean them up; evidence
is written under gitignored `verification/`. See the tool guides before running them against
an account.

## Packaging a tool

For recurring cleanup, package the `cleanup/` folder beneath a package root containing
`requirements.txt` and `LICENSE`; its README documents the direct-script commands. For
lifecycle analysis, package the scripts and README from `lifecycle_insights/` alongside
`requirements.txt` and `LICENSE` in one directory. These standalone packages do not include
the unified root command. Exclude credentials, journals, exports and verification data. `tests/lifecycle_insights/validate_customer_package_e2e.py`
builds and validates a lifecycle package, or checks the ZIP supplied through `LIFECYCLE_ZIP`.

## License

The tools imported from `e2b-dev/storage-management` retain their [Apache License 2.0](LICENSE).
