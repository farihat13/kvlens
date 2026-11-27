#!/bin/bash
# Show NebulaGraph service status, and return a meaningful exit code (0 = graphd accepting
# connections, 1 = not), so `just status; echo $?` behaves like dgraph/arango.
cd "$(dirname "$0")/.."
runs/scripts/nebula.service status all || true
if ss -ltn "( sport = :9669 )" 2>/dev/null | grep -q ":9669"; then
    echo "graphd is accepting connections on 9669"
    exit 0
else
    echo "graphd is NOT accepting connections on 9669"
    exit 1
fi
