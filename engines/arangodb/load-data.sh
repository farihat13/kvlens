#!/bin/bash
# engines/arangodb/load-data.sh — load LDBC with the engine's schema (schema/ldbc_schema.json) and back it up.
# Arango loads by importing into a RUNNING server, so this resets to an empty arangod (the kvstats binary from
# engines/arangodb/build.sh, installed in the runsdir), loads the schema, flushes, and backs up to
# engines/arangodb/backup/runs-sf<SF>.
# Thin caller — the lifecycle body lives in engines/arangodb/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}}"
export LC_SCHEMA_FILE="${LC_SCHEMA_FILE:-$HERE/schema/ldbc_schema.json}"

source "$HERE/lifecycle.sh"
arangodb_load_and_backup
