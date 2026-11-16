#!/usr/bin/env bash
# Windows Git Bash: same process ownership checks as PowerShell.
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT/backend/.venv/Scripts/python.exe" "$ROOT/run.py" start "$@"
