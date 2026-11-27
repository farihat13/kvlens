#!/usr/bin/env bash
# run-predict-statistics-uniform-params.sh -- predict mode's accuracy on two param sets, side by side: LDBC's curated
# params and the uniform set (LDBC's rows with the bound person or message drawn uniformly at random; --param-set uniform,
# python/kvevaluator/datasets/ldbc/params.py PARAM_SETS). Owner: the predict_statistics study. Both sets are measured on the same
# stores and binary into this run's own output tree (<output>/studies/uniform-params, kvcost measure under in_tree), so the main
# captures are untouched.
# Stages (in order; each prints its wall; a failure stops the chain):
#   params    the uniform files exist (inputs/params/ldbc/sf<SF>/interactive/uniform/; made when missing, gen_uniform_params)
#   measure   kvcost measure on LDBC's params (reported + the held-out rows the plan pick reads + the writes + its
#             check), then on the uniform set (its own params dir, interactive/uniform/; the check on it): the cells of
#             --cells, and with --cells all the ChK-ChK cell of each chunk size (the 49 layouts' 1, 4, 16, 256 KB)
#   accuracy  run/predict-accuracy.sh --captures on the three diagonal cells, I02's 19 queries, per set
#             -> <out>/accuracy-<ldbc-params|uniform>/ (summary.md, summary.json)
#   layouts   (--cells all) the statistics and the 49's tallies (kvcost build-statistics), kvcost
#             predict on the 49 per set, python -m kvcost.predict_accuracy.layouts per set -> <out>/layouts-<set>/; the LDBC
#             side's composed I01 must equal --i01-reference (fatal), the uniform side has no reference
#   compare   python -m kvcost.predict_accuracy.compare_params -> <out>/compare.md, compare.csv, compare_answers.csv
#             (and compare_drift.csv: the LDBC counts against --compare-to)
#   rescore   (not in the default list; --cells all; no store, no measurement) predict on the 49 per set with --stats, with the
#             cardinality rules on and, given --rules-off, again with those off; each scored by kvcost predict-accuracy
#             --layouts against the measured side an earlier run kept (--measured-table-root R: R/layouts-<set>/
#             layout_choice/), then rescore_summary.py -> <out>/rescore/: <set>-rules-<on|off>/layout_choice/,
#             predict-rules-<on|off>/ (the predicted captures), summary.csv, summary.md
# Output: the run's tree <output>/studies/uniform-params/ (python/kvevaluator/output_tree.py): captures/ldbc/sf<SF>/
#   (interactive/{reported,held-out,uniform}, writes) and composed/, and <out> = its summary/ldbc/sf<SF>/uniform-params/:
#   accuracy-<set>/, predict-<set>/, layouts-<set>/, compare*, runs.csv (one row per stage: wall seconds), DONE.
#   experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 0.1
#   experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 3 --P 16          # the slice: the diagonals
#   experiments/predict_statistics/run-predict-statistics-uniform-params.sh --sf 3 --P 16 --cells all \
#     --i01-reference output/summary/ldbc/sf3/layout-costs/i01/measured \
#     --compare-to paper-results
# Flags: --sf (default 0.1); --cells diagonals|all (default diagonals: Sp-Sp, Co-Co, Ch64k-Ch64k); --P N (grid_bodies'
#        threads, default 8; counts do not depend on it); --stages a,b (default all five; layouts runs with --cells all
#        only); --i01-reference DIR (layouts, the LDBC side: I01's 49 measured, the place kvcost cost-layouts ...
#        Sp-Sp Sp-Co ... Ch256k-Ch256k writes, summary/ldbc/sf<SF>/layout-costs/i01/measured); --compare-to DIR (compare: the drift section against another output tree);
#        --stats FILE (rescore: the statistics predict reads, default the dataset's stats.json; e.g. stats-tier1.json);
#        --rules-off a,b (rescore: the switches of a second, rules-off pass, as `kvevaluator predict --rule-names` prints
#        them); --measured-table-root DIR (rescore: required);
#        --fresh (accuracy and layouts: replace predicted captures made from other statistics, tallies or params);
#        --fresh-stats (accuracy: rebuild the statistics, once, before the first set: a stats.json made by older code
#        is otherwise reused, and predict refuses what it lacks); --allow-dirty (a no-op: a dirty binary is warned and
#        marked binary_unclean in <out>/stale.jsonl); -h.
# Expected run time: README.md "Expected run time".
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)     # before env.sh, which changes into the repo root
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
ROOT=$(cd "$HERE/../.." && pwd)

SF=0.1; CELLS=diagonals; P=8; STAGES=params,measure,accuracy,layouts,compare; W16_REF=""; COMPARE_TO=""; FRESH=(); FRESH_STATS=()
STATS=""; RULES_OFF=""; MEAS_ROOT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf) SF="${2:?}"; shift 2 ;;
    --cells) CELLS="${2:?}"; shift 2 ;;
    --P) P="${2:?}"; shift 2 ;;
    --stages) STAGES="${2:?}"; shift 2 ;;
    --i01-reference) W16_REF="${2:?}"; shift 2 ;;
    --compare-to) COMPARE_TO="${2:?}"; shift 2 ;;
    --stats) STATS="${2:?}"; shift 2 ;;
    --rules-off) RULES_OFF="${2:?}"; shift 2 ;;
    --measured-table-root) MEAS_ROOT="${2:?}"; shift 2 ;;
    --allow-dirty) shift ;;   # a no-op: a dirty binary is warned and marked binary_unclean
    --fresh) FRESH=(--fresh); shift ;;
    --fresh-stats) FRESH_STATS=(--fresh-stats); shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$HERE/$(basename "$0")" | sed '$d'; exit 0 ;;
    *) echo "run-predict-statistics-uniform-params: unknown flag $1" >&2; exit 2 ;;
  esac
done
case "$CELLS" in diagonals|all) ;; *) echo "--cells: diagonals or all" >&2; exit 2 ;; esac
BIN=$("$CPP_BIN" --version | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(d["binary_git_sha"], "dirty=" + str(d["binary_dirty"]))')
echo "run-predict-statistics-uniform-params: binary $CPP_BIN $BIN; tree $(git -C "$ROOT" rev-parse --short HEAD)"
SCHEMA=$("$PYTHON" -m kvevaluator.datasets schema-path ldbc) || exit 1
TREE=$("$PYTHON" -m kvevaluator.output_tree study --name uniform-params) || exit 1   # every capture this run reads or writes: its own output tree
OUT=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis uniform-params --root "$TREE") || exit 1
U49=$("$PYTHON" -m kvevaluator.output_tree stats --dataset ldbc --scale "$SF")/uniform-49 || exit 1   # the 49's specs and tallies (kvcost predict Sp-Sp Sp-Co ... Ch256k-Ch256k)
read -r MEASURED DATASET_STATS <<< "$("$PYTHON" -c 'import sys; from kvevaluator.output_tree import Tree
from kvevaluator.predict.build_stats import stats_file
print(Tree(sys.argv[1]).place_dir("captures", "ldbc", sys.argv[2]), stats_file("ldbc", sys.argv[2]))' "$TREE" "$SF")" || exit 1
capture() { "$PYTHON" -m kvevaluator.output_tree capture --family interactive --scale "$SF" --root "$TREE" "$@"; }
T2=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies tier-set 2) || exit 1   # the set a --stats-file predict writes: tier 2's
predicted() {   # predicted <root> <set> [param set]: kvcost predict's capture of interactive below <root>
  "$PYTHON" -c 'import sys; from kvcost.cli.predict import capture_dirs; print(capture_dirs(sys.argv[1], "interactive", sys.argv[3:4], sys.argv[2], sys.argv[4] or None)[0])' \
    "$SF" "$1" "$2" "${3:-}"; }
mkdir -p "$OUT"
"$PYTHON" -m kvevaluator.stale start --into "$OUT" || exit 1   # an earlier run's notes moved aside (kvevaluator.stale)
# a dirty binary is not a refusal: warned and marked binary_unclean in <out>/stale.jsonl
[[ "$BIN" == *"dirty=False" ]] || "$PYTHON" -m kvevaluator.stale note --into "$OUT" binary_unclean "the binary ${BIN%% *} is dirty (${BIN##* })" || exit 1
RUNS="$OUT/runs.csv"
[[ -f "$RUNS" ]] || echo "stage,cells,wall_s,P,binary" > "$RUNS"
has() { [[ ",$STAGES," == *",$1,"* ]]; }
t_all=$SECONDS
step() { local t0=$SECONDS name="$1"; shift; echo "run-predict-statistics-uniform-params: $name"; "$@";
         echo "run-predict-statistics-uniform-params:   $name done in $((SECONDS - t0)) s"
         echo "\"$name\",$CELLS,$((SECONDS - t0)),$P,${BIN%% *}" >> "$RUNS"; }   # the name quoted: it may hold commas

I02=ic1,ic2,ic3,ic4,ic5,ic6,ic7,ic8,ic9,ic10,ic11,ic12,is1,is2,is3,is4,is5,is6,is7   # I01's reads
UDIR="$("$PYTHON" -m kvevaluator.datasets params ldbc "$SF" interactive)/uniform" || exit 1

if has params; then
  if [[ ! -f "$UDIR/uniform_params.meta.json" ]]; then
    step "params: the uniform set (missing: made)" "$PYTHON" -m kvevaluator.workloads.interactive.gen_uniform_params --sf "$SF"
  fi
  command cp -f "$UDIR/uniform_params.meta.json" "$OUT/uniform_params.meta.json"
fi

if has measure; then
  layouts="Sp-Sp Co-Co Ch64k-Ch64k"      # the diagonals; all: the 9 measured (layouts --grid compares with them) and each size's ChK-ChK
  [[ "$CELLS" == all ]] && layouts="Sp-Sp Sp-Co Sp-Ch64k Co-Sp Co-Co Co-Ch64k Ch64k-Sp Ch64k-Co Ch64k-Ch64k Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch256k-Ch256k"
  for set in ldbc-params uniform; do
    set_arg=(); [[ "$set" == uniform ]] && set_arg=(--params uniform)   # its held-out rows are LDBC's, measured first
    # shellcheck disable=SC2086   # $layouts: the layouts, one word each
    step "measure $set: $layouts" in_tree "$TREE" "$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --workload i01 \
      --threads "$P" $layouts "${set_arg[@]}"
  done
fi

if has accuracy; then
  for set in ldbc-params uniform; do
    set_arg=(); params=(); [[ "$set" == uniform ]] && { set_arg=(--param-set uniform); params=(--params uniform); }
    step "accuracy $set: the diagonals, I02" "$ROOT/run/predict-accuracy.sh" --sf "$SF" --tier 2 \
      --captures "$(capture "${params[@]}")" --queries "$I02" --nparam 5 --out "$OUT/accuracy-$set" \
      "${FRESH_STATS[@]}" "${FRESH[@]}" "${set_arg[@]}"
    FRESH_STATS=()       # the statistics are shared: rebuilt once
  done
fi

if has layouts && [[ "$CELLS" == all ]]; then
  step "the statistics and the 49's tallies" "$PYTHON" -m kvcost.cli.cli build-statistics --dataset "ldbc=$SF"
  for set in uniform ldbc-params; do    # the uniform side first: the LDBC side's I01 check is fatal and stops the chain
    set_arg=(); ps=""; ref=("$W16_REF")
    [[ "$set" == uniform ]] && { set_arg=(--params uniform); ps=uniform; ref=(none); }
    [[ -n "${ref[0]}" ]] || { echo "run-predict-statistics-uniform-params: layouts needs --i01-reference for the LDBC side" >&2; exit 2; }
    cells=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies cells all | paste -sd,)
    step "predict $set: the 49" in_tree "$OUT/predict-$set" "$PYTHON" -m kvcost.cli.cli predict --dataset "ldbc=$SF" \
      --family interactive --queries "$I02" ${cells//,/ } --tally-dir "$U49/tally" --stats-file "$DATASET_STATS" \
      --schema-file "$SCHEMA" \
      --nparam 5 --work-dir "$OUT/predict-$set/predict-work" --jobs 8 "${FRESH[@]}" "${set_arg[@]}"
    # the measured side binds the predicted capture's param set (its params dir)
    step "layouts $set: the named best against the measured one" "$PYTHON" -m kvcost.predict_accuracy.layouts \
      --dataset "ldbc=$SF" --predicted "$(predicted "$OUT/predict-$set" "$T2" "$ps")" \
      --grid "$MEASURED" --cost-workload i02 --check-workload i01 --check-reference "${ref[0]}" \
      --expect-layouts 49 --out "$OUT/layouts-$set"
  done
fi

if has rescore; then
  [[ -n "$MEAS_ROOT" ]] || { echo "run-predict-statistics-uniform-params: rescore needs --measured-table-root" >&2; exit 2; }
  [[ "$CELLS" == all ]] || { echo "run-predict-statistics-uniform-params: rescore runs the 49: give --cells all" >&2; exit 2; }
  RS="$OUT/rescore"
  cells=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies cells all | paste -sd,)
  for rules in on ${RULES_OFF:+off}; do
    off=(); [[ "$rules" == off ]] && off=(--no-rules "$RULES_OFF")
    for set in ldbc-params uniform; do
      set_arg=(); ps=""
      [[ "$set" == uniform ]] && { set_arg=(--params uniform); ps=uniform; }
      step "rescore predict $set rules-$rules: the 49" in_tree "$RS/predict-rules-$rules" "$PYTHON" -m kvcost.cli.cli \
        predict --dataset "ldbc=$SF" --family interactive \
        --queries "$I02" ${cells//,/ } --tally-dir "$U49/tally" --stats-file "${STATS:-$DATASET_STATS}" \
        --schema-file "$SCHEMA" --nparam 5 --save-as "predicted-rules-$rules" \
        --work-dir "$RS/predict-rules-$rules/predict-work-$set" --jobs 8 --fresh "${off[@]}" "${set_arg[@]}"
      step "rescore layouts $set rules-$rules" "$PYTHON" -m kvcost.predict_accuracy.layouts --dataset "ldbc=$SF" \
        --predicted "$(predicted "$RS/predict-rules-$rules" "predicted-rules-$rules" "$ps")" \
        --measured-table "$MEAS_ROOT/layouts-$set/layout_choice" --cost-workload i02 --expect-layouts 49 \
        --out "$RS/$set-rules-$rules"
    done
  done
  step "rescore summary" "$PYTHON" "$HERE/rescore_summary.py" --root "$RS" --stats "${STATS:-$DATASET_STATS}" \
    ${RULES_OFF:+--rules-off "$RULES_OFF"}
fi

if has compare; then
  step "compare: the two sets side by side" "$PYTHON" -m kvcost.predict_accuracy.compare_params --root "$TREE" --sf "$SF" \
    ${COMPARE_TO:+--compare-to "$COMPARE_TO"}
fi
echo "run-predict-statistics-uniform-params: DONE sf$SF cells $CELLS in $((SECONDS - t_all)) s ($OUT)" | tee "$OUT/DONE"
