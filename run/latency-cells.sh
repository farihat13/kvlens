#!/usr/bin/env bash
# latency-cells.sh -- the timed stage of a study's cells, any read workload: per regime, per cell, one
# python/kvcost/latency/grid_latency.py call on the cell's store, then its gates (counts against the capture's picked
# variant: a difference marks the timing; cells per pass; a steady store whose reads all fit the cache is recorded as
# such, each pass's hit rate printed). Called by kvcost measure-latency (python/kvcost/cli/latency.py) and
# experiments/meta/run-bi-cells.sh (--stage latency); the caller builds its cell list and calls this.
#   steady: direct I/O, a bounded cache (inputs/contract/store_config.json's rule, store_config.steady_cache_mib), 3 measured
#           passes -> <root>/timings/<dataset>/<scale>/<workload>/meta-<cell>/steady-<MB>mb-p2/ (perf level 2, no KV
#           timer: the walls of record)
#   cold:   --cold N measured iterations at perf level 2, direct I/O -> .../meta-<cell>/cold-p2/
#   warm:   the store's blocks cached: grid_latency's warm regime (1 warm-up run, then 3 measured runs per param, perf
#           level 0) -> .../meta-<cell>/warm-p0/
# The records (grid_latency's picks.json, cells/, gate.csv), machine_state.csv and stale.jsonl:
#   <root>/summary/<dataset>/<scale>/grid-latency/<workload>/ (python/kvevaluator/output_tree.py).
# The machine as the freeze pass: every server bound to FREEZE_NUMA_NODE (unset: node 0, marked foreign_load "node 0 by
# default"), run/machine-for-timing.sh prepare (numa_balancing off, swappiness 1) before the first store and restore on
# exit, the page cache dropped before each store (rows in machine_state.csv).
#   run/latency-cells.sh --sf 3 --family bi --cells props_split_adj_split,props_chunked_adj_chunked
#   run/latency-cells.sh --sf full --family job --nparam 1 --spec-dir inputs/specs/grid_chunked_1k --cells props_chunked_c1k_adj_chunked_c1k
# Flags: --sf <SF>; --family W (required); --cells a,b (required); --root DIR (the output tree, default the output
#        dir; grid_latency keeps one counts capture per tree and workload, so cells timed against another --counts go
#        to another --root); --spec-dir DIR (the one spec set besides the grid: a cell with a spec there runs with it);
#        --nparam N (5);
#        --steady CACHE_MB,PASSES (default the store_config rule, 3); --regimes steady,cold,warm (default steady); --cold N (1);
#        --breakdown (a breakdown pass beside each wall, perf 3 and the KV timer, in its own dir; steady:
#        --breakdown-passes N passes, default 1); --kvtimer-passes N (steady: N passes at perf 2 with the KV timer after
#        the measured ones, in their own dir steady-<MB>mb-kvtimer: the time inside RocksDB calls; default 0);
#        --kv-timed (steady: the KV timer in every pass instead, the walls in steady-<MB>mb-p2-kvtimed; no
#        --kvtimer-passes beside it);
#        --counts DIR (the
#        counts capture the picks and the gate read; default the workload's capture in the --root tree);
#        --picks FILE (the body each cell times per query from this picks file, inputs/latency-picks/, for every cell it
#        covers: such a cell needs no counts, and is gated only where --counts holds it);
#        --queries a,b (time only these; the gate covers them only); --suffix S (appended to every regime dir: a store
#        timed again beside its first timing, e.g. -control, the drift control); --fresh; --dry-run;
#        --no-sudo (no passwordless sudo: the page cache is not dropped and the machine not prepared; every record marked
#        no_sudo in stale.jsonl); -h.
#        The binary: $CPP_BIN (when dirty, when its build had no git, or when its C++ build inputs differ from HEAD's:
#        warned and marked binary_unclean). Long stages: tmux + a .log.
# Marks (kvevaluator.stale): into the records dir above as stale.jsonl; the record's writer folds them in; the run
#        goes on. A gate's refusal stays one.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

SF=""; WL=""; CELLS=""; ROOT_ARG=(); SPEC_DIR=""; NPARAM=5; STEADY=""; REGIMES=steady; COLD=1; BREAKDOWN=0
BREAKDOWN_PASSES=1; KVTIMER_PASSES=0; KV_TIMED=()
COUNTS=""; PICKS_ARG=(); QUERIES=""; FRESH=(); SUFFIX_ARG=(); DRY=0; SUDO=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)         SF="${2:?}"; shift 2 ;;
    --family)   WL="${2:?}"; shift 2 ;;
    --cells)      CELLS="${2:?}"; shift 2 ;;
    --root)       ROOT_ARG=(--root "$(realpath -m "${2:?}")"); shift 2 ;;
    --spec-dir)   SPEC_DIR="$(cd "${2:?}" && pwd)"; shift 2 ;;
    --nparam)     NPARAM="${2:?}"; shift 2 ;;
    --steady)     STEADY="${2:?}"; shift 2 ;;
    --regimes)    REGIMES="${2:?}"; shift 2 ;;
    --cold)       COLD="${2:?}"; shift 2 ;;
    --breakdown)  BREAKDOWN=1; shift ;;
    --breakdown-passes) BREAKDOWN=1; BREAKDOWN_PASSES="${2:?}"; shift 2 ;;
    --kvtimer-passes)   KVTIMER_PASSES="${2:?}"; shift 2 ;;
    --kv-timed)   KV_TIMED=(--steady-kv-timed); shift ;;
    --counts)     COUNTS="${2:?}"; shift 2 ;;
    --picks)      PICKS_ARG=(--picks "$(realpath -m "${2:?}")"); shift 2 ;;
    --queries)    QUERIES="${2:?}"; shift 2 ;;
    --suffix)     SUFFIX_ARG=(--suffix="${2:?}"); shift 2 ;;
    --fresh)      FRESH=(--fresh); shift ;;
    --dry-run)    DRY=1; shift ;;
    --no-sudo)    SUDO=0; shift ;;
    -h|--help)    awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *)            echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[ -n "$SF" ] && [ -n "$WL" ] && [ -n "$CELLS" ] || { echo "--sf, --family and --cells are required" >&2; exit 1; }
for regime in ${REGIMES//,/ }; do
  [ "$regime" = steady ] || [ "$regime" = cold ] || [ "$regime" = warm ] || { echo "--regimes: steady | cold | warm, not $regime" >&2; exit 1; }
done
cells="${CELLS//,/ }"
BIN="$CPP_BIN"
source "$(dirname "${BASH_SOURCE[0]}")/measure-preflight.sh"
[ -n "$STEADY" ] || STEADY="$("$PYTHON" -c "import sys; from kvevaluator import workloads; from kvevaluator.measure.store_config import steady_cache_mib
print(f'{steady_cache_mib(sys.argv[1], dataset=workloads.get(sys.argv[2]).dataset)},3')" "$SF" "$WL")"   # inputs/contract/store_config.json's rule
t0=$SECONDS
run() { echo "+ $*"; [ $DRY = 1 ] || "$@"; }
# a cell's spec set: --spec-dir for one of its specs, nothing for a grid cell
spec_args() { [ -n "$SPEC_DIR" ] && [ -f "$SPEC_DIR/$1.json" ] && echo "--spec-dir $SPEC_DIR" || true; }
CCAP=$COUNTS; [ -n "$CCAP" ] || CCAP="$("$PYTHON" -m kvevaluator.output_tree capture --family "$WL" --scale "$SF" "${ROOT_ARG[@]}")"
OUT="$("$PYTHON" -m kvevaluator.output_tree summary --dataset "$("$PYTHON" -c 'import sys; from kvevaluator import workloads
print(workloads.get(sys.argv[1]).dataset)' "$WL")" --scale "$SF" --analysis grid-latency --subject "$WL" "${ROOT_ARG[@]}")"   # grid_latency's records dir

[ ${#PICKS_ARG[@]} = 0 ] || cells_counted=""   # with --picks, grid_latency says which cells the file and the counts cover
for cell in ${cells_counted-$cells}; do   # the capture that holds the cell: measured itself, else composed (output_tree.layout_capture)
  [ -f "$("$PYTHON" -c 'import sys; from kvevaluator.output_tree import Capture; print(Capture(sys.argv[1]).of_layout(sys.argv[2]).path_of("counts", sys.argv[2]))' "$CCAP" "$cell")" ] ||
    [ $DRY = 1 ] || { echo "no $WL counts for $cell at sf$SF ($CCAP, nor composed beside it): measure or compose it first" >&2; exit 2; }
done
MACHINE=run/machine-for-timing.sh
if [ $DRY = 0 ]; then
  preflight timed
  if [ -n "${FREEZE_NUMA_NODE:-}" ]; then export EVALUATOR_NUMA_NODE=$FREEZE_NUMA_NODE
  else export EVALUATOR_NUMA_NODE=0; mark foreign_load "node 0 by default"; fi   # one node for every store
  echo "numa node ${EVALUATOR_NUMA_NODE:-auto}; regimes $REGIMES; counts $CCAP"
  mkdir -p "$OUT"
  if [ $SUDO = 1 ]; then
    PREPARED=1   # before the call: a prepare that fails after its first change must still be restored
    trap '[ "$PREPARED" = 1 ] && { "$MACHINE" restore --state "$OUT" || echo "machine restore FAILED" >&2; }' EXIT
    trap 'exit 130' INT TERM HUP
    "$MACHINE" prepare --state "$OUT" --numa-balancing off
  else   # no passwordless sudo: the machine as it is, every record marked
    mark no_sudo "page cache not dropped, machine not prepared (numa_balancing, swappiness as found)"
  fi
  # its mark (a live engine or an evaluator server: foreign_load, in <out>/stale.jsonl) goes on this run's records too
  while IFS=$'\t' read -r k d; do NOTES+=("$k"$'\t'"$d"); done < <("$PYTHON" -c 'import json, os, sys
p = os.path.join(sys.argv[1], "stale.jsonl")
for n in (map(json.loads, open(p)) if os.path.exists(p) else []):
    print(n["kind"] + "\t" + n["detail"])' "$OUT")
fi
for regime in ${REGIMES//,/ }; do
  if [ "$regime" = steady ]; then flags=(--regimes steady --steady "$STEADY" --steady-perf-level 2 "${KV_TIMED[@]}")
    [ "$KVTIMER_PASSES" = 0 ] || flags+=(--steady-kvtimer-passes "$KVTIMER_PASSES")
  elif [ "$regime" = warm ]; then flags=(--regimes warm)   # grid_latency's --warm default: 1 warm-up, 3 measured runs
  else flags=(--regimes cold --cold "$COLD"); fi
  # --breakdown: grid_latency's breakdown pass beside the wall of record (perf 3 and the KV timer, its own dir),
  # steady: one more pass; cold: one more run per param
  if [ $BREAKDOWN = 1 ] && [ "$regime" = steady ]; then flags+=(--steady-breakdown --steady-breakdown-passes "$BREAKDOWN_PASSES")
  elif [ $BREAKDOWN = 1 ] && [ "$regime" = cold ]; then flags+=(--cold-breakdown --cold-breakdown-runs 1); fi
  for cell in $cells; do
    [ $DRY = 1 ] || [ $SUDO = 0 ] || "$MACHINE" drop --state "$OUT" --label "$regime $cell"   # no store inherits another's pages
    notes_into "$OUT"
    run "$PYTHON" -m kvcost.latency.grid_latency --sf "$SF" --family "$WL" --counts "$CCAP" "${PICKS_ARG[@]}" --cells "$cell" --nparam "$NPARAM" $(spec_args "$cell") \
        "${ROOT_ARG[@]}" "${flags[@]}" "${SUFFIX_ARG[@]}" --require-clean "${FRESH[@]}" ${QUERIES:+--queries "$QUERIES"}
  done
done
echo "[$(basename "$0")] DONE (sf$SF, $WL, $REGIMES, $((SECONDS - t0))s)"
