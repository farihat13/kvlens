#!/usr/bin/env bash
# One-off: generate explain + profile plan (json + figures) for LDBC IC1..IC12.
# Layout: logs/plan-query/ic<N>/{explain,profile}/{plan.json,plan_dag.png,plan_breakdown.png}
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
source ../../.venv/bin/activate 2>/dev/null
mkdir -p logs
exec > >(tee logs/plan-sweep.log) 2>&1   # self-log (no external tee needed)

PARAM_DIR=data/ldbc/substitution_parameters-sf0.3
OUT=logs/plan-query
start=$(date +%s)

for n in $(seq 1 12); do
    q=queries/interactive-complex-$n.aql
    pf=$PARAM_DIR/interactive_${n}_param.txt
    echo "==================== IC$n ===================="

    echo "--- IC$n explain ---"
    python3 ../../python/kvengines/arangodb/plan_query.py -f "$q" --param-file "$pf" --explain-only \
        --outdir "$OUT/ic$n/explain" 2>&1 | tail -3

    echo "--- IC$n profile ---"
    t0=$(date +%s)
    python3 ../../python/kvengines/arangodb/plan_query.py -f "$q" --param-file "$pf" \
        --outdir "$OUT/ic$n/profile" 2>&1 | tail -3
    echo "    (IC$n profile took $(( $(date +%s) - t0 ))s)"
done

echo "==================== DONE in $(( $(date +%s) - start ))s ===================="
echo "Outputs under arangodb/$OUT/ic<N>/{explain,profile}/"
