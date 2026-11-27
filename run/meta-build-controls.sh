#!/usr/bin/env bash
# run/meta-build-controls.sh -- build the known-bad control binaries that `run/meta-gates.sh --controls DIR` needs.
# Owner: the configurable format's gates (experiments/meta/controls/README.md: what each patch reintroduces).
# One binary per patch of run/meta-gates.sh's CONTROL_PATCHES: the sha under test plus that patch, built in the
# machine's build cache (run/build_cpp.py --sha --patch: the sha's controls entry, controls-<key>, applies each patch in
# turn, builds incrementally and keeps a copy; a control built once is reused). A patch that applies only with fuzz is a
# failure (a VOID control):
# re-cut it (the controls README); an offset is a warning. The binaries report dirty = true with the tested sha.
# Recorded cost: about 2 min for the six with the unpatched base (-j 56); a control already built is a lookup.
# Usage: run/meta-build-controls.sh <out dir> [sha, default this tree's HEAD] [-h]
#   <out dir>   gets one symlink per patch, named by the patch stem, to the binary in its cache entry, and
#               build-<stem>.log. A control finds its repo root from its own location (schema/schema_path.cc): through
#               the symlink that is its entry's source, the archive it was built from (check_control_tree.py checks
#               that it holds this tree's LDBC schema)
case "${1:-}" in ""|-h|--help) sed -n '2,/^case /p' "$0" | grep '^#' | cut -c3-; exit 0 ;; esac
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh" || exit 1
[ $# -le 2 ] || { echo "usage: run/meta-build-controls.sh <out dir> [sha] (no scratch checkout: the controls build in the build cache)" >&2; exit 2; }
OUT="${1:?the out dir}"; SHA="$(git rev-parse --verify "${2:-HEAD}^{commit}")" || exit 1
eval "$(grep '^CONTROL_PATCHES=' run/meta-gates.sh)"    # the one list of write-effects controls
eval "$(grep '^ROW_ORDER_CONTROL=' run/meta-gates.sh | cut -d' ' -f1)"   # and the row-order gate's one
eval "$(grep '^STORE_CONFIG_CONTROL=' run/meta-gates.sh | cut -d' ' -f1)"   # and the store-config gate's two
eval "$(grep '^STORE_FILL_CONTROL=' run/meta-gates.sh | cut -d' ' -f1)"
CONTROL_PATCHES+=("${ROW_ORDER_CONTROL:?no ROW_ORDER_CONTROL line in run/meta-gates.sh}" "${STORE_CONFIG_CONTROL:?no STORE_CONFIG_CONTROL line in run/meta-gates.sh}"
                  "${STORE_FILL_CONTROL:?no STORE_FILL_CONTROL line in run/meta-gates.sh}")
[ "${#CONTROL_PATCHES[@]}" -gt 0 ] || { echo "CONTROLS FAIL: no CONTROL_PATCHES line in run/meta-gates.sh"; exit 1; }
mkdir -p "$OUT"; OUT="$(realpath "$OUT")"
LDBC_SCHEMA=$("$PYTHON" -m kvevaluator.datasets schema-path ldbc --relative) || exit 1   # repo-relative: each binary resolves it against its own root
echo "controls at ${SHA:0:10} in the build cache, $(date -u +%T): ${CONTROL_PATCHES[*]}"
for p in "${CONTROL_PATCHES[@]}"; do
  s=$SECONDS
  # the patch as the tested sha has it (a later re-cut must not test an older sha)
  git show "$SHA:experiments/meta/controls/$p.patch" > "$OUT/$p.patch" || { echo "CONTROLS FAIL: no $p.patch at $SHA"; exit 1; }
  "$PYTHON" run/build_cpp.py --sha "$SHA" --patch "$OUT/$p.patch" > "$OUT/build-$p.log" 2>&1; rc=$?
  bin=$(tail -n 1 "$OUT/build-$p.log")
  off=$(grep -iE '^Hunk .*(offset|fuzz)' "$OUT/build-$p.log" | tr '\n' ' ')
  [ $rc = 0 ] && [ -x "$bin" ] && ln -sfn "$bin" "$OUT/$p"
  echo "=== rc=$rc control $p ($((SECONDS - s))s) $("$OUT/$p" --version 2>/dev/null | grep 'binary_' | tr -d '\n')${off:+  PATCH APPLIED WITH AN OFFSET (re-cut it when convenient): $off}"
  [ $rc = 0 ] || { echo "CONTROLS FAIL build $p ($OUT/build-$p.log)"; tail -n 5 "$OUT/build-$p.log"; exit 1; }
  root=$("$OUT/$p" schema-path resolve "$LDBC_SCHEMA" | sed -n 's/.*"root":"\([^"]*\)".*/\1/p')
  want=$(cd "$(dirname "$bin")/../.." && pwd -P)   # the entry's source: <entry>/src/build/cpp/kvevaluator
  [ "$root" = "$want" ] || { echo "CONTROLS FAIL: $OUT/$p resolves its root as '$root', not its entry's source ($want)"; exit 1; }
done
echo "CONTROLS DONE $(date -u +%T)"
