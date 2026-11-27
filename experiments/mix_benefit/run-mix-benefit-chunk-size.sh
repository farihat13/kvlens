#!/usr/bin/env bash
# run-mix-benefit-chunk-size.sh -- the Chunked setting at several chunk sizes side by side, for I01 and I02 (owner:
# experiments/mix_benefit). Analysis only: opens no store, measures nothing.
# The captures it reads: the nine cells (64 KB) at --grid, and one Chunked/Chunked cell per other size in
# --captures-root (measured by `kvcost measure --workload i01` and `--family microbench` on Ch<k>-Ch<k>: LDBC
# reported + held-out, writes, microbench); each a captures/ldbc/<scale> dir.
#   1. compose  compose_cells.py: the nine cells with Chunked at each other size, from the grid's diagonal cells and
#               that size's Chunked/Chunked cell -> <out>/composed-c<k>/composed/ldbc/sf<SF>/ (composed again every run, seconds: kept
#               when byte-identical; else the old grid and the fronts and pricing derived from it are moved aside)
#   2. report   chunk_size_report.py -> <out>/{uniform_cells,owners_per_chunk,mixed_layouts}.csv and tables.md
#   3. figure   plot_chunk_size_single_vs_mix.py -> <out>/plots/single-vs-mix-chunk-size.png and <out>/single_vs_mix.csv
#               (I01: the best single layout against the best mixed layout per byte limit, per chunk size)
#   4. fronts   experiments/layout_choice/equal_bytes_front.py per size, 64 KB included (I01, I02) ->
#               <out>/equal-bytes-front-c<k>/ (kept if present)
#   5. curve    plot_equal_bytes_curve_chunk_sizes.py -> <out>/plots/equal-bytes-curve_<workload>.png: one mix line per size
#   6. gain     plot_budget_gain_chunk_sizes.py and plot_budget_gain_3panel_chunk_sizes.py -> <out>/plots/budget-gain*_<workload>.png
#   7. price    (--price-all) experiments/layout_choice/run-layout-choice.sh on each size's nine-cell grid ->
#               <out>/layout-choice-c<k>/, an output tree of its own (every workload, W01-W15, I01, I02; kept if present)
#   experiments/mix_benefit/run-mix-benefit-chunk-size.sh --options FILE [--grid DIR] [--measured DIR] [--sizes 1k,4k,16k,128k,256k]
#       [--captures-root DIR] [--out DIR] [--jobs N] [--price-all] [--note TEXT]
# The scale is the grid's own (its run records' sf): the picks, the fronts and the captions follow it.
# Flags: --grid DIR (the nine cells at 64 KB, measured or composed; default output/captures/ldbc/sf3; a grid with only
#        the diagonals measured reads the other six from composed/ beside it: kvcost compose); --measured DIR
#        (the MEASURED grid whose diagonal cells' steps the report's owners table reads; default --grid; at a scale that
#        measured only the diagonal, --grid is the composed nine-cell grid and --measured the measured one: a composed
#        grid has no steps files); --sizes (the sizes besides 64 KB);
#        --captures-root DIR (holds each size's Chunked/Chunked cell; default --grid); --out DIR (default
#        output/summary/ldbc/sf<SF>/chunk-size-report);
#        --options FILE (I01's options.csv of the same grid's analysis: the single layouts with no Chunked side; required);
#        --jobs N (default 8); --price-all; --note TEXT (the captions' source sentence); -h.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"
export PYTHONDONTWRITEBYTECODE=1
GRID="$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale 3)"; MEASURED=""; SIZES=1k,4k,16k,128k,256k; CAPTURES=""; OUT=""; JOBS=8; OPTIONS=""; PRICE=0
NOTE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --grid)          GRID="${2:?--grid needs a directory}"; shift 2 ;;
    --measured)      MEASURED="${2:?--measured needs a directory}"; shift 2 ;;
    --sizes)         SIZES="${2:?--sizes needs a comma list}"; shift 2 ;;
    --captures-root) CAPTURES="${2:?--captures-root needs a directory}"; shift 2 ;;
    --out)           OUT="${2:?--out needs a directory}"; shift 2 ;;
    --options)       OPTIONS="${2:?--options needs a file}"; shift 2 ;;
    --jobs)          JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    --price-all)     PRICE=1; shift ;;
    --note)          NOTE="${2:?--note needs text}"; shift 2 ;;
    -h|--help)       awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *)               echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[[ -n "$OPTIONS" ]] || { echo "--options <I01's options.csv> is required" >&2; exit 1; }
CAPTURES=${CAPTURES:-$GRID}; MEASURED=${MEASURED:-$GRID}
reads() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import capture_in; print(capture_in(sys.argv[1], "interactive").path)' "$1"; }
has() { "$PYTHON" -c 'import os, sys; from kvevaluator.output_tree import Capture; sys.exit(not os.path.exists(Capture(sys.argv[1]).path_of("counts", sys.argv[2])))' "$1" "$2"; }
GRID_READS="$(reads "$GRID")"
SF=$("$PYTHON" -c 'import glob, json, sys; from kvevaluator.output_tree import Capture
print(json.load(open(sorted(glob.glob(Capture(sys.argv[1]).dir("counts") + "/*.json"))[0]))["sf"])' "$GRID_READS") \
  || { echo "no run record under $GRID_READS: the grid's scale is unknown" >&2; exit 1; }
OUT=${OUT:-$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis chunk-size-report)}
NOTE=${NOTE:-"SF$SF. Counts are deterministic: no error bars."}
echo "[$(basename "$0")] sf$SF (the grid's run records); grid $GRID; measured $MEASURED"
t0=$SECONDS
C=experiments/mix_benefit
mkdir -p "$OUT"
IFS=, read -ra KS <<< "$SIZES"
bytes() { echo $(( ${1%k} * 1024 )); }
composed() { "$PYTHON" -m kvevaluator.output_tree composed-dir --dataset ldbc --scale "$SF" --root "$OUT/composed-c$1"; }   # a size's composed grid: a tree of its own
# moved_aside <line> <k>: compose_cells replaced the grid at size k, so what steps 4 and 7 derived from it goes aside too
moved_aside() {
  [[ "$1" == *"moved aside"* ]] || return 0
  local d
  for d in "$OUT/equal-bytes-front-c$2" "$OUT/layout-choice-c$2"; do
    if [[ -e "$d" ]]; then mv "$d" "$d.aside-$(date -u +%Y%m%dT%H%M%SZ)"; echo "  $d: derived from the old grid, moved aside"; fi
  done
}
# the nine 64 KB layouts are read from --grid by one rule (output_tree.layout_capture: measured itself, else composed/
# beside it, by kvcost compose); a layout that is neither stops the run
miss=$("$PYTHON" -c 'import os, sys; from kvevaluator.layouts import CELLS; from kvevaluator.output_tree import layout_capture
print(" ".join(c for c in CELLS if not os.path.exists(layout_capture(sys.argv[1], "interactive", c).path_of("counts", c))))' "$GRID")
[[ -z "$miss" ]] || { echo "64 KB: no capture of $miss under $GRID or its composed/ (kvcost compose --dataset ldbc=$SF)" >&2; exit 1; }
COMPOSED=(); CAPTURE_ARGS=(); FRONTS=(--front "64 KB=$OUT/equal-bytes-front-c64k")
for k in "${KS[@]}"; do
  cell="props_chunked_c${k}_adj_chunked_c${k}"; dst="$(composed "$k")"
  has "$(reads "$CAPTURES")" "$cell" || { echo "no $cell in $CAPTURES (kvcost measure --dataset ldbc=<SF> --workload i01 Ch${k}-Ch${k}; and --family microbench)" >&2; exit 1; }
  # composed again every run (seconds): an existing grid is kept when byte-identical, else moved aside (compose_cells --replace)
  line=$("$PYTHON" -m kvcost.layout_cost.compose_cells --grid "$GRID" --out "$dst" --whole-grid --chunked-from "$CAPTURES" "$cell" --replace | tail -1)
  echo "$line"
  moved_aside "$line" "$k"
  COMPOSED+=(--composed "$(bytes $k)=$dst"); CAPTURE_ARGS+=(--captures "$(bytes $k)=$CAPTURES")
  FRONTS+=(--front "${k%k} KB=$OUT/equal-bytes-front-c$k")
done
# the mixed layouts each size's grid chooses, as mix-search result rows (as run-mix-benefit-scale-check.sh reads them):
# search-mix for the fewest ops + bytes / 4 KB, search-mix --byte-limit named for B = 1, 2, 3, 5 (the staircase); seconds each
RESULTS=()
rows_of() {   # rows_of <bytes> <grid> <dir>: I01 and I02, per type and direction, read from the grid's tree, into the tree <dir>
  local w from; from=$(tree_of "$2") || exit 1
  for w in i01 i02; do
    in_tree "$3" "$PYTHON" -m kvcost.cli.cli search-mix --dataset "ldbc=$SF" --from "$from" --workload $w \
      --settings Sp,Co,Ch64k --by-direction --cost-model wops_bytes4k | tail -1
    in_tree "$3" "$PYTHON" -m kvcost.cli.cli search-mix --dataset "ldbc=$SF" --from "$from" --workload $w \
      --settings Sp,Co,Ch64k --by-direction --byte-limit named | tail -1
  done
  RESULTS+=(--results "$1=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --root "$3")")
}
rows_of 65536 "$GRID" "$OUT/mix-search-c64k"
for k in "${KS[@]}"; do rows_of "$(bytes $k)" "$(composed "$k")" "$OUT/mix-search-c$k"; done
"$PYTHON" $C/chunk_size_report.py --measured "$MEASURED" --composed "65536=$GRID" "${COMPOSED[@]}" "${CAPTURE_ARGS[@]}" \
  "${RESULTS[@]}" --out "$OUT" --jobs "$JOBS"
"$PYTHON" $C/plot_chunk_size_single_vs_mix.py --root "$OUT" --options "$OPTIONS" --note "$NOTE" | tail -1
for k in 64k "${KS[@]}"; do
  g=$([[ $k == 64k ]] && echo "$GRID" || composed "$k")
  if [[ -f "$OUT/equal-bytes-front-c$k/i02-r-ldbc-mix-reads/equal_bytes_front.csv" ]]; then echo "front at $OUT/equal-bytes-front-c$k: kept"; continue; fi
  "$PYTHON" experiments/layout_choice/equal_bytes_front.py --sf "$SF" --grid "$g" --root "$OUT/equal-bytes-front-c$k" \
    --workload i01,i02 --no-microbench --jobs 12 | grep "limits, .* rows"
done
for w in i01 i02; do
  "$PYTHON" $C/plot_equal_bytes_curve_chunk_sizes.py --workload $w "${FRONTS[@]}" --out "$OUT/plots" --source "$NOTE"
  "$PYTHON" $C/plot_budget_gain_chunk_sizes.py --workload $w "${FRONTS[@]}" --out "$OUT/plots" --source "$NOTE"
  for dir in "" --by-direction; do   # per type, then per type and direction
    "$PYTHON" $C/plot_budget_gain_3panel_chunk_sizes.py --workload $w $dir "${FRONTS[@]}" --out "$OUT/plots" --source "$NOTE" | head -1
  done
done
if [[ $PRICE -eq 1 ]]; then
  for k in 64k "${KS[@]}"; do
    g=$([[ $k == 64k ]] && echo "$GRID" || composed "$k")
    # the marker is written only after a whole pricing run succeeds: a stopped run is priced again, never kept half done
    if [[ -f "$OUT/layout-choice-c$k/priced.done" ]]; then echo "pricing at $OUT/layout-choice-c$k: kept"; continue; fi
    # each size's pricing in a tree of its own (the output dir set for this call only): the sizes do not overwrite each other
    KVEVAL_OUTPUT_DIR="$OUT/layout-choice-c$k" experiments/layout_choice/run-layout-choice.sh --sf "$SF" --grid "$g" > "$OUT/layout-choice-c$k.log" 2>&1 \
      || { echo "run-layout-choice on $g failed: $OUT/layout-choice-c$k.log" >&2; exit 1; }
    date -u +%FT%TZ > "$OUT/layout-choice-c$k/priced.done"
    echo "priced c$k: $OUT/layout-choice-c$k"
  done
fi
echo "[$(basename "$0")] DONE: $(ls "$OUT"/*.csv | wc -l) CSVs, tables.md and plots/ in $OUT ($((SECONDS - t0))s)"
