#!/bin/bash
# ============================================================
# ArangoDB status check
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

PORT="${ARANGO_PORT}"

if curl -sf "http://localhost:${PORT}/_api/version" > /dev/null 2>&1; then
    version=$(curl -s "http://localhost:${PORT}/_api/version" | python3 -c "import sys,json; print(json.load(sys.stdin).get('version','unknown'))" 2>/dev/null)
    echo "ArangoDB ${version} running on port ${PORT}"
    exit 0
else
    if pgrep -x arangod > /dev/null 2>&1; then
        echo "arangod process exists but not responding on port ${PORT}"
    else
        echo "ArangoDB is not running"
    fi
    exit 1
fi
