#!/bin/bash
# engines/stop-server.sh -- stop the database server(s) engines/start-server.sh started: each engine's own stop
# (`just stop` in engines/<db>/). A database that is not running is fine.
# Usage: bash engines/stop-server.sh [--db dgraph|nebula|arangodb|tugraph|tugraph-detached|all|comma list]     (default all: the first three)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="all"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2;;
    -h|--help) sed -n '2,4p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
source "$HERE/env.sh"      # ENGINES_DIR, each engine's paths, engine_dir
if [[ "$DB" == "all" ]]; then dbs=(dgraph nebula arangodb); else IFS=',' read -ra dbs <<<"$DB"; fi
for db in "${dbs[@]}"; do
  [[ -d "$HERE/$(engine_dir "$db")" ]] || { echo "unknown db: $db (dgraph|nebula|arangodb|tugraph|tugraph-detached|all)" >&2; exit 1; }
done
for db in "${dbs[@]}"; do
  echo "== [$db] stop =="
  (cd "$HERE/$(engine_dir "$db")" && just stop)
done
echo "== engine stopped: ${dbs[*]} =="
