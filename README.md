# E2B storage tools

Manage E2B sandbox storage from one repository: clean up a downloaded inventory, enforce
recurring retention on paused sandboxes, and analyze reuse before choosing a retention period.
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

Run all three tools through `storage_tools.py`. Each command accepts `--help`.

```bash
# Preview resources in a dashboard inventory that were last used more than 90 days ago.
uv run storage_tools.py cleanup-storage-inventory inventory.csv --unused-for-days 90

# Preview live sandboxes that have been paused for more than 3 days.
uv run storage_tools.py cleanup-paused-sandboxes --unused-for-days 3 --metadata env=dev

# Analyze reuse before selecting a retention window; read-only.
uv run storage_tools.py analyze-sandbox-reuse --since-days 30 --max-events 0 --windows 1,2,3,7
```

| Command | Source and purpose | Detailed guide |
| --- | --- | --- |
| `cleanup-storage-inventory` | Dashboard CSV; bulk cleanup of paused sandboxes and snapshots, with per-row results and resume support | [Inventory cleanup](cleanup-storage-inventory/README.md) |
| `cleanup-paused-sandboxes` | Live API listing; streaming retention cleanup suitable for cron, with optional named-snapshot recovery | [Recurring cleanup](cleanup/README.md) |
| `analyze-sandbox-reuse` | Lifecycle events and paused listing; estimate how a retention window affects observed resumes | [Lifecycle insights](lifecycle_insights/README.md) |

### Shared flags

Both cleanup commands use these names and meanings:

| Flag | Meaning |
| --- | --- |
| `--unused-for-days N` | Select resources last used **more than** N days ago; fractions allowed. Inventory uses CSV `last_used_at`; recurring cleanup uses the latest pause by default. |
| `--apply` | Permanently delete selected resources. Without it, preview only. |
| `--rate N` | API request budget per second, default **20**. |
| `--concurrency N` | Resources in progress at once, default **10**. |
| `--limit N` | Bound selected resources per invocation; omitted means no cap. |

Inventory cleanup has no age filter unless supplied. Recurring cleanup defaults to 90 days.
In recurring cleanup, `--age-from started` explicitly switches the threshold to the latest
start/resume, and `--limit` counts parent sandboxes, not their optional named snapshots.
Inventory cleanup supports `--type` and `--interactive`; recurring cleanup supports `--metadata`,
`--recheck`, and `--delete-snapshots`. Lifecycle analysis also uses `--metadata`; its `--since-days`
limits event history, a different concept from a deletion threshold.

The recurring tool spaces every HTTP attempt, including retries, within its budget and bounds
transient retries. The inventory tool uses the SDK and queues rate-limited rows again; its rate
limiter allows bursts. Budget for other applications and concurrent jobs separately.

Both tools can race with a concurrent resume: the API has no atomic paused-only delete.
Inventory cleanup always checks current sandbox state; recurring cleanup adds that lookup with
`--recheck`. Coordinate resumes in your application when this race is unacceptable.

### Existing entry points and recovery state

The original Python and shell scripts remain usable. Recurring cleanup accepts `--days` as an
alias for `--unused-for-days`, with the same selection and recovery-journal identity.
When moving an installation, copy its pending `.cleanup-journal-*.json` files into the new
`cleanup/` folder, or pass the original absolute `--journal` path. Preserve inventory
`*.results.csv` files alongside their CSVs. Keep the original credentials, endpoint and
selection when retrying named-snapshot recovery. Stop the old scheduled job before enabling
the new one, and share `--lock-file` if multiple installations must coordinate on one host.

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

The `cleanup/` and `lifecycle_insights/` folders can each be packaged independently with their
scripts and README, plus root `requirements.txt` and `LICENSE`. Exclude credentials, journals,
exports and verification data. `tests/lifecycle_insights/validate_customer_package_e2e.py`
builds and validates a lifecycle package, or checks the ZIP supplied through `LIFECYCLE_ZIP`.

## License

The tools imported from `e2b-dev/storage-management` retain their [Apache License 2.0](LICENSE).
