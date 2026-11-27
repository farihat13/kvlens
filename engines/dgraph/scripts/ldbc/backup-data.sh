#!/bin/bash

set -euo pipefail

SF=${1:-0.3}
timestamp=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR=${2:-${MY_DGRAPH_ROOTDIR}/backup/runs-sf${SF}-${timestamp}}

mkdir -p ${BACKUP_DIR}
cp -r ${DGRAPH_RUNSDIR}/* ${BACKUP_DIR}/
BACKUP_DIR_SYMLINK=${MY_DGRAPH_ROOTDIR}/backup/runs-sf${SF}
ln -sfn ${BACKUP_DIR} ${BACKUP_DIR_SYMLINK}

echo "Backed up LDBC SF${SF} data to ${BACKUP_DIR}"