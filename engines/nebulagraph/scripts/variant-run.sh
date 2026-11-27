#!/bin/bash
set -euo pipefail

# Usage: ./variant-run.sh <variant_name>
# Copies variant binaries to runs/bin/, prints patch info, and starts services.

NAME=$1

# Look up variant output directory from config
VARIANT_DIR=$(python3 ../../python/kvengines/nebula/variant_build.py --list | awk -v name="${NAME}" '$1 == name {print $2}')
if [ -z "$VARIANT_DIR" ]; then
    echo "Error: Variant '${NAME}' not found. Use 'just build-variants-list' to see available variants."
    exit 1
fi

BIN_DIR="${NEBULA_BINARYDIR}/${VARIANT_DIR}"
if [ ! -d "$BIN_DIR" ]; then
    echo "Error: Variant directory not found: $BIN_DIR"
    echo "Build it first with: just build-variant ${NAME}"
    exit 1
fi

# Verify all 3 binaries exist
for binary in nebula-graphd nebula-metad nebula-storaged; do
    if [ ! -f "$BIN_DIR/$binary" ]; then
        echo "Error: Missing binary: $BIN_DIR/$binary"
        exit 1
    fi
done

echo "Using variant '${NAME}': $BIN_DIR"

# Copy binaries to runs/bin/
cp "$BIN_DIR"/nebula-graphd "$BIN_DIR"/nebula-metad "$BIN_DIR"/nebula-storaged "${NEBULA_RUNSDIR}/bin/"

# Copy build info for reference
rm -f "${NEBULA_RUNSDIR}/runinfo"
cp "$BIN_DIR/buildinfo" "${NEBULA_RUNSDIR}/runinfo"

# Show which patches produced this binary
PATCH_DIR="$BIN_DIR/patches"
if [ -d "$PATCH_DIR" ] && [ "$(ls -A "$PATCH_DIR")" ]; then
    echo "Patches:"
    ls -1 "$PATCH_DIR"
else
    echo "Patches: (none)"
fi

# Start services
bash scripts/start.sh
