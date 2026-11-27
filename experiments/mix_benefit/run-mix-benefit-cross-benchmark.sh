#!/usr/bin/env bash
# run-mix-benefit-cross-benchmark.sh -- every table, figure and check of the cross-benchmark layout-choice results
# (owner: experiments/mix_benefit). Analysis only: opens no store, measures nothing. The captures it reads: the nine
# uniform cells at SF3 (--grid, a captures/ldbc/sf3 dir holding BI's capture too, or BI's from --bi DIR), BI's
# bi_picks.csv (--bi-picks), LinkBench n1m's nine cells (--linkbench, a captures/linkbench/n1m dir, its whole log) and
# IMDb / JOB's job_picks.csv with its capture (--job, --job-capture; without them IMDb / JOB is passed as --pending: the
# tables leave it out, and the doc's numbers, which cover all four benchmarks, are not checked, so --doc needs --job).
#   1. solves   (each one reused when its output exists; --only solves reruns them) workload_story.py, I01's breakdown
#               (11 min); joint_search.py, one mix for W01-W15, I01, I02 (7 min), then a stop unless the equal-bytes fronts in --out
#               price the nine as the joint solve does (one plan rule per figure); kvcost search-mix --cost-model wops_bytes4k, the fewest ops +
#               bytes / 4 KB mix per workload and option set, read from the grid's tree (34 rows in <out> as an output
#               tree: <out>/summary/ldbc/sf3/mix-search/<workload>/)
#   2. tables   per_query_disagreement.py (the per-query costs with their raw channels), cross_benchmark_regret.py,
#               cross_benchmark_frequencies.py (its own check against the regret table), random_mix_robustness.py
#   3. plots    the seven figures, into <out>/plots
#   4. checks   check_headline_weighting.py, check_cross_benchmark_numbers.py (with --doc: the doc's numbers too)
# The equal-bytes fronts and the one mix for the dataset are run beside the re-measure (their commands: <out>/README.md).
# Output (--out, a working dir): summary/ldbc/sf3/layout-choice/ (workload_story.py --root), summary/ldbc/sf3/mix-search/,
#   joint-mix/, per-query-disagreement/,
#   cross-benchmark-frequencies/, random-mix/, plots/, logs/.
#   experiments/mix_benefit/run-mix-benefit-cross-benchmark.sh --grid DIR [--bi DIR] --bi-picks CSV --linkbench DIR --out DIR
#       [--job CSV --job-capture DIR] [--doc MD] [--source TEXT] [--only solves|tables|plots|checks]
# e.g. the paper's tables (the solves copied into the working dir first; BI composed to nine from its three measured
#      cells by kvcost.layout_cost.compose_cells --families bi, job_picks.csv by experiments/meta/bi_cells_table.py
#      --family job --sf full), each dir in the output tree's shape:
#      --grid <captures/ldbc/sf3> --bi <composed/ldbc/sf3> --bi-picks <bi_picks.csv> --linkbench <captures/linkbench/n1m>
#      --job <job_picks.csv> --job-capture <captures/imdb-rows/full/job/reported> --out <working dir> --only tables
# --out is a working dir: never a snapshot (the tables and plots are written into it).
# Every step prints its rc; the first failure stops the run.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
export PYTHONDONTWRITEBYTECODE=1
GRID=; BI=; BI_PICKS=; LB=; OUT=; JOB=; JOB_CAPTURE=; DOC=; SRC=; ONLY=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --grid) GRID="${2:?--grid needs a directory}"; shift 2 ;;
    --bi) BI="${2:?--bi needs a directory}"; shift 2 ;;   # BI's grid, when --grid holds none
    --bi-picks) BI_PICKS="${2:?--bi-picks needs a bi_picks.csv}"; shift 2 ;;
    --linkbench) LB="${2:?--linkbench needs a directory}"; shift 2 ;;
    --out) OUT="${2:?--out needs a directory}"; shift 2 ;;
    --job) JOB="${2:?--job needs a job_picks.csv}"; shift 2 ;;
    --job-capture) JOB_CAPTURE="${2:?--job-capture needs a directory}"; shift 2 ;;
    --doc) DOC="${2:?--doc needs the doc}"; shift 2 ;;
    --source) SRC="${2:?--source needs a text}"; shift 2 ;;
    --only) ONLY="${2:?--only needs a stage}"; shift 2 ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
capture_of() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import capture_in; print(capture_in(*sys.argv[1:]).path)' "$@"; }
[[ -n "$GRID" && -n "$LB" && -n "$OUT" && -f "$BI_PICKS" ]] || { echo "usage: --grid DIR [--bi DIR] --bi-picks CSV --linkbench DIR --out DIR (see -h)" >&2; exit 1; }
BI_CAPTURE=$(capture_of "${BI:-$GRID}" bi)
[[ -d "$BI_CAPTURE" ]] || { echo "no BI capture at $BI_CAPTURE" >&2; exit 1; }
[[ -z "$JOB" || ( -f "$JOB" && -d "$JOB_CAPTURE" ) ]] || { echo "--job needs a file and --job-capture its capture dir" >&2; exit 1; }
[[ -n "$JOB" || -z "$DOC" ]] || { echo "--doc needs --job: the doc's numbers cover IMDb / JOB" >&2; exit 1; }
# the captions' data line: the binary that measured, from the grid's run records
BIN=$("$PYTHON" - "$GRID" <<'EOF'
import glob, json, os, sys
from kvevaluator.output_tree import capture_in
b = set()
for p in glob.glob(os.path.join(capture_in(sys.argv[1], "interactive").dir("counts"), "*.json")):
    d = json.load(open(p))["binary"]
    b.add(d["binary_git_sha"][:10] + (" (dirty)" if d["binary_dirty"] else ""))
print(", ".join(sorted(b)) or "unknown")
EOF
)
shown() { local p; p=$(realpath -m "$1"); echo "${p#"$PWD"/}"; }   # from the repo root
SNAP=$(dirname "$(dirname "$(realpath -m "$GRID")")")             # a snapshot holds the grid at <snapshot>/sf3/meta-grid
JOBSRC=${JOB:+"; IMDb / JOB: $(shown "$JOB")"}                   # JOB's counts come from their own grid
if [[ -f "$SNAP/snapshot-meta.json" ]]; then
  SRC=${SRC:-"Data: the snapshot $(shown "$SNAP")$JOBSRC (the counts of binary $BIN). Counts are deterministic: no error bars."}
else
  SRC=${SRC:-"Data: the counts of binary $BIN, $(shown "$OUT")$JOBSRC, not yet a snapshot. Counts are deterministic: no error bars."}
fi
[[ -f "$(realpath -m "$OUT")/snapshot-meta.json" || -f "$(dirname "$(realpath -m "$OUT")")/snapshot-meta.json" ]] \
  && { echo "--out $OUT is (inside) a snapshot: use a working dir" >&2; exit 1; }
if [[ -n "$JOB" ]]; then JOBARG=(--job "$JOB" --job-capture "$JOB_CAPTURE"); PEND=(); else JOBARG=(); PEND=(--pending "IMDb / JOB"); fi
# I01's breakdown, where workload_story.py --root "$OUT" writes it (benchmarks.out_root, benchmarks.workload_dir)
B=$("$PYTHON" -c 'import sys; from kvcost.layout_cost import benchmarks as B
(n, w), = B.select("ldbc", "3", "i01").items()
print(B.workload_dir("ldbc", "3", n, w, B.out_root("ldbc", "3", None, root=sys.argv[1]), criterion=None))' "$OUT")/breakdown.csv
C=experiments/mix_benefit; LC=experiments/layout_choice
mkdir -p "$OUT/logs"
echo "=== cross-benchmark $(date -u +%FT%TZ) tree $(git rev-parse --short=10 HEAD) dirty $(git status --porcelain -uno -- experiments python cpp | wc -l); binary $BIN; JOB ${JOB:-pending}"
step() {   # step <name> <command...>: its log, its wall time, its exit code on its own line; the first failure stops
  local n="$1"; shift; local s=$SECONDS
  "$@" > "$OUT/logs/$n.log" 2>&1
  local rc=$?
  echo "=== rc=$rc $n ($((SECONDS - s)) s) $(date -u +%T): $(tail -n 1 "$OUT/logs/$n.log" | cut -c1-160)"
  [[ $rc -eq 0 ]] || { echo "CHAIN FAIL $n (see $OUT/logs/$n.log)"; exit 1; }
}
want() { [[ -z "$ONLY" || "$ONLY" == "$1" ]]; }
solve() {  # solve <name> <its output> <command...>: reused when its output exists, unless --only solves
  local n="$1" out="$2"; shift 2
  if [[ -e "$out" && "$ONLY" != solves ]]; then echo "=== reused $n: $out"; else step "$n" "$@"; fi
}
sum_rows() {   # the fewest wops + bytes / 4,096 mix of W01-W15, I01, I02, per type and per type and direction: one proven row each
  local w set
  for w in w01 w02 w03 w04 w05 w06 w07 w08 w09 w10 w11 w12 w13 w14 w15 i01 i02; do
    for set in "" --by-direction; do
      in_tree "$OUT" "$PYTHON" -m kvcost.cli.cli search-mix --dataset ldbc=3 --from "$(tree_of "$GRID")" --workload $w \
        --settings Sp,Co,Ch64k $set --cost-model wops_bytes4k || return 1
    done
  done
}
if want solves; then
  solve story "$B" "$PYTHON" $LC/workload_story.py --sf 3 --grid "$GRID" --root "$OUT" --workload i01
  solve joint "$OUT/joint-mix/joint_regret.csv" "$PYTHON" $LC/joint_search.py --sf 3 --grid "$GRID" --root "$OUT/joint-mix"
  # the equal-bytes fronts are made beside the re-measure, not here: their nine must be the joint solve's nine (one
  # plan rule per figure), else stop before the long steps
  [[ -d "$OUT/equal-bytes-front" ]] && step same-nine "$PYTHON" - "$OUT/joint-mix/joint_regret.csv" "$OUT/equal-bytes-front" <<'EOF'
import csv, glob, os, sys
joint = {(r["workload"], r["option"]): r for r in csv.DictReader(open(sys.argv[1])) if r["currency"] == "ops"}
gaps, n = [], 0
for f in sorted(glob.glob(os.path.join(sys.argv[2], "*", "uniform_layouts.csv"))):
    for r in csv.DictReader(open(f)):
        j = joint.get((r["workload"], r["cell"]))
        if j is None:
            sys.exit(f"{sys.argv[1]} has no row for {r['workload']} {r['cell']} of {f}")
        n += 1
        gaps += [(abs(float(j[c]) / float(r[c]) - 1), c, r["workload"], r["cell"], f) for c in ("total_ops", "total_bytes")]
if n == 0:
    sys.exit(f"no uniform_layouts.csv under {sys.argv[2]}")
g = max(gaps)
if g[0] > 1e-9:
    sys.exit(f"the joint solve ({sys.argv[1]}) and the equal-bytes front ({g[4]}) price the nine under different code: "
             f"{g[2]} {g[3]} {g[1]} differs by {g[0]:.1e}. The fronts are not made here: re-make them at this code "
             f"(experiments/layout_choice/run-layout-choice.sh, step 8), or reuse the joint solve made with them")
print(f"{n} (workload, layout) rows: the equal-bytes fronts' nine equal the joint solve's (largest gap {g[0]:.1e})")
EOF
  solve sum "$OUT/summary/ldbc/sf3/mix-search/i02/mixes-of-Sp-Co-Ch64k-by-direction.json" sum_rows
fi
if want tables; then
  step per-query-disagreement "$PYTHON" $C/per_query_disagreement.py --out "$OUT/per-query-disagreement" --ldbc "$B" --bi "$BI_PICKS" --bi-capture "$BI_CAPTURE" --linkbench "$LB" "${JOBARG[@]}"
  step regret "$PYTHON" $LC/cross_benchmark_regret.py --costs "$OUT/per-query-disagreement/costs.csv" --root "$OUT/joint-mix" "${PEND[@]}"
  step frequencies "$PYTHON" $C/cross_benchmark_frequencies.py --root "$OUT" --out "$OUT/cross-benchmark-frequencies" "${PEND[@]}"
  step random-mix "$PYTHON" $C/random_mix_robustness.py --costs "$OUT/per-query-disagreement/costs.csv" --out "$OUT/random-mix" "${PEND[@]}"
fi
if want plots; then
  step plot-regret "$PYTHON" $C/plot_cross_benchmark_regret.py --data "$OUT/cross-benchmark-frequencies/cross_benchmark_frequencies.csv" --out "$OUT/plots" --source "$SRC" "${PEND[@]}"
  step plot-per-query "$PYTHON" $C/plot_per_query_disagreement.py --costs "$OUT/per-query-disagreement/costs.csv" --out "$OUT/plots" "${PEND[@]}"
  step plot-one-mix "$PYTHON" $C/plot_one_mix_per_dataset.py --root "$OUT" --out "$OUT/plots" --source "$SRC"
  step plot-random-mix "$PYTHON" $C/plot_random_mix_robustness.py --data "$OUT/random-mix/random_mix_ratios.csv" --out "$OUT/plots" --source "$SRC" "${PEND[@]}"
  step plot-equal-bytes "$PYTHON" $C/plot_equal_bytes_curve.py --root "$OUT" --out "$OUT/plots" --workloads i01,i02 --source "$SRC" --no-built
fi
if want checks; then
  step check-headline-weighting "$PYTHON" $C/check_headline_weighting.py --costs "$OUT/per-query-disagreement/costs.csv" --regret "$OUT/joint-mix/cross_benchmark_regret.csv" "${PEND[@]}"
  step check-numbers "$PYTHON" $C/check_cross_benchmark_numbers.py --root "$OUT" --ldbc "$B" --bi "$BI_PICKS" --bi-capture "$BI_CAPTURE" --linkbench "$LB" "${JOBARG[@]}" "${PEND[@]}" ${DOC:+--doc "$DOC"}
fi
echo "CHAIN DONE $(date -u +%FT%TZ)"
