#!/usr/bin/env bash
# run-scale-transfer.sh -- the scale_transfer study's one entry point: a small scale's measured and predicted costs of
# the 49 uniform layouts against SF30's measured ones, for one workload. Analysis only: opens no store. Owner: the
# scale_transfer study (experiments/scale_transfer; README.md beside it). Not in paper-results: the paper does not report
# SF30, so SF30's captures are measured into the output dir first (`kvcost measure --dataset ldbc=30 ...`, README.md).
# Run from the artifact's root (its venv; the binary every output records: KVEVAL_CPP_BIN, or the build's).
# Stages (in order; each prints its wall; a failure stops the run):
#   measured   (on the output dir, no --root: kvcost compose at each scale first, the nine from the measured diagonals)
#              kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k at SF30 and at the small scale (the 49's totals and their own check), then
#              scale_transfer.py measured on each, checked against those totals
#   predicted  scale_transfer.py predicted on each `kvcost cost-layouts --count-source <a predicted set>` place given (its
#              query_costs.csv)
#   compare    scale_transfer.py compare (each side's per-query costs weighted into totals, layout by layout against
#              SF30's; with --weights, every side by that file's frequencies): panels.csv / .md, points.csv,
#              per_query.csv, query_ranks.csv
#   plot       one figure per small-scale panel (wops_bytes4k), A and B by the artifact's
#              experiments/predict_statistics/plot_predicted_vs_measured.py --totals on compare's totals/<panel>.csv,
#              the small scale's pick ringed (--mark-pick)
# Panels: a<n>-sf<small>-measured; b<n>-sf<small>-tier1, -tier2 (each predicted table given); ref-sf30-tier1, -tier2.
# Inputs, from the output tree --root names (default output/; paper-results holds no SF30): each scale's nine
#   (each by output_tree.layout_capture: measured itself in captures/, else composed/, by kvcost compose) and its Chunked
#   cells at 1k-256k in captures/. A tree given by --root is read only, so it must hold the nine of each scale the run reads.
#   The predicted tables: --small-tier1/--small-tier2/--large-tier1/--large-tier2 (each a cost-layouts place: a dir
#   holding query_costs.csv, made by `kvcost predict-accuracy --count-source predicted-tier1 | predicted-tier2`); no
#   default: a tier not given (or "-") is left out, as I01 at tier 1, which predict refuses.
# Weights: --weights FILE re-weights every side by the file's frequencies (compare). B01's default is its equal-share
#   frequency file, each query 1 / its measured wops_bytes4k on Sp-Co at SF3, the one file for every scale:
#   query_weighting.equal_share_path("b01"), inputs/workloads/ldbc/equal-share/b01.json; I01 and I02 take
#   their own frequencies (no --weights).
# Output: --out DIR (default output/summary/ldbc/sf30/scale-transfer/sf<small>/<workload>/), with
#   provenance.txt (the binary, the commits, every input).
#   experiments/scale_transfer/run-scale-transfer.sh --small 3 --workload i02                       (from the artifact's root)
# Flags: --small 3|0.3, --workload i02|i01|b01 (required); --root DIR; --small-tier1/2, --large-tier1/2 CSV;
#   --weights FILE; --out DIR; --stages a,b (default measured,predicted,compare,plot); --dry-run (print each command, run nothing); -h.
# Expected run time: README.md, "Plan and expected run time".
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SMALL=""; WL=""; OUTD=""; STAGES=measured,predicted,compare,plot; DRY=0; ROOT_TREE=""
ST1=""; ST2=""; LT1=""; LT2=""; WEIGHTS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --small) SMALL="${2:?}"; shift 2 ;;
    --workload) WL="${2:?}"; shift 2 ;;
    --root) ROOT_TREE="${2:?}"; shift 2 ;;
    --small-tier1) ST1="${2:?}"; shift 2 ;;
    --small-tier2) ST2="${2:?}"; shift 2 ;;
    --large-tier1) LT1="${2:?}"; shift 2 ;;
    --large-tier2) LT2="${2:?}"; shift 2 ;;
    --out) OUTD="${2:?}"; shift 2 ;;
    --weights) WEIGHTS="${2:?}"; shift 2 ;;
    --stages) STAGES="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "run-scale-transfer: unknown flag $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SMALL" && -n "$WL" ]] || { echo "run-scale-transfer: --small and --workload are required" >&2; exit 2; }
[[ -f python/kvcost/layout_cost/uniform_49.py ]] || { echo "run-scale-transfer: run from a kvlens checkout" >&2; exit 2; }
PY=(uv run --locked python); ST=(uv run --locked python "$HERE/scale_transfer.py")
CPP_BIN=$("${PY[@]}" -m kvevaluator.paths cpp_bin)   # the binary every output names (KVEVAL_CPP_BIN, or the build's)
TREE=(); FROM=(); [[ -n "$ROOT_TREE" ]] && { TREE=(--root "$ROOT_TREE"); FROM=(--from "$ROOT_TREE"); }
if [[ $WL == b01 ]]; then   # LDBC BI: its equal-share weights
  WEIGHTS=${WEIGHTS:-$("${PY[@]}" -c 'from kvcost.layout_cost import query_weighting as QW; print(QW.equal_share_path("b01"))')}
  [[ -f "$WEIGHTS" ]] || { echo "run-scale-transfer: b01 needs its weights file (--weights): $WEIGHTS not found" >&2; exit 2; }
fi
case "$SMALL" in
  3) N=1 ;;
  0.3) N=2 ;;
  *) echo "run-scale-transfer: --small 3 or 0.3" >&2; exit 2 ;;
esac
TAG=$(echo "sf$SMALL" | tr -d .)
OUTD=${OUTD:-$("${PY[@]}" -m kvevaluator.output_tree summary --dataset ldbc --scale 30 --analysis scale-transfer --subject "sf$SMALL")/$WL}
BIN=$("$CPP_BIN" --version | "${PY[@]}" -c 'import json,sys; d=json.load(sys.stdin); print(d["binary_git_sha"][:10], "dirty" if d["binary_dirty"] else "clean")')
ADIRTY=""; [[ -z $(git status --porcelain --untracked-files=no) ]] || ADIRTY=" dirty"
PROV="binary $BIN ($CPP_BIN); artifact $(git rev-parse --short HEAD)$ADIRTY"
echo "run-scale-transfer: sf$SMALL against sf30, $WL; $PROV; out $OUTD$([[ $DRY == 1 ]] && echo '; dry run')"
has() { [[ ",$STAGES," == *",$1,"* ]]; }
run() {   # run LOG CMD...: CMD's output to LOG and its last line here; with --dry-run, print it instead
  local log=$1; shift
  if [[ $DRY == 1 ]]; then echo "  $* > $log"; return 0; fi
  if "$@" > "$log" 2>&1; then tail -1 "$log" | sed 's/^/  /'; else local rc=$?; tail -5 "$log" | sed 's/^/  /'; echo "run-scale-transfer: FAIL ($log)" >&2; exit $rc; fi
}
step() { local t0=$SECONDS name="$1"; shift; echo "run-scale-transfer: $name"; "$@"; echo "run-scale-transfer:   $name done in $((SECONDS - t0)) s"; }
[[ $DRY == 1 ]] || { mkdir -p "$OUTD/logs" "$OUTD/costs"; printf '%s\n' "$PROV" "tree: ${ROOT_TREE:-the output dir}" \
  "small tier 1: ${ST1:--}" "small tier 2: ${ST2:--}" "large tier 1: ${LT1:--}" "large tier 2: ${LT2:--}" \
  "weights: ${WEIGHTS:-none, each workload item at its own frequency}" > "$OUTD/provenance.txt"; }

measured() {   # measured SF NAME: the 49 at the scale (their totals and their own check), then the per-query rows checked
  local sf=$1 name=$2 ref
  if [[ -z "$ROOT_TREE" ]]; then   # the output dir: the nine composed from the measured diagonals (kept when unchanged)
    run "$OUTD/logs/compose-$name.log" "${PY[@]}" -m kvcost.cli.cli compose --dataset "ldbc=$sf"
  fi
  run "$OUTD/logs/cost-layouts-$name.log" "${PY[@]}" -m kvcost.cli.cli cost-layouts --dataset "ldbc=$sf" --workload "$WL" \
    $("${PY[@]}" -m kvcost.predict_accuracy.uniform_49_tallies cells all) "${FROM[@]}"
  ref=$("${PY[@]}" -m kvevaluator.output_tree summary --dataset ldbc --scale "$sf" --analysis layout-costs --subject "$WL")/measured
  run "$OUTD/logs/measured-$name.log" "${ST[@]}" measured --dataset ldbc --sf "$sf" --workload "$WL" "${FROM[@]}" \
    --check-against "$ref" --out "$OUTD/costs/$name-measured.csv"
}
if has measured; then
  step "measured sf30" measured 30 sf30
  step "measured sf$SMALL" measured "$SMALL" "$TAG"
fi
PANELS=(--source "a$N-$TAG-measured=$OUTD/costs/$TAG-measured.csv")
pred() {   # pred SF CSV LABEL FILE
  [[ -z "$2" || "$2" == "-" ]] && return 0
  run "$OUTD/logs/predicted-$4.log" "${ST[@]}" predicted --sf "$1" --costs "$2" --label "$3" --workload "$WL" --out "$OUTD/costs/$4.csv"
}
preds() {
  pred "$SMALL" "$ST1" predicted-tier1 "$TAG-tier1"; pred "$SMALL" "$ST2" predicted-tier2 "$TAG-tier2"
  pred 30 "$LT1" predicted-tier1 sf30-tier1; pred 30 "$LT2" predicted-tier2 sf30-tier2
}
has predicted && step "predicted" preds
for t in 1 2; do
  v=ST$t; [[ -n "${!v}" && "${!v}" != "-" ]] && PANELS+=(--source "b$N-$TAG-tier$t=$OUTD/costs/$TAG-tier$t.csv")
done
for t in 1 2; do
  v=LT$t; [[ -n "${!v}" && "${!v}" != "-" ]] && PANELS+=(--source "ref-sf30-tier$t=$OUTD/costs/sf30-tier$t.csv")
done
WARGS=(); [[ -n "$WEIGHTS" ]] && WARGS=(--weights "$WEIGHTS")
has compare && step "compare" run "$OUTD/logs/compare.log" "${ST[@]}" compare --large "$OUTD/costs/sf30-measured.csv" "${PANELS[@]}" \
  "${WARGS[@]}" --provenance "$PROV" --out "$OUTD"
plots() {   # A and B: the artifact's predicted-vs-measured panel from compare's totals/ (x: SF30 measured; y: the source),
  # the source's pick ringed (SF30's own predicted panels, ref, are main's predicted-vs-measured/w17_tier*.png)
  local t v xlab="SF30 measured cost (wops_bytes4k) over its best"
  local P=("${PY[@]}" experiments/predict_statistics/plot_predicted_vs_measured.py --mark-pick --x-label "$xlab")
  [[ $DRY == 1 ]] || mkdir -p "$OUTD/figures"
  run "$OUTD/logs/plot-a.log" "${P[@]}" --totals "$OUTD/totals/a$N-$TAG-measured.csv" \
    --scale "${WL^^}: SF$SMALL measured, SF30 measured" --pick-words "SF$SMALL's best" --best-words "SF30's best" \
    --y-label "SF$SMALL measured cost"$'\n'"over SF$SMALL's best" --out "$OUTD/figures/a$N-$TAG-measured_wops_bytes4k.png"
  for t in 1 2; do
    v=ST$t; [[ -n "${!v}" && "${!v}" != "-" ]] || continue
    run "$OUTD/logs/plot-b$t.log" "${P[@]}" --totals "$OUTD/totals/b$N-$TAG-tier$t.csv" \
      --scale "${WL^^}: SF$SMALL statistics, SF30 measured" --label "tier $t" --best-words "SF30's best" \
      --y-label "SF$SMALL predicted cost"$'\n'"over predict's cheapest" --out "$OUTD/figures/b$N-$TAG-tier${t}_wops_bytes4k.png"
  done
}
has plot && step "plot" plots
[[ $DRY == 1 ]] || cat "$OUTD/panels.md" 2>/dev/null | sed -n '/^| panel/,/^$/p'
echo "run-scale-transfer: DONE (sf$SMALL, $WL) -> $OUTD"
