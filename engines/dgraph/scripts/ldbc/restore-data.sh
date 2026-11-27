#!/bin/bash
set -euo pipefail

SF=${1:-0.3}

# Ensure required environment variables are set
: "${MY_DGRAPH_ROOTDIR:?MY_DGRAPH_ROOTDIR is not set}"
: "${DGRAPH_RUNSDIR:?DGRAPH_RUNSDIR is not set}"

BACKUP_SYMLINK=${MY_DGRAPH_ROOTDIR}/backup/runs-sf${SF}

if [ ! -L "${BACKUP_SYMLINK}" ]; then
    echo "Backup symlink ${BACKUP_SYMLINK} does not exist. Exiting."
    exit 1
fi

# WIPE the data dirs before restoring. `cp -r backup/. runs/` MERGES over whatever is
# already there, leaving a mix of two stores: Badger then reads stale vlog/SST files
# alongside the restored ones. The symptom is subtle and easy to misread as a query bug --
# writes commit but never apply (reads are unaffected), so the store looks fine until you
# mutate it. Remove the four data dirs explicitly rather than `rm -rf runs/*`, to preserve
# logs/ and bin/ if present.
rm -rf "${DGRAPH_RUNSDIR}/p" "${DGRAPH_RUNSDIR}/w" "${DGRAPH_RUNSDIR}/zw" "${DGRAPH_RUNSDIR}/t"
cp -r "${BACKUP_SYMLINK}/." "${DGRAPH_RUNSDIR}/"

echo "Restored LDBC SF${SF} data from ${BACKUP_SYMLINK} to ${DGRAPH_RUNSDIR}"
