#!/usr/bin/env bash
# Shared launcher: prefer the repository virtualenv; allow an explicit interpreter override.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${E2B_TOOLS_PYTHON:-}"
if [[ -z "$python_bin" ]]; then
  python_bin="$script_dir/../.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then python_bin=python3; fi
fi
exec "$python_bin" "$script_dir/$1" "${@:2}" --backend cli
