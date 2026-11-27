#!/bin/bash
# Apply the profile patches and build Dgraph with kvstats or histogram support.
#
# Patches (what each adds: patches/provenance.txt): dgraph-profile.patch, badger-profile.patch,
# dgraph-use-local-badger.patch.
#
# Usage:
#   bash build.sh           # default: kvstats mode (lightweight atomic counters)
#   bash build.sh kvstats   # same as above
#   bash build.sh histogram # HdrHistogram recording, /debug/vars export
#
# Binaries are stashed separately: bin/dgraph-kvstats or bin/dgraph-histogram.
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR

MODE="${1:-kvstats}"
case "$MODE" in
    kvstats)   BUILD_TAGS="kvstats"   ;;
    histogram) BUILD_TAGS="histogram" ;;
    *)
        echo "ERROR: unknown mode '$MODE' (expected: kvstats, histogram)" >&2
        exit 1
        ;;
esac

EXP_DGRAPH_DIR="${ENGINES_DIR}/dgraph"
PATCH_DIR="$EXP_DGRAPH_DIR/patches"

# Verify patches exist
for p in dgraph-use-local-badger.patch dgraph-profile.patch badger-profile.patch; do
    if [ ! -f "$PATCH_DIR/$p" ]; then
        echo "ERROR: $PATCH_DIR/$p not found" >&2
        exit 1
    fi
done

# Skip if binary already built
if [[ -f "$EXP_DGRAPH_DIR/bin/dgraph-$MODE" ]]; then
    echo "=== Skipping build ($MODE): $EXP_DGRAPH_DIR/bin/dgraph-$MODE already exists ==="
    exit 0
fi

# the tools make install calls (engines/needs.tsv: go, and what the jemalloc it builds first needs)
source "$ENGINES_DIR/check-tools.sh"
check_tools dgraph build

cd "$ENGINES_DIR/dgraph"

echo "=== Applying patches ==="
# apply-patch.sh restores and cleans each source first, so files a failed build left behind are gone
bash scripts/apply-patch.sh dgraph -f "$PATCH_DIR/dgraph-use-local-badger.patch"
patch --forward --batch -p1 < "$PATCH_DIR/dgraph-profile.patch"                 # on top: needs the go.mod line
bash scripts/apply-patch.sh badger -f "$PATCH_DIR/badger-profile.patch"

echo "=== Building Dgraph (mode=$MODE, BUILD_TAGS=$BUILD_TAGS) ==="
cd "${DGRAPH_SRCDIR:-dgraph-25.0.0}"
make install BUILD_CODENAME=dgraph-$MODE BUILD_TAGS=$BUILD_TAGS |& tee build.log

echo "=== Stashing binary ==="
mkdir -p "$EXP_DGRAPH_DIR/bin"
cp "$GOPATH/bin/dgraph" "$EXP_DGRAPH_DIR/bin/dgraph-$MODE"
echo "Binary saved to $EXP_DGRAPH_DIR/bin/dgraph-$MODE"

echo "=== Restoring source tree ==="
cd "$ENGINES_DIR/dgraph"
git -C dgraph-25.0.0 restore . && git -C dgraph-25.0.0 clean -fd
git -C badger restore . && git -C badger clean -fd

echo "=== Done ($MODE) ==="
