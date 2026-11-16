#!/usr/bin/env bash
set -eu
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$ROOT/backend/.venv/Scripts/python.exe" "$ROOT/run.py" stop
