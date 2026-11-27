#!/bin/bash
# ============================================================
# Force kill ArangoDB processes
# ============================================================

pkill -9 -x arangod 2>/dev/null || true
tmux kill-session -t arangod 2>/dev/null || true

# pkill -9 is immediate, but the kernel needs a moment to reap the process.
# A single check here races with reaping: the process can still appear "alive"
# one instant and be gone the next. Poll for up to ~5s so the result is stable,
# and exit deterministically (0 = gone, 1 = genuinely stuck) so callers like
# `just reset` don't fail spuriously on a process that is actually dying.
for _ in $(seq 1 10); do
    pgrep -x arangod > /dev/null 2>&1 || break
    sleep 0.5
done

if pgrep -x arangod > /dev/null 2>&1; then
    echo "WARNING: arangod processes still running"
    pgrep -x arangod
    exit 1
fi
echo "No arangod processes running"
exit 0
