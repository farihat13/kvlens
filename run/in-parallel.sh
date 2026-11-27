#!/usr/bin/env bash
# run/in-parallel.sh -- run the lines of a command file N at a time: one log per line, each line's rc and wall time
# printed when it ends, exit 1 when any line failed. Once a line fails, no new line starts; the running ones finish.
# A line is one shell command (blank lines and lines starting with # are skipped), e.g. one store's measure:
#   uv run --locked kvcost measure --dataset imdb=full --workload j01 props_split_adj_split --nparam 1 --no-check
# Stores side by side are several such lines: their counts are the ones a single measure of every store makes (each
# store is its own grid_bodies process either way; the run ids and wall times differ).
#
#   run/in-parallel.sh -j N [--first-alone] [--logs DIR] FILE
#     -j N           lines at once (default 4)
#     --first-alone  the first line alone, before the others start (a first store may build a one-time record stream
#                    that two first users would race on)
#     --logs DIR     one log per line, <DIR>/<line number>.log (default <output>/logs/in-parallel/<FILE's stem>/)
# Its last line: "[in-parallel] DONE: <n> lines, <failed> failed (<s>s)".
set -uo pipefail
JOBS=4; FIRST=0; LOGS=""; FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -j)            JOBS="${2:?-j needs a number}"; shift 2 ;;
    --first-alone) FIRST=1; shift ;;
    --logs)        LOGS="${2:?--logs needs a directory}"; shift 2 ;;
    -h|--help)     awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    -*)            echo "run/in-parallel.sh: unknown flag $1 (-h)" >&2; exit 2 ;;
    *)             FILE="$1"; shift ;;
  esac
done
[[ -n "$FILE" && -f "$FILE" ]] || { echo "run/in-parallel.sh: give a command file (-h)" >&2; exit 2; }
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "run/in-parallel.sh: -j needs a positive number" >&2; exit 2; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -z "$LOGS" ]]; then
  stem=$(basename "$FILE"); stem=${stem%.*}
  LOGS="$(cd "$ROOT" && uv run --locked python -m kvevaluator.output_tree logs --name in-parallel --name "$stem")" || exit 2
fi
mkdir -p "$LOGS"
mapfile -t LINES < <(grep -v '^[[:space:]]*\(#\|$\)' "$FILE")
t0=$SECONDS; failed=0; running=0; stop=0
declare -A START=() LINE_OF=()
one() {   # one <n>: line n in its own shell from the repo root, its output to <LOGS>/<n>.log
  (cd "$ROOT" && bash -c "${LINES[$1]}") > "$LOGS/$(( $1 + 1 )).log" 2>&1
}
finished() {   # finished: wait for one line to end; its rc and wall
  local pid rc=0
  wait -n -p pid || rc=$?
  local n=${LINE_OF[$pid]}
  echo "[in-parallel] line $(( n + 1 )) rc=$rc wall_s=$(( SECONDS - START[$pid] )) log $LOGS/$(( n + 1 )).log: ${LINES[$n]:0:160}"
  running=$(( running - 1 ))
  if [[ $rc != 0 ]]; then failed=$(( failed + 1 )); stop=1; fi
}
echo "[in-parallel] ${#LINES[@]} lines from $FILE, $JOBS at once$([[ $FIRST == 1 ]] && echo ', the first alone'); logs in $LOGS"
for n in "${!LINES[@]}"; do
  [[ $stop == 1 ]] && break
  while [[ $running -ge $JOBS ]]; do finished; done
  [[ $stop == 1 ]] && break
  one "$n" & START[$!]=$SECONDS; LINE_OF[$!]=$n; running=$(( running + 1 ))
  echo "[in-parallel] line $(( n + 1 )) started: ${LINES[$n]:0:160}"
  if [[ $FIRST == 1 && $n == 0 ]]; then finished; fi
done
while [[ $running -gt 0 ]]; do finished; done
[[ $stop == 1 ]] && echo "[in-parallel] a line failed: the lines after it were not started"
echo "[in-parallel] DONE: ${#LINES[@]} lines, $failed failed ($(( SECONDS - t0 ))s)"
[[ $failed == 0 ]]
