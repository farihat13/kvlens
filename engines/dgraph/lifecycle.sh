#!/bin/bash
# engines/dgraph/lifecycle.sh — Dgraph lifecycle: restore+start and load+backup.
#
# Shared by the start and load callers (engines/dgraph/{start-server,load-data}.sh); a caller adds its own
# steps through the hooks in ../lifecycle-common.sh (LC_PRE_RESTORE_CHECK, LC_POST_START_CHECK, ...).
#
# Interface (see ../lifecycle-common.sh for the hooks):
#   LC_BACKUP_DIR    the runs-sf<SF> backup dir — restore source / backup target
#   LC_SCHEMA_FILE   DQL schema for the load (load only)
#   LC_DGRAPH_BIN    the dgraph binary to run (engines/dgraph/bin/dgraph-kvstats, from build.sh)
#   DGRAPH_RUNSDIR, DGRAPH_CACHE_MB, DGRAPH_CACHE_PERCENTAGE, DGRAPH_VERBOSITY   (.env-owned)
#   The 5 knobs (values set in engines/env.sh): DGRAPH_DETSORT,
#   DGRAPH_EQFILTERPATH, DGRAPH_TYPEFILTERPATH, DGRAPH_INEQFILTERPATH, DGRAPH_LOCALCACHE.
#   The fallbacks here (ineq → per_vertex) never fire: env.sh pins all five.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lifecycle-common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
source "$ENGINES_DIR/check-tools.sh"
check_tools dgraph run   # engines/needs.tsv: the server runs in a tmux session; its health checks use curl and jq

# Apply the 5 determinism knobs + pause rollup on a freshly started alpha.
# All endpoints come from dgraph-profile.patch; on a stock binary the request
# 404s and we only warn (knob absent → engine heuristic in effect).
_dgraph_apply_knobs() {
    local http="${DGRAPH_ALPHA_HTTP:-http://localhost:8080}"

    local detsort_mode="${DGRAPH_DETSORT:-enable}"
    case "$detsort_mode" in enable|disable) ;; *) echo "ERROR: DGRAPH_DETSORT must be 'enable' or 'disable', got '$detsort_mode'" >&2; exit 1 ;; esac
    local detsort_http
    detsort_http=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${http}/detsort/${detsort_mode}" || echo "000")
    case "$detsort_http" in
        200) echo "detsort: POST /detsort/${detsort_mode} → 200 OK (deterministic sort ${detsort_mode})" ;;
        404) echo "detsort: POST /detsort/${detsort_mode} → 404 — binary lacks dgraph-profile.patch detsort knob; race behavior in effect" >&2 ;;
        *)   echo "detsort: POST /detsort/${detsort_mode} → HTTP $detsort_http (unexpected; check alpha.log)" >&2 ;;
    esac

    # Filter access-paths: each accepts default|index|per_vertex.
    local eq_mode="${DGRAPH_EQFILTERPATH:-index}"
    local type_mode="${DGRAPH_TYPEFILTERPATH:-index}"
    local ineq_mode="${DGRAPH_INEQFILTERPATH:-per_vertex}"
    local fp_spec fp_knob fp_mode fp_http
    for fp_spec in "eqfilterpath:${eq_mode}" "typefilterpath:${type_mode}" "ineqfilterpath:${ineq_mode}"; do
        IFS=: read -r fp_knob fp_mode <<<"$fp_spec"
        case "$fp_mode" in default|index|per_vertex) ;; *) echo "ERROR: ${fp_knob} mode must be default|index|per_vertex, got '${fp_mode}'" >&2; exit 1 ;; esac
        fp_http=$(curl -s -o /dev/null -w '%{http_code}' -X POST "${http}/${fp_knob}/${fp_mode}" || echo "000")
        case "$fp_http" in
            200) echo "${fp_knob}: POST /${fp_knob}/${fp_mode} → 200 OK" ;;
            404) echo "${fp_knob}: POST /${fp_knob}/${fp_mode} → 404 — binary lacks dgraph-profile.patch knob; engine heuristic in effect" >&2 ;;
            *)   echo "${fp_knob}: POST /${fp_knob}/${fp_mode} → HTTP $fp_http (unexpected; check alpha.log)" >&2 ;;
        esac
    done

    # LocalCache: disable|lock|enable. disable = no caching (deterministic, every
    # read hits Badger). The server-start value IS the localcache state for
    # measurements (measure_kv does not re-pin).
    local localcache_mode="${DGRAPH_LOCALCACHE:-disable}"
    local localcache_http
    localcache_http=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "${http}/localcache/${localcache_mode}" || echo "000")
    case "$localcache_http" in
        200) echo "localcache: PUT /localcache/${localcache_mode} → 200 OK" ;;
        404) echo "localcache: PUT /localcache/${localcache_mode} → 404 — binary lacks dgraph-profile.patch localcache knob" >&2 ;;
        *)   echo "localcache: PUT /localcache/${localcache_mode} → HTTP $localcache_http (unexpected; check alpha.log)" >&2 ;;
    esac

    # Pause posting-list rollup — a rollup running concurrently with a measurement
    # corrupts the global kvstats counters mid-query (observed IC4: GET 32671 with
    # rollup active vs 114650 paused — a 3.5× error). The experiments are read-only
    # over a rolled-up backup, so pausing is safe and keeps counts stable.
    local rollup_http
    rollup_http=$(curl -s -o /dev/null -w '%{http_code}' -X PUT "${http}/rollup/pause" || echo "000")
    case "$rollup_http" in
        200) echo "rollup: PUT /rollup/pause → 200 OK (paused for stable kvstats)" ;;
        404) echo "rollup: PUT /rollup/pause → 404 — binary lacks dgraph-profile.patch rollup control" >&2 ;;
        *)   echo "rollup: PUT /rollup/pause → HTTP $rollup_http (unexpected; check alpha.log)" >&2 ;;
    esac
}

dgraph_restore_and_start() {
    lc_require ENGINES_DIR LC_BACKUP_DIR LC_DGRAPH_BIN DGRAPH_RUNSDIR DGRAPH_CACHE_MB DGRAPH_CACHE_PERCENTAGE
    [[ -x "$LC_DGRAPH_BIN" ]] || { echo "ERROR: $LC_DGRAPH_BIN not found. Run 'bash engines/dgraph/build.sh <mode>' first." >&2; exit 1; }
    [[ -d "$LC_BACKUP_DIR" ]] || { echo "ERROR: backup not found: $LC_BACKUP_DIR" >&2; exit 1; }

    # a regime with direct reads (steady): Badger has none (its tables are always mapped), and a cgroup cap cannot hold
    # its page cache to the block-cache size while the Go heap moves by about 1 GB
    [[ "${ENGINES_REGIME_DIRECT_IO:-0}" == 1 ]] && { echo "ERROR: Dgraph cannot hold ENGINES_REGIME=$ENGINES_REGIME: Badger has no direct reads; its bounded regime is warm-bounded" >&2; exit 1; }

    lc_run_hook LC_PRE_RESTORE_CHECK

    cd "$ENGINES_DIR/dgraph"
    just kill || true

    # WIPE the data dir before restoring. `cp -r backup/. runs/` MERGES over any
    # existing p/w/zw/t — leaving stale SSTs and a Raft WAL / posting-store mismatch.
    # On a merged store, reads still work (badger's MANIFEST ignores stale SSTs) but
    # MUTATIONS silently fail to apply (commit at the Raft layer, never materialize;
    # set_count stays 0). A CLEAN restore (wipe first) of the
    # same backup is fully writable. Wipe first so the restore is always consistent.
    mkdir -p "${DGRAPH_RUNSDIR}"   # gitignored: absent in a fresh clone
    rm -rf "${DGRAPH_RUNSDIR}/p" "${DGRAPH_RUNSDIR}/w" "${DGRAPH_RUNSDIR}/zw" "${DGRAPH_RUNSDIR}/t"
    cp -r "${LC_BACKUP_DIR}/." "${DGRAPH_RUNSDIR}/"

    # a cold start: the restored store alone leaves the OS page cache; any other start drops the whole cache, as ever
    if [[ "${ENGINES_REGIME_COLD_START:-0}" == 1 ]]; then lc_evict_files "${DGRAPH_RUNSDIR}"; else lc_drop_page_cache; fi
    cd "${DGRAPH_RUNSDIR}"
    local go_env=""   # equivalent: one Go thread runs at a time (GOMAXPROCS must be set at launch)
    [[ "${ENGINES_SETTING:-counting}" == equivalent ]] && go_env="GOMAXPROCS=1 "
    tmux new-session -d -s zero "${go_env}$LC_DGRAPH_BIN zero 2>&1 | tee zero.log"
    sleep 5
    tmux new-session -d -s alpha "${go_env}$LC_DGRAPH_BIN alpha --v ${DGRAPH_VERBOSITY:-0} --cache \"size-mb=${DGRAPH_CACHE_MB}; percentage=${DGRAPH_CACHE_PERCENTAGE}\" --badger 'numgoroutines=64' --limit 'mutations-nquad=10000000;' --security whitelist=0.0.0.0/0 2>&1 | tee alpha.log"
    sleep 10
    sleep 10
    lc_pin_to_cpu $(pgrep -f "^$LC_DGRAPH_BIN (zero|alpha)")

    _dgraph_apply_knobs

    lc_run_hook LC_POST_START_CHECK
}

dgraph_load_and_backup() {
    lc_require ENGINES_DIR SF LC_BACKUP_DIR LC_SCHEMA_FILE LC_DGRAPH_BIN DGRAPH_RUNSDIR

    if [ -d "$LC_BACKUP_DIR" ]; then
        echo "=== Skipping load: backup already exists at $LC_BACKUP_DIR ==="
        return 0
    fi
    [ -f "$LC_DGRAPH_BIN" ] || { echo "ERROR: binary not found: $LC_DGRAPH_BIN — run 'bash engines/dgraph/build.sh kvstats' first." >&2; exit 1; }
    [ -f "$LC_SCHEMA_FILE" ] || { echo "ERROR: schema not found: $LC_SCHEMA_FILE" >&2; exit 1; }

    # Put the binary on PATH as 'dgraph' so the repo loader finds it.
    local temp_bin_dir
    temp_bin_dir=$(mktemp -d)
    ln -s "$LC_DGRAPH_BIN" "$temp_bin_dir/dgraph"
    export PATH="$temp_bin_dir:$PATH"

    echo "=== Loading LDBC SF${SF} data into Dgraph ==="
    # Fresh, empty server via the symmetric scripts: reset (wipe) + start with the
    # load-tuned alpha knobs (the knobs live in the dgraph 'alpha' recipe).
    bash "$ENGINES_DIR/dgraph/scripts/reset.sh"
    DGRAPH_ALPHA_NUMGOROUTINES=64 DGRAPH_ALPHA_MUTATIONS_NQUAD=10000000 \
        bash "$ENGINES_DIR/dgraph/scripts/start.sh" 0 8192

    cd "$ENGINES_DIR/dgraph"
    bash scripts/ldbc/load.sh --sf "$SF" --schema "$LC_SCHEMA_FILE"

    lc_run_hook LC_POST_LOAD_HOOK

    echo "=== Force rollup ==="
    curl -X POST localhost:8080/rollup/forceall | jq || true
    sleep 2
    sync   # plain sync: flush to disk, no root needed
    sleep 2

    echo "=== Stopping Dgraph ==="
    pkill -f 'dgraph.* (zero|alpha)' || true
    sleep 2
    pkill -9 -f 'dgraph.* (zero|alpha)' || true
    tmux kill-session -t zero 2>/dev/null || true
    tmux kill-session -t alpha 2>/dev/null || true
    sleep 1

    echo "=== Backing up to $LC_BACKUP_DIR ==="
    mkdir -p "$LC_BACKUP_DIR"
    cp -r "${DGRAPH_RUNSDIR}"/* "$LC_BACKUP_DIR"/

    rm -rf "$temp_bin_dir"
    echo "=== Done: dgraph backup at $LC_BACKUP_DIR ==="
}
