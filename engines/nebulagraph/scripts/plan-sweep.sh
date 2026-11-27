#!/usr/bin/env bash
# One-off: generate a profiled plan (plan.txt + figures) for LDBC IC1..IC12.
#
# NOTE: kvengines.nebula.plan_query always runs PROFILE, which EXECUTES the query
# (there is no static explain-only mode like ArangoDB's). So this sweep produces
# one profiled plan per query — not an explain/profile pair. graphd must be up.
#
# Layout: logs/plan-query/ic<N>/{plan.txt,plan_dag.png,plan_breakdown.png}
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
source ../../.venv/bin/activate 2>/dev/null
mkdir -p logs
exec > >(tee logs/plan-sweep.log) 2>&1   # self-log (no external tee needed)

# PROFILE needs a live graphd — fail early with a clear message otherwise.
if ! (exec 3<>/dev/tcp/127.0.0.1/9669) 2>/dev/null; then
    echo "ERROR: graphd (127.0.0.1:9669) is not reachable. Start Nebula first: just start" >&2
    exit 1
fi

PARAM_DIR=data/ldbc/substitution_parameters-sf0.3
OUT=logs/plan-query
start=$(date +%s)

for n in $(seq 1 12); do
    q=queries/interactive-complex-$n.gql
    pf=$PARAM_DIR/interactive_${n}_param.txt
    echo "==================== IC$n ===================="
    t0=$(date +%s)
    python3 ../../python/kvengines/nebula/plan_query.py -f "$q" --param-file "$pf" \
        --outdir "$OUT/ic$n" 2>&1 | tail -4
    echo "    (IC$n took $(( $(date +%s) - t0 ))s)"
done

echo "==================== DONE in $(( $(date +%s) - start ))s ===================="
echo "Outputs under nebulagraph/$OUT/ic<N>/"
