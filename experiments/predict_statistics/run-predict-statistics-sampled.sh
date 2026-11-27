#!/usr/bin/env bash
# run-predict-statistics-sampled.sh -- can the layout statistics come from a sample of owners? The seven diagonal
# tallies from P% of the owners (kvevaluator tally --owner-sample P --seed S), the 49 assembled from them, predict
# mode on the 49, and both compared with the full tallies' (kvcost predict --dataset ldbc=<SF> --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k
# --tier 2, run first at the same scale: its statistics, 49 tallies and predicted captures are read, never re-made). Owner: the predict_statistics study.
# Opens no store. Stages (in order; each prints its wall; a failure stops the chain):
#   control  --owner-sample 100 on --control-cells: must equal the full tally (wall time and provenance aside)
#   tallies  per (percent, seed): the seven diagonals (--jobs at once) and the 49 assembled (sampled.py tallies); with
#            --floors, each (percent, seed) also with each floor (tally --owner-floor F: at least F owners per vertex type)
#   predict  per (percent, seed): kvcost predict on the 49 (every legal body of I02's queries, as the full run)
#   compare  sampled.py errors (each row against the full tally) and picks (the layouts named against the full ones)
# Output: output/summary/ldbc/sf<SF>/predict-statistics/sampled/: p100-control/, p<P>-s<S>/{tally-diagonal,specs,tally,
#   predicted,predict-work}/ (and p<P>-floor<F>-s<S>/ with --floors), tally_errors.csv, tally_errors_summary.csv, picks_vs_full.csv, runs.csv, DONE. The measured side:
#   python -m kvcost.predict_accuracy.layouts --dataset ldbc=3 --predicted <out>/p<P>-s<S>/predicted/captures/ldbc/sf3/interactive/reported \
#       --grid output/captures/ldbc/sf3 --cost-workload i02 --check-workload i01 \
#       --check-reference <uniform_layouts_by_size_measured.csv> --out <out>/p<P>-s<S>/report
#   experiments/predict_statistics/run-predict-statistics-sampled.sh --sf 3 --percents 1,10 --seeds 1,2,3
# Flags: --sf (default 3); --percents a,b (default 1,10); --seeds a,b (default 1,2,3); --control-cells all|a,b (default
#        the three of the predict study: Split, Condensed, Chunked 64 KB); --floors a,b (default none: each
#        (percent, seed) also with these owner floors, 100 = "1% + floor"); --jobs N (tallies at once, default 7; a
#        sampled tally holds little, a --owner-sample 100 one twice a full tally's memory, ~10 GB at SF3);
#        --stages a,b (default all four); --allow-dirty (a no-op: a dirty binary is warned and marked binary_unclean in
#        <out>/stale.jsonl); -h.
# The paper's workloads (--workloads i01,b01,j01; --sf 3): I01 and B01 at LDBC SF3, J01 at full IMDb, on the 49, from
#   three kinds of tallies (--variants): full (the reference snapshot's tally libraries, read in place), no-locality (the
#   full tallies with their locality rows removed: predict's landing estimate then takes the uniform spread,
#   cpp/kvevaluator/src/predict/landing.cc) and sampled (each --percents x --seeds x --floors). Stages, in order:
#   tallies (LDBC and IMDb), control (--owner-sample 100 against the full tally, one diagonal per dataset:
#   --control-cells), no-locality, full-bound, predict (I01 tier 1: interactive + writes; B01 and J01 tiers 1 and 2; each with
#   --tally-dir <variant>/tally: no fallback, a missing tally refuses), report (cost-layouts, kvcost.predict_accuracy.workload_pick for I01,
#   the layout report (python -m kvcost.predict_accuracy.layouts) for B01 and J01, on the composed grids), provenance (sampled.py check-provenance: every
#   capture read a sampled tally of its own sample), derive (paper/derive/predict_49.py, unchanged, on each variant's
#   tree, tier 1 and tier 2), picks (sampled.py paper-picks, against the full variant), errors (sampled.py errors, both
#   datasets). Output: output/summary/ldbc/sf3/predict-statistics/sampled/<variant>/{tally,i01-tier1,bi-b01,...} and
#   output/summary/imdb-rows/full/predict-statistics/sampled/<variant>/{tally,job-j01,...}; the trees the derive reads
#   and picks_by_workload.{csv,md} in output/summary/ldbc/sf3/predict-statistics/sampled/paper/.
#   experiments/predict_statistics/run-predict-statistics-sampled.sh --sf 3 --workloads i01,b01,j01 --percents 1 \
#       --floors-only 100 --seeds 1
#   full-bound-<sample> (the control for the sample's last chunk): the full tallies with only last_chunk_value_bytes
#   swapped for that sample's assumed one (sampled.py full-bound); I01's writes on it give its effect apart from the
#   sample's; its J01 is the full variant's (J01 reads no last chunk; run --variants full first).
#   The statistics are the full ones in every variant, read in place from --statistics.
# Paper flags: --workloads i02 (default: the I02 study above) | a,b of i01, b01, j01; --variants a,b (default
#   full,no-locality,sampled,full-bound); --floors-only a,b (the sampled variants with these floors only, no floor-0 sample);
#   --reference <dir> (required: a prediction run's dir holding its tally libraries, specs and the measured 49);
#   --statistics <dir> (required: a run's statistics/ and job-j01/statistics/); --bi-captures, --job-captures <dir> (required: the
#   three measured diagonals at SF3, each a dir holding grid/).
# Expected run time: README.md "Expected run time".
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)     # before env.sh, which changes into the repo root
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
ROOT=$(cd "$HERE/../.." && pwd)
# spec_files <dir> <cell,cell,...>: each cell's spec file in the dir, one per line (kvcost predict takes them as its
# layouts, each named by its cell id)
spec_files() { local c; for c in ${2//,/ }; do printf '%s\n' "$1/$c.json"; done; }
# cost_of <tree> <workload id> <set>: where kvcost cost-layouts --count-source <set> --cost-model wops_bytes4k wrote in the output
# tree <tree> at LDBC SF3 (kvcost.cli.runs.cost_dir)
cost_of() { "$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale 3 --root "$1" --analysis layout-costs \
              --subject "$2/$3"; }   # cost-layouts' place: layouts.csv, query_costs.csv

SF=3; PERCENTS=1,10; SEEDS=1,2,3; FLOORS=""; JOBS=7; STAGES=""
WORKLOADS=i02; VARIANTS=full,no-locality,sampled,full-bound; FLOORS_ONLY=""; REFERENCE=""; STATISTICS=""; BI_CAPTURES=""; JOB_CAPTURES=""
CONTROL_CELLS=props_split_adj_split,props_condensed_adj_condensed,props_chunked_adj_chunked; CONTROL_SET=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf) SF="${2:?}"; shift 2 ;;
    --percents) PERCENTS="${2:?}"; shift 2 ;;
    --seeds) SEEDS="${2:?}"; shift 2 ;;
    --floors) FLOORS="${2:?}"; shift 2 ;;
    --control-cells) CONTROL_CELLS="${2:?}"; CONTROL_SET=1; shift 2 ;;
    --jobs) JOBS="${2:?}"; shift 2 ;;
    --workloads) WORKLOADS="${2:?}"; shift 2 ;;
    --variants) VARIANTS="${2:?}"; shift 2 ;;
    --floors-only) FLOORS_ONLY="${2:?}"; shift 2 ;;
    --reference) REFERENCE="${2:?}"; shift 2 ;;
    --statistics) STATISTICS="${2:?}"; shift 2 ;;
    --bi-captures) BI_CAPTURES="${2:?}"; shift 2 ;;      # BI's three measured diagonals at SF3 (a dir holding grid/)
    --job-captures) JOB_CAPTURES="${2:?}"; shift 2 ;;    # JOB's (a dir holding grid/)
    --stages) STAGES="${2:?}"; shift 2 ;;
    --allow-dirty) shift ;;   # a no-op: a dirty binary is warned and marked binary_unclean
    -h|--help) sed -n '2,/^set -euo/p' "$HERE/$(basename "$0")" | sed '$d'; exit 0 ;;
    *) echo "run-predict-statistics-sampled: unknown flag $1" >&2; exit 2 ;;
  esac
done
if [[ $WORKLOADS != i02 ]]; then   # the paper's workloads: this block runs alone and exits
  STAGES=${STAGES:-tallies,control,no-locality,full-bound,predict,report,provenance,derive,picks,errors}
  [[ $SF == 3 ]] || { echo "run-predict-statistics-sampled: --workloads $WORKLOADS reads the SF3 reference: --sf 3" >&2; exit 2; }
  [[ $CONTROL_SET ]] || CONTROL_CELLS=props_split_adj_split
  [[ -n $REFERENCE && -n $STATISTICS && -n $BI_CAPTURES && -n $JOB_CAPTURES ]] || { echo "run-predict-statistics-sampled: --workloads $WORKLOADS needs --reference, --statistics, --bi-captures and --job-captures (inputs the artifact does not ship)" >&2; exit 2; }
  FULL_L=$REFERENCE/bi-b01/tally-library/tally; FULL_I=$REFERENCE/job-j01/tally-library/tally
  SPECS=$REFERENCE/bi-b01/specs
  # the statistics, read in place from the snapshot the paper's Figure 15 read (every variant: only tallies vary)
  ST_L=$STATISTICS/statistics; ST_I=$STATISTICS/job-j01/statistics
  for x in "$FULL_L" "$FULL_I" "$SPECS" "$ST_L" "$ST_I"; do [[ -d $x ]] || { echo "run-predict-statistics-sampled: no $x (in --reference / --statistics)" >&2; exit 1; }; done
  BIN=$("$CPP_BIN" --version | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(d["binary_git_sha"], "dirty=" + str(d["binary_dirty"]))')
  echo "run-predict-statistics-sampled: binary $CPP_BIN $BIN; tree $(git -C "$ROOT" rev-parse --short HEAD); workloads $WORKLOADS; variants $VARIANTS; reference $REFERENCE"
  LDIR=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale 3 --analysis predict-statistics --subject sampled) || exit 1
  IDIR=$("$PYTHON" -m kvevaluator.output_tree summary --dataset imdb --scale full --analysis predict-statistics --subject sampled) || exit 1
  SCHEMA_L=$("$PYTHON" -m kvevaluator.datasets schema-path ldbc); SCHEMA_I=$("$PYTHON" -m kvevaluator.datasets schema-path imdb)
  # the record streams: this machine's (kvevaluator.measure.records.records_file), checked against the stream the full
  # tallies name by content sha256 (their path is the machine that made them): the sample and the reference read the
  # same records
  records_of() { "$PYTHON" - "$1" "$2" "$3" <<'PY'
import glob, json, sys
from kvevaluator.measure.records import records_file
path = str(records_file(sys.argv[2], sys.argv[3]))
t = json.load(open(sorted(glob.glob(sys.argv[1] + "/*.json"))[0]))["records"]
k = json.load(open(path + ".key.json"))
if k.get("content_sha256") != t.get("content_sha256"):
    sys.exit(f"the stream {path} is not the one the full tallies read (content sha256 {k.get('content_sha256')} vs {t.get('content_sha256')})")
print(path)
PY
  }
  REC_L=$(records_of "$FULL_L" ldbc 3) || exit 1
  REC_I=$(records_of "$FULL_I" imdb full) || exit 1
  mkdir -p "$LDIR/paper" "$IDIR"
  for d in "$LDIR" "$IDIR"; do "$PYTHON" -m kvevaluator.stale start --into "$d" || exit 1; done
  [[ "$BIN" == *"dirty=False" ]] || for d in "$LDIR" "$IDIR"; do
    "$PYTHON" -m kvevaluator.stale note --into "$d" binary_unclean "the binary ${BIN%% *} is dirty (${BIN##* })" || exit 1; done
  RUNS="$LDIR/paper/runs.csv"
  [[ -f "$RUNS" ]] || echo "stage,sample,wall_s,binary" > "$RUNS"
  has() { [[ ",$STAGES," == *",$1,"* ]]; }
  wl() { [[ ",$WORKLOADS," == *",$1,"* ]]; }
  t_all=$SECONDS
  step() { local t0=$SECONDS name="$1" sample="$2"; shift 2; echo "run-predict-statistics-sampled: $name $sample"
           "$@" || { echo "run-predict-statistics-sampled: FAILED $name $sample" >&2; exit 1; }
           echo "run-predict-statistics-sampled:   $name $sample done in $((SECONDS - t0)) s"; echo "$name,$sample,$((SECONDS - t0)),${BIN%% *}" >> "$RUNS"; }
  # the variants: name, its LDBC dir, its IMDb dir, the LDBC and IMDb tally dirs predict reads (never another)
  names=()
  for v in ${VARIANTS//,/ }; do
    case $v in
      full|no-locality) names+=("$v") ;;
      sampled|full-bound) ;;
      *) echo "run-predict-statistics-sampled: unknown variant $v (full, no-locality, sampled, full-bound)" >&2; exit 2 ;;
    esac
  done
  smp=()   # the samples (named as uniform_49_tallies.sample_name: the percent as %g)
  for p in ${PERCENTS//,/ }; do p=$(printf '%g' "$p"); for s in ${SEEDS//,/ }; do
    if [[ -z $FLOORS_ONLY ]]; then smp+=("p$p-s$s"); fi
    for f in ${FLOORS_ONLY//,/ }; do smp+=("p$p-floor$f-s$s"); done; done; done
  # full-bound-<sample>: the full tallies with only the last-chunk bytes swapped for that sample's assumed ones (their
  # effect on I01's writes, apart from the sample's); its J01 is the full variant's (J01 reads no last chunk)
  [[ ",$VARIANTS," == *",sampled,"* || ",$VARIANTS," == *",full-bound,"* ]] && names+=("${smp[@]}")
  [[ ",$VARIANTS," == *",full-bound,"* ]] && for n in "${smp[@]}"; do names+=("full-bound-$n"); done
  tally_l() { case $1 in full) echo "$FULL_L" ;; *) echo "$LDIR/$1/tally" ;; esac; }
  tally_i() { case $1 in full) echo "$FULL_I" ;; *) echo "$IDIR/$1/tally" ;; esac; }
  sampled_names() { for n in "${names[@]}"; do [[ $n == p* ]] && echo "$n"; done; }
  pfs() { local n=$1 p s f=0; p=${n#p}; p=${p%%-*}; s=${n##*-s}; [[ $n == *floor* ]] && { f=${n#*floor}; f=${f%%-*}; }; echo "$p $s $f"; }
  CELLS=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies cells all | paste -sd,)
  # each tier's predicted set (kvcost.cli.predict.TIER_SETS), beside the statistics file of that tier
  T1=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies tier-set 1) || exit 1
  T2=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies tier-set 2) || exit 1
  W16_FILE=inputs/workloads/ldbc/i01.json
  fam_queries() { "$PYTHON" -c 'import json,sys; print(",".join(i["query"] for i in json.load(open(sys.argv[1]))["items"] if i["family"] == sys.argv[2]))' "$1" "$2"; }
  capture() { "$PYTHON" -c 'import sys; from kvcost.cli.predict import capture_dirs; print(capture_dirs(sys.argv[1], sys.argv[2], sys.argv[3:4], sys.argv[4])[0])' "$@"; }
  BIQ=bi1,bi2,bi3,bi4,bi5,bi6,bi7,bi8,bi9,bi10,bi11,bi12,bi13,bi14,bi16,bi17,bi18   # B01's 17 (the reference's)
  JQ=$(fam_queries inputs/workloads/imdb/j01.json job)

  if has tallies; then
    for n in $(sampled_names); do   # the samples a variant reads (sampled, or full-bound's)
      read -r p s f < <(pfs "$n")
      if wl i01 || wl b01; then step tallies "ldbc $n" "$PYTHON" "$HERE/sampled.py" tallies --dataset ldbc --records "$REC_L" \
        --out "$LDIR" --percent "$p" --seed "$s" --floor "$f" --jobs "$JOBS"; fi
      if wl j01; then step tallies "imdb $n" "$PYTHON" "$HERE/sampled.py" tallies --dataset imdb --records "$REC_I" \
        --out "$IDIR" --percent "$p" --seed "$s" --floor "$f" --jobs "$JOBS"; fi
    done
  fi
  if has control; then   # --owner-sample 100 equals the full tally, one diagonal per dataset
    if wl i01 || wl b01; then step control "ldbc p100" "$PYTHON" "$HERE/sampled.py" control --dataset ldbc --records "$REC_L" \
      --full-tallies "$FULL_L" --out "$LDIR" --cells "$CONTROL_CELLS" --jobs 1; fi
    if wl j01; then step control "imdb p100" "$PYTHON" "$HERE/sampled.py" control --dataset imdb --records "$REC_I" \
      --full-tallies "$FULL_I" --out "$IDIR" --cells "$CONTROL_CELLS" --jobs 1; fi
  fi
  if has full-bound && (wl i01 || wl b01); then
    for n in "${names[@]}"; do [[ $n == full-bound-* ]] || continue
      step full-bound "$n" "$PYTHON" "$HERE/sampled.py" full-bound --full-tallies "$FULL_L" --sample "$LDIR/${n#full-bound-}" --out "$LDIR"
    done
  fi
  if has no-locality && [[ " ${names[*]} " == *" no-locality "* ]]; then
    if wl i01 || wl b01; then step no-locality ldbc "$PYTHON" "$HERE/sampled.py" no-locality --full-tallies "$FULL_L" --out "$LDIR"; fi
    if wl j01; then step no-locality imdb "$PYTHON" "$HERE/sampled.py" no-locality --full-tallies "$FULL_I" --out "$IDIR"; fi
  fi
  predict() {   # predict <sf> <family> <queries> <tally dir> <stats> <nparam> <set> <root> [--schema-file S]
    local sf=$1 fam=$2 q=$3 td=$4 st=$5 np=$6 set=$7 root=$8 ds; shift 8
    [[ -d $td ]] || { echo "run-predict-statistics-sampled: no tally dir $td: refused (no fallback)" >&2; return 1; }
    ds=$("$PYTHON" -c 'import sys; from kvevaluator import workloads; print(workloads.get(sys.argv[1]).dataset)' "$fam")
    in_tree "$root" "$PYTHON" -m kvcost.cli.cli predict --dataset "$ds=$sf" --family "$fam" --queries "$q" --tally-dir "$td" \
      $(spec_files "$SPECS" "$CELLS") --stats-file "$st" --nparam "$np" --save-as "$set" \
      --work-dir "$root/work-$fam-$set" --jobs "$JOBS" --fresh "$@"
  }
  compose_grid() {   # compose_grid <compose_cells flags> <measured captures> <out captures>: the nine from the three
    # measured (no --check: SF3 measured no off-diagonal cell), the chunk sizes' captures copied beside
    mkdir -p "$3"
    "$PYTHON" -m kvcost.layout_cost.compose_cells $1 --whole-grid --grid "$2/grid" --out "$3/grid" || return 1
    for k in 1k 4k 16k 256k; do [[ -d $3/grid_chunked_$k ]] || command cp -r "$2/grid_chunked_$k" "$3/" || return 1; done
  }
  derive_variant() {   # derive_variant <name> <LDBC dir> <IMDb dir>: the variant's tree (i01-tier1, bi-b01, job-j01:
    # sampled.py paper-picks reads it) and predict_49.py run on its dirs, its tables in <tree>/derived/tier<T>; tier 2
    # reads the tier-2 reports ($T2/), I01's rows are tier 1's (I01 has no tier 2; not read)
    local n=$1 lv=$2 iv=$3 tree=$LDIR/paper/$1 t d rep
    mkdir -p "$tree"
    ln -sfn "$lv/i01-tier1" "$tree/i01-tier1"; ln -sfn "$lv/bi-b01" "$tree/bi-b01"; ln -sfn "$iv/job-j01" "$tree/job-j01"
    for t in 1 2; do
      d=$LDIR/paper/.derive/$n-tier$t; rep=$([[ $t == 1 ]] && echo "$T1" || echo "$T2")
      mkdir -p "$d" "$tree/derived/tier$t"
      KVEVAL_OUTPUT_DIR="$d/out" "$PYTHON" "$ROOT/paper/derive/predict_49.py" --i01 "$lv/i01-tier1/pick" \
        --b01 "$lv/bi-b01/reports/$rep/layout_choice" --j01 "$iv/job-j01/reports/$rep/layout_choice" || return 1
      command cp -f "$d/out/paper/build/data/"predict-49-*.csv "$tree/derived/tier$t/" || return 1
    done
  }
  for n in "${names[@]}"; do
    LV=$LDIR/$n; IV=$IDIR/$n; J=1
    [[ $n == full-bound-* ]] && { IV=$IDIR/full; J=0; }   # its J01: the full variant's (made by --variants full)
    if has predict; then
      if wl i01; then
        step predict "i01 $n" predict 3 interactive "$(fam_queries $W16_FILE interactive)" "$(tally_l "$n")" "$ST_L/stats-tier1.json" 5 "$T1" "$LV/i01-root" --schema-file "$SCHEMA_L"
        step predict "i01 writes $n" predict 3 writes "$(fam_queries $W16_FILE writes)" "$(tally_l "$n")" "$ST_L/stats-tier1.json" 5 "$T1" "$LV/i01-root" --schema-file "$SCHEMA_L"
      fi
      if wl b01; then
        step predict "b01 t1 $n" predict 3 bi "$BIQ" "$(tally_l "$n")" "$ST_L/stats-tier1.json" 5 "$T1" "$LV/bi-root" --schema-file "$SCHEMA_L"
        step predict "b01 t2 $n" predict 3 bi "$BIQ" "$(tally_l "$n")" "$ST_L/stats.json" 5 "$T2" "$LV/bi-root" --schema-file "$SCHEMA_L"
      fi
      if wl j01 && [[ $J == 1 ]]; then
        step predict "j01 t1 $n" predict full job "$JQ" "$(tally_i "$n")" "$ST_I/stats-tier1.json" 1 "$T1" "$IV/job-root"
        step predict "j01 t2 $n" predict full job "$JQ" "$(tally_i "$n")" "$ST_I/stats.json" 1 "$T2" "$IV/job-root"
      fi
    fi
    if has report; then
      if wl i01; then
        step report "i01 cost $n" in_tree "$LV/i01-tier1/cost" "$PYTHON" -m kvcost.cli.cli cost-layouts --dataset ldbc=3 \
          --workload "$W16_FILE" --cost-model wops_bytes4k --count-source "$T1" --from "$LV/i01-root"
        step report "i01 pick $n" "$PYTHON" -m kvcost.predict_accuracy.workload_pick \
          --predicted "$(cost_of "$LV/i01-tier1/cost" i01 "$T1")" \
          --measured "$REFERENCE/i01-measured-49" --out "$LV/i01-tier1/pick"
      fi
      if wl b01; then
        GRID=$LDIR/inputs/grid-bi/captures
        [[ -d $GRID/grid ]] || step report "compose the BI grid" compose_grid "--dataset ldbc --families bi" "$BI_CAPTURES" "$GRID"
        for set in "$T1" "$T2"; do
          step report "b01 cost $set $n" in_tree "$LV/bi-b01/cost" "$PYTHON" -m kvcost.cli.cli cost-layouts --dataset ldbc=3 \
            --workload b01 --cost-model wops_bytes4k --count-source $set --from "$LV/bi-root"
          step report "b01 report $set $n" "$PYTHON" -m kvcost.predict_accuracy.layouts --dataset ldbc=3 \
            --predicted "$(capture 3 bi $set "$LV/bi-root")" --grid "$GRID/grid" --cost-workload b01 --check-workload b01 \
            --check-reference "$REFERENCE/bi-b01/measured-49" --param-rows 1-5 \
            --expect-layouts 49 --out "$LV/bi-b01/reports/$set"
        done
      fi
      if wl j01 && [[ $J == 1 ]]; then
        GRID=$IDIR/inputs/grid-job/captures
        [[ -d $GRID/grid ]] || step report "compose the JOB grid" compose_grid "--dataset imdb" "$JOB_CAPTURES" "$GRID"
        for set in "$T1" "$T2"; do
          step report "j01 cost $set $n" in_tree "$IV/job-j01/cost" "$PYTHON" -m kvcost.cli.cli cost-layouts --dataset imdb=full \
            --workload j01 --cost-model wops_bytes4k --count-source $set --from "$IV/job-root"
          step report "j01 report $set $n" "$PYTHON" -m kvcost.predict_accuracy.layouts --dataset imdb=full \
            --predicted "$(capture full job $set "$IV/job-root")" --grid "$GRID/grid" --cost-workload j01 --check-workload j01 \
            --check-reference "$REFERENCE/job-j01/measured-49" --param-rows 1-1 \
            --expect-layouts 49 --out "$IV/job-j01/reports/$set"
        done
      fi
    fi
    if has provenance && [[ $n == p* ]]; then
      read -r p s f < <(pfs "$n")
      # the captures expected: 49 layouts x (I01: interactive + writes; B01: two tiers) and J01's two tiers
      ncap=$(( 49 * ( $(wl i01 && echo 2 || echo 0) + $(wl b01 && echo 2 || echo 0) ) ))
      if wl i01 || wl b01; then step provenance "ldbc $n" "$PYTHON" "$HERE/sampled.py" check-provenance --variant "$LV" \
        --full-tallies "$FULL_L,$FULL_I" --percent "$p" --seed "$s" --floor "$f" --expect "$ncap" --out "$LV"; fi
      if wl j01; then step provenance "imdb $n" "$PYTHON" "$HERE/sampled.py" check-provenance --variant "$IV" \
        --full-tallies "$FULL_L,$FULL_I" --percent "$p" --seed "$s" --floor "$f" --expect 98 --out "$IV"; fi
    fi
    if has derive && wl i01 && wl b01 && wl j01; then
      step derive "$n" derive_variant "$n" "$LV" "$IV"
    fi
  done
  if has picks && wl i01 && wl b01 && wl j01; then
    step picks all "$PYTHON" "$HERE/sampled.py" paper-picks --out "$LDIR/paper" --reference full \
      --variants "$(for n in "${names[@]}"; do printf '%s=%s,' "$n" "$LDIR/paper/$n"; done | sed 's/,$//')"
  fi
  if has errors; then
    if wl i01 || wl b01; then step errors ldbc "$PYTHON" "$HERE/sampled.py" errors --full-tallies "$FULL_L" --out "$LDIR"; fi
    if wl j01; then step errors imdb "$PYTHON" "$HERE/sampled.py" errors --full-tallies "$FULL_I" --out "$IDIR"; fi
  fi
  echo "run-predict-statistics-sampled: DONE paper $WORKLOADS ${names[*]} in $((SECONDS - t_all)) s ($LDIR/paper)" | tee "$LDIR/paper/DONE"
  exit 0
fi
STAGES=${STAGES:-control,tallies,predict,compare}
BIN=$("$CPP_BIN" --version | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(d["binary_git_sha"], "dirty=" + str(d["binary_dirty"]))')
echo "run-predict-statistics-sampled: binary $CPP_BIN $BIN; tree $(git -C "$ROOT" rev-parse --short HEAD)"
summary() { "$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis predict-statistics --subject "$1"; }
T2=$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies tier-set 2) || exit 1   # the full run's set: tier 2's (--stats-file)
FULL=$OUTPUT_DIR                     # the full run: kvcost predict Sp-Sp Sp-Co ... Ch256k-Ch256k's places in this output tree
OUT=$(summary sampled) || exit 1
STATS=$("$PYTHON" -c 'import sys; from kvevaluator.predict.build_stats import stats_file; print(stats_file("ldbc", sys.argv[1]))' "$SF") || exit 1
SCHEMA=$("$PYTHON" -m kvevaluator.datasets schema-path ldbc) || exit 1
U49=$("$PYTHON" -m kvevaluator.output_tree stats --dataset ldbc --scale "$SF")/uniform-49
[[ -d "$U49/tally" ]] || { echo "run-predict-statistics-sampled: no 49 tallies at $U49 (kvcost predict --dataset ldbc=$SF --workload i02 <the 49 layouts> --tier 2, as run/reproduce.py runs it)" >&2; exit 1; }
# this machine's record stream (a tally names its stream without the machine's dirs); the control stage checks that the
# full tallies were counted on it, by content
RECORDS=$("$PYTHON" -c 'import sys; from kvevaluator.measure.records import records_file; print(records_file("ldbc", sys.argv[1]))' "$SF")
mkdir -p "$OUT"
"$PYTHON" -m kvevaluator.stale start --into "$OUT" || exit 1   # an earlier run's notes moved aside (kvevaluator.stale)
# a dirty binary is not a refusal: warned and marked binary_unclean in <out>/stale.jsonl
[[ "$BIN" == *"dirty=False" ]] || "$PYTHON" -m kvevaluator.stale note --into "$OUT" binary_unclean "the binary ${BIN%% *} is dirty (${BIN##* })" || exit 1
RUNS="$OUT/runs.csv"
[[ -f "$RUNS" ]] || echo "stage,sample,wall_s,binary" > "$RUNS"
echo "run-predict-statistics-sampled: records $RECORDS; full run $FULL; out $OUT"
has() { [[ ",$STAGES," == *",$1,"* ]]; }
t_all=$SECONDS
step() { local t0=$SECONDS name="$1" sample="$2"; shift 2; echo "run-predict-statistics-sampled: $name $sample"; "$@";
         echo "run-predict-statistics-sampled:   $name $sample done in $((SECONDS - t0)) s"; echo "$name,$sample,$((SECONDS - t0)),${BIN%% *}" >> "$RUNS"; }
samples=()   # percent:seed:floor (floor 0: none)
for p in ${PERCENTS//,/ }; do for s in ${SEEDS//,/ }; do
  samples+=("$p:$s:0")
  for f in ${FLOORS//,/ }; do samples+=("$p:$s:$f"); done
done; done
# a sample's dir name, as uniform_49_tallies.sample_name: p<P>-s<S>, or p<P>-floor<F>-s<S>
sample_name() { local p s f; IFS=: read -r p s f <<< "$1"; if [[ $f == 0 ]]; then echo "p$p-s$s"; else echo "p$p-floor$f-s$s"; fi; }

if has control; then
  step control p100 "$PYTHON" "$HERE/sampled.py" control --sf "$SF" --records "$RECORDS" --full "$FULL" --out "$OUT" \
    --cells "$CONTROL_CELLS" --jobs 3
fi
if has tallies; then
  for ps in "${samples[@]}"; do
    IFS=: read -r p s f <<< "$ps"
    step tallies "$(sample_name "$ps")" "$PYTHON" "$HERE/sampled.py" tallies --sf "$SF" --records "$RECORDS" --out "$OUT" \
      --percent "$p" --seed "$s" --floor "$f" --jobs "$JOBS"
  done
fi
if has predict; then
  I02=ic1,ic2,ic3,ic4,ic5,ic6,ic7,ic8,ic9,ic10,ic11,ic12,is1,is2,is3,is4,is5,is6,is7   # I01's reads
  predict_sample() {   # every layout of one sample, $JOBS at once (kvcost predict, every legal body, as the full run)
    local d="$1"
    in_tree "$d" "$PYTHON" -m kvcost.cli.cli predict --dataset "ldbc=$SF" --family interactive --queries "$I02" \
      $(spec_files "$d/specs" "$("$PYTHON" -m kvcost.predict_accuracy.uniform_49_tallies cells all | paste -sd,)") \
      --tally-dir "$d/tally" --stats-file "$STATS" --schema-file "$SCHEMA" --work-dir "$d/predict-work" --jobs "$JOBS"
  }
  for ps in "${samples[@]}"; do
    step predict "$(sample_name "$ps")" predict_sample "$OUT/$(sample_name "$ps")"
  done
fi
if has compare; then
  step compare all bash -c '"$1" "$2/sampled.py" errors --full "$3" --sf "$5" --out "$4" && "$1" "$2/sampled.py" picks --full "$3" --out "$4" --sf "$5" --set "$6"' \
    _ "$PYTHON" "$HERE" "$FULL" "$OUT" "$SF" "$T2"
fi
echo "run-predict-statistics-sampled: DONE sf$SF in $((SECONDS - t_all)) s ($OUT)" | tee "$OUT/DONE"
