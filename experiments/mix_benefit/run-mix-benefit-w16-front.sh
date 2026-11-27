#!/usr/bin/env bash
# run-mix-benefit-w16-front.sh -- what each mix on I01's complete per-type front is made of, at SF3: the complete front
# (every mix no other mix beats in both wops and bytes), all 3^21 mixes priced with the exhaustive check, and six figures
# (owner: experiments/mix_benefit). Analysis only: opens no store, measures nothing. The two interactive pages drawn from
# this output are kept outside the artifact.
#   experiments/mix_benefit/run-mix-benefit-w16-front.sh [--scale S] [--root DIR] [--grid DIR] [--front CSV] [--out DIR] [--fresh]
# --scale: the LDBC scale factor whose grid it reads (default 3, the paper's).
# Default inputs, in the output tree --root names (default output/): the nine cells at composed/ldbc/sf<S> when kvcost
# compose wrote them, else captures/ldbc/sf<S> (output_tree's grid), and I01's equal_bytes_front.csv
# (summary/ldbc/sf<S>/layout-choice/reported/wops_bytes4k/equal-bytes-front/i01-rw-ldbc-mix/, by run-layout-choice.sh).
# --grid: a captures/ldbc/sf<S> (or composed/ldbc/sf<S>) dir with the nine cells.
# Output (a working dir): output/summary/ldbc/sf<S>/mix-benefit/i01-front/{complete_front.json, uniform_layouts.json,
# all_mixes.npz, plots/*.{png,pdf}}.
#   experiments/mix_benefit/run-mix-benefit-w16-front.sh --scale 0.1     # a small run: about 3 min
# Time at SF3: 15 min the first time (the front 3 min, all mixes 11.5 min on 32 cores, the
# figures 15 s); the all-mixes step is reused when its grid and front are unchanged (--fresh
# re-prices), so a rerun takes about 3.5 min.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
export PYTHONDONTWRITEBYTECODE=1
ROOT_TREE="" GRID="" FRONT="" OUT="" SCALE=3
FRESH=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --scale)  SCALE="${2:?--scale needs an LDBC scale factor}"; shift 2 ;;
    --root)   ROOT_TREE="${2:?--root needs a directory}"; shift 2 ;;
    --grid)   GRID="${2:?--grid needs a directory}"; shift 2 ;;
    --front)  FRONT="${2:?--front needs a file}"; shift 2 ;;
    --out)    OUT="${2:?--out needs a directory}"; shift 2 ;;
    --fresh)  FRESH=(--fresh); shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *)        echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
TREE=(); [[ -n "$ROOT_TREE" ]] && TREE=(--root "$ROOT_TREE")
OUT="${OUT:-$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SCALE" --analysis mix-benefit --subject i01-front)}"   # written to output/, whatever tree it reads
GRID="${GRID:-$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale "$SCALE" "${TREE[@]}")}"   # each layout read from it by output_tree.layout_capture
FRONT="${FRONT:-$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SCALE" --analysis layout-choice "${TREE[@]}")/reported/wops_bytes4k/equal-bytes-front/i01-rw-ldbc-mix/equal_bytes_front.csv}"
# the grid's reads and writes counts
counts_dir() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import capture_in; print(capture_in(*sys.argv[1:]).dir("counts"))' "$GRID" "$1"; }
READS=$(counts_dir interactive) && WRITES=$(counts_dir writes) || exit 1
for f in "$READS" "$WRITES" "$FRONT"; do
  if [[ ! -e "$f" ]]; then
    echo "missing input: $f" >&2
    echo "The grid is measured by kvcost measure --dataset ldbc=$SCALE --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k, then composed by kvcost compose" >&2
    echo "--dataset ldbc=$SCALE (writes included; hours at SF3; the layouts not measured go to output/composed/ldbc/sf$SCALE), the front by" >&2
    echo "experiments/layout_choice/equal_bytes_front.py over that grid. Neither is run from here." >&2
    exit 1
  fi
done
mkdir -p "$OUT"
IN=(--grid "$GRID" --front "$FRONT" --out "$OUT")
M=experiments/mix_benefit
t0=$SECONDS
step() {           # step <name> <script> [args]: run one stage, log its wall time, stop on failure
  local name=$1 s=$SECONDS; shift
  echo "[i01-front] $name ..."
  "$PYTHON" "$@"
  echo "[i01-front] $name done in $((SECONDS - s)) s"
}
step "complete front" $M/w16_complete_front.py "${IN[@]}"
step "all mixes" $M/w16_all_mixes.py "${IN[@]}" "${FRESH[@]}"
step "figures" $M/plot_w16_front_makeup.py "${IN[@]}"
echo "[i01-front] done in $((SECONDS - t0)) s -> $OUT"
