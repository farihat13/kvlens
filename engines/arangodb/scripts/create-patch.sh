#!/bin/bash
set -euo pipefail

# Usage: ./create-patch.sh [-f patchfile]

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

patchfile="patches/arangodb.patch"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f)
            if [[ -n "${2:-}" ]]; then
                patchfile="$2"
                shift 2
            else
                echo "Error: -f requires a patch file argument."
                exit 1
            fi
            ;;
        *)
            echo "Usage: $0 [-f patchfile]"
            exit 1
            ;;
    esac
done

echo "Creating patch from ${ARANGO_SRCDIR} changes..."
cd "${MY_ARANGO_ROOTDIR}"
# Include NEW (untracked) files, not just edits to tracked ones. Plain `git diff`
# shows only tracked-file changes, so a patch that ADDS files (e.g. the KV REST
# handlers RocksDBRestKVStatsHandler.*, TimedRocksDBIterator.h) would silently drop
# them and the rebuilt binary would lose /kvstats. Mark untracked files under the
# source dir intent-to-add (--exclude-standard respects .gitignore, so build/ is
# skipped), diff, then unstage so the index is left clean.
git ls-files --others --exclude-standard -- "${ARANGO_SRCDIR}" | xargs -r git add -N --
git diff -- "${ARANGO_SRCDIR}" > "$patchfile"
git reset -q -- "${ARANGO_SRCDIR}" 2>/dev/null || true
echo "Patch saved to: $patchfile ($(wc -l < "$patchfile") lines)"
