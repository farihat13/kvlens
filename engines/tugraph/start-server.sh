#!/bin/bash
# engines/tugraph/start-server.sh — start TuGraph on the engine's OWN LDBC backup (engines/tugraph/backup/; the
# configuration ENGINES_DB names: tugraph, or tugraph-detached, each with its backup), with
# our counter library loaded. Uses ports 7070 (REST) and 7072 (the counters) and the tugraph runsdir, so it replaces
# any other TuGraph there. Checks run as lifecycle hooks: the provenance sha gate before the restore, and after the
# start scripts/check_load.py (every label holds the rows of its CSV files; the indexes exist; at a cold start, the
# indexes alone).
# Thin caller — the lifecycle body lives in engines/tugraph/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}$(engine_backup_tag "${ENGINES_DB:-tugraph}")}"
export LC_PRE_RESTORE_CHECK="source '$ENGINE/provenance.sh' && check_provenance ${ENGINES_DB:-tugraph}"
# a cold start: the check counts no rows (a count scans its label, and a timed run must find the store unread)
_no_rows=""; [[ "${ENGINES_REGIME_COLD_START:-0}" == 1 ]] && _no_rows=" --no-rows"
export LC_POST_START_CHECK="'$ENGINES_PYTHON' '$HERE/scripts/check_load.py' --expected '$LC_BACKUP_DIR/expected_rows.tsv' --schema '$HERE/schema/ldbc_import.json'$_no_rows"

source "$HERE/lifecycle.sh"
tugraph_restore_and_start
echo "=== TuGraph up as ${ENGINES_DB:-tugraph} on its backup (sf${SF}). ==="
