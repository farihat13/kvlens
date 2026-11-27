#!/bin/bash
# engines/arangodb/start-server.sh — start ArangoDB on the engine's OWN LDBC backup (engines/arangodb/backup/).
# It keeps the Post/Comment.creationDate indexes: the engine measures the fully-indexed LDBC design. The binary
# is the kvstats arangod from engines/arangodb/build.sh, installed in the runsdir. Uses port 8529 and the arango
# runsdir, so it replaces any other ArangoDB there. Checks run as lifecycle hooks: the provenance sha gate before
# the restore, python/kvengines/verify_schema.py after the start.
# Thin caller — the lifecycle body lives in engines/arangodb/lifecycle.sh. Sources engines/env.sh itself.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${SF:?Set SF}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd "$HERE/.." && pwd)"

export LC_BACKUP_DIR="${LC_BACKUP_DIR:-$HERE/backup/runs-sf${SF}}"
export LC_PRE_RESTORE_CHECK="source '$ENGINE/provenance.sh' && check_provenance arangodb"
export LC_POST_START_CHECK="'$ENGINES_PYTHON' -m kvengines.verify_schema --db arangodb"

source "$HERE/lifecycle.sh"
arangodb_restore_and_start
echo "=== ArangoDB up on the engine backup (sf${SF}); canonical indexes kept. ==="
