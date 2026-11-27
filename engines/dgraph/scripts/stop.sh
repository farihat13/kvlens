#!/bin/bash
# Graceful stop: send Ctrl-C to the zero/alpha tmux sessions, then verify and force-kill if needed
# (arango/nebula stop scripts already fall back to a kill; this evens dgraph up with them).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/../.env"
tmux send-keys -t zero C-c 2>/dev/null || true
tmux send-keys -t alpha C-c 2>/dev/null || true
rm -f "${DGRAPH_RUNSDIR}/runinfo" || true

# Wait for graceful exit; fall back to kill.sh (TERM then -9) if anything is still running.
for _ in $(seq 1 10); do
    pgrep -f 'dgraph.* (zero|alpha)' >/dev/null 2>&1 || { echo "Dgraph stopped."; exit 0; }
    sleep 1
done
echo "Graceful stop timed out — force killing." >&2
exec bash "${SCRIPT_DIR}/kill.sh"
