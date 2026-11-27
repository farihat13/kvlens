#!/bin/bash
# engines/nebulagraph/start-server.sh — start NebulaGraph on the engine's OWN LDBC backup
# (engines/nebulagraph/backup/), with the binaries from engines/nebulagraph/build.sh and the configs in configs/.
# Uses NebulaGraph's ports, so it replaces any other NebulaGraph there. Checks run as lifecycle hooks: the
# provenance sha gate before the restore, python/kvengines/verify_schema.py against the live store after the start.
# Thin caller — the lifecycle body lives in engines/nebulagraph/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_NEBULA_DIR="$HERE"
export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}}"
export LC_BINARY_MODE="${NEBULA_MODE:-kvstats}"
export LC_PRE_RESTORE_CHECK="source '$ENGINE/provenance.sh' && check_provenance nebula"
export LC_POST_START_CHECK="'$ENGINES_PYTHON' -m kvengines.verify_schema --db nebula"

source "$HERE/lifecycle.sh"
nebula_restore_and_start
