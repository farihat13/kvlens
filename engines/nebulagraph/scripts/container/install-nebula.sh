#!/bin/bash
set -exuo pipefail

BUILD_DIR=${1:-/workspace/nebula/build}
RUNS_DIR=${2:-/workspace/runs}
echo "Using build directory: $BUILD_DIR"

rm -rf /usr/local/nebula/
cd $BUILD_DIR && make install && cd /workspace

if [ ! -d "$RUNS_DIR" ]; then
    cp -r /usr/local/nebula $RUNS_DIR
    cd $RUNS_DIR/etc
    cp nebula-graphd.conf.default nebula-graphd.conf
    cp nebula-metad.conf.default nebula-metad.conf
    cp nebula-storaged.conf.default nebula-storaged.conf
    echo "Initialized $RUNS_DIR with config files from $BUILD_DIR/etc"
else
    echo "$RUNS_DIR already exists, updating binaries only"
    cp $BUILD_DIR/bin/nebula-* $RUNS_DIR/bin/
    echo "Updated binaries in $RUNS_DIR/bin/"
fi