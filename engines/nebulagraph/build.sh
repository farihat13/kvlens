#!/bin/bash
# Apply patches and build NebulaGraph for this experiment.
#
# Usage:
#   bash build.sh             # default: kvstats mode
#   bash build.sh kvstats     # same as above
#   bash build.sh histogram   # kvstats + histogram percentiles
#   bash build.sh plan-debug  # storage plan debugging (ENABLE_QUERY_PLAN_DEBUG=ON)
#
# Modes:
#   kvstats    — nebula.unified.patch + ENABLE_KV_STATS=ON → bin/
#   histogram  — nebula.unified.patch + ENABLE_KV_STATS=ON ENABLE_KV_HISTOGRAMS=ON → bin-histogram/
#   plan-debug — nebula.unified.patch + nebula.plan-debug.patch
#                + ENABLE_QUERY_PLAN_DEBUG=ON → bin-plan-debug/
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR

MODE="${1:-kvstats}"
PATCH_DIR="$ENGINES_DIR/nebulagraph/patches"

case "$MODE" in
    kvstats)
        CMAKE_FLAGS="ENABLE_KV_STATS=ON"
        BIN_DIR="$ENGINES_DIR/nebulagraph/bin"
        PATCHES=("$PATCH_DIR/nebula.unified.patch")
        ;;
    histogram)
        CMAKE_FLAGS="ENABLE_KV_STATS=ON ENABLE_KV_HISTOGRAMS=ON"
        BIN_DIR="$ENGINES_DIR/nebulagraph/bin-histogram"
        PATCHES=("$PATCH_DIR/nebula.unified.patch")
        ;;
    plan-debug)
        CMAKE_FLAGS="ENABLE_QUERY_PLAN_DEBUG=ON"
        BIN_DIR="$ENGINES_DIR/nebulagraph/bin-plan-debug"
        PATCHES=(
            "$PATCH_DIR/nebula.unified.patch"
            "$PATCH_DIR/nebula.plan-debug.patch"
        )
        ;;
    *)
        echo "ERROR: unknown mode '$MODE' (expected: kvstats, histogram, plan-debug)" >&2
        exit 1
        ;;
esac

# Verify patches exist
for p in "${PATCHES[@]}"; do
    if [ ! -f "$p" ]; then
        echo "ERROR: $p not found" >&2
        exit 1
    fi
done

# Skip if all binaries already built
ALL_PRESENT=true
for b in nebula-graphd nebula-metad nebula-storaged; do
    [[ -f "$BIN_DIR/$b" ]] || ALL_PRESENT=false
done
if $ALL_PRESENT; then
    echo "=== Skipping build ($MODE): binaries already in $BIN_DIR ==="
    exit 0
fi

# NebulaGraph builds inside its dev image (scripts/start-container.sh runs it with sudo docker); just applies the patches.
# engines/needs.tsv: docker, its daemon answering, sudo, just, git, patch
source "$ENGINES_DIR/check-tools.sh"
check_tools nebulagraph build

cd "$ENGINES_DIR/nebulagraph"

echo "=== Applying patches (mode=$MODE) ==="
for p in "${PATCHES[@]}"; do
    just patch nebula -f "$p"
done

echo "=== Building NebulaGraph (mode=$MODE, $CMAKE_FLAGS) ==="
for flag in $CMAKE_FLAGS; do
    export "$flag"
done
# Call build script directly (not via tmux) so env vars propagate.
# just build uses tmux which drops exported env vars.
bash scripts/build.sh

echo "=== Installing binaries ==="
just stop
just install

echo "=== Stashing binaries ==="
mkdir -p "$BIN_DIR"
for b in nebula-graphd nebula-metad nebula-storaged; do
    cp "$ENGINES_DIR/nebulagraph/runs/bin/$b" "$BIN_DIR/$b"
done
echo "Binaries saved to $BIN_DIR"

echo "=== Restoring source tree ==="
cd "$ENGINES_DIR/nebulagraph"
git -C nebula restore . && git -C nebula clean -fd

echo "=== Done ($MODE) ==="
