#!/usr/bin/env bash
# Install project-memory hooks into ~/.copilot/hooks/ (macOS / Linux)
set -euo pipefail
ROOT="$(pwd)"
if [[ ! -f "$ROOT/Setup-ProjectMemoryHooks.py" ]]; then
  echo "error: Setup-ProjectMemoryHooks.py not found in $ROOT" >&2
  exit 1
fi
exec python3 "$ROOT/Setup-ProjectMemoryHooks.py"
