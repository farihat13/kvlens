#!/bin/bash
# engines/start-server.sh — bring up the REAL database engine(s) on the physical design the evaluator models
# for each engine format (the same schema and indexes).
#
# The evaluator (cpp/kvevaluator) prices each format's KV layout; this dir is its "real engine" side. The
# studies that compare with the real engines (experiments/engine_fidelity) use it to measure live
# dgraph / nebula / arango /kvstats.
#
# SELF-CONTAINED: this dir owns its per-DB start/load scripts (engines/<db>/), its kvstats binaries
# (engines/<db>/build.sh) and its backups (engines/<db>/backup/). It restores the engine's OWN backup
# (built by engines/load-data.sh / once.sh); a missing backup is an error. arango always keeps the canonical creationDate indexes
# (engines/arangodb/start-server.sh has no index-drop policy), so no separate index safety net is
# needed — python/kvengines/verify_schema.py below checks the live index set regardless.
#
# The 5 dgraph knobs (detsort, eq/type/ineqfilterpath, localcache) are set ONCE here at server start from
# the engine's .env (ineq=index — the fully-indexed canonical design); measure_kv does NOT re-pin them,
# the operating point is stable. `cd dgraph && just knob-status` shows the live values.
#
# Usage:
#   bash engines/start-server.sh --db all                 # dgraph + nebula + arangodb (SF from --sf, else $SF, else env.sh's default 0.1)
#   bash engines/start-server.sh --db tugraph             # TuGraph is started by name (it has no evaluator format);
#                                                         # --db tugraph-detached: its second configuration (same ports)
#   bash engines/start-server.sh --db dgraph              # one
#   SF=0.3 bash engines/start-server.sh --db nebula,arangodb
#
# WARNING: a start replaces whatever runs on that DB's ports and runsdir. The three LDBC servers coexist
# on different ports. Needs sudo for drop_caches (dgraph and nebula; not at a cold start, which puts only the
# restored store out of the page cache).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DB="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2;;
    --sf) SF="$2"; shift 2;;
    -h|--help) sed -n '2,31p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done

export SF="${SF:-}"      # empty -> env.sh's default (0.1); --sf or $SF wins
source "$HERE/env.sh"      # ENGINES_DIR, ENGINES_PYTHON, SF, the dgraph knobs, per-engine paths, the LDBC data root
                               # 5 dgraph knobs at the engine's declared point (ineq=index).
export SF COSTEVAL_SF="$SF"

# The guardrails live in each setup caller as lifecycle hooks (engines/lifecycle-common.sh):
# provenance sha-gate (LC_PRE_RESTORE_CHECK) before the restore — a stale backup would measure
# a physical design that is not the one in data/schema/<db>/ — and python/kvengines/verify_schema.py
# (LC_POST_START_CHECK) after the start — provenance catches "schema edited, never reloaded";
# the store check catches "the load ran but produced the wrong design". A failing hook aborts.
# Each caller also errors on a missing engine backup (self-contained: no cost_eval fallback;
# build one with `SF=$SF bash engines/load-data.sh --db <db>` or `bash engines/once.sh`).
if [[ "$DB" == "all" ]]; then dbs=(dgraph nebula arangodb); else IFS=',' read -ra dbs <<<"$DB"; fi
for db in "${dbs[@]}"; do
  script="$HERE/$(engine_dir "$db")/start-server.sh"
  [[ -f "$script" ]] || { echo "ERROR: no engine start-server for '$db' at $script" >&2; exit 1; }
  echo "== [$db] start (engine backup, sf$SF) via engines/$(engine_dir "$db")/start-server.sh =="
  ENGINES_DB="$db" bash "$script"                        # ENGINES_DB: which configuration of the engine
done

echo "== engine up: ${dbs[*]} (sf$SF) at the .env-declared operating point (dgraph: ineq=index, localcache=disable). =="
