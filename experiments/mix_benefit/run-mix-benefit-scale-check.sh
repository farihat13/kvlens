#!/usr/bin/env bash
# run-mix-benefit-scale-check.sh -- does a layout chosen at a small scale hold at a larger one: every table of
# the layout-choice scale check (owner: experiments/mix_benefit). Analysis only: opens no store,
# measures nothing; a few minutes. The captures it reads are measured on the measuring machine (three diagonal
# cells, Condensed/Split as the control, Chunked/Chunked at 4 KB, the built mixes).
#   1. compose   compose_cells.py: the nine cells at the large scale from its diagonal cells, Chunked at 64 KB
#                and at 4 KB (--check: each measured off-diagonal cell against its composed rows; at 4 KB only Sp-Co and
#                Co-Sp, the cells with no Chunked part); the small scale's at 4 KB. A scale with no such cell measured
#                (SF30 from 10-30: the diagonal only) composes without --check, and the run says the check was not run
#   2. choose    kvcost search-mix and front: the layouts each grid chooses (result rows in the output tree
#                results-<scale>-<size>/, at summary/ldbc/sf<scale>/mix-search/);
#                layout_scale_check.py: I01 and I02, the layout chosen at the small scale priced at the large one,
#                per chunk size (each scale's own held-out picks)
#   3. tables    scale_check_bytes_per_part.py, scale_check_owners_per_chunk.py, scale_check_record_vs_own_picks.py
#   4. built     scale_check_composed_vs_measured.py: the built mixes, composed against measured, per chunk size
#                (the default set not measured at this scale: skipped, and said; a --mixes given that names no
#                capture: a failure)
# Output (--out, default output/summary/ldbc/sf<large>/scale-check, a working dir): composed-{c64k,c4k}/ and
#   small-scale-composed-c4k/ (composed captures, each a tree: composed/ldbc/sf<scale>/), results-{small,large}-{c64k,c4k}/
#   (mix-search rows, each a tree: summary/ldbc/sf<scale>/mix-search/), analysis-{c64k,c4k}/, tables/,
#   composed-vs-measured-{c64k,c4k}/.
#   experiments/mix_benefit/run-mix-benefit-scale-check.sh [--small DIR] [--large DIR] [--mixes NAME] [--out DIR] [--only STAGE]
# Flags: --small DIR (the small scale's captures/ldbc/<scale> dir; default output/captures/ldbc/sf3);
#        --large DIR (the large scale's captures/ldbc/<scale> dir; default output/captures/ldbc/sf30);
#        --mixes NAME (the built mixes' spec set, inputs/specs/NAME, measured in --large; default mixes_scale_check_20261013);
#        --only compose|choose|tables|built; -h.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
export PYTHONDONTWRITEBYTECODE=1
SMALL="$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale 3)"; LARGE="$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale 30)"; MIXES=mixes_scale_check_20261013; MIXES_GIVEN=; OUT=; ONLY=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --small) SMALL="${2:?--small needs a directory}"; shift 2 ;;
    --large) LARGE="${2:?--large needs a directory}"; shift 2 ;;
    --mixes) MIXES="${2:?--mixes needs a name}"; MIXES_GIVEN=1; shift 2 ;;
    --out)   OUT="${2:?--out needs a directory}"; shift 2 ;;
    --only)  ONLY="${2:?--only needs a stage}"; shift 2 ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
reads() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import capture_in; print(capture_in(sys.argv[1], "interactive").path)' "$1"; }
has() { "$PYTHON" -c 'import os, sys; from kvevaluator.output_tree import Capture; sys.exit(not os.path.exists(Capture(sys.argv[1]).path_of("counts", sys.argv[2])))' "$1" "$2"; }
scale_of() { "$PYTHON" -c 'import glob, json, sys; from kvevaluator.output_tree import Capture
print(json.load(open(sorted(glob.glob(Capture(sys.argv[1]).dir("counts") + "/*.json"))[0]))["sf"])' "$1"; }   # a capture's own scale
LARGE_READS="$(reads "$LARGE")"
[[ -d "$LARGE_READS" ]] || { echo "no capture under $LARGE" >&2; exit 1; }
SF=$(scale_of "$LARGE_READS")
OUT="${OUT:-$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis scale-check)}"
C=experiments/mix_benefit; C4K=props_chunked_c4k_adj_chunked_c4k
mkdir -p "$OUT/logs"
fails=0; unchecked=()   # unchecked: the checks a capture gives nothing to compare with (not run, not passed)
step() {   # step <name> <command...>: its log, its wall time, its exit code on its own line
  local n="$1"; shift; local s=$SECONDS
  "$@" > "$OUT/logs/$n.log" 2>&1
  local rc=$?
  echo "=== rc=$rc $n ($((SECONDS - s)) s): $(tail -n 1 "$OUT/logs/$n.log" | cut -c1-200)"
  [[ $rc -eq 0 ]] || fails=$((fails + 1))
  return $rc
}
want() { [[ -z "$ONLY" || "$ONLY" == "$1" ]]; }
SF_SMALL=$(scale_of "$(reads "$SMALL")")
composed() { "$PYTHON" -m kvevaluator.output_tree composed-dir --dataset ldbc --scale "$2" --root "$OUT/$1"; }
L64=$(composed composed-c64k "$SF"); L4=$(composed composed-c4k "$SF"); S4=$(composed small-scale-composed-c4k "$SF_SMALL")   # composed captures, each a tree's composed/ldbc/sf<scale> place
measured_offdiag() {   # measured_offdiag <setting...>: how many off-diagonal cells over these settings --large measured
  local n=0 p a
  for p in "$@"; do for a in "$@"; do [[ "$p" != "$a" ]] && has "$LARGE_READS" "props_${p}_adj_${a}" && n=$((n + 1)); done; done
  echo "$n"
}
if want compose; then
  CHK64=(--check); CHK4=(--check)
  if [[ $(measured_offdiag split condensed chunked) -eq 0 ]]; then
    echo "=== compose-large-c64k: no measured off-diagonal cell under $LARGE: composed WITHOUT --check (no control cell at sf$SF)"
    CHK64=(); unchecked+=(compose-large-c64k)
  fi
  if [[ $(measured_offdiag split condensed) -eq 0 ]]; then   # a measured cell with a Chunked side is the grid's 64 KB: no control for 4 KB
    echo "=== compose-large-c4k: no measured Sp-Co or Co-Sp under $LARGE: composed WITHOUT --check (no control cell at sf$SF)"
    CHK4=(); unchecked+=(compose-large-c4k)
  fi
  step compose-large-c64k "$PYTHON" -m kvcost.layout_cost.compose_cells --grid "$LARGE" --out "$L64" --whole-grid "${CHK64[@]}"
  step compose-large-c4k "$PYTHON" -m kvcost.layout_cost.compose_cells --grid "$LARGE" --whole-grid --chunked-from "$LARGE" $C4K --out "$L4" "${CHK4[@]}"
  step compose-small-c4k "$PYTHON" -m kvcost.layout_cost.compose_cells --grid "$SMALL" --whole-grid --chunked-from "$SMALL" $C4K --out "$S4"
fi
if want choose; then
  # the layouts each grid chooses, as mix-search result rows (kvcost search-mix: the fewest ops + bytes / 4 KB; search-mix
  # --byte-limit named: the fewest ops within B = 1, 2, 3, 5, the staircase), each grid into its own results dir; seconds
  # per call. Then layout_scale_check.py reads them and prices each layout at the large scale (nothing is solved there)
  rows_of() {   # rows_of <name> <sf> <grid>: search-mix (and --front) for I01 and I02, both option sets, read from the
    # grid's tree, into the output tree $OUT/results-<name>
    local n="$1" sf="$2" g="$3" w s from; from=$(tree_of "$g") || exit 1
    for w in i01 i02; do for s in by-direction per-type; do
      local d=(); [[ $s == by-direction ]] && d=(--by-direction)
      step "rows-$n-$w-$s-fewest" in_tree "$OUT/results-$n" "$PYTHON" -m kvcost.cli.cli search-mix --dataset "ldbc=$sf" \
        --from "$from" --workload $w --settings Sp,Co,Ch64k "${d[@]}" --cost-model wops_bytes4k
      step "rows-$n-$w-$s-named" in_tree "$OUT/results-$n" "$PYTHON" -m kvcost.cli.cli search-mix --dataset "ldbc=$sf" \
        --from "$from" --workload $w --settings Sp,Co,Ch64k "${d[@]}" --byte-limit named
    done; done
  }
  results() { "$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$2" --root "$OUT/results-$1"; }
  rows_of small-c64k "$SF_SMALL" "$SMALL"; rows_of large-c64k "$SF" "$L64"
  rows_of small-c4k "$SF_SMALL" "$S4"; rows_of large-c4k "$SF" "$L4"
  step choose-c64k "$PYTHON" $C/layout_scale_check.py --small "$SMALL" --small-results "$(results small-c64k "$SF_SMALL")" \
    --large "$L64" --large-results "$(results large-c64k "$SF")" --picks own --out "$OUT/analysis-c64k"
  step choose-c4k "$PYTHON" $C/layout_scale_check.py --small "$S4" --small-results "$(results small-c4k "$SF_SMALL")" --small-own-picks \
    --large "$L4" --large-results "$(results large-c4k "$SF")" --picks own --out "$OUT/analysis-c4k"
fi
if want tables; then
  step bytes-per-part "$PYTHON" $C/scale_check_bytes_per_part.py --small-c64k "$SMALL" --small-c4k "$S4" --large-c64k "$L64" --large-c4k "$L4" --out "$OUT/tables"
  step owners-per-chunk "$PYTHON" $C/scale_check_owners_per_chunk.py --small "$SMALL" --small-c4k "$S4" --large "$LARGE" --large-c64k "$L64" --large-c4k "$L4" --out "$OUT/tables"
  step record-vs-own-picks "$PYTHON" $C/scale_check_record_vs_own_picks.py --grid "$L64" --out "$OUT/tables"
fi
mixes_measured() { local m; for m in "inputs/specs/$MIXES"/*.json; do has "$LARGE_READS" "$(basename "$m" .json)" && return 0; done; return 1; }
if want built && ! mixes_measured && [[ -n "$MIXES_GIVEN" ]]; then
  echo "=== rc=1 built: --mixes $MIXES: none of its layouts measured under $LARGE"; fails=$((fails + 1))
elif want built && ! mixes_measured; then   # the default set, not measured at this scale
  echo "=== built: no built mixes of $MIXES measured under $LARGE: SKIPPED (composed vs measured not checked at sf$SF)"
  unchecked+=(built)
elif want built; then
  step built-c64k "$PYTHON" $C/scale_check_composed_vs_measured.py --sf "$SF" --cells "$L64" --mixes "$LARGE" --spec-dir "inputs/specs/$MIXES" --chunk-bytes 65536 --out "$OUT/composed-vs-measured-c64k"
  step built-c4k "$PYTHON" $C/scale_check_composed_vs_measured.py --sf "$SF" --cells "$L4" --mixes "$LARGE" --spec-dir "inputs/specs/$MIXES" --chunk-bytes 4096 --out "$OUT/composed-vs-measured-c4k"
fi
echo "[$(basename "$0")] $fails steps failed; ${#unchecked[@]} checks not run (nothing measured to compare with${unchecked[*]:+: ${unchecked[*]}}); $SECONDS s; output $OUT"
[[ $fails -eq 0 ]]
