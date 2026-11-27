#!/usr/bin/env bash
# One-off: capture the Dgraph SubGraph-tree plan for LDBC IC1..IC12.
#
# Dgraph's "plan" is the SubGraph tree (the logical access structure: which
# predicates are fetched and which filters apply). Unlike Nebula PROFILE or
# ArangoDB profile=2, it carries NO per-node wall times and there is NO figure —
# the artifact is a readable TEXT tree. There is also no explain/profile split.
#
# REQUIREMENTS (same as `just ldbc-plan`):
#   1. The live dgraph binary is the profile variant (dgraph-profile.patch applied);
#      without it ProcessQuery never prints the tree.
#   2. alpha runs with --v >= 1 (the emission is gated by glog.V(1)). `just start`
#      uses --v 3; `just alpha-now` uses --v 0 and captures NOTHING.
#   3. sf data is loaded and alpha is up.
# This script probes for (1)+(2) by running IC1 and checking a tree came out; if
# not, it aborts with guidance instead of writing 12 empty files.
#
# Layout: logs/plan-query/ic<N>/plan.txt  (formatted tree + raw block)
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
source ../../.venv/bin/activate 2>/dev/null   # the repo venv (just setup)

# Resolve $DGRAPH_RUNSDIR (alpha.log lives there). Prefer the env; fall back to .env
# then to ./runs so the script works in a bare shell.
if [ -z "${DGRAPH_RUNSDIR:-}" ]; then
    set -a; source .env 2>/dev/null || true; set +a
fi
RUNSDIR="${DGRAPH_RUNSDIR:-$(pwd)/runs}"
ALPHA_LOG="$RUNSDIR/alpha.log"

mkdir -p logs
exec > >(tee logs/plan-sweep.log) 2>&1   # self-log

PARAM_DIR=data/ldbc/substitution_parameters-sf0.3
OUT=logs/plan-query

# alpha must be up (queries execute).
if ! (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null; then
    echo "ERROR: alpha (127.0.0.1:8080) not reachable. Start Dgraph (profile variant, --v>=1): just start" >&2
    exit 1
fi
if [ ! -f "$ALPHA_LOG" ]; then
    echo "ERROR: alpha.log not found at $ALPHA_LOG. Set \$DGRAPH_RUNSDIR or start via 'just start' (which tees alpha.log)." >&2
    exit 1
fi

# Capture one query's plan: run it (plan -> alpha.log), then parse the last
# user-query block from the NEW log bytes into $OUT/ic<N>/plan.txt.
capture_one() {
    local n="$1"
    local out="$OUT/ic$n"; mkdir -p "$out"
    local before; before=$(stat -c%s "$ALPHA_LOG")
    just ldbc-icq "$n" 1 >/dev/null 2>&1 || { echo "  IC$n: query failed"; return 1; }
    # Parse just the bytes appended by this query, take the last user-query plan.
    tail -c "+$((before + 1))" "$ALPHA_LOG" > "$out/.new.log"
    python3 ../../python/kvengines/dgraph/parse_plan.py "$out/.new.log" --last 1 --save "$out/plan.txt" >/dev/null 2>&1
    rm -f "$out/.new.log"
    if [ -s "$out/plan.txt" ]; then echo "  IC$n: plan -> $out/plan.txt"; return 0; fi
    echo "  IC$n: no plan captured"; return 1
}

start=$(date +%s)

echo "==================== PROBE (IC1) ===================="
if ! capture_one 1; then
    echo "ERROR: no SubGraph tree captured for IC1." >&2
    echo "       The live binary is likely NOT the profile variant, or alpha is at --v 0." >&2
    echo "       Apply dgraph-profile.patch + rebuild, start with --v>=1 (just start), then retry." >&2
    echo "       Confirm the active binary with: just which-variant" >&2
    exit 2
fi

for n in $(seq 2 12); do
    echo "==================== IC$n ===================="
    capture_one "$n" || true
done

echo "==================== DONE in $(( $(date +%s) - start ))s ===================="
echo "Outputs under dgraph/$OUT/ic<N>/plan.txt"
