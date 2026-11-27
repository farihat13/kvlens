#!/usr/bin/env bash
# run-bi-cells.sh -- LDBC BI on the nine uniform cells of the configurable format: counts, then latency (steady, cold).
# Owner: the meta grid study (experiments/meta). BI is priced IN-SAMPLE on its 5 params (its param files have no
# held-out rows: picks.IN_SAMPLE_WORKLOADS); every output of the grid tools says so.
# Stages (one at a time, --stage; each prints its wall; a failure stops the chain):
#   counts   per cell ONE `kvcost measure --family bi <cell> --no-check` (one grid_bodies process and one server per
#            cell, closed after it; measure marks the cell's record by its binary rule), its servers through
#            cpp/kvevaluator/run-interleaved.sh (numactl --interleave=all + a virtual-memory ceiling: this script wraps
#            measure, KVEVAL_CPP_BIN), with the servers' peak RSS sampled every 5 s by run/watch-memory.sh (beside the
#            log, peak/<cell>/total.peak) and its STOP rule (--stop-rss-gb: the run is killed, exit 9, the cell named).
#            Then check_grid.py --family bi --in-sample over the cells on disk: full coverage, the same answers on
#            every cell, every step's cost follows its own part, its planted errors caught.
#            -> output/captures/ldbc/sf<SF>/bi/reported/{counts,steps,parts}/<cell>.csv, one line per cell in
#            output/summary/ldbc/sf<SF>/bi-cells/cells-runs.log
#   table    experiments/meta/bi_cells_table.py: per (query, cell) the pick, the planner's body, their ratio, and the
#            mark "inside the margin" for a pick less than 10% cheaper -> output/summary/ldbc/sf<SF>/bi-cells/bi_picks.csv + a printed table
#   latency  python/kvcost/latency/grid_latency.py --family bi per cell and regime (--regimes, in that order: every cell's
#            steady, then every cell's cold), then its gates (counts equal to the capture's picked variant, cells per
#            pass). steady: direct I/O, a bounded cache (SF x 0.1 GiB), 3 measured passes ->
#            <root>/timings/ldbc/sf<SF>/bi/meta-<cell>/steady-<MB>mb-p2/ (perf level 2, no KV timer: the walls of record).
#            cold: --cold N measured iterations at perf level 2, direct I/O -> <root>/timings/ldbc/sf<SF>/bi/meta-<cell>/cold-p2/.
#            The machine as the freeze pass: every server bound to FREEZE_NUMA_NODE
#            (unset: node 0, marked foreign_load "node 0 by default"), run/machine-for-timing.sh prepare (numa_balancing off, swappiness 1) before
#            the first store and restore on exit, the page cache dropped before each store (rows in machine_state.csv, in
#            <root>/summary/ldbc/sf<SF>/grid-latency/bi/: run/latency-cells.sh)
#   check    only the grid check of the counts stage, over the cells on disk (after cells counted in parallel calls with
#            --no-check)
# --chunked-size <1k|4k|16k|128k|256k>: Chunked at that size, from the spec set inputs/specs/grid_chunked_<size>
#   (its cells in the same capture as the grid's), beside the grid's four cells with no Chunked side.
#   4k has all five Chunked-side cells, so the check runs over the nine together (check_grid.py --spec-dir ...
#   --grid-cells <the four> --decompose); the other sizes have Chunked/Chunked only, so the nine-cell check refuses
#   them (their check: experiments/meta/check_bi_chunk_answers.py). --chunked-4k = --chunked-size 4k. Without either:
#   the grid's nine (64 KB).
# --spec-dir DIR: another spec set (e.g. inputs/specs/bi_winner_20261020): its cells join the grid's nine, run with
#   --spec-dir DIR, in the same capture; the check is check_grid.py --spec-dir DIR.
#   One extra spec set per call: --spec-dir with --chunked-size / --chunked-4k is refused.
#   experiments/meta/run-bi-cells.sh --sf 0.1 --stage counts
#   experiments/meta/run-bi-cells.sh --sf 3 --stage counts --chunked-4k --cells props_split_adj_split --no-check   # one cell of a parallel run
#   experiments/meta/run-bi-cells.sh --sf 3 --stage check --chunked-4k
#   experiments/meta/run-bi-cells.sh --sf 3 --stage counts --chunked-size 16k --cells props_chunked_c16k_adj_chunked_c16k --no-check
#   experiments/meta/run-bi-cells.sh --sf 3 --stage counts --cells props_chunked_adj_chunked          # a pilot cell
#   experiments/meta/run-bi-cells.sh --sf 3 --stage latency --cells props_chunked_adj_chunked
# Flags: --sf <SF> | $SF; --stage counts|check|table|latency (required); --cells a,b (default: the nine); --nparam N (5);
#        --chunked-size SIZE | --chunked-4k; --spec-dir DIR; --no-check (counts: skip the grid check); --timeout S (counts: seconds per run, default 1800);
#        --stop-rss-gb G (default 60) ; --vlimit-gb G (the server's virtual-memory ceiling, default 120);
#        --steady CACHE_MB,PASSES (default SF x 0.1 GiB, 3); --queries a,b (latency: time only these; the gate covers
#        them only); --regimes steady,cold (latency, default steady); --cold N (latency: cold's measured iterations,
#        default 1, as the freeze pass); --breakdown (latency: a breakdown pass beside each wall, perf 3 and the KV
#        timer, in its own dir: steady-<MB>mb-breakdown, one pass; cold-breakdown, one run per param); --counts DIR (latency: the BI counts capture the picks and the gate read,
#        default output/captures/ldbc/sf<SF>/bi/reported; e.g. a composed capture with newer plans);
#        --root DIR (latency: the output tree the timings go to, default the output dir; grid_latency keeps one counts
#        capture per tree, so cells timed against another --counts go to another --root); --fresh; --allow-foreign (a
#        no-op, kept so old commands run: another kvevaluator process alive is warned and marked foreign_load);
#        --dry-run; -h. The binary: $CPP_BIN (when dirty, when its build had no git, or when its C++ build inputs
#        differ from HEAD's: warned and marked binary_unclean, with cpp_diff_files). Long stages: tmux + a .log.
# Marks (kvevaluator.stale): by kvcost measure into the shared counts capture as the cell's own stale-<cell>.jsonl
#        (run_measure.binary_notes), and by run/latency-cells.sh beside its timings; the record's writer folds them
#        in; the run goes on. A gate's refusal stays one.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"

STAGE=""; CELLS=""; NPARAM=5; STOP_RSS_GB=60; VLIMIT_GB=120; STEADY=""; QUERIES=""; ROOT_ARG=(); FRESH=(); DRY=0; C4K=0; NOCHECK=0; TIMEOUT=1800
REGIMES=steady; COLD=1; COUNTS=""; SPEC_DIR=""; BREAKDOWN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)          SF="${2:?}"; shift 2 ;;
    --stage)       STAGE="${2:?}"; shift 2 ;;
    --cells)       CELLS="${2:?}"; shift 2 ;;
    --nparam)      NPARAM="${2:?}"; shift 2 ;;
    --stop-rss-gb) STOP_RSS_GB="${2:?}"; shift 2 ;;
    --vlimit-gb)   VLIMIT_GB="${2:?}"; shift 2 ;;
    --steady)      STEADY="${2:?}"; shift 2 ;;
    --queries)     QUERIES="${2:?}"; shift 2 ;;
    --regimes)     REGIMES="${2:?}"; shift 2 ;;
    --cold)        COLD="${2:?}"; shift 2 ;;
    --breakdown)   BREAKDOWN=1; shift ;;
    --counts)      COUNTS="${2:?}"; shift 2 ;;
    --root)        ROOT_ARG=(--root "${2:?}"); shift 2 ;;
    --fresh)       FRESH=(--fresh); shift ;;
    --chunked-4k)  C4K=1; shift ;;
    --spec-dir)    SPEC_DIR="$(cd "${2:?}" && pwd)"; shift 2 ;;
    --chunked-size) CSIZE="${2:?}"; shift 2 ;;
    --no-check)    NOCHECK=1; shift ;;
    --timeout)     TIMEOUT="${2:?}"; shift 2 ;;
    --allow-foreign) shift ;;   # a no-op: foreign processes and no NUMA node are warned and marked foreign_load
    --dry-run)     DRY=1; shift ;;
    -h|--help)     awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *)             echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[ -n "$STAGE" ] || { echo "--stage is required" >&2; exit 1; }
CSIZE="${CSIZE:-}"
if [ $C4K = 1 ]; then CSIZE=4k; fi        # --chunked-4k = --chunked-size 4k
GRID_CELLS="props_split_adj_split props_split_adj_condensed props_split_adj_chunked props_condensed_adj_split props_condensed_adj_condensed props_condensed_adj_chunked props_chunked_adj_split props_chunked_adj_condensed props_chunked_adj_chunked"
# --chunked-size: the four grid cells with no Chunked side + the size's specs (all five Chunked-side cells for 4k,
# Chunked/Chunked alone for the other sizes)
SPECSZ="$PWD/inputs/specs/grid_chunked_${CSIZE:-none}"
NO_CHUNKED="props_split_adj_split props_split_adj_condensed props_condensed_adj_split props_condensed_adj_condensed"
[ -n "$CSIZE" ] && [ -n "$SPEC_DIR" ] && { echo "--chunked-size/--chunked-4k and --spec-dir: one spec set per call" >&2; exit 1; }
# SPECSET: the call's one extra spec set (none, a chunk size's, or --spec-dir's); layout_of reads it
SPECSET=""
if [ -n "$CSIZE" ]; then
  [ -d "$SPECSZ" ] || { echo "--chunked-size $CSIZE: no spec set $SPECSZ" >&2; exit 1; }
  SPECSET="$SPECSZ"
  GRID_CELLS="$NO_CHUNKED $(cd "$SPECSZ" && ls *.json | sed 's/\.json$//' | tr '\n' ' ')"
elif [ -n "$SPEC_DIR" ]; then   # --spec-dir: its cells join the grid's nine
  SPECSET="$SPEC_DIR"
  GRID_CELLS="$GRID_CELLS $(cd "$SPEC_DIR" && ls *.json | sed 's/\.json$//' | tr '\n' ' ')"
fi
[ -n "$CELLS" ] && cells="${CELLS//,/ }" || cells="$GRID_CELLS"
for cell in $cells; do case " $GRID_CELLS " in *" $cell "*) ;; *) echo "$cell is not one of: $GRID_CELLS" >&2; exit 1 ;; esac; done
# a cell's spec set: --spec-dir SPECSET for one of its specs, nothing for a grid cell; and its counts capture
layout_of() { [ -n "$SPECSET" ] && [ -f "$SPECSET/$1.json" ] && echo "$SPECSET/$1.json" || echo "$1"; }   # a cell of the extra set: its spec file
CAP="$("$PYTHON" -m kvevaluator.output_tree capture --family bi --scale "$SF")"   # every cell's BI counts
LOG="$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis bi-cells)/cells-runs.log"   # one line per cell, beside bi_picks.csv
grid_check() {
  if [ -n "$CSIZE" ] && [ "$(ls "$SPECSZ"/*.json | wc -l)" != 5 ]; then
    echo "check: grid_chunked_$CSIZE has $(ls "$SPECSZ"/*.json | wc -l) of the five Chunked-side cells; the nine-cell check" \
         "needs all five (use --no-check; this set's check is experiments/meta/check_bi_chunk_answers.py)" >&2; exit 2
  elif [ -n "$CSIZE" ]; then
    ( run "$PYTHON" tests/gates/check_grid.py --sf "$SF" --family bi --nparam "$NPARAM" --in-sample \
        --spec-dir "$SPECSZ" --grid-cells "${NO_CHUNKED// /,}" --decompose )
  elif [ -n "$SPEC_DIR" ]; then
    ( run "$PYTHON" tests/gates/check_grid.py --sf "$SF" --family bi --nparam "$NPARAM" --in-sample --spec-dir "$SPEC_DIR" )
  else
    ( run "$PYTHON" tests/gates/check_grid.py --sf "$SF" --family bi --nparam "$NPARAM" --in-sample )
  fi
}
t0=$SECONDS
run() { echo "+ $*"; [ $DRY = 1 ] || "$@"; }

case "$STAGE" in
  counts)
    echo "machine: $(nproc) cores, $(free -g | awk 'NR==2{print $7}') GB available, load $(cut -d' ' -f1-3 /proc/loadavg)"
    # grid_bodies runs the server through the wrapper (numactl --interleave=all + the virtual-memory ceiling)
    WRAP=(env KVEVAL_CPP_BIN="$PWD/cpp/kvevaluator/run-interleaved.sh" KVEVAL_CPP_VLIMIT_KB=$((VLIMIT_GB * 1048576)))
    for cell in $cells; do
      echo "=== $cell"
      mkdir -p "$CAP" "$(dirname "$LOG")"
      M=("$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --family bi "$(layout_of "$cell")" --nparam "$NPARAM"
         --timeout "$TIMEOUT" --no-check "${FRESH[@]}")
      [ $DRY = 1 ] && { echo "+ ${M[*]}"; continue; }
      before=$(free -g | awk 'NR==2{print $7}'); s=$SECONDS; peakd="$(dirname "$LOG")/peak/$cell"
      # the servers' peak RSS, sampled every 5 s; past --stop-rss-gb the measure is stopped and watch-memory exits 9
      rc=0; "${WRAP[@]}" run/watch-memory.sh --out "$peakd" --stop-gb "$STOP_RSS_GB" -- "${M[@]}" || rc=$?
      peak_gb=$(cat "$peakd/total.peak" 2>/dev/null || echo 0.0)
      echo "$(date -u +%FT%TZ) sf$SF $cell wall_s=$((SECONDS - s)) avail_before_gb=$before peak_server_rss_gb=$peak_gb rc=$rc$([ $rc = 9 ] && echo ' STOPPED_BY_RSS')" | tee -a "$LOG"
      if [ $rc = 9 ]; then echo "STOP: $cell passed --stop-rss-gb $STOP_RSS_GB (peak $peak_gb GB): raise it for this cell on purpose, or cut the query" >&2; exit 9; fi
      [ $rc = 0 ] || { echo "FAILED: $cell rc=$rc" >&2; exit "$rc"; }
    done
    [ $NOCHECK = 1 ] || grid_check
    ;;
  check)
    grid_check
    ;;
  table)
    case "$CSIZE" in ""|4k) ;; *) echo "table: bi_cells_table.py knows 64 KB and 4 KB only" >&2; exit 1 ;; esac
    run "$PYTHON" experiments/meta/bi_cells_table.py --sf "$SF" $([ "$CSIZE" = 4k ] && echo --chunked-4k)
    ;;
  latency)   # the shared timed stage (run/latency-cells.sh): this study's cells, its spec set and its root
    LAT=(run/latency-cells.sh --sf "$SF" --family bi --cells "$(echo $cells | tr ' ' ',')" "${ROOT_ARG[@]}"
         --nparam "$NPARAM" --regimes "$REGIMES" --cold "$COLD" ${SPECSET:+--spec-dir "$SPECSET"} ${STEADY:+--steady "$STEADY"}
         ${COUNTS:+--counts "$COUNTS"} ${QUERIES:+--queries "$QUERIES"} "${FRESH[@]}")
    [ $BREAKDOWN = 1 ] && LAT+=(--breakdown)
    [ $DRY = 1 ] && LAT+=(--dry-run)
    "${LAT[@]}"
    ;;
  *) echo "unknown stage $STAGE (counts | check | table | latency)" >&2; exit 1 ;;
esac
echo "[$(basename "$0")] DONE (sf$SF, $STAGE, $((SECONDS - t0))s)"
