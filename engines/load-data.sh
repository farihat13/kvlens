#!/bin/bash
# engines/load-data.sh — make the engine's OWN LDBC backups, so the engine setup is self-contained. It
# DRIVES the engine's per-DB loaders (engines/<db>/load-data.sh), which load from engines/<db>/schema
# and back up to engines/<db>/backup/runs-sf<SF> (gitignored), with the kvstats binaries built by
# engines/<db>/build.sh. engines/start-server.sh restores these backups (a missing one is an error, no fallback).
#
# Usage:
#   bash engines/load-data.sh --db dgraph            # SF from --sf, else $SF, else env.sh's default 0.1 (minutes; CLOBBERS the DB)
#   SF=0.3 bash engines/load-data.sh --db dgraph,nebula
#   bash engines/load-data.sh --db all                # the three with an evaluator format; TuGraph by name: --db tugraph,
#                                                     # --db tugraph-detached (its second configuration, its own backup)
#
# WARNING: a load stops + wipes + reloads the DB's runsdir (any server on that DB's ports). Long
# (minutes/DB). Needs the kvstats binary built (engines/once.sh checks) and the LDBC CsvBasic data present.
# The loaders SKIP if the target backup already exists (idempotent).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DB=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2;;
    --sf) SF="$2"; shift 2;;
    -h|--help) sed -n '2,24p' "$0"; exit 0;;   # keep this range covering the whole header block
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
[[ -n "$DB" ]] || { echo "ERROR: --db required (dgraph|nebula|arangodb|tugraph|tugraph-detached|all|comma list)" >&2; exit 1; }

export SF="${SF:-}"      # empty -> env.sh's default (0.1); --sf or $SF wins
source "$HERE/env.sh"      # ENGINES_DIR, ENGINES_PYTHON, SF, the dgraph knobs, per-engine paths, the LDBC data root
export SF COSTEVAL_SF="$SF"

source "$HERE/provenance.sh"   # write_provenance / check_provenance

if [[ "$DB" == "all" ]]; then dbs=(dgraph nebula arangodb); else IFS=',' read -ra dbs <<<"$DB"; fi
for db in "${dbs[@]}"; do
  # Uniform skip guard for all three. The engine setup loaders skip internally too, but the check has to
  # happen HERE as well: a skipped load must not reach write_provenance, or we would stamp the CURRENT
  # schema's hash onto a backup built from an older one — certifying the exact staleness the fingerprint
  # exists to catch.
  eng_bak="$HERE/$(engine_dir "$db")/backup/runs-sf${SF}$(engine_backup_tag "$db")"      # where each loader writes it (its LC_BACKUP_DIR)
  if [[ -e "$eng_bak" ]]; then
    echo "== [$db] Skipping load: engine backup already exists at $eng_bak =="
    # For arango runs-sf<SF> is a SYMLINK (backup-data.sh keeps timestamped dirs), so `rm -rf` on it
    # would remove only the link and orphan the multi-GB target: move the target aside too.
    echo "   (to force a reload after editing the schema: move $eng_bak aside, and its target if it is a link)"
    continue
  fi

  case "$db" in
    dgraph|nebula|arangodb|tugraph|tugraph-detached)
      echo "== [$db] engine load: schema=engines/$(engine_dir "$db")/schema -> backup=engines/$(engine_dir "$db")/backup =="
      # The per-DB loader defaults its schema + backup to the engine's own (engines/<db>/schema, engines/<db>/backup), so no env
      # override is needed here.
      ENGINES_DB="$db" bash "$HERE/$(engine_dir "$db")/load-data.sh"      # ENGINES_DB: which configuration of the engine
      ;;
    *) echo "unknown db: $db" >&2; exit 1;;
  esac
  # Only reached when a load actually ran (the skip guard `continue`s above, and set -e aborts on a
  # failed load), so the fingerprint always describes the schema this backup was really built from.
  write_provenance "$db"
done
echo "== engine load done for: ${dbs[*]}  (sf$SF) =="
