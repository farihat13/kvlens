#!/bin/bash
# ============================================================
# Build ArangoDB from source
# ============================================================
# Usage: bash scripts/build.sh
#
# Builds ArangoDB in Release mode. First build compiles all
# third-party dependencies (V8, ICU, RocksDB, etc.) and takes
# significantly longer than subsequent rebuilds.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

SRCDIR="${ARANGO_SRCDIR}"
BUILD_DIR="${SRCDIR}/build"

if [ ! -d "${SRCDIR}" ]; then
    echo "ERROR: Source directory not found: ${SRCDIR}"
    echo "Clone with: git clone --recurse-submodules --branch v3.12.8 https://github.com/arangodb/arangodb.git ${SRCDIR}"
    exit 1
fi

echo "=== Building ArangoDB from ${SRCDIR} ==="
echo "Build directory: ${BUILD_DIR}"

mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

START_TIME=$(date +%s)

# Configure with CMake
echo "=== CMake configure ==="
# Extra cmake flags for instrumented builds (e.g. the arangodb.unified.patch
# feature flags: -DARANGO_KV_TICKERS=ON -DARANGO_KV_STATS=ON -DARANGO_KV_HISTOGRAMS=ON).
# Set ARANGO_CMAKE_FLAGS in the environment; empty for a plain build.
read -r -a _extra_cmake_flags <<< "${ARANGO_CMAKE_FLAGS:-}"

# USE_GOOGLE_TESTS defaults ON; triggers "maintainer version" startup warning
cmake \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=clang-17 \
    -DCMAKE_CXX_COMPILER=clang++-17 \
    -DUSE_ENTERPRISE=OFF \
    -DUSE_MAINTAINER_MODE=OFF \
    -DUSE_GOOGLE_TESTS=OFF \
    -DCMAKE_CXX_FLAGS="-Wno-error=missing-field-initializers -Wno-error=unused-variable" \
    -DCMAKE_EXE_LINKER_FLAGS="-Wl,--start-group" \
    "${_extra_cmake_flags[@]}" \
    ..

# Build
NPROC=$(nproc)
echo "=== Building with ${NPROC} parallel jobs ==="
make -j"${NPROC}"

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
echo "=== Build finished in ${ELAPSED} seconds ==="
echo "Binaries are in: ${BUILD_DIR}/bin/"
