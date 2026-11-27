#!/bin/bash
# engines/tugraph/lifecycle.sh — TuGraph lifecycle: load+backup and restore+start.
#
# Shared by the load and start callers (engines/tugraph/{load-data,start-server}.sh). The server is TuGraph's own
# release (get.sh), unchanged; our counter library (build.sh) is loaded before it and counts its calls into LMDB.
#
# Interface (see ../lifecycle-common.sh for the hooks):
#   LC_BACKUP_DIR    the runs-sf<SF> backup dir; the loaded LMDB data dir is $LC_BACKUP_DIR/data
#   LC_SCHEMA_FILE   the physical design lgraph_import loads (schema/ldbc_import.json; load only)
#   ENGINES_DB       which configuration: tugraph (default) or tugraph-detached; engines/load-data.sh and
#                    engines/start-server.sh set it from their --db. tugraph_configuration below is the one place
#                    a configuration's settings are written; each configuration has its own backup.
#   TUGRAPH_HOME, TUGRAPH_BINARYDIR, TUGRAPH_RUNSDIR, TUGRAPH_PORT, TUGRAPH_KVSTATS_PORT   (.env-owned)
#
# Settings: LMDB has no block cache and TuGraph keeps no data cache above it, so there is no cache to size or to
# switch off; the server maps the store's file, so its pages are the OS page cache's. ENGINES_SETTING=equivalent pins
# the server to one CPU. A regime changes nothing here but two things: one with direct reads (steady) is refused (LMDB
# maps its file), and at a cold start (ENGINES_REGIME_COLD_START) the restored store is put out of the OS page cache
# and the start's check reads no rows (start-server.sh). At any other regime nothing is dropped here: its runs are
# warmed up.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lifecycle-common.sh"
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
source "$ENGINES_DIR/check-tools.sh"
check_tools tugraph run   # engines/needs.tsv: the server runs in a tmux session; its health checks use curl

_tugraph_lib_path="$TUGRAPH_HOME/lib64/lgraph"

# TuGraph's configurations, by the db key engines/*.sh take: the settings each is loaded with.
#   tugraph           detach_property false: a vertex's properties sit in its packed record, with its edges while the
#                     record is under 1,000 bytes (src/core/graph_data_pack.h)
#   tugraph-detached  detach_property true: the properties are in a table of their own, each read by a GET
#                     (lgraph_import's own default at v4.5.2; its documentation says false)
tugraph_configuration() {
    case "${1:-tugraph}" in
        tugraph)          TUGRAPH_DETACH_PROPERTY=false ;;
        tugraph-detached) TUGRAPH_DETACH_PROPERTY=true ;;
        *) echo "ERROR: unknown TuGraph configuration '$1' (tugraph | tugraph-detached)" >&2; exit 1 ;;
    esac
}
tugraph_configuration "${ENGINES_DB:-tugraph}"

tugraph_load_and_backup() {
    lc_require ENGINES_DIR SF TUGRAPH_HOME TUGRAPH_RUNSDIR LC_BACKUP_DIR LC_SCHEMA_FILE LDBC_SNB_DATA_DIR
    local detach="$TUGRAPH_DETACH_PROPERTY"
    if [ -d "$LC_BACKUP_DIR/data" ]; then
        echo "=== Skipping load: backup already exists at $LC_BACKUP_DIR ==="
        return 0
    fi
    [ -f "$LC_SCHEMA_FILE" ] || { echo "ERROR: schema not found: $LC_SCHEMA_FILE" >&2; exit 1; }
    [ -x "$TUGRAPH_HOME/bin/lgraph_import" ] || { echo "ERROR: no lgraph_import; run engines/tugraph/get.sh" >&2; exit 1; }
    local csv="$LDBC_SNB_DATA_DIR/sf${SF}/CsvBasic-LongDateFormatter" work="$TUGRAPH_RUNSDIR/load-sf${SF}$(engine_backup_tag "${ENGINES_DB:-tugraph}")"
    [ -d "$csv/dynamic" ] || { echo "ERROR: no LDBC CsvBasic data at $csv" >&2; exit 1; }
    mkdir -p "$work"

    echo "=== Preparing the import (person's emails and languages joined; detach_property=$detach) ==="
    "$ENGINES_PYTHON" "$TUGRAPH_ROOTDIR/scripts/prepare_import.py" --csv "$csv" --schema "$LC_SCHEMA_FILE" \
        --out "$work" --detach-property "$detach" > "$work/expected_rows.tsv"

    echo "=== lgraph_import (offline; log: $work/import.log) ==="
    local t0=$SECONDS
    (cd "$work" && LD_LIBRARY_PATH="$_tugraph_lib_path" "$TUGRAPH_HOME/bin/lgraph_import" -c "$work/import.json" \
        -d "$work/data" --overwrite true --delimiter '|' > "$work/import.log" 2>&1) \
        || { tail -5 "$work/import.log" >&2; echo "ERROR: lgraph_import failed" >&2; exit 1; }
    grep -q "Import finished" "$work/import.log" || { tail -5 "$work/import.log" >&2; echo "ERROR: the import did not finish" >&2; exit 1; }
    echo "    imported in $((SECONDS - t0)) s: $(du -sh "$work/data" | cut -f1)"

    lc_run_hook LC_POST_LOAD_HOOK

    mkdir -p "$LC_BACKUP_DIR"
    cp -r "$work/data" "$LC_BACKUP_DIR/data"
    cp "$work/expected_rows.tsv" "$work/import.log" "$LC_BACKUP_DIR/"
    echo "$detach" > "$LC_BACKUP_DIR/detach_property"
    echo "=== Done: TuGraph backup at $LC_BACKUP_DIR ==="
}

tugraph_restore_and_start() {
    lc_require ENGINES_DIR TUGRAPH_HOME TUGRAPH_BINARYDIR TUGRAPH_RUNSDIR TUGRAPH_PORT TUGRAPH_KVSTATS_PORT LC_BACKUP_DIR
    local lib="$TUGRAPH_BINARYDIR/libtugraph_kvstats.so"
    [ -d "$LC_BACKUP_DIR/data" ] || { echo "ERROR: backup not found at $LC_BACKUP_DIR/data" >&2; exit 1; }
    [ "$(cat "$LC_BACKUP_DIR/detach_property")" = "$TUGRAPH_DETACH_PROPERTY" ] \
        || { echo "ERROR: $LC_BACKUP_DIR was not loaded as ${ENGINES_DB:-tugraph} (its detach_property: $(cat "$LC_BACKUP_DIR/detach_property"))" >&2; exit 1; }
    [ -f "$lib" ] || { echo "ERROR: no counter library at $lib; run engines/tugraph/build.sh" >&2; exit 1; }

    [[ "${ENGINES_REGIME_DIRECT_IO:-0}" == 1 ]] && { echo "ERROR: TuGraph cannot hold ENGINES_REGIME=$ENGINES_REGIME: LMDB maps its file and has no direct reads" >&2; exit 1; }

    lc_run_hook LC_PRE_RESTORE_CHECK

    echo "=== Stopping TuGraph, restoring the backup ($LC_BACKUP_DIR) ==="
    bash "$TUGRAPH_ROOTDIR/scripts/stop.sh"
    mkdir -p "$TUGRAPH_RUNSDIR/data" "$TUGRAPH_RUNSDIR/log"
    rsync -a --delete "$LC_BACKUP_DIR/data/" "$TUGRAPH_RUNSDIR/data/"     # the run dir becomes the backup, exactly
    # a cold start: the restored store leaves the OS page cache before the launch (after it, nothing reaches the
    # pages the server has mapped)
    [[ "${ENGINES_REGIME_COLD_START:-0}" == 1 ]] && lc_evict_files "$TUGRAPH_RUNSDIR/data"

    cat > "$TUGRAPH_RUNSDIR/lgraph.json" <<JSON
{"directory": "$TUGRAPH_RUNSDIR/data", "host": "127.0.0.1", "port": $TUGRAPH_PORT, "enable_rpc": false,
 "enable_ha": false, "verbose": 1, "log_dir": "$TUGRAPH_RUNSDIR/log", "disable_auth": false, "ssl_auth": false,
 "durable": false, "web": "$TUGRAPH_HOME/share/lgraph/browser-resource"}
JSON
    echo "=== Starting TuGraph ${TUGRAPH_VERSION} as ${ENGINES_DB:-tugraph} (REST $TUGRAPH_PORT, counters $TUGRAPH_KVSTATS_PORT; detach_property=$TUGRAPH_DETACH_PROPERTY) ==="
    # exec: the tmux pane's process is the server itself, so its pid is the pane's
    tmux new-session -d -s tugraph-server "exec env LD_PRELOAD='$lib' TUGRAPH_KVSTATS_PORT=$TUGRAPH_KVSTATS_PORT \
LD_LIBRARY_PATH='$_tugraph_lib_path' '$TUGRAPH_HOME/bin/lgraph_server' -c '$TUGRAPH_RUNSDIR/lgraph.json' -d run \
> '$TUGRAPH_RUNSDIR/lgraph_server.log' 2>&1"
    local pid up=0
    pid="$(tmux display-message -p -t "=tugraph-server:" '#{pane_pid}')"
    echo "$pid" > "$TUGRAPH_RUNSDIR/lgraph_server.pid"
    for _ in $(seq 1 120); do
        if curl -sf -m 2 -o /dev/null -X POST "http://127.0.0.1:${TUGRAPH_PORT}/login" -H 'Content-Type: application/json' \
            -H 'Accept: application/json' -d "{\"user\":\"$TUGRAPH_USER\",\"password\":\"$TUGRAPH_PASSWORD\"}"; then up=1; break; fi
        [ -d "/proc/$pid" ] || break
        sleep 0.5
    done
    [ "$up" = 1 ] || { tail -5 "$TUGRAPH_RUNSDIR/lgraph_server.log" >&2; echo "ERROR: TuGraph did not come up on $TUGRAPH_PORT" >&2; exit 1; }
    curl -sf -m 3 -o /dev/null "http://127.0.0.1:${TUGRAPH_KVSTATS_PORT}/kvstats" \
        || { echo "ERROR: the counters do not answer on $TUGRAPH_KVSTATS_PORT (is the library loaded?)" >&2; exit 1; }
    lc_pin_to_cpu "$pid"

    lc_run_hook LC_POST_START_CHECK
}
