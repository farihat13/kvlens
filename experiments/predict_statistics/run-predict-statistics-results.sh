#!/usr/bin/env bash
# run-predict-statistics-results.sh -- predict mode's results at one SF: the LDBC parts (a), (b) and (d) (SF3, and the
# SF30 rows of (a)).
# Owner: the predict_statistics study (experiments/predict_statistics).
# Needs the binary built, the statistics at that SF (output/stats/ldbc/sf<SF>/stats.json and anchor_stats.json), the
# tally library and the 49's tallies (kvcost build-statistics --dataset ldbc=<SF>: stats/ldbc/sf<SF>/
# tallies/ and uniform-49/).
# Stages (in order; each prints its wall; a failure stops the chain):
#   tier1       tier 2 against tier 1 on the 49 uniform layouts: kvcost predict --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k and kvcost
#               predict-accuracy uniform-49 (the named best against the measured one, I01's check against
#               --i01-reference), once with --tier 2 and once with --tier 1 (the counts and means and the 10-value lists,
#               the distributions set flat), into one output root; layout_reports_w17.py on I02's 19 queries and
#               on the 12 that tier 1 alone prices; tier1_spread_rows.py
#   burstiness  IC2's ranked walk with burstiness as built and with b = 1: run/predict-accuracy.sh --cells
#               props_chunked_adj_chunked --queries ic2 (a traced run: it opens that store, read-only), once per
#               statistics file; burstiness_walk.py
#   writes      I01 (reads and updates) on the nine grid layouts: kvcost predict --workload i01; cost-layouts measured
#               (--from the measured grid's tree) and predicted, wops and wops_bytes4k; search-mix on each; the predicted
#               mix priced on measured; w16_over_measured.py (each output at its place in the writes/ output root)
# The anchor level (tier 2 + the bound person's own statistics) is not run here.
# The LDBC-vs-uniform-params rows (c) come from run-predict-statistics-uniform-params.sh.
# Output: --work DIR (default output/summary/ldbc/sf<SF>/predict-statistics/results): one output root per run (tier1/,
#   burstiness-b-as-built/, burstiness-b-1/, writes/), the tables beside them (reports/, i01/, spread_rows.md,
#   burstiness_walk.md, w16_over_measured.md), a log per command, runs.csv (stage, wall seconds), DONE.
#   experiments/predict_statistics/run-predict-statistics-results.sh --sf 3 \
#     --grid output/captures/ldbc/sf3 \
#     --i01-reference output/summary/ldbc/sf3/layout-costs/i01/measured
#     (I01's 49 in the measured chunk view, the one the check composes: kvcost cost-layouts --dataset ldbc=3
#     --workload i01 Sp-Sp Sp-Co ... Ch256k-Ch256k --cost-model bill_exact; a reference in another view is refused)
#   SF30's rows (the tier1 stage): --sf 30 --stages tier1 --grid output/composed/ldbc/sf30 --i01-reference
#     <I01's measured 49 at SF30: a uniform_layouts_by_size_measured.csv>
#     (the layouts step reads the other chunk sizes' Chunked/Chunked from output/captures/ldbc/sf30)
# Flags: --sf, --grid, --i01-reference (required); --work DIR; --stages a,b (default tier1,burstiness,writes); --jobs N
#   (default 7); --dry-run (print each command and its log, run nothing); -h.
# Expected run time: README.md "Expected run time" (SF3: tier1 ~2 min, burstiness ~8 min, writes ~1 min; SF30 tier1 ~2 h).
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)     # before env.sh, which changes into the repo root
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
ROOT=$(cd "$HERE/../.." && pwd)
SF=""; WORK=""; GRID=""; REF=""; STAGES=tier1,burstiness,writes; JOBS=7; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf) SF="${2:?}"; shift 2 ;;
    --work) WORK="${2:?}"; shift 2 ;;
    --grid) GRID="${2:?}"; shift 2 ;;
    --i01-reference) REF="${2:?}"; shift 2 ;;
    --stages) STAGES="${2:?}"; shift 2 ;;
    --jobs) JOBS="${2:?}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$HERE/$(basename "$0")" | sed '$d'; exit 0 ;;
    *) echo "run-predict-statistics-results: unknown flag $1" >&2; exit 2 ;;
  esac
done
[[ -n "$SF" && -n "$GRID" && -n "$REF" ]] || { echo "run-predict-statistics-results: --sf, --grid and --i01-reference are required" >&2; exit 2; }
for s in ${STAGES//,/ }; do
  [[ ",tier1,burstiness,writes," == *",$s,"* ]] || { echo "run-predict-statistics-results: unknown stage $s (tier1, burstiness, writes)" >&2; exit 2; }
done
cd "$ROOT"
K=("$PYTHON" -m kvcost.cli.cli)
# a path of the output tree under an output root <O> (python/kvevaluator/output_tree.py): tree <O> <kind> [--analysis A ...]
tree() { KVEVAL_OUTPUT_DIR="$1" "$PYTHON" -m kvevaluator.output_tree "$2" --dataset ldbc --scale "$SF" "${@:3}"; }
SD=$(tree "$OUTPUT_DIR" stats)
STATS=$SD/stats.json; ANCHOR=$SD/anchor_stats.json      # build_stats.stats_file and anchor_path
WORK=${WORK:-$(tree "$OUTPUT_DIR" summary --analysis predict-statistics --subject results)}
[[ $DRY == 1 ]] || mkdir -p "$WORK"
WORK=$(cd "$WORK" 2>/dev/null && pwd || echo "$WORK")
echo "run-predict-statistics-results: sf$SF; tree $(git rev-parse --short HEAD); work $WORK$([[ $DRY == 1 ]] && echo '; dry run')"
RUNS="$WORK/runs.csv"
[[ $DRY == 1 || -f "$RUNS" ]] || echo "stage,wall_s" > "$RUNS"
has() { [[ ",$STAGES," == *",$1,"* ]]; }
step() { local t0=$SECONDS name="$1"; shift; echo "run-predict-statistics-results: $name"; "$@"
         echo "run-predict-statistics-results:   $name done in $((SECONDS - t0)) s"; [[ $DRY == 1 ]] || echo "$name,$((SECONDS - t0))" >> "$RUNS"; }
# run LOG CMD...: run CMD, its output to LOG ("-": to this output; "tee:F": to this output and to F). With --dry-run,
# print it instead, on one line: the command, then "> LOG". An environment setting goes in CMD as `env K=V`.
run() {
  local log=$1; shift
  if [[ $DRY == 1 ]]; then printf '%q ' "$@"; if [[ $log == - ]]; then echo; else echo "> $log"; fi; return 0; fi
  case "$log" in
    -) "$@" ;;
    tee:*) "$@" | tee "${log#tee:}" ;;
    *) "$@" > "$log" 2>&1 ;;
  esac
}
root() {   # an output root reading this output dir's record stream and anchor file
  local O=$1 f D
  D=$(tree "$O" stats)
  run - mkdir -p "$D" "$(tree "$O" stores)"
  for f in records.jsonl records.jsonl.key.json; do run - ln -sfn "$SD/$f" "$D/$f"; done
  run - ln -sfn "$ANCHOR" "$D/anchor_stats.json"
}

if has tier1; then
  tier1() {   # one root: each tier's captures in its own set (predicted-tier2/, predicted-tier1/; stats-tier1.json beside stats.json)
    local O=$WORK/tier1 n f t D
    root "$O"; D=$(tree "$O" stats)
    for f in tallies uniform-49; do run - ln -sfn "$SD/$f" "$D/$f"; done      # this output dir's library and 49 tallies
    run - command cp -f "$STATS" "$D/stats.json"
    for f in captures-dir composed-dir; do           # the measured side: this output dir's layouts (layout_capture)
      run - mkdir -p "$(dirname "$(tree "$O" $f)")"
      run - ln -sfn "$(tree "$OUTPUT_DIR" $f)" "$(tree "$O" $f)"
    done
    local u49
    u49=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies cells all) || return 1   # the 49, one cell id per line
    for t in 2 1; do
      local set; set=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies tier-set "$t") || return 1
      run "$WORK/tier1-predict-tier$t.log" env KVEVAL_OUTPUT_DIR="$O" "${K[@]}" predict --dataset "ldbc=$SF" --workload i02 \
        $u49 --tier $t --jobs "$JOBS"
      run "$WORK/tier1-layouts-tier$t.log" env KVEVAL_OUTPUT_DIR="$O" "${K[@]}" predict-accuracy --dataset "ldbc=$SF" \
        --workload i02 $u49 --count-source $set --from "$O"
      for n in 19 12; do
        run "$WORK/tier1-report-tier$t-q$n.log" env KVEVAL_OUTPUT_DIR="$O" "$PYTHON" "$HERE/layout_reports_w17.py" "$SF" \
          "$("$PYTHON" -c 'import sys; from kvcost.cli.predict import capture_dirs; print(capture_dirs(sys.argv[1], "interactive", sys.argv[2:3], sys.argv[3])[0])' "$SF" "$set" "$O")" \
          "$WORK/reports/tier$t-q$n" $n "$GRID" "$REF" ldbc i02 i01
      done
    done
    run "tee:$WORK/spread_rows.md" "$PYTHON" "$HERE/tier1_spread_rows.py" "$WORK/reports"
  }
  step tier1 tier1
fi
if has burstiness; then
  burstiness() {   # the traced run opens Chunked/Chunked's store (named by its spec, meta-<sha12>) read-only
    local v O D S
    S=$("$PYTHON" -c 'import sys; from kvevaluator.measure import stores; print(stores.meta_store_name(sys.argv[1]))' \
      inputs/specs/grid/props_chunked_adj_chunked.json)
    for v in b-as-built b-1; do
      O=$WORK/burstiness-$v; root "$O"; D=$(tree "$O" stats)
      [[ "$(tree "$OUTPUT_DIR" stores)" == "$(tree "$O" stores)" ]] || run - ln -sfn "$(tree "$OUTPUT_DIR" stores)/$S" "$(tree "$O" stores)/$S"   # one root (KVEVAL_STORE_DIR): no link
      if [[ $v == b-as-built ]]; then run - ln -sfn "$STATS" "$D/stats.json"; else
        run - "$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); d["burstiness"]={k: 1 for k in d["burstiness"]}; json.dump(d, open(sys.argv[2], "w"))' "$STATS" "$D/stats.json"
      fi
      run "$WORK/burstiness-$v.log" env KVEVAL_OUTPUT_DIR="$O" bash run/predict-accuracy.sh --sf "$SF" --tier 2 --cells props_chunked_adj_chunked --queries ic2 --nparam 5
    done
    local b0 b1                          # assigned first: set -e stops on a refusal here, not inside an argument
    b0=$(tree "$WORK/burstiness-b-as-built" summary --analysis predict-accuracy)
    b1=$(tree "$WORK/burstiness-b-1" summary --analysis predict-accuracy)
    run "tee:$WORK/burstiness_walk.md" "$PYTHON" "$HERE/burstiness_walk.py" "$b0/ratios.csv" "$b1/ratios.csv"
  }
  step burstiness burstiness
fi
if has writes; then
  writes() {
    local O=$WORK/writes D m src x W=inputs/workloads/ldbc/i01.json
    local E=(env KVEVAL_OUTPUT_DIR="$O")
    root "$O"; D=$(tree "$O" stats); run - ln -sfn "$STATS" "$D/stats.json"
    run - ln -sfn "$SD/tallies" "$D/tallies"       # the tally library kvcost predict reads (tally_library.library_dir)
    run "$WORK/writes-predict.log" "${E[@]}" "${K[@]}" predict --workload $W --dataset "ldbc=$SF" --tier 2
    local t2; t2=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies tier-set 2) || return 1   # its set
    # every output in the writes tree $O at its own place: layout-costs/i01/[<set>/]<model>/, mix-search/i01[-<set>]/
    local from; from=$(tree_of "$GRID") || return 1
    for m in wops_bytes4k wops; do for src in measured "$t2"; do
      x=(--from "$from"); [[ $src != measured ]] && x=(--count-source $src)
      run "$WORK/writes-cost-$m-$src.log" "${E[@]}" "${K[@]}" cost-layouts --workload $W --dataset "ldbc=$SF" --cost-model $m --uniform-only "${x[@]}"
    done; done
    for src in measured "$t2"; do
      x=(--from "$from"); [[ $src != measured ]] && x=(--count-source $src)
      run "$WORK/writes-mix-$src.log" "${E[@]}" "${K[@]}" search-mix --workload $W --dataset "ldbc=$SF" --cost-model wops_bytes4k "${x[@]}"
    done
    local costs; costs=$(tree "$O" summary --analysis layout-costs --subject i01)
    run "$WORK/writes-price-predicted.log" "${E[@]}" "${K[@]}" cost-layouts --workload $W --dataset "ldbc=$SF" --cost-model wops_bytes4k \
      --mix "$(tree "$O" summary --analysis mix-search --subject "i01-$t2")/mixes/per-type-wops_bytes4k.json" \
      --name pred-predicted --from "$from"
    run "tee:$WORK/w16_over_measured.md" "$PYTHON" "$HERE/w16_over_measured.py" "$costs/wops_bytes4k/uniform.csv" \
      "$costs/$t2/wops_bytes4k/uniform.csv" writes
  }
  step writes writes
fi
[[ $DRY == 1 ]] || echo "run-predict-statistics-results: DONE sf$SF ($WORK)" > "$WORK/DONE"
echo "run-predict-statistics-results: DONE ($WORK)"
