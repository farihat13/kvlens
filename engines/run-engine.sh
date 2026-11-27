#!/bin/bash
# engines/run-engine.sh — bring up the real engine(s), then measure their live /kvstats into
# engine_kvstats.csv for a given SF. (Engine fidelity does not read that file: it measures with its own
# experiments/engine_fidelity/measure.py, into live_kvstats.csv.)
# python/kvengines/measure.py's docstring refers to this script ("run-engine.sh brings the servers up").
#
# Two steps: start-server.sh sets the 5 dgraph knobs ONCE from .env (the canonical ineq=index point) and
# brings up the engines; then python/kvengines/measure.py runs every (db, query, param) on the LIVE server, writing each
# cell's durable output/summary/ldbc/sf<SF>/engine/<db>/<query>/p<param>/{kvstats.csv,answer.json} and rebuilding the
# flat engine_kvstats.csv from them. python/kvengines/measure.py only READS the declared operating point — it never
# re-pins — so the config is exactly what start-server.sh set. RESUMABLE: re-running skips cells already
# done (Ctrl-C mid-sweep, re-run the same command to finish); --overwrite forces a full re-measure.
#
# Usage:
#   bash engines/run-engine.sh                        # SF from $SF/.env: start all 3, measure all (resume)
#   bash engines/run-engine.sh 0.1                    # positional SF
#   bash engines/run-engine.sh --sf 0.1 --overwrite   # re-measure every cell (forwarded to python/kvengines/measure.py)
#   bash engines/run-engine.sh --db dgraph --query ic2  # start+measure just dgraph, one query
#   bash engines/run-engine.sh --no-start             # servers already up: skip the (destructive) restart
#   bash engines/run-engine.sh --verbose              # also print the per-cell S/G/N lines (default: quiet)
#
# --db selects which servers to start AND is forwarded to python/kvengines/measure.py. --query / --param / --nparam /
# --overwrite are forwarded to python/kvengines/measure.py. --no-start skips the server bring-up (measure what is up).
# By default python/kvengines/measure.py runs --quiet: a one-line progress marker per cell (which db/query/param ran) but
# no S/G/N counts (those are in the CSV). --verbose adds the counts back to each line.
#
# WARNING: starting a server replaces whatever runs on that DB's ports and runsdir. Use --no-start
# when the engines are already up. Needs sudo for drop_caches (dgraph). Requires the engine's own backups
# (bash engines/once.sh) — start-server.sh errors if a backup is missing.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NOSTART=0
DB="all"
QUIET=1                         # python/kvengines/measure.py runs quiet by default (S/G/N are in the CSV); --verbose to see cells
FWD=()                          # flags forwarded verbatim to python/kvengines/measure.py
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)                 SF="$2"; shift 2;;
    --no-start)           NOSTART=1; shift;;
    --verbose)            QUIET=0; shift;;
    --db)                 DB="$2"; FWD+=(--db "$2"); shift 2;;
    --query|--param|--nparam) FWD+=("$1" "$2"); shift 2;;
    --overwrite)          FWD+=("$1"); shift;;   # re-measure every cell (default is resume: skip done cells)
    --fill)               FWD+=("$1"); shift;;   # a no-op: resume is the default
    -h|--help)            sed -n '2,26p' "$HERE/run-engine.sh"; exit 0;;
    -*)                   echo "unknown flag: $1" >&2; exit 1;;
    *)                    SF="$1"; shift;;   # bare positional -> SF
  esac
done
(( QUIET )) && FWD+=(--quiet)

export SF="${SF:-}"             # empty -> env.sh's default; a value here wins (SF="${SF:-0.1}" in env.sh)
source "$HERE/env.sh"       # ENGINES_DIR, ENGINES_PYTHON, SF, the dgraph knobs, per-engine paths, the LDBC data root
export SF COSTEVAL_SF="$SF"     # python/kvengines/measure.py + cost_eval read these

t0=$(date +%s)
log() { echo "[run-engine $(date +%H:%M:%S) +$(( $(date +%s) - t0 ))s] $*"; }

if (( NOSTART )); then
  log "--no-start: skipping server bring-up; measuring against the already-up engine(s)"
else
  log "1/2 start engine(s) --db $DB (sf$SF) at the .env operating point (ineq=index, localcache=disable)"
  bash "$HERE/start-server.sh" --db "$DB" --sf "$SF"
fi

log "2/2 measure live /kvstats -> output/summary/ldbc/sf$SF/engine/engine_kvstats.csv"
# -u: unbuffered stdout so the per-cell progress markers stream live through tee/tail, not in chunks.
"$ENGINES_PYTHON" -u -m kvengines.measure --sf "$SF" "${FWD[@]}"

log "done -- engine_kvstats.csv rebuilt (python/kvengines/measure.py is its sole writer)."
