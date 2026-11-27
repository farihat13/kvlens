#!/bin/bash
# Force kill all NebulaGraph processes (prefer stop.sh). Exits nonzero if any survive, so a
# caller like reset.sh won't wipe data out from under a live process.
pkill -9 -x nebula-metad 2>/dev/null || true
pkill -9 -x nebula-storaged 2>/dev/null || true
pkill -9 -x nebula-graphd 2>/dev/null || true
for _ in $(seq 1 10); do
    pgrep -x "nebula-metad|nebula-storaged|nebula-graphd" >/dev/null 2>&1 || { echo "No nebula processes running"; exit 0; }
    sleep 0.5
done
echo "WARNING: nebula processes still running:" >&2
pgrep -a -x "nebula-metad|nebula-storaged|nebula-graphd" >&2
exit 1
