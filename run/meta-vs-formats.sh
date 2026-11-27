#!/usr/bin/env bash
# run/meta-vs-formats.sh -- meta against the separate engine formats (experiments/meta/check_meta_vs_formats.py):
#   1. each engine-shaped corner spec (inputs/specs/corners/corner_<E>.json) vs the format E, LDBC and writes:
#      the same answers, and the same per-step work outside the differences corners/expected_diffs.json lists;
#   2. every grid cell (inputs/specs/grid/*.json) vs nebula on LDBC: the same answers.
# Builds its stores under output/checks/meta-vs-formats/ldbc/sf<SF>/ (LDBC) and output/stores/ldbc/sf<SF>/ (writes, grid_writes)
# on first run, reused after. Each check's full output goes to output/logs/meta-vs-formats/sf<SF>/.
# The LDBC checks open only stores under output/checks/meta-vs-formats/ldbc/sf<SF>/, so they can run beside any gate; they run in
# lanes, one per engine format's store there (a store has one opener at a time; nebula's lane holds its corner and the
# nine grid cells). The writes checks go through grid_writes on the shared stores, one after another.
# The verdict lines print in the fixed order below, whichever lane ends first.
#   Usage:  run/meta-vs-formats.sh [--sf 0.1] [--nparam 3] [--families interactive|writes|all]
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"

NPARAM=3; WORKLOADS=all
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)     SF="${2:?--sf needs a value}"; shift 2;;
    --nparam) NPARAM="${2:?--nparam needs a value}"; shift 2;;
    --families) WORKLOADS="${2:?--families needs interactive, writes or all}"; shift 2;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *)        echo "unknown argument: $1" >&2; exit 1;;
  esac
done

LOGS=$("$PYTHON" -c 'import sys; from kvevaluator.output_tree import Tree, place; print(Tree().logs("meta-vs-formats", place("ldbc", sys.argv[1]).name))' "$SF") || exit 1
mkdir -p "$LOGS"
t0=$(date +%s)
[[ "$WORKLOADS" == interactive || "$WORKLOADS" == writes || "$WORKLOADS" == all ]] || { echo "--families: interactive, writes or all" >&2; exit 1; }
check() {   # name, args...: one check; its verdict = its own exit code, kept in <name>.rc beside its log
  local name=$1; shift
  local rc=0
  "$PYTHON" experiments/meta/check_meta_vs_formats.py "$@" --sf "$SF" --nparam "$NPARAM" > "$LOGS/$name.txt" 2>&1 || rc=$?
  echo "$rc" > "$LOGS/$name.rc"
}
NAMES=()   # the checks of this call, in the order they print
interactive_lane() {   # one engine format's store under meta-vs-formats/: its corner, then (nebula) the grid cells' answers
  local e=$1 spec
  check "corner_$e-interactive" --corner "$e" --family interactive
  if [[ "$e" == nebula ]]; then
    for spec in inputs/specs/grid/*.json; do
      check "$(basename "$spec" .json)-answers" --spec "$spec" --format nebula --answers-only
    done
  fi
}
for e in nebula pertype_csr dgraph; do
  for wl in interactive writes; do
    [[ "$WORKLOADS" == all || "$WORKLOADS" == "$wl" ]] && NAMES+=("corner_$e-$wl")
  done
done
if [[ "$WORKLOADS" != writes ]]; then
  for spec in inputs/specs/grid/*.json; do NAMES+=("$(basename "$spec" .json)-answers"); done
fi
for n in "${NAMES[@]}"; do rm -f "$LOGS/$n.rc"; done
if [[ "$WORKLOADS" != writes ]]; then
  PIDS=()
  for e in nebula pertype_csr dgraph; do interactive_lane "$e" & PIDS+=($!); done
  for p in "${PIDS[@]}"; do wait "$p" || true; done   # a check's verdict is its rc file, read below
fi
if [[ "$WORKLOADS" != interactive ]]; then
  for e in nebula pertype_csr dgraph; do check "corner_$e-writes" --corner "$e" --family writes; done
fi
fail=0
for n in "${NAMES[@]}"; do
  if [[ ! -f "$LOGS/$n.rc" ]]; then echo "[$n] DID NOT RUN"; fail=1; continue; fi
  echo "[$n] $(tail -1 "$LOGS/$n.txt")"
  [[ "$(cat "$LOGS/$n.rc")" == 0 ]] || fail=1
done
echo "[$(basename "$0")] $([[ $fail -eq 0 ]] && echo PASS || echo FAIL) (sf$SF, ${#NAMES[@]} checks$([[ "$WORKLOADS" == all ]] || echo ", $WORKLOADS only"), $(( $(date +%s) - t0 ))s)"
exit $fail
