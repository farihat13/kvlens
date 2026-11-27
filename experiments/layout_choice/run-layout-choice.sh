#!/usr/bin/env bash
# run-layout-choice.sh -- which meta layout fits which workload, including mixed layouts (per type; per type and direction)
# (experiments/layout_choice/README.md). Analysis only: it reads the grid captures that kvcost measure
# measured and kvcost compose composed (the 9 uniform cells: LDBC + microbench reads with per-part rows, writes with per-part rows)
# and the built mixed stores (--spec-dir inputs/specs/mixes). Nothing is re-measured. Every mixed
# layout is an exact optimum (python/kvcost/layout_search/search.py, branch and bound).
#   1. cost_workloads.py   every workload x every single layout (read ops / bytes, bytes written, ...)
#   2. python/kvcost/layout_cost/compose.py   checks each single layout priced from the parts reproduces step 1; builds the mix cost cache
#                      per type (props per vertex type, adjacency per edge type), then per type and direction
#   3. check_mix_vs_built.py    each BUILT mix: measured == predicted, per workload
#   4. sweep.py + plot_sweep.py   the write:read ratio sweep and its figures (the per type mix solved at each ratio)
#   5. workload_story.py, check_why_numbers.py + plot_workload_story.py   one folder per workload: its story (README), the options (the
#                      9 single layouts + 6 exact solves per option set), each query's cost by store part, the figures
#   6. budget.py       the byte-budget pick per workload: fewest total ops within B x Sp-Co's (the reference)
#                      total bytes, B = 1, 2, 3, 5 -> budget_picks.csv (the rule: criteria.budget_pick)
#   7. kvcost search-mix --cost-model bill_<form>, bill_search.py + plot_bill.py   (LDBC) the best per type mix under a billed
#                      key-value service's rule (DynamoDB's request units; one result row per workload and form, in
#                      <out root>/bill/rows/), the report and its figure per workload -> <out root>/bill/chunked-64k/, bill/plots/
#   8. equal_bytes_front.py + plot_equal_bytes_front.py   (LDBC) at the same bytes, the fewest-ops mix against the best
#                      uniform layout that fits, per byte limit (each mix line a kvcost search-mix --front staircase row, under
#                      equal-bytes-front/rows/), and its figure per workload -> <out root>/equal-bytes-front/.
#                      Without --workload: the headline workloads I01 and I02. --uniform-curve: the nine uniform layouts only
#                      (points and the best one that fits; no search, seconds)
#   9. plot_cross_workload.py   the figures across workloads: each single layout's cost, what mixing buys
# Steps 7 and 8 run as side chains beside the others (7 after step 1, 8 after step 2, step 8's workloads side by side);
#   their logs are printed at the end, and the run fails if one failed. At sf3 the run takes about 3 min (the README, its run-time paragraph).
# Output: output/summary/<dataset>/<scale>/layout-choice/<params>/<criterion>/ (benchmarks.out_root; README.md = the
#   index; w<NN>-<r|rw>-<workload>/ per workload).
# --dataset linkbench --scale <tag> --params quick|quick-next|log: LinkBench's store and workloads (L01-L04, inputs/workloads/
#   linkbench/<tag>/: make_workloads.py --dataset linkbench --scale <tag> writes them for a new tag) over
#   output/captures/linkbench/<tag>/ (no built-mix check: the built mixes are LDBC-store specs), plus
#   sensitivity_linkbench.py -> sensitivity.csv.
#   experiments/layout_choice/run-layout-choice.sh --sf 0.1
#   experiments/layout_choice/run-layout-choice.sh --dataset linkbench --scale n1m --params quick
#   experiments/layout_choice/run-layout-choice.sh --sf 3 --workload i01          # one workload through every stage (a new workload, a
#                                                         # quick check); files covering every workload are not written
#   experiments/layout_choice/run-layout-choice.sh --sf 3 --grid output/composed/ldbc/sf3   # the nine cells, six composed
#   experiments/layout_choice/run-layout-choice.sh --sf 3 --fresh      # build the mix cost cache again (default: kept and reused
#                                          # in <stats>/<dataset>/<scale>/mix-cost-cache across runs)
#   experiments/layout_choice/run-layout-choice.sh --sf 3 --allow-candidate-drift      # go on past step 0 (the capture vs HEAD's candidate set); NOT for the paper
#   experiments/layout_choice/run-layout-choice.sh --sf 3 --pick-by wops   # the ops-only plan pick, for a before/after
#                                          # diff (default: wops_bytes4k, the pins' rule)
# --grid DIR: a captures/<dataset>/<scale> or composed/<dataset>/<scale> dir (default: output_tree's grid, composed/ when
# kvcost compose wrote the nine, else captures/).
# Measure first (tmux at sf3):  kvcost measure --dataset ldbc=<SF> --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k (the diagonals and ChK-ChK at 1, 4,
#   16 and 256 KB); the same with --family microbench, and with the spec dir inputs/specs/mixes in place of the seven; then
#   kvcost compose --dataset ldbc=<SF>. The grid's nine cells are then at output/composed/ldbc/sf<SF>, the default.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
SF_ARG=""
DATASET=ldbc
WORKLOAD=""
GRID=""
CRITERION=""     # the plan pick rule (candidates.pick_body); empty = the optimizer's default, wops_bytes4k
PSET=""         # LinkBench: its param set (--params quick|quick-next|log), passed to every stage as --params
JOBS=""          # --jobs N: fill the mix cost cache N slices at a time (default: min(32, the cores)); 1 = one process, as before
UNIFORM_CURVE="" # --uniform-curve: step 8 draws the uniform layouts only (equal_bytes_front.py --no-mix)
TC=()            # --fresh: the stages that build the mix cost cache build each query's costs again (else they keep and reuse
                 # them across runs: python/kvcost/layout_search/search.py set_cache)
ALLOW_DRIFT=""   # --allow-candidate-drift: go on also when a captured body is one HEAD refuses on a cell (said loudly;
                 # the numbers then price a capture that predates a plan file, or a body HEAD refuses -- never for the paper)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf) SF_ARG="${2:?--sf needs a value}"; shift 2 ;;
    --dataset) DATASET="${2:?--dataset needs ldbc|linkbench}"; shift 2 ;;
    --scale) SF_ARG="${2:?--scale needs a LinkBench scale tag}"; shift 2 ;;
    --params) PSET="${2:?--params needs quick, quick-next or log}"; shift 2 ;;   # LinkBench's param set, passed on as --params
    --workload) WORKLOAD="${2:?--workload needs ids or names, e.g. i01}"; shift 2 ;;
    --grid) GRID="${2:?--grid needs a grid capture dir}"; shift 2 ;;
    --pick-by) CRITERION="${2:?--pick-by needs a named pick rule or spec}"; shift 2 ;;
    --uniform-curve) UNIFORM_CURVE=1; shift ;;
    --jobs) JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    --allow-candidate-drift) ALLOW_DRIFT=1; shift ;;
    --fresh) TC+=(--fresh); shift ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
SF="${SF_ARG:-$SF}"
L=experiments/layout_choice
t0=$SECONDS
LOGS="$(mktemp -d "${TMPDIR:-/tmp}/layout-choice-logs.XXXXXX")"   # the side chains' logs (below), printed at the end
SIDE=()                                           # the side chains started below: name, pid
# at exit, a side chain still running (the main chain failed) is stopped before its log dir goes
trap 'for (( i = 1; i < ${#SIDE[@]}; i += 2 )); do kill "${SIDE[i]}" 2> /dev/null || true; done; rm -rf "$LOGS"' EXIT
# the cost cache is filled in a fork pool, a query's costs in slices (python/kvcost/layout_search/search.py problems; the same entries as one process:
# the files are byte-identical). A worker peaks at about 0.4 GB at SF3.
NCPU="$(nproc)"; export LAYOUT_CHOICE_JOBS="${JOBS:-$(( NCPU < 32 ? NCPU : 32 ))}"
step() {         # one stage, with its wall time (a failure stops the run: set -e at the caller)
  local t=$SECONDS
  "$@" || return $?
  local name                                     # the script's name, or the module's for `python -m <module>`
  if declare -F "$1" > /dev/null; then name=$1; elif [[ "$2" == -m ]]; then name=$3; else name=$(basename -- "$2"); fi
  echo "[$(basename "$0")] step $name$( [[ "$*" == *--by-direction* ]] && echo " --by-direction" ): $(( SECONDS - t ))s"
}
S=(--sf "$SF" --dataset "$DATASET" ${WORKLOAD:+--workload "$WORKLOAD"} ${CRITERION:+--pick-by "$CRITERION"} ${PSET:+--params "$PSET"})   # every stage: the
                                                 # plan pick rule names the output level (benchmarks.out_root/<criterion>)
# no --grid: the scale's captures, resolved once here, so every stage and both side chains read the same one (kvcost
# search-mix with no --from would read a run dir's composed capture instead)
if [[ -z "$GRID" ]]; then       # the dataset's place: each layout read from it by output_tree.layout_capture
  GRID="$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset "$DATASET" --scale "$SF")" || exit 1
fi
G=(--grid "$GRID")                               # the stages that read the grid capture
# the grid's read capture (the dataset's first read workload, at --params): the counts dir every check below reads
read -r GRID_CAPTURE GRID_COUNTS <<< "$("$PYTHON" -c 'import sys; from kvevaluator import workloads as W; from kvevaluator.output_tree import capture_in
cap = capture_in(sys.argv[1], W.names(kind="read", dataset=sys.argv[2])[0], sys.argv[3] or None); print(cap.path, cap.dir("counts"))' "$GRID" "$DATASET" "${PSET:-}")"
# the grid must hold captures (counts/<cell>.csv in the capture), whatever --allow-candidate-drift says: a missing or empty grid
# would otherwise run every stage and price nothing
if ! compgen -G "$GRID_COUNTS/*.csv" > /dev/null; then
  echo "[$(basename "$0")] REFUSED: no grid captures under $GRID_COUNTS (a missing or empty --grid)"; exit 1
fi
# a spec set measured beside the grid (the built mixes): a counts file of one of its layouts in the grid's capture
has_counts() { "$PYTHON" -c 'import os, sys; from kvevaluator.capture.spec_sets import spec_cells; from kvevaluator.output_tree import Capture
sys.exit(not any(os.path.exists(Capture(sys.argv[1]).path_of("counts", c)) for c in spec_cells(sys.argv[2])))' "$GRID_CAPTURE" "inputs/specs/$1"; }
# step 0: the capture against HEAD's candidate set (experiments/meta/check_candidate_drift.py: every cell, both directions).
# Candidates never run (rc 3): go on; the picks are among the bodies run and each priced row is marked (its `stale`
# column), and the list is kept in the output dir. A captured body HEAD refuses on a cell (rc 1): stop, unless
# --allow-candidate-drift
OUT_ROOT="$("$PYTHON" -c 'import sys; from kvcost.layout_cost import benchmarks as B
print(B.out_root(sys.argv[1], sys.argv[2], sys.argv[3] or None, sys.argv[4] or None))' "$DATASET" "$SF" "$CRITERION" "${PSET:-}")"
mkdir -p "$OUT_ROOT"
drift_rc=0
"$PYTHON" experiments/meta/check_candidate_drift.py --sf "$SF" --dataset "$DATASET" ${GRID:+--grid "$GRID"} ${PSET:+--param-set "$PSET"} \
  > "$OUT_ROOT/candidate_drift.txt" 2>&1 || drift_rc=$?
cat "$OUT_ROOT/candidate_drift.txt"
if [[ "$drift_rc" -eq 3 ]]; then
  echo "[$(basename "$0")] CANDIDATE DRIFT: the capture never ran some of HEAD's candidates -- going on; these numbers are NOT final (the priced rows are marked; the list: $OUT_ROOT/candidate_drift.txt)"
elif [[ "$drift_rc" -ne 0 ]]; then
  if [[ -n "$ALLOW_DRIFT" ]]; then
    echo "[$(basename "$0")] CANDIDATE DRIFT between the capture and HEAD's plan files -- going on because of --allow-candidate-drift: these numbers are NOT final"
  else
    echo "[$(basename "$0")] CANDIDATE DRIFT between the capture and HEAD's plan files -- stopping (a captured body HEAD refuses on a cell, or no capture; re-measure the cells, or --allow-candidate-drift for a look)"; exit 1
  fi
fi
step "$PYTHON" $L/cost_workloads.py "${S[@]}" "${G[@]}"
# Three chains side by side from here; each writes only its own files (the shared cost cache dir: atomic per-process writes,
# python/kvcost/layout_search/search.py), stops at its first failure, and the run fails when any chain does. The main chain prints as it goes;
# the two side chains' lines come after it, bill then front.
#   bill:  kvcost search-mix per workload and bill form -> bill_search (the report, from those rows) -> plot_bill
#   front: equal_bytes_front, one process per workload, after compose --by-direction built the cost cache it loads -> its figure
bill_rows() {      # bill_rows <tree>: each workload's fewest bill per type, one kvcost search-mix row per form, into the output tree <tree>
  local w form from; from=$(tree_of "$GRID") || return 1
  for w in $("$PYTHON" -c 'import sys; from kvcost.layout_cost import benchmarks as B
print(" ".join(x["id"] for x in B.select(sys.argv[1], sys.argv[2], sys.argv[3] or None).values()))' "$DATASET" "$SF" "$WORKLOAD"); do
    for form in lower upper exact; do
      in_tree "$1" "$PYTHON" -m kvcost.cli.cli search-mix --dataset "$DATASET=$SF" ${CRITERION:+--pick-by "$CRITERION"} \
        ${PSET:+--params "$PSET"} --from "$from" --workload "$w" --settings Sp,Co,Ch64k --cost-model "bill_$form" "${TC[@]}" || return 1
    done
  done
}
chain_bill() {
  local rows
  rows="$("$PYTHON" -c 'import sys; from kvcost.layout_cost import benchmarks as B
print(B.out_root(sys.argv[1], sys.argv[2], sys.argv[3] or None, sys.argv[4] or None))' "$DATASET" "$SF" "$CRITERION" "${PSET:-}")/bill/rows" || return 1
  step bill_rows "$rows"
  step "$PYTHON" $L/bill_search.py "${S[@]}" "${G[@]}" \
    --results "$("$PYTHON" -m kvevaluator.output_tree summary --dataset "$DATASET" --scale "$SF" --root "$rows")"
  step "$PYTHON" $L/plot_bill.py "${S[@]}"
}
chain_front() {
  local wl pids=() rc=0
  local -a wls
  IFS=, read -ra wls <<< "${WORKLOAD:-i01,i02}"
  local -a FRONT=(--sf "$SF" --dataset "$DATASET" ${CRITERION:+--pick-by "$CRITERION"} ${PSET:+--params "$PSET"})
  local -a BUILT=()                               # the built I01 mixes as points, where this grid holds their capture
  [[ -z "$UNIFORM_CURVE" ]] && has_counts mixes_w16 && BUILT=(--built inputs/specs/mixes_w16)
  for wl in "${wls[@]}"; do                       # each workload writes its own folder
    step "$PYTHON" $L/equal_bytes_front.py "${FRONT[@]}" --workload "$wl" "${G[@]}" ${UNIFORM_CURVE:+--no-mix} \
      "${BUILT[@]}" --jobs "$LAYOUT_CHOICE_JOBS" "${TC[@]}" > "$LOGS/front-$wl.log" 2>&1 &
    pids+=($!)
  done
  for wl in "${!pids[@]}"; do wait "${pids[$wl]}" || rc=$?; done
  for wl in "${wls[@]}"; do cat "$LOGS/front-$wl.log"; done
  [[ $rc -eq 0 ]] || return $rc
  step "$PYTHON" $L/plot_equal_bytes_front.py "${FRONT[@]}" --workload "${WORKLOAD:-i01,i02}"
}
if [[ "$DATASET" == ldbc ]]; then
  ( chain_bill ) > "$LOGS/bill.log" 2>&1 &
  SIDE+=(bill $!)
fi
step "$PYTHON" -m kvcost.layout_cost.compose "${S[@]}" "${G[@]}" "${TC[@]}"
step "$PYTHON" -m kvcost.layout_cost.compose "${S[@]}" "${G[@]}" "${TC[@]}" --by-direction
if [[ "$DATASET" == ldbc ]]; then
  ( chain_front ) > "$LOGS/front.log" 2>&1 &
  SIDE+=(front $!)
fi
if [[ "$DATASET" == ldbc ]]; then                   # the built mixes are LDBC-store specs; a grid with none measured says so
  if has_counts mixes; then
    step "$PYTHON" tests/gates/check_mix_vs_built.py --sf "$SF" "${G[@]}" ${WORKLOAD:+--workload "$WORKLOAD"} ${CRITERION:+--pick-by "$CRITERION"}
  else
    echo "[$(basename "$0")] no built mixes measured under $GRID_COUNTS: check_mix_vs_built SKIPPED (Result 3 is not checked at sf$SF)"
  fi
fi
step "$PYTHON" $L/sweep.py "${S[@]}" "${G[@]}" "${TC[@]}"
step "$PYTHON" $L/plot_sweep.py "${S[@]}"
step "$PYTHON" $L/workload_story.py "${S[@]}" "${G[@]}" "${TC[@]}"
step "$PYTHON" $L/check_why_numbers.py "${S[@]}"       # the why texts: computed numbers only, each renders from the snapshots and from this run
step "$PYTHON" $L/plot_workload_story.py "${S[@]}"
step "$PYTHON" $L/budget.py "${S[@]}"
[[ -z "$WORKLOAD" ]] && step "$PYTHON" $L/plot_cross_workload.py --sf "$SF" --dataset "$DATASET" ${CRITERION:+--pick-by "$CRITERION"} ${PSET:+--params "$PSET"}   # covers every workload
[[ "$DATASET" == linkbench ]] && step "$PYTHON" $L/sensitivity_linkbench.py --scale "$SF" ${WORKLOAD:+--workload "$WORKLOAD"} ${CRITERION:+--pick-by "$CRITERION"} ${PSET:+--params "$PSET"}   # does the ranking survive the modeling choices
side_rc=0
for (( i = 0; i < ${#SIDE[@]}; i += 2 )); do      # the side chains: their rc, then their lines
  rc=0; wait "${SIDE[i+1]}" || rc=$?
  cat "$LOGS/${SIDE[i]}.log"
  echo "[$(basename "$0")] chain ${SIDE[i]}: rc=$rc"
  [[ $rc -eq 0 ]] || side_rc=$rc
done
[[ $side_rc -eq 0 ]] || { echo "[$(basename "$0")] FAILED: a side chain failed (its lines above)"; exit "$side_rc"; }
echo "[$(basename "$0")] DONE (sf$SF, $(( SECONDS - t0 ))s)"
