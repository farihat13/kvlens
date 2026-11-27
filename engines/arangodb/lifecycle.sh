#!/bin/bash
# engines/arangodb/lifecycle.sh — ArangoDB lifecycle: restore+start and load+backup.
#
# Shared by the start and load callers (engines/arangodb/{start-server,load-data}.sh). It never drops an
# index, so no setting can change which physical design gets measured.
#
# Interface (see ../lifecycle-common.sh for the hooks):
#   LC_BACKUP_DIR    the runs-sf<SF> backup dir. arango backups are SYMLINKS to
#                    timestamped dirs (backup-data.sh keeps history), so probes
#                    look one level down at $LC_BACKUP_DIR/data.
#   LC_SCHEMA_FILE   declarative JSON index set for the load (load only)
#   LC_BINARY_MODE   kvstats (default) | histogram | tickers — only `tickers`
#                    needs --rocksdb.enable-statistics true (its kvstats read
#                    RocksDB's native statistics object; the engine-wrapper
#                    counters of kvstats/histogram are hand-maintained).
#   MY_ARANGO_ROOTDIR, ARANGO_RUNSDIR, ARANGODB_CACHE_MB, ARANGODB_INTERNAL_CACHE_MB   (.env-owned)
#
# Deliberately NO page-cache drop here — none of the historical arango copies
# dropped caches (unlike dgraph/nebula).

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lifecycle-common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
source "$ENGINES_DIR/check-tools.sh"
check_tools arangodb run   # engines/needs.tsv: the server runs in a tmux session; its health checks use curl

arangodb_restore_and_start() {
    lc_require ENGINES_DIR MY_ARANGO_ROOTDIR ARANGO_RUNSDIR LC_BACKUP_DIR
    local cache_mb="${ARANGODB_CACHE_MB:-}"                     # RocksDB block cache (MB); empty = arango auto-size
    local internal_mb="${ARANGODB_INTERNAL_CACHE_MB:-0}"       # in-memory hash (edge) cache (MB); 0 = DISABLED
    local mode="${LC_BINARY_MODE:-kvstats}"
    case "$mode" in kvstats|histogram|tickers) ;; *) echo "ERROR: LC_BINARY_MODE must be kvstats|histogram|tickers, got '$mode'" >&2; exit 1 ;; esac

    if [ ! -d "$LC_BACKUP_DIR/data" ]; then
        echo "ERROR: backup not found at $LC_BACKUP_DIR/data" >&2
        exit 1
    fi

    lc_run_hook LC_PRE_RESTORE_CHECK

    echo "=== Resetting ArangoDB ==="
    cd "$MY_ARANGO_ROOTDIR"
    just kill 2>/dev/null || true
    sleep 2
    rm -rf "${ARANGO_RUNSDIR}/data"
    rm -f "${ARANGO_RUNSDIR}"/*.log

    echo "=== Restoring backup ($LC_BACKUP_DIR) ==="
    cp -r "${LC_BACKUP_DIR}/data" "${ARANGO_RUNSDIR}/data"

    # a cold start: the restored store leaves the OS page cache before the launch
    [[ "${ENGINES_REGIME_COLD_START:-0}" == 1 ]] && lc_evict_files "${ARANGO_RUNSDIR}/data"

    echo "Block cache: ${cache_mb:-default} MB   in-memory (edge) cache: ${internal_mb} MB"

    # --cache.size 0 (internal_mb=0): DISABLE arango's in-memory edge-index hash
    #   cache so every edge lookup goes through RocksDB and ALL KV ops are visible.
    #   This is the cross-DB-comparable baseline — dgraph runs LocalCache OFF and
    #   nebula has no such second cache; with it on, edge expansions hit the cache
    #   and SEEK/NEXT collapse to ~0, hiding the traversal's real KV cost.
    # --server.statistics false: turn OFF arango's own background statistics
    #   collector — its periodic reads go through RocksDBMethods and leak into the
    #   kvstats counters (~2 GETs/10s idle drift). Needed in EVERY mode.
    local startup_opts="--server.statistics false"
    if [[ "$mode" == "tickers" ]]; then
        startup_opts="--rocksdb.enable-statistics true ${startup_opts}"
    fi
    # equivalent: no asynchronous prefetch, so a query runs on one thread
    [[ "${ENGINES_SETTING:-counting}" == equivalent ]] && startup_opts="${startup_opts} --query.max-total-async-prefetch-slots 0 --query.max-query-async-prefetch-slots 0"
    # the bounded regimes: index and filter blocks stay outside the block cache (in memory), as NebulaGraph and Badger
    # keep them, so the three caches hold data blocks only; a regime with direct reads (steady): a miss reads the disk
    [[ "${ENGINES_REGIME_INDEX_FILTER_IN_CACHE:-}" == 0 ]] && startup_opts="${startup_opts} --rocksdb.cache-index-and-filter-blocks false"
    [[ "${ENGINES_REGIME_DIRECT_IO:-0}" == 1 ]] && startup_opts="${startup_opts} --rocksdb.use-direct-reads true"
    echo "=== Starting ArangoDB (mode=${mode}) ==="
    bash scripts/start.sh "info" "${cache_mb}" "${internal_mb}" "${startup_opts}"
    sleep 3
    lc_pin_to_cpu $(pgrep -x arangod)

    lc_run_hook LC_POST_START_CHECK
}

arangodb_load_and_backup() {
    lc_require ENGINES_DIR SF MY_ARANGO_ROOTDIR LC_BACKUP_DIR LC_SCHEMA_FILE

    if [ -d "$LC_BACKUP_DIR/data" ]; then
        echo "=== Skipping load: backup already exists at $LC_BACKUP_DIR ==="
        return 0
    fi
    [ -f "$LC_SCHEMA_FILE" ] || { echo "ERROR: schema not found: $LC_SCHEMA_FILE" >&2; exit 1; }

    # Fresh, empty arangod using the installed binary. Arango loads by importing
    # into a RUNNING server (no offline bulk path).
    cd "$MY_ARANGO_ROOTDIR"
    bash scripts/reset.sh
    bash scripts/start.sh

    bash scripts/ldbc/load.sh --sf "$SF" --schema "$LC_SCHEMA_FILE"

    lc_run_hook LC_POST_LOAD_HOOK

    # Flush first: backup-data.sh cp's a LIVE arangod's data dir, so unflushed
    # memtables would land torn.
    sleep 10; sync; sleep 2
    # backup-data.sh derives runs-sf<SF> (a symlink to a timestamped dir) under
    # the root itself — hand it the parent of LC_BACKUP_DIR.
    ARANGO_BACKUP_DIR="$(dirname "$LC_BACKUP_DIR")" bash scripts/ldbc/backup-data.sh "$SF"
    echo "=== Done: arango backup at $LC_BACKUP_DIR ==="
}
