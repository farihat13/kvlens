#!/bin/bash
set -euo pipefail

# Usage: ./apply-patch.sh [-f patchfile]

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

if [[ ! -f "$patchfile" ]]; then
    echo "ERROR: Patch file not found: $patchfile"
    exit 1
fi

echo "Restoring source to clean state..."
# restore resets tracked files; clean removes the files an earlier patch created (a failed build leaves them, and a
# second patch would then stop on them). build/ (build.sh's build dir; a rebuild is incremental, ~11 min) is kept.
git -C "${ARANGO_SRCDIR}" restore . || { echo "ERROR: git restore ${ARANGO_SRCDIR} failed"; exit 1; }
git -C "${ARANGO_SRCDIR}" clean -fdq -e /build/ || { echo "ERROR: git clean ${ARANGO_SRCDIR} failed"; exit 1; }

echo "Applying patch: $patchfile"
# Patches are produced by create-patch.sh via `git diff`, whose paths are
# repo-root-relative (a/arangodb/arangodb-3.12.8/...). Apply from the repo root
# (parent of arangodb/) so `patch -p1` resolves them. Applying from arangodb/
# would leave an unstrippable `arangodb/` prefix and create a nested arangodb/arangodb/.
# --forward --batch: never prompt; a patch that looks already applied fails (--batch alone would silently reverse it).
patch --forward --batch -d "$(dirname "${MY_ARANGO_ROOTDIR}")" -p1 < "$patchfile"
echo "Patch applied successfully"
