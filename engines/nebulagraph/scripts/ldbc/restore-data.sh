#!/bin/bash

set -e  # Exit on error

SF=${1:-0.3}
BACKUP_DIR=${2:-backup}
BACKUP_SF_DIR=${BACKUP_DIR}/runs-sf${SF}

echo ""
echo "Restoring LDBC SF${SF} data"

cd runs
./scripts/nebula.service stop all || true
sleep 2
cd ..

rm -rf runs/data/*
rm -rf runs/etc/*
rm -rf runs/pids/*
rm -rf runs/logs/*
rm -f runs/cluster.id
cp -rf   ${BACKUP_SF_DIR}/data       runs/
cp -f    ${BACKUP_SF_DIR}/cluster.id runs/
cp -rf   ${BACKUP_SF_DIR}/etc        runs/
cp -rf   ${BACKUP_SF_DIR}/pids       runs/
# cp -rf   ${BACKUP_SF_DIR}/logs       runs/

echo ""
echo "LDBC SF${SF} data restored"

# cd runs
# ./scripts/nebula.service restart all || true