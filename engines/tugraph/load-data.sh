#!/bin/bash
# engines/tugraph/load-data.sh — load LDBC with the engine's design (schema/ldbc_import.json) and back it up.
# TuGraph loads offline: lgraph_import writes a fresh LMDB data dir from the CSV files (no server), and this copies
# it to engines/tugraph/backup/runs-sf<SF> (the configuration ENGINES_DB names: tugraph, or tugraph-detached, whose
# backup is runs-sf<SF>-detached; lifecycle.sh's tugraph_configuration holds their settings).
# Thin caller — the lifecycle body lives in engines/tugraph/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}$(engine_backup_tag "${ENGINES_DB:-tugraph}")}"
export LC_SCHEMA_FILE="${LC_SCHEMA_FILE:-$HERE/schema/ldbc_import.json}"

source "$HERE/lifecycle.sh"
tugraph_load_and_backup
