#!/bin/bash
# engines/nebulagraph/load-data.sh — load LDBC with the engine's schema (schema/schema.gql, which keeps the
# post/comment creationDate indexes) and back it up to engines/nebulagraph/backup/runs-sf<SF>, with the binaries
# from engines/nebulagraph/build.sh.
# Thin caller — the lifecycle body lives in engines/nebulagraph/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_NEBULA_DIR="$HERE"
export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}}"
export LC_SCHEMA_FILE="${LC_SCHEMA_FILE:-$HERE/schema/schema.gql}"

source "$HERE/lifecycle.sh"
nebula_load_and_backup
