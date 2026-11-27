#!/bin/bash

set -e  # Exit on error

SF=${1:-0.3}
BACKUP_DIR=${2:-backup/runs-sf${SF}}

echo ""
echo "Backing up LDBC SF${SF} data"

cd runs
./scripts/nebula.service stop all || true
cd ..

# Wait for all nebula processes to fully exit (RocksDB cleanup/compaction)
echo "Waiting for nebula processes to exit..."
while pgrep -x "nebula-storaged|nebula-graphd|nebula-metad" > /dev/null 2>&1; do
    sleep 1
done
echo "✓ All nebula processes exited"

rm -rf ${BACKUP_DIR}
mkdir -p ${BACKUP_DIR}
cp -r   runs/data       ${BACKUP_DIR}/
cp      runs/cluster.id ${BACKUP_DIR}/
cp -r   runs/etc        ${BACKUP_DIR}/
cp -r   runs/pids       ${BACKUP_DIR}/
cp -r   runs/logs       ${BACKUP_DIR}/

# Strip WAL from backup — after flush + clean stop, all data is in SST files
# and the WAL is redundant. This can save >1 GB for large datasets.
WAL_SIZE=$(du -sh ${BACKUP_DIR}/data/storage/*/1/wal 2>/dev/null | awk '{print $1}' || echo "0")
find ${BACKUP_DIR}/data -name "wal" -type d -exec rm -rf {} + 2>/dev/null || true
echo "✓ Stripped WAL from backup (was ${WAL_SIZE})"

echo ""
echo "Backup saved to: ${BACKUP_DIR}"
du -sh ${BACKUP_DIR}