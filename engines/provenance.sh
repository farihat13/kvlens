#!/bin/bash
# engines/provenance.sh — tie an engine backup to the schema that built it.
#
# The hazard this closes: engines/load-data.sh builds a backup from engines/<db>/schema/, and from
# then on engines/start-server.sh PREFERS that backup — forever, with no staleness check. Edit the schema
# and the next start silently restores the old physical design, so every KV-op comparison is measured
# against a design that is not the one in the file you are reading. Nothing about it looks wrong.
#
# So each load records the schema's sha256 next to the backup, and each start re-checks it. Same idea as
# the dgraph variant builds' `buildinfo` + archived patches: fingerprint the input that decides what the
# store contains.
#
# Sourced by load-data.sh and each <db>/start-server.sh hook. Needs SF and engine_dir (env.sh).
#
#   write_provenance <db>    # after a successful load
#   check_provenance <db>    # before trusting an engine backup
#
# The provenance file is a sibling of the backup, not a file inside it, so it works the same whether the
# backup is a plain dir (dgraph, nebula) or a symlink to a timestamped dir (arango).
#
# A load writes its record to backup/load-sf<SF>.txt (untracked, like the backup itself), so a load leaves the
# tree clean. The tracked backup/provenance-sf<SF>.txt is the record of the backups the archived results were
# measured on; it is read only when no load record exists (a checkout that restores those backups).

# Paths from this file's own dir, never a caller's $HERE (a <db>/start-server.sh hook's HERE is engines/<db>/).
_PROV_ENGINES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The schema file that decides each DB's physical design.
_schema_file() {
  case "$1" in
    dgraph)   echo "$_PROV_ENGINES/dgraph/schema/ldbcTypes.schema" ;;
    nebula)   echo "$_PROV_ENGINES/nebulagraph/schema/schema.gql" ;;
    arangodb) echo "$_PROV_ENGINES/arangodb/schema/ldbc_schema.json" ;;
    tugraph|tugraph-detached) echo "$_PROV_ENGINES/tugraph/schema/ldbc_import.json" ;;
    *) echo "unknown db for provenance: $1" >&2; return 1 ;;
  esac
}

_load_record() { echo "$_PROV_ENGINES/$(engine_dir "$1")/backup/load-sf${SF}$(engine_backup_tag "$1").txt"; }
_provenance_file() {   # the record a check reads: this machine's load, else the tracked one
  local own; own="$(_load_record "$1")"
  if [[ -f "$own" ]]; then echo "$own"; else echo "$_PROV_ENGINES/$(engine_dir "$1")/backup/provenance-sf${SF}$(engine_backup_tag "$1").txt"; fi
}

write_provenance() {
  local db="$1" schema pfile
  schema="$(_schema_file "$db")" || return 1
  pfile="$(_load_record "$db")"
  mkdir -p "$(dirname "$pfile")"
  {
    echo "schema_sha256=$(sha256sum "$schema" | cut -d' ' -f1)"
    echo "schema_path=${schema#"$_PROV_ENGINES/"}"
    echo "sf=${SF}"
    echo "created=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git_sha=$(git -C "$_PROV_ENGINES" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  } > "$pfile"
  echo "   provenance: ${pfile#"$_PROV_ENGINES/"}"
}

# Returns 0 to proceed, 1 to abort. A MISSING record is a warning (backups predating this file are
# legitimate); a MISMATCH is an error (someone edited the schema and did not reload).
check_provenance() {
  local db="$1" schema pfile recorded current
  schema="$(_schema_file "$db")" || return 0
  pfile="$(_provenance_file "$db")"

  if [[ ! -f "$pfile" ]]; then
    echo "   WARNING: no provenance for this engine backup — cannot tell which schema built it."
    echo "            (made before provenance existed? rebuild to fingerprint it:"
    echo "             rm -rf \"$_PROV_ENGINES/$(engine_dir "$db")/backup\" && bash engines/load-data.sh --db $db)"
    return 0
  fi

  recorded=$(grep '^schema_sha256=' "$pfile" | cut -d= -f2)
  current=$(sha256sum "$schema" | cut -d' ' -f1)
  if [[ "$recorded" != "$current" ]]; then
    echo "ERROR: [$db] engine backup is STALE — it was built from a different schema." >&2
    echo "  schema:   ${schema#"$_PROV_ENGINES/"}" >&2
    echo "  recorded: $recorded  (see ${pfile#"$_PROV_ENGINES/"})" >&2
    echo "  current:  $current" >&2
    echo "  Restoring it would measure a physical design that is NOT the one in that file." >&2
    echo "  Rebuild:  rm -rf \"$_PROV_ENGINES/$(engine_dir "$db")/backup\" && bash engines/load-data.sh --db $db" >&2
    return 1
  fi
  echo "   provenance OK (schema sha256 ${current:0:12}…)"
  return 0
}
