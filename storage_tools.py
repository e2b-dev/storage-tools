#!/usr/bin/env python3
"""Manage E2B storage with inventory cleanup, scheduled cleanup, and lifecycle insights."""

import argparse
import os
from pathlib import Path
import sys


COMMANDS = {
    "cleanup-storage-inventory": "bulk-cleanup/cleanup_storage_inventory.py",
    "cleanup-unused-snapshots": "scheduled-cleanup/cleanup_unused_snapshots.py",
    "cleanup-paused-sandboxes": "scheduled-cleanup/cleanup_paused_sandboxes.py",
    "analyze-sandbox-reuse": "lifecycle_insights/analyze_sandbox_reuse.py",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("args", nargs=argparse.REMAINDER, help="command options; use COMMAND --help")
    args = parser.parse_args()
    script = Path(__file__).resolve().parent / COMMANDS[args.command]
    os.execv(sys.executable, [sys.executable, str(script), *args.args])


if __name__ == "__main__":
    main()
