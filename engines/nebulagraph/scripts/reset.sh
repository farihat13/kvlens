#!/bin/bash
set -euo pipefail

START_TIME=$(date +%s)

# Force kill any remaining processes (works even without runs/)
pkill -9 -x nebula-metad || true
pkill -9 -x nebula-storaged || true
pkill -9 -x nebula-graphd || true
sleep 2

# Nothing else to clean if runs/ doesn't exist yet
if [ ! -d runs ]; then
    echo "Done! No runs/ directory — nothing to clean."
    exit 0
fi

# change ownership of runs directory to current user
user_owner=$(stat -c "%U" "$HOME")
grp_owner=$(stat -c "%G" "$HOME")
sudo chown -R "$user_owner":"$grp_owner" runs

cd runs

# ========================================
# Stop all processes and clean data
# ========================================
echo ""
echo "Stopping existing nebula processes and cleaning data..."
./scripts/nebula.service stop all > /dev/null 2>&1 || true
sleep 2

# Clean up pids
rm -f pids/* 2>/dev/null || true

# Verify all processes stopped; abort before wiping if any survive (don't rm under a live process).
if pgrep -x "nebula-metad|nebula-storaged|nebula-graphd" > /dev/null; then
  echo "WARNING: Some nebula processes are still running:"
  pgrep nebula -l
  echo "Waiting 5 more seconds..."
  sleep 5
  if pgrep -x "nebula-metad|nebula-storaged|nebula-graphd" > /dev/null; then
    echo "ERROR: nebula processes still running — refusing to wipe data/." >&2
    exit 1
  fi
fi

# Complete data wipe for fresh start (avoid cluster ID mismatches)
echo "Wiping all data and logs for fresh start..."
rm -rf data/*
rm -rf logs/*
# CRITICAL: Remove cluster ID files to avoid cluster ID mismatch
rm -f cluster.id
rm -f .cluster_id

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo "Done! Nebula server stopped and data/logs cleaned in ${ELAPSED} seconds."