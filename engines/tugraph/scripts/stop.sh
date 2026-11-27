#!/bin/bash
# Stop the TuGraph server this tree started: its tmux session, then wait until its process is gone.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../.env"
PIDFILE="$TUGRAPH_RUNSDIR/lgraph_server.pid"
tmux kill-session -t "=tugraph-server" 2>/dev/null || true
if [[ -f "$PIDFILE" ]]; then
  pid=$(cat "$PIDFILE")
  if [[ -n "$pid" && -d "/proc/$pid" && "$(cat /proc/$pid/comm 2>/dev/null)" == lgraph_server ]]; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 40); do [[ -d "/proc/$pid" ]] || break; sleep 0.25; done
    [[ -d "/proc/$pid" ]] && { echo "WARNING: lgraph_server (pid $pid) is still running" >&2; exit 1; }
  fi
fi
echo "TuGraph stopped"
