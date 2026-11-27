#!/bin/bash
set -euo pipefail

# Build ArangoDB with the unified instrumentation patch, in one of three modes —
# mirroring NebulaGraph's build.sh variants.
# Flag names are aligned by layer, so "kvstats" means the same engine-wrapper counter
# on both DBs.
#
# Usage:
#   bash build.sh            # default: kvstats mode
#   bash build.sh kvstats    # same as above
#   bash build.sh histogram  # kvstats + engine latency histograms
#   bash build.sh tickers    # kvstats + RocksDB native tickers (process-global cross-check)
#
# Modes (binary stashed in a per-mode dir, like Nebula's bin/ vs bin-histogram/):
#   kvstats    ARANGO_KV_STATS=ON                          -> bin/         (the measured binary)
#              engine-wrapper hand counters: get/seek/next/prev count + found +
#              time_us + bytes. THE query-scoped KV-op counter (= NebulaGraph KV_STATS).
#              Needs no runtime stats flag.
#   histogram  ARANGO_KV_STATS=ON ARANGO_KV_HISTOGRAMS=ON  -> bin-histogram/
#              + engine get/seek/next/prev latency histograms (= NebulaGraph histogram variant).
#   tickers    ARANGO_KV_TICKERS=ON ARANGO_KV_STATS=ON     -> bin-tickers/
#              + RocksDB native tickers/histograms (process-global; the layer NebulaGraph
#              lacks). For A/B cross-checks; the server then needs --rocksdb.enable-statistics true.
#
# Flags are passed explicitly (including =OFF) because cmake caches them in the build
# dir across builds, so an earlier mode's flags would otherwise leak in.

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../env.sh"   # always this tree's ENGINES_DIR
: "${MY_ARANGO_ROOTDIR:?}"

MODE="${1:-kvstats}"
case "$MODE" in
    kvstats)
        CMAKE_FLAGS="-DARANGO_KV_STATS=ON -DARANGO_KV_TICKERS=OFF -DARANGO_KV_HISTOGRAMS=OFF"
        BIN_DIR="$ENGINES_DIR/arangodb/bin" ;;
    histogram)
        CMAKE_FLAGS="-DARANGO_KV_STATS=ON -DARANGO_KV_HISTOGRAMS=ON -DARANGO_KV_TICKERS=OFF"
        BIN_DIR="$ENGINES_DIR/arangodb/bin-histogram" ;;
    tickers)
        CMAKE_FLAGS="-DARANGO_KV_TICKERS=ON -DARANGO_KV_STATS=ON -DARANGO_KV_HISTOGRAMS=OFF"
        BIN_DIR="$ENGINES_DIR/arangodb/bin-tickers" ;;
    *)
        echo "ERROR: unknown mode '$MODE' (expected: kvstats, histogram, tickers)" >&2
        exit 1 ;;
esac

PATCH_FILE="$ENGINES_DIR/arangodb/patches/arangodb.unified.patch"

# Skip if binary already built
if [[ -f "$BIN_DIR/arangod" ]]; then
    echo "=== Skipping build: $BIN_DIR/arangod already exists ==="
    exit 0
fi

if [ ! -f "$PATCH_FILE" ]; then
    echo "ERROR: Patch not found: $PATCH_FILE"
    exit 1
fi

# scripts/build.sh configures with clang-17 (its archiver llvm-ar-17) and links the system OpenSSL; ICU 64's configure
# runs python3 ($PYTHON when set). engines/needs.tsv holds each need and its check
source "$ENGINES_DIR/check-tools.sh"
check_tools arangodb build

echo "=== Applying arangodb.unified.patch ==="
cd "$MY_ARANGO_ROOTDIR"
bash scripts/apply-patch.sh -f "$PATCH_FILE"                                      # restores the source first
COMMUNITY_PATCH="$ENGINES_DIR/arangodb/patches/arangodb-community-build.patch"   # on top: lets the community edition build
patch --forward --batch -d "$(dirname "$MY_ARANGO_ROOTDIR")" -p1 < "$COMMUNITY_PATCH"

echo ""
echo "=== Building ArangoDB (mode=$MODE: $CMAKE_FLAGS) ==="
# Call the build script directly (blocking) rather than `just build`, which detaches
# into tmux and returns immediately — `just install` below would then copy a stale
# binary. scripts/build.sh threads ARANGO_CMAKE_FLAGS into its cmake invocation.
export ARANGO_CMAKE_FLAGS="$CMAKE_FLAGS"
bash scripts/build.sh

echo ""
echo "=== Installing ==="
just install

echo ""
echo "=== Stashing binary ==="
mkdir -p "$BIN_DIR"
# `command cp` bypasses any `cp -i` alias that would prompt and silently skip.
command cp -f "$ENGINES_DIR/arangodb/runs/bin/arangod" "$BIN_DIR/arangod"
echo "Binary saved to $BIN_DIR/arangod"

echo ""
echo "=== Restoring source tree ==="
cd "$ENGINES_DIR/arangodb"
git -C arangodb-3.12.8 restore . && git -C arangodb-3.12.8 clean -fd -e /build/   # keep the incremental build

echo ""
echo "=== Done (mode=$MODE) ==="
