#!/bin/bash
# engines/dgraph/load-data.sh — load LDBC with the engine's schema (schema/ldbcTypes.schema, creationDate as ONE
# @index(day) predicate) and back it up to engines/dgraph/backup/runs-sf<SF>, with bin/dgraph-kvstats.
# Thin caller — the lifecycle body lives in engines/dgraph/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_DGRAPH_BIN="$HERE/bin/dgraph-kvstats"
export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}}"
export LC_SCHEMA_FILE="${LC_SCHEMA_FILE:-$HERE/schema/ldbcTypes.schema}"

source "$HERE/lifecycle.sh"
dgraph_load_and_backup
