#!/bin/bash
set +e  # Don't exit on error

echo "=== Dgraph Status Check ==="
echo ""

# Check if processes are running.
# Match '^dgraph' (anchored regex on the process name), NOT '-x dgraph' (exact): the
# measured builds run renamed binaries (dgraph-kvstats / dgraph-histogram), and an
# exact match misses those — reporting "NOT RUNNING" while the cluster is actually up.
ALPHA_PID=$(pgrep '^dgraph' | head -1)
ZERO_PID=$(pgrep '^dgraph' | tail -1)

ALPHA_OKAY=true
ZERO_OKAY=true

if [ -z "$ALPHA_PID" ]; then
    echo "❌ Alpha: NOT RUNNING"
    ALPHA_RUNNING=false
    ALPHA_OKAY=false
else
    echo "✓ Alpha: Running (PID: $ALPHA_PID)"
    ALPHA_RUNNING=true
fi

if [ -z "$ZERO_PID" ]; then
    echo "❌ Zero: NOT RUNNING"
    ZERO_RUNNING=false
    ZERO_OKAY=false
else
    echo "✓ Zero: Running (PID: $ZERO_PID)"
    ZERO_RUNNING=true
fi
echo ""

# Check tmux sessions
echo "=== Tmux Sessions ==="
if tmux has-session -t alpha 2>/dev/null; then
    echo "✓ Alpha tmux session: Active"
else
    echo "❌ Alpha tmux session: Not found"
fi

if tmux has-session -t zero 2>/dev/null; then
    echo "✓ Zero tmux session: Active"
else
    echo "❌ Zero tmux session: Not found"
fi
echo ""

# Check Alpha health endpoint
if [ "$ALPHA_RUNNING" = true ]; then
    echo "=== Alpha Health Check ==="
    if curl -s --max-time 2 localhost:8080/health >/dev/null 2>&1; then
        HEALTH=$(curl -s --max-time 2 localhost:8080/health | jq -r '.status // "unknown"' 2>/dev/null || echo "unknown")
        if [ "$HEALTH" = "healthy" ] || [ -n "$(curl -s --max-time 2 localhost:8080/health 2>/dev/null)" ]; then
            echo "✓ Alpha HTTP endpoint: Healthy (port 8080)"
        else
            echo "⚠️ Alpha HTTP endpoint: Responding but status unclear"
            ALPHA_OKAY=false
        fi
    else
        echo "❌ Alpha HTTP endpoint: Not responding (port 8080)"
        ALPHA_OKAY=false
    fi
    
    # Check Alpha state
    if curl -s --max-time 2 localhost:8080/state >/dev/null 2>&1; then
        LEADER=$(curl -s --max-time 2 localhost:8080/state | jq -r '.groups."1".members | to_entries[] | select(.value.leader==true) | .value.addr' 2>/dev/null || echo "unknown")
        if [ -n "$LEADER" ] && [ "$LEADER" != "unknown" ]; then
            echo "✓ Alpha state: Leader at $LEADER"
        else
            echo "⚠️ Alpha state: No leader detected"
            ALPHA_OKAY=false
        fi
    fi
    echo ""
fi

# Check Zero (port 6080 for HTTP, 5080 for gRPC)
if [ "$ZERO_RUNNING" = true ]; then
    echo "=== Zero Health Check ==="
    if curl -s --max-time 2 localhost:6080/state >/dev/null 2>&1; then
        echo "✓ Zero HTTP endpoint: Responding (port 6080)"
        ZERO_LEADER=$(curl -s --max-time 2 localhost:6080/state | jq -r '.zeros | to_entries[] | select(.value.leader==true) | .value.addr' 2>/dev/null || echo "unknown")
        if [ -n "$ZERO_LEADER" ] && [ "$ZERO_LEADER" != "unknown" ]; then
            echo "✓ Zero state: Leader at $ZERO_LEADER"
        fi
    else
        echo "❌ Zero HTTP endpoint: Not responding (port 6080)"
        ZERO_OKAY=false
    fi
    echo ""
fi

# Summary
echo "=== Summary ==="
if [ "$ALPHA_OKAY" = true ] && [ "$ZERO_OKAY" = true ]; then
    if curl -s --max-time 2 localhost:8080/health >/dev/null 2>&1; then
        echo "✓ Dgraph cluster is running and healthy"
        exit 0
    else
        echo "⚠️ Dgraph processes running but Alpha not responding"
        exit 1
    fi
elif [ "$ALPHA_OKAY" = true ] || [ "$ZERO_OKAY" = true ]; then
    echo "⚠️ Partial cluster (some components not running)"
    exit 1
else
    echo "❌ Dgraph cluster is not running"
    exit 1
fi