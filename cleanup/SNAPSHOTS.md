# Clean up unused snapshots

`cleanup-unused-snapshots` deletes snapshot resources by their own recorded use. It does not
delete source sandboxes or use their pause timestamps.

```bash
uv run storage_tools.py cleanup-unused-snapshots --name my-snapshot --unused-for-days 30
uv run storage_tools.py cleanup-unused-snapshots --name my-snapshot --unused-for-days 30 --apply
```

Omit `--name` to scan the team's snapshots. Names and IDs are accepted. A name or tag selects
the whole snapshot resource: deletion removes all its names, tags, and builds. The command
resolves names to canonical IDs, so a name reassignment cannot redirect a pending DELETE.

## Age and selection

The primary timestamp is the API's `lastSpawnedAt`, the last recorded start using the snapshot.
It also advances when a sandbox derived from that snapshot resumes. Snapshots themselves do
not resume. A source sandbox's pause time is never used for snapshot selection.

Snapshots with unknown last use are skipped; null is not treated as proof that a snapshot was
never used. Recent creation, updates, or builds also preserve a snapshot until they pass the
cutoff. A changed usage count, timestamp, or build identity cancels a pending deletion.

Apply runs hold each candidate for at least two minutes before rechecking it and deleting.
Candidates wait in batches of up to 1,000; dry runs do not wait. This allows normally buffered
usage updates to arrive without adding two minutes per deletion.

Output separates `last_used` (the API's `lastSpawnedAt`) from `retention_at` (the latest use,
creation, update, or build timestamp used for selection).

| Flag | Meaning |
| --- | --- |
| `--unused-for-days N` | Whole-day cutoff; default 90. |
| `--unused-for-hours N` | Hour cutoff; use either hours or days. |
| `--name NAME` | Select a snapshot by name or ID. Omit to scan the team. |
| `--apply` | Delete matches. Without it, preview only. |
| `--rate N` | Combined HTTP attempts per second, including retries; default 20. |
| `--concurrency N` | Maximum deletes in flight; default 10. |
| `--limit N` | Maximum matching candidates processed per run. |
| `--page-size N` | Listing page size, 1–100; default 100. |
| `--retries N` | Retries per request, 0–10; default 5. |
| `--lock-file PATH` | Override the local account/endpoint overlap lock. |
| `--recheck` | Accepted for consistency; snapshot rechecks are always enabled. |

`--metadata` is unsupported: snapshot APIs expose no workload metadata. Metadata on a source
or derived sandbox is not treated as snapshot metadata.

## Scheduling and failure handling

Use the [cron wrapper](README.md#cron), replacing the command and selection flags with
`cleanup-unused-snapshots --unused-for-days 30`. Preview the selection first. Both live cleanup
commands share the account/endpoint lock, so stagger their jobs on the same host. Coordinate
jobs across hosts externally.

The tool rechecks snapshot membership, usage, and build identity before deletion and every
retry. HTTP 401/403 stops the run; retries are bounded and honor `Retry-After`. Missing resources
are already gone. In-use resources are preserved and cause a nonzero exit. Later runs discover
remaining work; completed deletes stand after interruption.

**Usage is best-effort, not an atomic inactivity check.** Updates normally flush about once a
minute. The two-minute hold lets earlier updates arrive; it does not recover lost updates or
cover longer delays. New use during the hold may still be unreported at the final recheck.
Usage or a new capture can also occur between the last check and
DELETE. Coordinate snapshot creation, recapture, and derived-sandbox resumes with cleanup when
that race is unacceptable. Building a template from a snapshot is not covered by this usage
counter; exclude snapshots used as template bases.

Exit codes: 0 for no failures, 1 for failed or blocked operations, 2 for invalid arguments,
130 for interruption. Dry runs and unknown-usage skips do not delete resources.

The CSV-based `cleanup-storage-inventory --type snapshot` remains available. Its `last_used_at`
is an exported creation-use timestamp, rebuilt daily; it does not recheck use after export.

## Verification

Run offline tests with `uv run python -m unittest discover -s tests -v`. The opt-in
`uv run python tests/cleanup/validate_snapshot_cleanup_e2e.py` creates isolated real snapshots,
observes API usage updates, checks selection and deletion, and cleans up its fixtures.
