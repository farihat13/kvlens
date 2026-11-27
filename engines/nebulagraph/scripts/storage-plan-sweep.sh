#!/usr/bin/env bash
# One-off: capture the STORAGED-side execution plan (per-storage-request processor
# traces) for LDBC IC1..IC12, via the storage_log_level / PLAN_DEBUG mechanism.
#
# This is the storaged-side counterpart to plan-sweep.sh (which captures the
# graphd-side PROFILE plan + figures). The storaged plan is a TEXT trace, not a
# figure — it shows what each storage request (GetNeighbors, LookupProcessor, ...)
# did per partition.
#
# REQUIREMENT: the live nebula-storaged must be built with QUERY_PLAN_DEBUG=ON
# (nebula.unified.patch, cmake -DQUERY_PLAN_DEBUG=ON). A binary without it rejects
# the storage_log_level gflag — this script probes for that and aborts early.
#
# Layout: logs/storage-plan-level<L>/ic<N>/p1/{storaged.log,parsed.json}
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
source ../../.venv/bin/activate 2>/dev/null
mkdir -p logs
exec > >(tee logs/storage-plan-sweep.log) 2>&1   # self-log (no external tee needed)

LEVEL=${1:-1}

# graphd must be up (queries execute), storaged flag endpoint must accept the gflag.
if ! (exec 3<>/dev/tcp/127.0.0.1/9669) 2>/dev/null; then
    echo "ERROR: graphd (127.0.0.1:9669) not reachable. Start Nebula: just start" >&2
    exit 1
fi
probe=$(curl -s -X PUT http://localhost:19779/flags -d "{\"storage_log_level\": \"$LEVEL\"}")
if echo "$probe" | grep -q "failedOptions"; then
    echo "ERROR: the live nebula-storaged does not accept 'storage_log_level'." >&2
    echo "       The installed binary lacks QUERY_PLAN_DEBUG. Rebuild + swap to a" >&2
    echo "       binary built with nebula.unified.patch + cmake -DQUERY_PLAN_DEBUG=ON." >&2
    echo "       (Server response: $probe)" >&2
    curl -s -X PUT http://localhost:19779/flags -d '{"storage_log_level": "0"}' >/dev/null
    exit 2
fi

start=$(date +%s)
for n in $(seq 1 12); do
    echo "==================== IC$n (storaged plan) ===================="
    # storage-plan-query sets the flags, truncates the storaged log, runs the query,
    # copies the log to logs/storage-plan-level<L>/ic<N>/p1/, and prints the parse.
    just storage-plan-query "$n" 1 "$LEVEL" 2>&1 | tail -6
    out="logs/storage-plan-level$LEVEL/ic$n/p1"
    if [ -f "$out/storaged.log" ]; then
        python3 -u ../../python/kvengines/nebula/parse_storage_plan_log.py "$out/storaged.log" --json \
            > "$out/parsed.json" 2>/dev/null \
            && echo "    parsed JSON -> $out/parsed.json" \
            || echo "    (no PLAN_DEBUG blocks for IC$n)"
    fi
done

just storage-plan-off 2>&1 | tail -1   # restore concurrency / log level
echo "==================== DONE in $(( $(date +%s) - start ))s ===================="
echo "Outputs under nebulagraph/logs/storage-plan-level$LEVEL/ic<N>/p1/"
