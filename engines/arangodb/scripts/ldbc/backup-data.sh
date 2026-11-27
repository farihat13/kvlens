#!/bin/bash
# ============================================================
# Backup ArangoDB LDBC data
# ============================================================
# Usage: bash scripts/ldbc/backup-data.sh [sf]
# Creates a timestamped backup of runs/data/ and symlinks
# backup/runs-sf<sf> to the latest.
#
# ARANGO_BACKUP_DIR is env-overridable so another setup can keep its own backups
# outside this checkout (engines/arangodb/load-data.sh points it at
# engines/arangodb/backup/runs-sf<SF>). Default = the main arango backup dir, so
# nothing changes for `just ldbc-backup`. The timestamped-dir + symlink layout is
# the same wherever it is rooted.
#
# It is a ROOT (the dir holding runs-sf<SF>-<ts>), not the sf dir itself — unlike
# the cost_eval dgraph/nebula loaders' BACKUP_TARGET/BACKUP_SF_DIR, which name the
# sf dir directly. Hence the distinct name: same idea, different contract.
# ============================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../../.env"

SF="${1:-0.3}"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_ROOT="${ARANGO_BACKUP_DIR:-${MY_ARANGO_ROOTDIR}/backup}"
BACKUP_DIR="${BACKUP_ROOT}/runs-sf${SF}-${TIMESTAMP}"

echo "=== Backing up ArangoDB data (sf=${SF}) ==="
echo "Backup root: ${BACKUP_ROOT}"

mkdir -p "${BACKUP_ROOT}"

# Copy data only (not binaries)
mkdir -p "${BACKUP_DIR}"
cp -r "${ARANGO_RUNSDIR}/data" "${BACKUP_DIR}/data"
echo "Backed up to: ${BACKUP_DIR}"
du -sh "${BACKUP_DIR}"

# Update symlink to latest.
# Guard: if runs-sf<SF> already exists as a REAL directory (hand-made, or copied in from another
# machine), `ln -sfn` would silently create the link INSIDE it and exit 0 — leaving runs-sf<SF>/data
# absent, so every caller that probes that path fails open and reloads forever with no error.
cd "${BACKUP_ROOT}"
if [ -d "runs-sf${SF}" ] && [ ! -L "runs-sf${SF}" ]; then
    echo "ERROR: ${BACKUP_ROOT}/runs-sf${SF} is a real directory, not the expected symlink." >&2
    echo "  The new backup is at ${BACKUP_DIR}. Move or remove that directory, then re-link." >&2
    exit 1
fi
ln -sfn "runs-sf${SF}-${TIMESTAMP}" "runs-sf${SF}"
echo "Symlink: ${BACKUP_ROOT}/runs-sf${SF} -> runs-sf${SF}-${TIMESTAMP}"
