#!/bin/bash
# engines/once.sh -- one-time SETUP for the real-engine side (only the studies that measure
# a live engine need this; the evaluator needs none of it). For each database named by --db, it:
#   1. BUILDS its kvstats binary when engines/<db>/bin has none: the sources fetched at their pinned commits
#      (engines/<db>/get.sh), our patches applied and the build run (engines/<db>/build.sh kvstats, which checks its
#      tools first: `just setup-engines` installs them); NebulaGraph also gets its console and importer. A binary
#      that is there is kept. A failed build stops before any load.
#   2. LINKS the LDBC files at this SF into the engine's data dir (`just ldbc-download <SF>`; Dgraph also converts
#      them to RDF), when the engine has no backup at this SF yet.
#   3. LOADS the engine's OWN LDBC backup by driving engines/load-data.sh --db <the same DBs>. The engine
#      builds and owns its data at engines/<db>/backup/ -- no cost_eval / main-arango fallback.
# Idempotent: a binary and a backup that exist are skipped, so re-runs are cheap.
#
# WARNING: a first run WIPES + reloads each DB's runsdir (any server on those ports); the builds take 2 to 25 minutes
# per DB (engines/README.md) and a load minutes. It needs the raw LDBC CsvBasic data (`just fetch-dataset ldbc=<SF>`).
# Usage: bash engines/once.sh [--db dgraph|nebula|arangodb|tugraph|tugraph-detached|all|comma list] [--sf SF]
#   --db  default all: the three with an evaluator format (NebulaGraph's key is nebula). TuGraph is set up by name; its
#         release is fetched, not built (seconds). SF from --sf, else $SF, else env.sh's default (0.1)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DB="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2;;
    --sf) SF="$2"; shift 2;;
    -h|--help) sed -n '2,19p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
source "$HERE/env.sh"      # ENGINES_DIR, ENGINES_PYTHON, SF (default 0.1), the dgraph knobs, per-engine paths, the LDBC data root
export SF COSTEVAL_SF="$SF"

if [[ "$DB" == "all" ]]; then dbs=(dgraph nebula arangodb); else IFS=',' read -ra dbs <<<"$DB"; fi

# 1. kvstats binaries (each DB names its binary differently): build the missing ones, every named DB before
#    loading anything: the load clobbers runsdirs, so a failed build must stop us up front, not after we have wiped a DB.
declare -A BIN_PROBE=(
  [dgraph]="$HERE/dgraph/bin/dgraph-kvstats"
  [nebula]="$HERE/nebulagraph/bin/nebula-graphd"
  [arangodb]="$HERE/arangodb/bin/arangod"
  [tugraph]="$HERE/tugraph/bin/libtugraph_kvstats.so"     # the release is fetched, not built: our counter library
  [tugraph-detached]="$HERE/tugraph/bin/libtugraph_kvstats.so"
)
for db in "${dbs[@]}"; do
  [[ -n "${BIN_PROBE[$db]:-}" ]] || { echo "unknown db: $db (dgraph|nebula|arangodb|tugraph|tugraph-detached|all)" >&2; exit 1; }
done
# An engine builds with the system toolchain that `just setup-engines` installs and checks, never a Python venv on PATH
# (`uv run` puts the repo's first: ArangoDB's bundled ICU configure would then run a python3 without distutils)
build_path=$(IFS=:; for d in $PATH; do [[ -f "$d/../pyvenv.cfg" ]] || printf '%s:' "$d"; done)
for db in "${dbs[@]}"; do
  dir="$HERE/$(engine_dir "$db")"
  if [[ -x "${BIN_PROBE[$db]}" ]]; then
    echo "ok    [$db] kvstats binary present"
    continue
  fi
  echo "MISS  [$db] kvstats binary: fetching and building it (engines/$(engine_dir "$db")/get.sh, build.sh kvstats)"
  t0=$SECONDS
  env -u VIRTUAL_ENV PATH="${build_path%:}" bash "$dir/get.sh"
  [[ "$db" != nebula ]] || (cd "$dir" && env -u VIRTUAL_ENV PATH="${build_path%:}" just setup-console setup-nebula-importer)   # its start and load run them
  env -u VIRTUAL_ENV PATH="${build_path%:}" bash "$dir/build.sh" kvstats
  [[ -x "${BIN_PROBE[$db]}" ]] || { echo "FAIL  [$db] no kvstats binary at ${BIN_PROBE[$db]} after the build. NOT loading." >&2; exit 1; }
  echo "built [$db] kvstats binary in $((SECONDS - t0)) s"
done

# 2. The LDBC files at this SF in each engine's data dir, for the engines the load below will load
for db in "${dbs[@]}"; do
  dir="$HERE/$(engine_dir "$db")"
  [[ -d "$dir/backup/runs-sf${SF}$(engine_backup_tag "$db")" ]] || (cd "$dir" && just ldbc-download "$SF")
done

# 3. Load the engine's OWN backups. load-data.sh is idempotent (skips a DB already loaded) and stamps
#    provenance, so this is safe to re-run and only loads what is missing.
echo ""
echo "== Loading engine backups (sf$SF) via engines/load-data.sh --db $DB =="
bash "$HERE/load-data.sh" --db "$DB" --sf "$SF"

echo ""
echo "Ready (sf$SF). Next: bash engines/run-engine.sh --db $DB --sf $SF   # restore, start, measure"
