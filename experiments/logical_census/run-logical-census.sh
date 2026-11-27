#!/usr/bin/env bash
# run-logical-census.sh -- the logical access census (owner: experiments/logical_census): per LDBC query (IC, IS, IU, BI),
# the elements a run must touch whatever the layout (edges, edge props, vertex props, index entries, items written), and
# each workload's share per kind. Analysis only: reads a grid's step rows and the plans, opens no store.
#
#   experiments/logical_census/run-logical-census.sh [--sf SF] [--root DIR] [--ldbc-grid DIR] [--bi-grid DIR] [--out DIR]
#   --sf: the LDBC scale (default 3, the paper's); --root: the output tree the grids are read from (default output/;
#   paper-results for the paper's run);
#   --ldbc-grid: a captures/ldbc/<scale> place (default <root>/captures/ldbc/sf<SF>); --bi-grid: the place holding BI's
#                 captures (default the LDBC place); --out: default output/summary/ldbc/sf<SF>/logical-census
# Stages: census.py -> plot_census.py. Output: <out>/{census_steps,census,census_totals}.csv, census_{per_query,workloads}.{png,pdf}
# Time: about 10 s at SF3 (analysis only, any machine). An unknown flag is an error.
set -euo pipefail
case "${1:-}" in -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0;; esac   # before env.sh changes dir
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
SF=3 OUT="" ROOT_TREE="" LDBC="" BI=""
while [ $# -gt 0 ]; do case "$1" in --sf) SF="$2"; shift 2;; --root) ROOT_TREE="$2"; shift 2;; --ldbc-grid) LDBC="$2"; shift 2;;
    --bi-grid) BI="$2"; shift 2;; --out) OUT="$2"; shift 2;; *) echo "unknown flag: $1"; exit 2;; esac; done
[ -n "$OUT" ] || OUT=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis logical-census) || exit 1
TREE=(); [ -n "$ROOT_TREE" ] && TREE=(--root "$ROOT_TREE")
LDBC="${LDBC:-$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale "$SF" "${TREE[@]}")}"
BI="${BI:-$LDBC}"
for g in "$LDBC" "$BI"; do   # refuse by name before any work
  [ -d "$g" ] || { echo "run-logical-census: no grid at $g: measure one (kvcost measure, then kvcost compose), or read the paper's (--root paper-results after just fetch-results), or name one (--ldbc-grid / --bi-grid)" >&2; exit 1; }
done
t0=$(date +%s)
mkdir -p "$OUT"
echo "LDBC grid: $LDBC"; echo "BI grid: $BI"
"$PYTHON" experiments/logical_census/census.py --ldbc-grid "$LDBC" --bi-grid "$BI" --out "$OUT"
"$PYTHON" experiments/logical_census/plot_census.py --in "$OUT"
echo "logical-census done in $(( $(date +%s) - t0 )) s -> $OUT"
