#!/bin/bash
set -euo pipefail

NAME=$1
VERBOSITY=${2:-3}
CACHE_SIZE_MB=${3:-100}
# Get the binary path for this variant (match exact name, not prefix)
VARIANT_INFO=$(python3 ../../python/kvengines/dgraph/variant_build.py --list | awk -v name="${NAME}" '$1 == name {print $2}')
if [ -z "$VARIANT_INFO" ]; then
    echo "Error: Variant '${NAME}' not found. Use 'just build-variants-list' to see available variants."
    exit 1
fi
VARIANT_DIR="${DGRAPH_BINARYDIR}/${VARIANT_INFO}"
BINARY="${VARIANT_DIR}/dgraph"
if [ ! -f "$BINARY" ]; then
    echo "Error: Binary not found: $BINARY"
    echo "Build it first with: just build-variant ${NAME}"
    exit 1
fi
echo "Using variant '${NAME}': $BINARY"
mkdir -p "$GOPATH/bin"
ln -sf "$BINARY" "$GOPATH/bin/dgraph"
mkdir -p ${DGRAPH_RUNSDIR} && cd ${DGRAPH_RUNSDIR}
# Start zero
tmux new-session -d -s zero "$BINARY zero 2>&1 | tee zero.log"
sleep 1
# Start alpha
tmux new-session -d -s alpha "$BINARY alpha --v ${VERBOSITY} --cache \"size-mb=${CACHE_SIZE_MB}; percentage=40,40,20\" --security whitelist=0.0.0.0/0 2>&1 | tee alpha.log"
# copy the build info for this variant to the runs dir for reference
rm -f ./runinfo
cp "${VARIANT_DIR}/buildinfo" ./runinfo

# show which patches produced this binary
PATCH_DIR="${VARIANT_DIR}/patches"
if [ -d "$PATCH_DIR" ] && [ "$(ls -A "$PATCH_DIR")" ]; then
    echo "Patches:"
    ls -1 "$PATCH_DIR"
else
    echo "Patches: (none)"
fi