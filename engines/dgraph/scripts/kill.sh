#!/bin/bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

# Try graceful shutdown first (SIGTERM)
if pgrep -f 'dgraph.* (zero|alpha)' >/dev/null 2>&1; then
    echo "Stopping dgraph processes..."
    pkill -TERM -f 'dgraph.* (zero|alpha)' 2>/dev/null || true
    sleep 2
fi
# Force kill if still running (SIGKILL)
if pgrep -f 'dgraph.* (zero|alpha)' >/dev/null 2>&1; then
    echo "Force killing remaining dgraph processes..."
    pkill -9 -f 'dgraph.* (zero|alpha)' 2>/dev/null || true
    sleep 1
fi
# Verify cleanup — exit nonzero if anything survived, so reset.sh won't wipe data under a live process.
if pgrep -f 'dgraph.* (zero|alpha)' >/dev/null 2>&1; then
    echo "Warning: Some dgraph processes still running:" >&2
    pgrep -fa 'dgraph.* (zero|alpha)' >&2
    exit 1
fi
echo "All dgraph processes stopped."
rm -f "${DGRAPH_RUNSDIR}/runinfo" || true
sleep 1