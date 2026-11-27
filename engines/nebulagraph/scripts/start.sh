#!/bin/bash
set -euo pipefail

nebula_console_cmd="nebula-console -u root -p nebula -addr 127.0.0.1 -port 9669"
which nebula-console > /dev/null || { echo "nebula-console not found in PATH"; exit 1; }

START_TIME=$(date +%s)

cd runs

# ========================================
# Start services
# ========================================
echo ""
echo "Starting nebula services..."
./scripts/nebula.service start all || echo "(Some services may already be running, continuing...)"

# Wait for graphd to open port 9669
wait_for_port() {
  local host=$1
  local port=$2
  local timeout=${3:-60}
  local end=$((SECONDS + timeout))
  
  while [ $SECONDS -lt $end ]; do
    if ss -ltn "( sport = :$port )" 2>/dev/null | grep -q ":$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

echo "Waiting for nebula-graphd to accept connections on 127.0.0.1:9669..."
if ! wait_for_port 127.0.0.1 9669 60; then
  echo "ERROR: nebula-graphd did not open port 9669 within timeout." >&2
  ./scripts/nebula.service status all
  echo "=== nebula-graphd.INFO (last 100 lines) ===" >&2
  tail -n 100 logs/nebula-graphd.INFO 2>/dev/null || true
  exit 1
fi
echo "✓ nebula-graphd is ready"

# Wait for metad
echo "Waiting for nebula-metad to accept connections on 127.0.0.1:9559..."
if ! wait_for_port 127.0.0.1 9559 60; then
  echo "ERROR: nebula-metad did not open port 9559 within timeout." >&2
  ./scripts/nebula.service status all
  exit 1
fi
echo "✓ nebula-metad is ready"

# Give metad a moment to initialize cluster ID
sleep 3

# Wait for storaged process to start (it may take a bit to register)
echo "Waiting for nebula-storaged to start..."
storaged_started=false
for i in {1..60}; do
  if ss -ltn "( sport = :9779 )" 2>/dev/null | grep -q ":9779"; then
    storaged_started=true
    break
  fi
  # Check if storaged is waiting for registration (process running but not listening yet)
  if pgrep -x nebula-storaged > /dev/null && grep -q "Waiting for the metad to be ready" logs/nebula-storaged.INFO 2>/dev/null; then
    # This is expected - storaged is running but waiting to join cluster
    storaged_started=true
    break
  fi
  sleep 1
done

if [ "$storaged_started" = false ]; then
  echo "ERROR: nebula-storaged did not start within timeout." >&2
  ./scripts/nebula.service status all
  echo "=== nebula-storaged.INFO (last 100 lines) ===" >&2
  tail -n 100 logs/nebula-storaged.INFO 2>/dev/null || true
  exit 1
fi
echo "✓ nebula-storaged process is running"

# ========================================
# Add storage host to cluster
# ========================================
echo ""
echo "Adding storage host to cluster..."

add_hosts_retry() {
  local tries=0
  local max=20
  while [ $tries -lt $max ]; do
    local output
    output=$(${nebula_console_cmd} -e 'ADD HOSTS "127.0.0.1":9779' 2>&1)
    if echo "$output" | grep -q "Execution succeeded"; then
      return 0
    fi
    # Host was already registered from a previous start — not an error
    if echo "$output" | grep -qi "existed"; then
      echo "(Host already registered, skipping ADD HOSTS)"
      return 0
    fi
    tries=$((tries + 1))
    sleep 2
  done
  return 1
}

if ! add_hosts_retry; then
  echo "ERROR: ADD HOSTS failed after retries." >&2
  ${nebula_console_cmd} -e 'SHOW HOSTS' || true
  exit 1
fi
echo "✓ Storage host added to cluster"

# ========================================
# Wait for storage host to become ONLINE
# ========================================
echo ""
echo "Waiting for storage host to become ONLINE..."

wait_for_host_online() {
  local timeout=${1:-120}
  local interval=3
  local end=$((SECONDS + timeout))
  
  while [ $SECONDS -lt $end ]; do
    if ${nebula_console_cmd} -e 'SHOW HOSTS' 2>/dev/null | grep -q '"127.0.0.1".*9779.*"ONLINE"'; then
      return 0
    fi
    sleep $interval
  done
  return 1
}

if ! wait_for_host_online 120; then
  echo "ERROR: Storage host did not become ONLINE within timeout." >&2
  echo ""
  echo "=== SHOW HOSTS ===" >&2
  ${nebula_console_cmd} -e 'SHOW HOSTS' || true
  echo ""
  echo "=== nebula-storaged.INFO (last 100 lines) ===" >&2
  tail -n 100 logs/nebula-storaged.INFO || true
  echo ""
  echo "=== nebula-metad.INFO (last 100 lines) ===" >&2
  tail -n 100 logs/nebula-metad.INFO || true
  echo ""
  echo "Check for cluster ID mismatches in logs above." >&2
  exit 1
fi

echo "✓ Storage host is ONLINE"
${nebula_console_cmd} -e 'SHOW HOSTS'

END_TIME=$(date +%s)
ELAPSED=$((END_TIME - START_TIME))
echo "Done! Nebula server started successfully. (Elapsed time: ${ELAPSED} seconds)"