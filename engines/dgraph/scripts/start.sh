#!/bin/bash
# Start Dgraph (zero + alpha) with the default flags. Args: [v] [cache_mb] (defaults 3, 100).
# Delegates to the `just zero` / `just alpha` recipes so the alpha flags stay defined in one place.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"          # self-contained (paths from BASH_SOURCE); provides DGRAPH_RUNSDIR
V="${1:-3}"; CACHE="${2:-100}"
cd "$SCRIPT_DIR/.."                      # dgraph dir, where the justfile + zero/alpha recipes live

# Idempotent: if alpha already answers /health, do nothing (matches arango/nebula start).
if curl -sf localhost:8080/health >/dev/null 2>&1; then
    echo "Dgraph already running on :8080."
    exit 0
fi

just zero
sleep 1
just alpha "$V" "$CACHE"
rm -f "${DGRAPH_RUNSDIR}/runinfo" || true

# Block until alpha answers /health, so `just start` returns only when the cluster is usable
# (arango/nebula start scripts already wait for readiness; this evens dgraph up with them).
echo "Waiting for Dgraph alpha to be ready..."
for _ in $(seq 1 30); do
    if curl -sf localhost:8080/health >/dev/null 2>&1; then
        echo "Dgraph is ready."
        exit 0
    fi
    sleep 2
done
echo "ERROR: Dgraph alpha did not become ready within 60s" >&2
exit 1
