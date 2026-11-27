#!/bin/bash

set -euo pipefail
. "$(dirname "$0")/../.env"     # this engine's wiring (MY_NEBULA_*)

# Argument: whether to compile RocksDB from scratch
# Default: false
COMPILE_ROCKSDB=${1:-false}
ENABLE_KV_STATS_STMT=${ENABLE_KV_STATS:-OFF}
ENABLE_KV_PROFILE_STMT=${ENABLE_KV_PROFILE:-OFF}
ENABLE_KV_HISTOGRAMS_STMT=${ENABLE_KV_HISTOGRAMS:-OFF}
ENABLE_QUERY_PLAN_DEBUG_STMT=${ENABLE_QUERY_PLAN_DEBUG:-OFF}
ENABLE_PLANNER_FIXES_STMT=${ENABLE_PLANNER_FIXES:-OFF}

START_TIME=$(date +%s)

bash scripts/start-container.sh ${MY_NEBULA_CONTAINER_NAME}


# Build RocksDB if explicitly requested or if the static library is missing.
if [ "${COMPILE_ROCKSDB}" = "true" ] || [ ! -f rocksdb/librocksdb.a ]; then
    echo
    sudo docker exec ${MY_NEBULA_CONTAINER_NAME} bash -c "
    set -euo pipefail

    echo 'Building RocksDB (workspace copy)'
    cd /workspace/rocksdb
    # Build a static library that Nebula will link against.
    # Use the same compiler toolchain as Nebula.
    make clean-rocks
    echo    
    echo 'Compiling RocksDB'
    make static_lib -j10 \
        CC=\$TOOLSET_CLANG_DIR/bin/gcc \
        CXX=\$TOOLSET_CLANG_DIR/bin/g++ \
        DEBUG_LEVEL=0 \
        USE_RTTI=1 \
        SNAPPY=1 \
        ZLIB=1 \
        BZ2=1 \
        LZ4=1 \
        ZSTD=1 \
        EXTRA_CFLAGS=\"-I/opt/vesoft/third-party/3.3/include\" \
        EXTRA_CXXFLAGS=\"-I/opt/vesoft/third-party/3.3/include\" \
        EXTRA_LDFLAGS=\"-L/opt/vesoft/third-party/3.3/lib -L/opt/vesoft/third-party/3.3/lib64\"
    "
else
    echo "Skipping RocksDB compilation."
fi

echo
sudo docker exec ${MY_NEBULA_CONTAINER_NAME} bash -c "
set -euo pipefail

echo 'Building Nebula (linking against workspace RocksDB)'
cd /workspace/nebula
mkdir -p build && cd build
cmake \
    -DCMAKE_CXX_COMPILER=\$TOOLSET_CLANG_DIR/bin/g++ \
    -DCMAKE_C_COMPILER=\$TOOLSET_CLANG_DIR/bin/gcc \
    -DENABLE_WERROR=OFF \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_TESTING=OFF \
    -DRocksdb_INCLUDE_DIR=/workspace/rocksdb/include \
    -DRocksdb_LIBRARY=/workspace/rocksdb/librocksdb.a \
    -DENABLE_KV_STATS:BOOL=${ENABLE_KV_STATS_STMT} \
    -DENABLE_KV_PROFILE:BOOL=${ENABLE_KV_PROFILE_STMT} \
    -DENABLE_KV_HISTOGRAMS:BOOL=${ENABLE_KV_HISTOGRAMS_STMT} \
    -DENABLE_QUERY_PLAN_DEBUG:BOOL=${ENABLE_QUERY_PLAN_DEBUG_STMT} \
    -DENABLE_PLANNER_FIXES:BOOL=${ENABLE_PLANNER_FIXES_STMT} \
    ..
make -j10
make install
"
# make install will install the binaries to /usr/local/nebula/bin

END_TIME=$(date +%s)
ELAPSED_TIME=$((END_TIME - START_TIME))
echo "Done! Build completed in $((ELAPSED_TIME / 60)) minutes and $((ELAPSED_TIME % 60)) seconds."