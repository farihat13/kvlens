#!/bin/bash
# engines/setup.sh -- install what the databases' builds and servers need on this machine (Ubuntu 24.04; needs sudo).
# The needs are engines/needs.tsv. It prints a line per need (present / to install), installs the missing ones (apt
# packages in one call; Go's tarball into /usr/local/go; a service started), then checks every need again.
# A second run installs nothing. `just setup-engines` runs it for all three.
# Usage: bash engines/setup.sh [--db dgraph|nebula|arangodb|tugraph|all|comma list] [--dry-run]
#   --db       default all: dgraph, nebula, arangodb (NebulaGraph's key is nebula); TuGraph by name
#   --dry-run  print the lines and what would be installed; install nothing
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="all"; DRY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --db) DB="$2"; shift 2;;
    --dry-run) DRY=1; shift;;
    -h|--help) sed -n '2,9p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 1;;
  esac
done
source "$HERE/env.sh"          # PATH with /usr/local/go/bin, as the builds see it
source "$HERE/check-tools.sh"

if [[ "$DB" == "all" ]]; then dbs=(dgraph nebula arangodb); else IFS=',' read -ra dbs <<<"$DB"; fi
rows=""                        # every need of the named databases, once: how, what, check
for db in "${dbs[@]}"; do
  db="$(engine_dir "$db")"          # the key's dir: nebula is nebulagraph, tugraph-detached is tugraph
  [[ -d "$HERE/$db" ]] || { echo "unknown db: $db (dgraph|nebula|arangodb|tugraph|tugraph-detached|all)" >&2; exit 1; }
  rows+="$(needs_of "$db")"$'\n'
done
rows="$(sed '/^$/d' <<<"$rows" | awk '!seen[$0]++')"

unmet() {                      # the rows whose check fails now
  local how what check
  while IFS=$'\t' read -r how what check; do
    need_met "$what" "$check" || printf '%s\t%s\t%s\n' "$how" "$what" "$check"
  done <<<"$rows"
}

missing="$(unmet)"
total=$(wc -l <<<"$rows")
while IFS=$'\t' read -r how what check; do
  if grep -qxF "$how"$'\t'"$what"$'\t'"$check" <<<"$missing"; then state="to install"; else state="present"; fi
  [[ "$state" == "to install" && "$how" == "main" ]] && state="MISSING (the main README's Needs; not installed here)"
  printf '  %-11s %-8s %-20s %s\n' "$state" "$how" "$what" "${check#cmd:}"
done <<<"$rows"

apt_pkgs="$(awk -F'\t' '$1 == "apt" { print $2 }' <<<"$missing" | awk '!seen[$0]++' | tr '\n' ' ')"
go_version="$(awk -F'\t' '$1 == "go" { print $2 }' <<<"$missing" | head -1)"
services="$(awk -F'\t' '$1 == "service" { print $2 }' <<<"$missing" | tr '\n' ' ')"
main_missing="$(awk -F'\t' '$1 == "main" { print $2 }' <<<"$missing" | tr '\n' ' ')"
n_missing=$( [[ -z "$missing" ]] && echo 0 || wc -l <<<"$missing" )
echo "setup-engines (${dbs[*]}): $total needs, $((total - n_missing)) present, $n_missing to install" \
     "${apt_pkgs:+; apt: $apt_pkgs}${go_version:+; Go $go_version into /usr/local/go (a 75 MB download)}${services:+; start: $services}"
[[ -z "$main_missing" ]] || { echo "missing from the main README's Needs: $main_missing" >&2; exit 1; }
if [[ -z "$missing" ]]; then echo "setup-engines OK: nothing to install"; exit 0; fi
if [[ "$DRY" == 1 ]]; then
  [[ -z "$apt_pkgs" ]] || apt-get -s install $apt_pkgs 2> /dev/null | grep -E "newly installed" || true
  echo "setup-engines: a dry run, nothing installed"; exit 0
fi

t0=$SECONDS
if [[ -n "$apt_pkgs" ]]; then
  sudo apt-get update -qq
  sudo apt-get install --assume-no $apt_pkgs 2> /dev/null | grep -E "Need to get|After this operation" || true   # the size
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $apt_pkgs
fi
if [[ -n "$go_version" ]]; then
  arch="$(dpkg --print-architecture 2> /dev/null || echo amd64)"
  curl -LsSf "https://go.dev/dl/go${go_version}.linux-${arch}.tar.gz" | sudo tar -C /usr/local -xz
fi
for s in $services; do sudo systemctl start "$s"; done

left="$(unmet)"
if [[ -n "$left" ]]; then
  echo "setup-engines FAILED: still missing after the install:" >&2
  awk -F'\t' '{ print "  " $2 " (" $3 ")" }' <<<"$left" >&2
  exit 1
fi
echo "setup-engines OK: $total needs present, $n_missing installed now in $((SECONDS - t0)) s"
