#!/bin/bash
# engines/lifecycle-common.sh — shared primitives for the per-DB lifecycle helpers.
#
# Neutral infra: no experiment owns this. Sourced by engines/<db>/lifecycle.sh,
# which are in turn sourced by each experiment's thin setup/<db>/{start-server,load-data}.sh caller.
# Callers run under `set -euo pipefail`; these helpers assume it.
#
# Hooks — the extension mechanism that keeps experiment-specific logic OUT of the shared
# helpers (a caller sets the var to a command string; unset = no-op):
#   LC_PRE_RESTORE_CHECK  run before a restore   (e.g. the engine's provenance sha-gate)
#   LC_POST_START_CHECK   run after a start      (e.g. the engine's python/kvengines/verify_schema.py)
#   LC_POST_LOAD_HOOK     run after a load, before flush/backup
#                         (e.g. microbench dgraph's creationDate split)
# A failing hook aborts the script — a guardrail that fails must stop the bring-up.

lc_require() {
    local v
    for v in "$@"; do
        [[ -n "${!v:-}" ]] || { echo "ERROR: $v must be set (source engines/env.sh, or export it)" >&2; exit 1; }
    done
}

# Drop the OS page cache so a measurement starts cold. Writing /proc/sys/vm/drop_caches
# needs root: passwordless sudo for `tee /proc/sys/vm/drop_caches` (or for all commands).
# A machine may instead allow only a fixed root-owned wrapper at /usr/local/sbin/gsbench-coldcache
# (`sync; echo 3 > /proc/sys/vm/drop_caches`; not shipped here); it is used when present.
lc_drop_page_cache() {
    if [[ -x /usr/local/sbin/gsbench-coldcache ]]; then
        sudo /usr/local/sbin/gsbench-coldcache
    else
        sync
        echo 3 | sudo -n tee /proc/sys/vm/drop_caches >/dev/null || {
            echo "lc_drop_page_cache: cannot drop the page cache: needs passwordless sudo for 'tee /proc/sys/vm/drop_caches'" >&2
            return 1; }
    fi
}

# Put the files under a dir out of the OS page cache, each written back first; the rest of the machine's cache is not
# touched and no root is needed (dd's nocache with no data: an fdatasync and a POSIX_FADV_DONTNEED over the whole
# file). A cold start's step between its restore and its launch (ENGINES_REGIME_COLD_START): while a server runs, the
# pages it has mapped stay cached whatever is dropped.
lc_evict_files() {
    [[ -d "$1" ]] || { echo "ERROR: lc_evict_files: no dir $1" >&2; exit 1; }
    find "$1" -type f -size +0 -exec dd of={} oflag=nocache conv=notrunc,fdatasync count=0 status=none \;
}

# ENGINES_SETTING=equivalent: pin every thread of the given processes to ENGINES_CPU and print each one's CPUs. A
# taskset around the launcher does not reach an engine started inside tmux, so the pin comes after the start; threads
# made later inherit it.
lc_pin_to_cpu() {
    [[ "${ENGINES_SETTING:-counting}" == equivalent ]] || return 0
    lc_require ENGINES_CPU
    [[ $# -gt 0 ]] || { echo "ERROR: lc_pin_to_cpu: no process to pin" >&2; exit 1; }
    local p
    for p in "$@"; do
        taskset -a -cp "$ENGINES_CPU" "$p" >/dev/null
        echo "pinned: $(cat /proc/$p/comm) (pid $p) on CPU $(grep Cpus_allowed_list /proc/$p/status | cut -f2)"
    done
}

# Run the hook whose variable NAME is $1, if set. eval'd so a hook can be
# "source x.sh && fn arg" — hooks are set by our own callers, not user input.
lc_run_hook() {
    local name="$1" cmd="${!1:-}"
    [[ -z "$cmd" ]] && return 0
    echo "== [hook $name] $cmd"
    eval "$cmd" || { echo "ERROR: hook $name failed" >&2; exit 1; }
}
