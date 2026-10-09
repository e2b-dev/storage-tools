# Delete resources listed in a storage inventory

Deletes the paused sandboxes and snapshots listed in the storage inventory CSV you download
from the E2B dashboard. By default it only prints what it would delete; `--apply` deletes
permanently, with no undo.

**Full guide:** https://docs.e2b.dev/sandbox/cleanup-storage-inventory (flags, what happens to
each row, and what to check before you run it).

```bash
export E2B_API_KEY=e2b_***    # the key of the team the inventory belongs to, or use .env.template

uv run cleanup_storage_inventory.py inventory.csv                       # dry run
uv run cleanup_storage_inventory.py inventory.csv --unused-for-days 90 --apply
uv run cleanup_storage_inventory.py inventory.csv --apply --interactive --limit 20  # confirm each row, at most 20
uv run cleanup_storage_inventory.py --help                              # all flags
```

## Tests

```bash
uv run python -m unittest discover -s tests -v
```
