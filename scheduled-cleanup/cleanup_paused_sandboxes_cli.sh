#!/usr/bin/env bash
# Shared paced HTTP transport, cleanup behavior and flags.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$script_dir/run_python.sh" cleanup_paused_sandboxes.py "$@"
