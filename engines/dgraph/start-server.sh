#!/bin/bash
# engines/dgraph/start-server.sh — start Dgraph on the engine's OWN LDBC backup (engines/dgraph/backup/), with
# the kvstats binary from engines/dgraph/build.sh (bin/dgraph-$MODE, default kvstats). The 5 dgraph knobs come from
# engines/env.sh (ineq=index — the fully-indexed design). Uses port 8080 and DGRAPH_RUNSDIR, so it replaces any
# other Dgraph there. Checks run as lifecycle hooks: the provenance sha gate before the restore,
# python/kvengines/verify_schema.py after the start.
# Thin caller — the lifecycle body lives in engines/dgraph/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_DGRAPH_BIN="$HERE/bin/dgraph-${MODE:-kvstats}"
export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}}"
export LC_PRE_RESTORE_CHECK="source '$ENGINE/provenance.sh' && check_provenance dgraph"
export LC_POST_START_CHECK="'$ENGINES_PYTHON' -m kvengines.verify_schema --db dgraph"

source "$HERE/lifecycle.sh"
dgraph_restore_and_start
