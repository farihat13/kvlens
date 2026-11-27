#!/usr/bin/env bash
# run-job-counts-chain.sh -- the JOB workload's counts at one data tag, unattended.
# Owner: the JOB workload (python/kvevaluator/workloads/job, its README's "Run").
# Stages, one after another; the chain stops at the first failure and prints CHAIN DONE only if all passed:
#   stores   the nine cells of the configurable format and Dgraph (512 KB), NebulaGraph, ArangoDB. The first store alone
#            (it also dumps the tag's record cache, timed on its own), then four ingests at a time (a store already
#            there is checked against the schema hash and left)
#   counts   every valid body of every query, one `kvcost measure` per store (--nparam 1 --timeout 3600 --fresh
#            --no-check), at most --parallel stores side by side through run/in-parallel.sh, in the order given (the
#            first ones are the pilots: watch them). Per store: wall, rc, the evaluator server's peak RSS (sampled every
#            5 s by run/watch-memory.sh around the runner) -> output/captures/imdb-rows/<tag>/job/reported/{counts,steps,parts}/ (the cells and
#            the named formats side by side); measure marks each store's record (its binary rule: kvcost measure)
#   checks   check_grid.py over the cells (--in-sample), check_job_reference.py --from-capture over every captured store
#   tables   experiments/meta/bi_cells_table.py --family job, the cells and (unless --cells-only) --formats
#            -> output/summary/imdb-<mapping>/<tag>/job-cells/job_picks.csv, job_picks_formats.csv
# Recorded cost (README "Expected run time"): sample1pct 25-41 min; full 7 h 00 min, 21-25 GB peak per server
# (six at a time: about 150 GB). Run it in tmux with a log, and watch the available memory:
#   tmux new-session -d -s jobchain "experiments/job/run-job-counts-chain.sh full > output/logs/job/counts-chain-full.log 2>&1"
# Usage: experiments/job/run-job-counts-chain.sh <tag: sample1pct|full> [--cells a,b] [--cells-only | --format F]
#                                                [--parallel N] [--logs DIR] [--dry-run] [-h]
#   --cells a,b    these cells, in this order (default: the nine); fewer than nine is a slice: its checks say so and
#                  the tables are skipped
#   --cells-only   no named formats (Dgraph, NebulaGraph, ArangoDB)
#   --format F     one named format alone (dgraph, nebula or arangodb), beside an existing capture (after a change that
#                  touches only it): its store, its bodies, the reference check and both picks tables; no grid check
#   --parallel N   stores measured side by side (default 6; at the full tag each server peaks at 21-25 GB)
#   --logs DIR     per-unit logs (default output/logs/job/counts-chain-<tag>)
#   --dry-run      print the binary's identity and the units of each stage, run nothing
# The binary: $CPP_BIN. kvcost measure marks each store's record by its one rule (run_measure.binary_notes): dirty, or
# uncommitted changes under the code that decides counts: binary_unclean; built at a commit that is not an ancestor of
# HEAD, or with a change since under that code: binary_behind_head (a pinned binary measures across doc commits unmarked).
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh" || exit 1
TAG=""; L=""; DRY=0; CELLS_ARG=""; CELLS_ONLY=0; PAR=6; ONE_FORMAT=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)    sed -n '2,/^source /p' "$0" | grep '^#' | cut -c3-; exit 0 ;;
    --logs)       L="${2:?--logs needs a directory}"; shift 2 ;;
    --cells)      CELLS_ARG="${2:?--cells needs a comma list}"; shift 2 ;;
    --cells-only) CELLS_ONLY=1; shift ;;
    --format)     ONE_FORMAT="${2:?--format needs dgraph, nebula or arangodb}"; shift 2 ;;
    --parallel)   PAR="${2:?--parallel needs a number}"; shift 2 ;;
    --dry-run)    DRY=1; shift ;;
    -*)           echo "unknown flag $1 (-h for help)" >&2; exit 2 ;;
    *)            TAG="$1"; shift ;;
  esac
done
[ "$TAG" = sample1pct ] || [ "$TAG" = full ] || { echo "the tag is sample1pct or full (-h for help)" >&2; exit 2; }
[[ "$PAR" =~ ^[1-9][0-9]*$ ]] || { echo "--parallel needs a positive number" >&2; exit 2; }
L="${L:-$("$PYTHON" -c 'import sys; from kvevaluator.output_tree import Tree; print(Tree().logs("job", sys.argv[1]))' "counts-chain-$TAG")}"
have="$(git log -1 --format=%H)"
bin_sha="$("$CPP_BIN" --version | grep binary_git_sha | cut -d'"' -f4)"
dirty="$("$CPP_BIN" --version | grep binary_dirty | tr -d ' ,')"
echo "CHAIN START $(date -u +%FT%TZ) tag $TAG tree ${have:0:10} binary ${bin_sha:0:10} $dirty"
GRID_CELLS="props_split_adj_split props_split_adj_condensed props_split_adj_chunked props_condensed_adj_split props_condensed_adj_condensed props_condensed_adj_chunked props_chunked_adj_split props_chunked_adj_condensed props_chunked_adj_chunked"
CELLS="${CELLS_ARG//,/ }"; CELLS="${CELLS:-$GRID_CELLS}"
for c in $CELLS; do [[ " $GRID_CELLS " == *" $c "* ]] || { echo "--cells: $c is not a grid cell" >&2; exit 2; }; done
FORMATS="dgraph nebula arangodb"; [ $CELLS_ONLY = 1 ] && FORMATS=""
if [ -n "$ONE_FORMAT" ]; then   # one named format alone: no cells
  case "$ONE_FORMAT" in dgraph|nebula|arangodb) ;; *) echo "--format: dgraph, nebula or arangodb" >&2; exit 2 ;; esac
  [ $CELLS_ONLY = 0 ] && [ -z "$CELLS_ARG" ] || { echo "--format: not with --cells or --cells-only" >&2; exit 2; }
  CELLS=""; FORMATS="$ONE_FORMAT"
fi
SLICE=0; [ -z "$ONE_FORMAT" ] && [ "$(echo $CELLS | wc -w)" -lt 9 ] && SLICE=1
UNITS=""; for c in $CELLS; do UNITS="$UNITS cell:$c"; done; for f in $FORMATS; do UNITS="$UNITS format:$f"; done
measure_line() {   # measure_line cell:<cell> | format:<fmt>: one store's kvcost measure
  echo "\"$PYTHON\" -m kvcost.cli.cli measure --dataset imdb=$TAG --workload j01 ${1#*:} --nparam 1 --timeout 3600 --fresh --no-check"
}
if [ $DRY = 1 ]; then
  echo "dry run: logs in $L"
  echo "  stores: $UNITS (the first alone, then four ingests at a time)"
  echo "  counts: $UNITS (at most $PAR side by side: run/in-parallel.sh, one store per kvcost measure)"
  for u in $UNITS; do echo "    $(measure_line "$u")"; done
  echo "  checks: $([ -n "$ONE_FORMAT" ] || echo "check_grid.py --sf $TAG --family job --nparam 1 --in-sample$([ $SLICE = 1 ] && echo " --cells ${CELLS// /,}"); ")check_job_reference.py --tag $TAG --from-capture"
  echo "  tables: $([ $SLICE = 1 ] && echo "skipped (a slice)" || echo "bi_cells_table.py --family job --sf $TAG$([ -n "$FORMATS" ] && echo ", and --formats")")"
  exit 0
fi
mkdir -p "$L"
fail() { echo "CHAIN FAIL: $1"; exit 1; }

# ---- the stores: the first alone (it dumps the record cache), then 4 ingests at a time (a store already there is checked and left)
ingest_one() {   # ingest_one cell:<cell> | format:<fmt>
  ( "$PYTHON" - "$TAG" "$1" <<'PY'
import os, sys, time
from kvevaluator.measure.grid_capture import GRID, format_store, meta_store
tag, unit = sys.argv[1:3]
kind, name = unit.split(":", 1)
t0 = time.time()
path = format_store(tag, "job", name) if kind == "format" else meta_store(tag, "job", os.path.join(GRID, name + ".json"))
print(f"store {unit}: {path} ({time.time() - t0:.0f} s)")
PY
  ) > "$L/ingest-${1//:/-}.log" 2>&1
}
ingest_batch() {   # ingest_batch <unit>...: side by side, each one's rc on its own line
  local pids=() batch=() u i=0 r
  for u in "$@"; do ingest_one "$u" & pids+=($!); batch+=("$u"); done
  for u in "${batch[@]}"; do
    r=0; wait "${pids[$i]}" || r=$?
    echo "=== rc=$r $(tail -1 "$L/ingest-${u//:/-}.log" | cut -c1-200); disk free $(df -BG --output=avail output | tail -1 | tr -d ' ')"
    [ $r = 0 ] || fail "ingest $u"
    i=$((i + 1))
  done
}
s=$SECONDS
set -- $UNITS
ingest_batch "$1"; shift
echo "=== first store and record cache ($((SECONDS - s))s)"
while [ $# -gt 0 ]; do
  batch=(); for u in "$1" "${2:-}" "${3:-}" "${4:-}"; do [ -n "$u" ] && batch+=("$u"); done
  shift $(( $# < 4 ? $# : 4 ))
  ingest_batch "${batch[@]}"
done
echo "=== stores ready ($((SECONDS - s))s); mem available $(free -g | awk 'NR==2{print $7}') GB; disk free $(df -BG --output=avail output | tail -1 | tr -d ' ')"

# ---- the captures: one kvcost measure per store, at most $PAR side by side (run/in-parallel.sh), in the given order
s=$SECONDS
rm -f "$L/counts.cmds"
for u in $UNITS; do measure_line "$u" >> "$L/counts.cmds"; done
echo "=== counts start $(date -u +%T): at most $PAR at once; mem available $(free -g | awk 'NR==2{print $7}') GB"
# per store, the peak RSS of its evaluator servers, sampled every 5 s, in $L/peak/<store>.peak
rc=0; run/watch-memory.sh --out "$L/peak" -- run/in-parallel.sh -j "$PAR" --logs "$L/units" "$L/counts.cmds" || rc=$?
for u in $UNITS; do   # per store: its peak, and the runner's line for it (rc, wall)
  echo "=== $u peak_server_rss_gb=$(cat "$L/peak/${u#*:}.peak" 2>/dev/null || echo -) binary ${bin_sha:0:10}"
done
echo "=== counts end ($((SECONDS - s))s) rc=$rc"
[ $rc = 0 ] || fail "counts"

# ---- the checks and the tables
# one check: a start line, a line each minute while it runs (its log's last line), then its verdict
g() {
  local n="$1"; shift; local s=$SECONDS t=0
  echo "=== start $n $(date -u +%T) (log: $L/$n.log)"
  "$@" > "$L/$n.log" 2>&1 & local pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    sleep 5; t=$((t + 5))
    (( t % 60 == 0 )) && echo "=== $n running, $((SECONDS - s))s: $(grep -v '^ ' "$L/$n.log" | tail -1 | cut -c1-200)"
  done
  wait "$pid"; local rc=$?
  echo "=== rc=$rc $n ($((SECONDS - s))s): $(grep -v '^ ' "$L/$n.log" | tail -1 | cut -c1-260)"; return $rc
}
SLICEARG=""; [ $SLICE = 1 ] && SLICEARG="--cells ${CELLS// /,}"
if [ -z "$ONE_FORMAT" ]; then
  g grid-check "$PYTHON" tests/gates/check_grid.py --sf "$TAG" --family job --nparam 1 --in-sample $SLICEARG || fail "grid check"
  grep -q "RESULT: PASS" "$L/grid-check.log" || fail "grid check: no PASS line"
fi
g reference-from-capture "$PYTHON" tests/gates/check_job_reference.py --tag "$TAG" --from-capture || fail "reference"
if [ $SLICE = 1 ]; then
  echo "=== tables skipped: a slice of $(echo $CELLS | wc -w) cells"
else
  g table-cells "$PYTHON" experiments/meta/bi_cells_table.py --family job --sf "$TAG" || fail "table, cells"
  [ -n "$FORMATS" ] && { g table-formats "$PYTHON" experiments/meta/bi_cells_table.py --family job --sf "$TAG" --formats || fail "table, formats"; }
fi
echo "CHAIN DONE $(date -u +%FT%TZ): every stage passed (stores, counts, grid check, reference, tables)"
