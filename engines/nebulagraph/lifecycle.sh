#!/bin/bash
# engines/nebulagraph/lifecycle.sh — NebulaGraph lifecycle: restore+start and load+backup.
#
# Shared by the start and load callers (engines/nebulagraph/{start-server,load-data}.sh), which set the
# LC_* vars below.
#
# Interface (see ../lifecycle-common.sh for the hooks):
#   LC_BACKUP_DIR    the runs-sf<SF> backup dir — restore source / backup target
#   LC_SCHEMA_FILE   .gql schema for the load (load only)
#   LC_NEBULA_DIR    the binary+config stash (bin*/ + configs/): engines/nebulagraph
#   LC_BINARY_MODE   kvstats (default) | histogram | plan-debug  → bin/ | bin-histogram/ | bin-plan-debug/
#   NEBULA_CACHE_MB, NEBULA_STORAGED_MEM_RATIO, NEBULA_GRAPHD_MEM_RATIO   (.env-owned knobs)

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lifecycle-common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR

_nebula_bin_dir() {
    local mode="${LC_BINARY_MODE:-kvstats}"
    case "$mode" in
        kvstats)    echo "$LC_NEBULA_DIR/bin" ;;
        histogram)  echo "$LC_NEBULA_DIR/bin-histogram" ;;
        plan-debug) echo "$LC_NEBULA_DIR/bin-plan-debug" ;;
        *) echo "ERROR: LC_BINARY_MODE must be kvstats|histogram|plan-debug, got '$mode'" >&2; exit 1 ;;
    esac
}

# Stop all nebula services and wait for the processes to actually exit.
# nebula.service returns after sending the stop signal, but storaged can stay
# alive briefly while flushing RocksDB, which makes a following cp fail with
# "Text file busy" (or backs up a torn store). Erroring after the wait (instead
# of proceeding, as the pre-2026-08-31 load script did) is deliberate: a torn
# backup is a silent measurement confound. If an sf1+ stop legitimately needs
# more than 30s, raise the loop bound here.
_nebula_stop_and_wait() {
    ( cd "$ENGINES_DIR/nebulagraph/runs" && ./scripts/nebula.service stop all || true )
    local i
    for i in $(seq 1 30); do
        if ! pgrep -x nebula-storaged >/dev/null \
           && ! pgrep -x nebula-graphd  >/dev/null \
           && ! pgrep -x nebula-metad   >/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: nebula processes still running after 30s; aborting." >&2
    pgrep -af 'nebula-(storaged|graphd|metad)' >&2 || true
    exit 1
}

nebula_restore_and_start() {
    lc_require ENGINES_DIR LC_BACKUP_DIR LC_NEBULA_DIR NEBULA_CACHE_MB
    local storaged_ratio="${NEBULA_STORAGED_MEM_RATIO:-0.95}"
    local graphd_ratio="${NEBULA_GRAPHD_MEM_RATIO:-0.95}"
    local bin_dir; bin_dir="$(_nebula_bin_dir)"
    [[ -d "$bin_dir" ]] || { echo "ERROR: $bin_dir not found. Run 'build.sh ${LC_BINARY_MODE:-kvstats}' first." >&2; exit 1; }
    [[ -d "$LC_BACKUP_DIR" ]] || { echo "ERROR: backup not found: $LC_BACKUP_DIR" >&2; exit 1; }

    lc_run_hook LC_PRE_RESTORE_CHECK

    echo "Cache size: ${NEBULA_CACHE_MB} MB"
    cd "$ENGINES_DIR/nebulagraph"
    _nebula_stop_and_wait
    rm -rf runs/data/* runs/etc/* runs/pids/* runs/logs/*
    rm -f runs/cluster.id
    cp -rf "$LC_BACKUP_DIR"/data       runs/
    cp -f  "$LC_BACKUP_DIR"/cluster.id runs/
    cp -rf "$LC_BACKUP_DIR"/etc        runs/
    cp -rf "$LC_BACKUP_DIR"/pids       runs/

    # Install the stash's config files (base settings — RocksDB statistics,
    # threading — are baked in; cache size + memory ratio are per-run knobs).
    echo ""
    echo "Configuring Nebula..."
    rm -f runs/etc/nebula-graphd.conf runs/etc/nebula-metad.conf runs/etc/nebula-storaged.conf runs/etc/nebula-storaged-listener.conf || true
    cp "$LC_NEBULA_DIR/configs/nebula-graphd.conf.ldbc"    runs/etc/nebula-graphd.conf
    cp "$LC_NEBULA_DIR/configs/nebula-metad.conf.ldbc"     runs/etc/nebula-metad.conf
    cp "$LC_NEBULA_DIR/configs/nebula-storaged.conf.ldbc"  runs/etc/nebula-storaged.conf
    sed -i "s/--rocksdb_block_cache=[0-9]\+/--rocksdb_block_cache=${NEBULA_CACHE_MB}/" runs/etc/nebula-storaged.conf
    sed -i "s/--memory_tracker_limit_ratio=[0-9.]\+/--memory_tracker_limit_ratio=${storaged_ratio}/" runs/etc/nebula-storaged.conf
    sed -i "s/--memory_tracker_limit_ratio=[0-9.]\+/--memory_tracker_limit_ratio=${graphd_ratio}/" runs/etc/nebula-graphd.conf
    if [[ "${ENGINES_SETTING:-counting}" == equivalent ]]; then
        # one query on one thread: partitions read one after another, operators not split into jobs; no row cache
        sed -i "s/--query_concurrently=[a-z]\+/--query_concurrently=false/" runs/etc/nebula-storaged.conf
        sed -i "s/--max_job_size=[0-9]\+/--max_job_size=1/" runs/etc/nebula-graphd.conf
        echo "--rocksdb_row_cache_num=0" >> runs/etc/nebula-storaged.conf
        # a regime with direct reads (steady): a block-cache miss reads the disk (RocksEngineConfig.cpp sets use_direct_reads)
        [[ "${ENGINES_REGIME_DIRECT_IO:-0}" == 1 ]] && sed -i "s/--disable_page_cache=[a-z]\+/--disable_page_cache=true/" runs/etc/nebula-storaged.conf
        grep -q -- "--query_concurrently=false" runs/etc/nebula-storaged.conf && grep -q -- "--max_job_size=1" runs/etc/nebula-graphd.conf \
            || { echo "ERROR: the equivalent setting's flags did not land in runs/etc/" >&2; exit 1; }
    fi

    local b
    for b in nebula-graphd nebula-metad nebula-storaged; do
        cp "$bin_dir/$b" runs/bin/"$b"
    done
    echo "Stash binaries installed to runs/bin/ (${LC_BINARY_MODE:-kvstats})"

    # a cold start: the restored store alone leaves the OS page cache; any other start drops the whole cache, as ever
    if [[ "${ENGINES_REGIME_COLD_START:-0}" == 1 ]]; then lc_evict_files "$ENGINES_DIR/nebulagraph/runs/data"; else lc_drop_page_cache; fi

    cd runs
    ./scripts/nebula.service restart all || true
    sleep 20
    ./scripts/nebula.service status all
    cd ..
    lc_pin_to_cpu $(pgrep -x nebula-metad) $(pgrep -x nebula-graphd) $(pgrep -x nebula-storaged)

    lc_run_hook LC_POST_START_CHECK
}

nebula_load_and_backup() {
    lc_require ENGINES_DIR SF LC_BACKUP_DIR LC_SCHEMA_FILE LC_NEBULA_DIR

    if [ -d "$LC_BACKUP_DIR" ]; then
        echo "=== Skipping load: backup already exists at $LC_BACKUP_DIR ==="
        return 0
    fi

    local b
    for b in nebula-graphd nebula-metad nebula-storaged; do
        [ -f "$LC_NEBULA_DIR/bin/$b" ] || { echo "ERROR: stashed binary not found: $LC_NEBULA_DIR/bin/$b — run 'bash engines/nebulagraph/build.sh kvstats' first." >&2; exit 1; }
    done
    [ -f "$LC_SCHEMA_FILE" ] || { echo "ERROR: schema not found: $LC_SCHEMA_FILE" >&2; exit 1; }

    cd "$ENGINES_DIR/nebulagraph"
    just stop || true

    echo "Deploying stash binaries to runs/bin/"
    cp "$LC_NEBULA_DIR"/bin/nebula-graphd "$LC_NEBULA_DIR"/bin/nebula-metad "$LC_NEBULA_DIR"/bin/nebula-storaged runs/bin/
    echo "Deploying stash configs to runs/etc/"
    rm -f runs/etc/nebula-graphd.conf runs/etc/nebula-metad.conf runs/etc/nebula-storaged.conf
    cp "$LC_NEBULA_DIR"/configs/nebula-graphd.conf.ldbc runs/etc/nebula-graphd.conf
    cp "$LC_NEBULA_DIR"/configs/nebula-metad.conf.ldbc runs/etc/nebula-metad.conf
    cp "$LC_NEBULA_DIR"/configs/nebula-storaged.conf.ldbc runs/etc/nebula-storaged.conf

    echo "=== Loading LDBC SF${SF} data into NebulaGraph ==="
    just reset
    ulimit -n 65536
    just start
    just status

    source "${ENGINES_DIR}/../.venv/bin/activate"
    bash scripts/ldbc/load.sh --sf "$SF" --schema "$LC_SCHEMA_FILE"

    lc_run_hook LC_POST_LOAD_HOOK

    # Flush before backing up — memtables may not be on disk yet.
    sleep 10
    sync   # plain sync: flush to disk, no root needed
    sleep 2

    echo "=== Stopping NebulaGraph before backup ==="
    _nebula_stop_and_wait
    cd "$ENGINES_DIR/nebulagraph"

    echo "=== Backing up to $LC_BACKUP_DIR ==="
    rm -rf "$LC_BACKUP_DIR"
    mkdir -p "$LC_BACKUP_DIR"
    cp -r runs/data       "$LC_BACKUP_DIR"/
    cp    runs/cluster.id "$LC_BACKUP_DIR"/
    cp -r runs/etc        "$LC_BACKUP_DIR"/
    cp -r runs/pids       "$LC_BACKUP_DIR"/
    cp -r runs/logs       "$LC_BACKUP_DIR"/

    echo "=== Done: nebula backup at $LC_BACKUP_DIR ==="
}
