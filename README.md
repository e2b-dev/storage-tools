# E2B storage tools

Delete unused paused sandboxes and snapshots, or analyze sandbox reuse before choosing a retention period.
Cleanup commands preview by default; add `--apply` to delete permanently.

[Bulk cleanup guide](https://docs.e2b.dev/sandbox/cleanup-storage-inventory) · [Scheduled cleanup guide](https://docs.e2b.dev/sandbox/storage-management)

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Scheduled cleanup runs on Linux or macOS.

```bash
git clone https://github.com/e2b-dev/storage-tools.git
cd storage-tools
uv sync --locked
export E2B_API_KEY=your_team_key
```

Use your team's API key. Set `E2B_DOMAIN` for a nondefault region.

## Tools

| Folder | Purpose |
| --- | --- |
| [bulk-cleanup](bulk-cleanup/README.md) | Delete resources listed in a dashboard inventory CSV. |
| [scheduled-cleanup](scheduled-cleanup/README.md) | Run recurring retention cleanup against live API listings. Includes [snapshot cleanup](scheduled-cleanup/SNAPSHOTS.md). |
| [lifecycle_insights](lifecycle_insights/README.md) | Analyze sandbox reuse to choose a retention period. |

Run commands from the repository root; each accepts `--help`:

```bash
# Preview bulk cleanup from a CSV.
uv run storage_tools.py cleanup-storage-inventory inventory.csv --unused-for-days 90

# Preview sandboxes paused for more than three days.
uv run storage_tools.py cleanup-paused-sandboxes --unused-for-days 3 --metadata env=dev --recheck

# Preview snapshots by their own recorded last use.
uv run storage_tools.py cleanup-unused-snapshots --name my-snapshot --unused-for-days 30

# Analyze observed sandbox reuse.
uv run storage_tools.py analyze-sandbox-reuse --since-days 30 --max-events 0 --windows 1,2,3,7
```

Cleanup supports whole `--unused-for-days` or `--unused-for-hours`, plus `--rate`, `--concurrency`, and an optional `--limit`. Live cleanup defaults to 90 days; CSV cleanup has no age filter by default. Use `--metadata` for paused sandboxes and `--name` for snapshots.

Paused-sandbox age starts at its latest pause. Snapshot age uses recorded last use, with a two-minute retention buffer. Usage updates can be delayed or lost, and deletion checks can race with reuse. Read the [paused-sandbox](scheduled-cleanup/README.md) and [snapshot](scheduled-cleanup/SNAPSHOTS.md) safeguards before enabling a schedule.

See [development](DEVELOPMENT.md) for tests and standalone packaging, and [LICENSE](LICENSE) for Apache License 2.0 terms.
