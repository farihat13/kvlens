#!/bin/bash
# ============================================================
# Install built ArangoDB binaries into runs/
# ============================================================
# Copies compiled binaries and creates the runtime directory
# structure needed by start.sh.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"

BUILD_DIR="${ARANGO_SRCDIR}/build"

if [ ! -d "${BUILD_DIR}" ]; then
    echo "ERROR: Build directory not found: ${BUILD_DIR}"
    echo "Run 'just build' first."
    exit 1
fi

BIN_SRC="${BUILD_DIR}/bin"

if [ ! -f "${BIN_SRC}/arangod" ]; then
    echo "ERROR: arangod not found at ${BIN_SRC}/arangod"
    echo "Build may have failed. Check build.log."
    exit 1
fi

echo "=== Installing from ${BIN_SRC} ==="

# A running arangod must not survive this install.
#
# The `rm -rf runs/bin` below does NOT fail against a live server: Linux lets
# you unlink a running executable and keeps the inode alive for the process
# holding it. So the install silently succeeds, the server carries on running
# the OLD build, and runs/bin/arangod on disk is now a DIFFERENT build. Nothing
# errors, and any measurement taken afterwards is attributed to the wrong
# binary — the failure mode this repo can least afford.
#
# Set ARANGO_NO_AUTO_STOP=1 to refuse instead of stopping (e.g. if a server is
# deliberately serving something you cannot interrupt).
if pgrep -x arangod > /dev/null 2>&1; then
    if [ "${ARANGO_NO_AUTO_STOP:-0}" = "1" ]; then
        echo "ERROR: arangod is running and ARANGO_NO_AUTO_STOP=1 is set."
        echo "  Installing now would leave it executing a stale, unlinked binary."
        echo "  Stop it first:  cd ${MY_ARANGO_ROOTDIR} && just stop"
        exit 1
    fi
    echo "--- arangod is running; stopping it first ---"
    echo "    (otherwise it would keep executing the old, unlinked binary)"
    bash "${SCRIPT_DIR}/stop.sh"
    echo "    Restart when ready with: just start"
fi

# Create runtime directories
mkdir -p "${ARANGO_RUNSDIR}/data"
mkdir -p "${ARANGO_RUNSDIR}/apps"

# Copy entire build/bin/ (binaries, ICU data, tzdata, etc.).
# Remove any existing runs/bin first: `cp -r build/bin runs/bin` would otherwise
# nest into runs/bin/bin/ on a re-install (dest dir already exists), leaving the
# stale binary in place at runs/bin/arangod.
rm -rf "${ARANGO_RUNSDIR}/bin"
cp -r "${BIN_SRC}" "${ARANGO_RUNSDIR}/bin"

# arangod looks for tzdata at share/arangodb3/tzdata relative to binary
if [ -d "${ARANGO_RUNSDIR}/bin/tzdata" ]; then
    mkdir -p "${ARANGO_RUNSDIR}/share/arangodb3"
    ln -sfn "${ARANGO_RUNSDIR}/bin/tzdata" "${ARANGO_RUNSDIR}/share/arangodb3/tzdata"
fi

# Create empty config for client tools (arangosh, arangoimport, etc.)
touch "${ARANGO_RUNSDIR}/arangosh.conf"
touch "${ARANGO_RUNSDIR}/arangoimport.conf"

echo "=== Install complete ==="
ls "${ARANGO_RUNSDIR}/bin/"
