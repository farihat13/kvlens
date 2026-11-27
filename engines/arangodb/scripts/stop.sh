#!/bin/bash
# ============================================================
# Graceful stop of ArangoDB server
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

PORT="${ARANGO_PORT}"

# Try graceful shutdown via API
if curl -sf "http://localhost:${PORT}/_api/version" > /dev/null 2>&1; then
    echo "Sending shutdown request..."
    curl -sf -X DELETE "http://localhost:${PORT}/_admin/shutdown" || true
    # Wait for process to exit
    TIMEOUT=30
    ELAPSED=0
    while [ $ELAPSED -lt $TIMEOUT ]; do
        if ! pgrep -x arangod > /dev/null 2>&1; then
            echo "ArangoDB stopped gracefully (${ELAPSED}s)"
            break
        fi
        sleep 1
        ELAPSED=$((ELAPSED + 1))
    done
    if pgrep -x arangod > /dev/null 2>&1; then
        echo "WARNING: ArangoDB did not stop within ${TIMEOUT}s, sending SIGTERM"
        pkill -x arangod 2>/dev/null || true
        sleep 3
        # Still alive after SIGTERM? Force kill (matches dgraph/nebula stop fallback).
        if pgrep -x arangod > /dev/null 2>&1; then
            echo "Still running after SIGTERM — force killing."
            exec bash "${SCRIPT_DIR}/kill.sh"
        fi
    fi
else
    echo "ArangoDB is not running on port ${PORT}"
fi

# Clean up tmux session
tmux kill-session -t arangod 2>/dev/null || true
echo "Done"
