#!/usr/bin/env sh
# Launch only the supervisor. It owns the Termux wake lock and child lifecycle.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN=python
else
    echo "Python 3 is required but was not found on PATH." >&2
    exit 127
fi

exec "$PYTHON_BIN" "$SCRIPT_DIR/supervisor.py"
