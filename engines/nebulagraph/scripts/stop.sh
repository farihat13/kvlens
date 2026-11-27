#!/bin/bash
set -euo pipefail

# Nothing to stop if runs/ hasn't been created yet.
if [[ ! -d runs ]]; then
    echo "runs/ does not exist — nothing to stop."
    exit 0
fi

# change ownership of runs directory to current user
user_owner=$(stat -c "%U" "$HOME")
grp_owner=$(stat -c "%G" "$HOME")
sudo chown -R "$user_owner":"$grp_owner" runs

START_TIME=$(date +%s)

cd runs

# ========================================
# Stop all processes and clean data
# ========================================
echo ""
echo "Stopping existing nebula processes..."
./scripts/nebula.service stop all > /dev/null 2>&1 || true
sleep 2

# Force kill any remaining processes
pkill -9 -x nebula-metad 2>/dev/null || true
pkill -9 -x nebula-storaged 2>/dev/null || true
pkill -9 -x nebula-graphd 2>/dev/null || true
sleep 2

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))

echo "Done! Nebula server stopped in ${ELAPSED} seconds."