#!/bin/bash
# ============================================================
# Start ArangoDB server
# ============================================================
# Starts arangod in a tmux session with health check polling.
# Idempotent: succeeds if server is already running.
#
# Usage: bash scripts/start.sh [log_level] [rocksdb_cache_mb] [cache_mb] [extra_flags]
#   log_level:        arangod --log.level value (default: info)
#   rocksdb_cache_mb: RocksDB block cache in MB (default: arangod default)
#   cache_mb:         ArangoDB in-memory cache in MB (default: arangod default)
#   extra_flags:      additional arangod flags (e.g., "--rocksdb.enable-statistics true")
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

LOG_LEVEL="${1:-info}"
ROCKSDB_CACHE_MB="${2:-}"  # empty = arangod auto-sizes (~30% of RAM for rocksdb, few GB for cache)
CACHE_MB="${3:-}"
EXTRA_USER_FLAGS="${4:-}"

ARANGOD="${ARANGO_RUNSDIR}/bin/arangod"
PORT="${ARANGO_PORT}"
DATA_DIR="${ARANGO_RUNSDIR}/data"
LOG_FILE="${ARANGO_RUNSDIR}/arangod.log"
CONF_TEMPLATE="${ARANGO_DATADIR}/conf/arangod.conf"
CONF_FILE="${ARANGO_RUNSDIR}/arangod.conf"

# Check if already running
if curl -sf "http://localhost:${PORT}/_api/version" > /dev/null 2>&1; then
    echo "ArangoDB is already running on port ${PORT}"
    curl -s "http://localhost:${PORT}/_api/version" | python3 -m json.tool
    exit 0
fi

if [ ! -f "${ARANGOD}" ]; then
    echo "ERROR: arangod binary not found at ${ARANGOD}"
    echo "Run 'just build' and 'just install' first."
    exit 1
fi

# Raise vm.max_map_count if needed (ArangoDB recommends >= 3584000 for RocksDB)
CURRENT_MAP_COUNT=$(cat /proc/sys/vm/max_map_count)
if [ "$CURRENT_MAP_COUNT" -lt 3584000 ]; then
    sudo sysctl -w vm.max_map_count=3584000 > /dev/null
    echo "Set vm.max_map_count to 3584000 (was ${CURRENT_MAP_COUNT})"
fi

# Create runtime directories
mkdir -p "${DATA_DIR}"

# Generate config from template
sed -e "s|@DATADIR@|${DATA_DIR}|g" \
    -e "s|@LOGFILE@|${LOG_FILE}|g" \
    -e "s|@SRCDIR@|${ARANGO_SRCDIR}|g" \
    "${CONF_TEMPLATE}" > "${CONF_FILE}"

# Build extra flags from arguments
EXTRA_FLAGS="--log.level '${LOG_LEVEL}'"
if [ -n "${ROCKSDB_CACHE_MB}" ]; then
    EXTRA_FLAGS="${EXTRA_FLAGS} --rocksdb.block-cache-size $((ROCKSDB_CACHE_MB * 1048576))"
fi
if [ -n "${CACHE_MB}" ]; then
    EXTRA_FLAGS="${EXTRA_FLAGS} --cache.size $((CACHE_MB * 1048576))"
fi
if [ -n "${EXTRA_USER_FLAGS}" ]; then
    EXTRA_FLAGS="${EXTRA_FLAGS} ${EXTRA_USER_FLAGS}"
fi

echo "=== Starting ArangoDB on port ${PORT} ==="
echo "  log.level=${LOG_LEVEL}  rocksdb_cache=${ROCKSDB_CACHE_MB:-default}MB  cache=${CACHE_MB:-default}MB"

# Start arangod in a tmux session (single quotes around log level protect semicolons)
tmux new-session -d -s arangod "\
    ${ARANGOD} \
    --configuration ${CONF_FILE} \
    ${EXTRA_FLAGS} \
    2>&1 | tee -a ${LOG_FILE}.console"

# Wait for server to become ready
echo "Waiting for ArangoDB to start..."
TIMEOUT=120
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    if curl -sf "http://localhost:${PORT}/_api/version" > /dev/null 2>&1; then
        echo "ArangoDB is ready (${ELAPSED}s elapsed)"
        curl -s "http://localhost:${PORT}/_api/version" | python3 -m json.tool
        exit 0
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

echo "ERROR: ArangoDB did not start within ${TIMEOUT}s"
echo "=== Last 20 lines of log ==="
tail -20 "${LOG_FILE}" 2>/dev/null || tail -20 "${LOG_FILE}.console" 2>/dev/null || true
exit 1
