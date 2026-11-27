#!/usr/bin/env bash
# run/watch-memory.sh -- run a command and sample the memory of the evaluator servers under it: every N seconds, the
# RSS of each kvevaluator process below the command, summed per store; the peak per store and of the total is kept.
# A store is the --cells or --format of the nearest grid_bodies process above the server (one per measure of one
# store), else its --spec-dir's name (a whole spec set measured in one call, e.g. grid_chunked_1k); a server under no
# grid_bodies counts toward the total only. The command's exit code is passed through.
#
#   run/watch-memory.sh [--every 5] [--out DIR] [--stop-gb N] -- <command> [args...]
#     --every S    seconds between samples (default 5)
#     --out DIR    one file per store, <DIR>/<store>.peak, and <DIR>/total.peak: the peak in GB, one decimal
#                  (default: no files, the log lines only)
#     --stop-gb N  stop the command (TERM to it and every process under it) when the total passes N GB; exit 9
# Its last lines: "[watch-memory] <store> peak_rss_gb=<GB>" per store, then "[watch-memory] total peak_rss_gb=<GB>
# rc=<rc>" (and " STOPPED_BY_RSS" when the ceiling stopped it). e.g. the JOB chain's counts:
#   run/watch-memory.sh --out "$L/peak" -- run/in-parallel.sh -j 6 "$L/counts.cmds"
set -uo pipefail
EVERY=5; OUT=""; STOP_GB=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --every)   EVERY="${2:?--every needs seconds}"; shift 2 ;;
    --out)     OUT="${2:?--out needs a directory}"; shift 2 ;;
    --stop-gb) STOP_GB="${2:?--stop-gb needs GB}"; shift 2 ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    --)        shift; break ;;
    *)         echo "run/watch-memory.sh: unknown flag $1 (-h); the command goes after --" >&2; exit 2 ;;
  esac
done
[[ $# -gt 0 ]] || { echo "run/watch-memory.sh: give the command after -- (-h)" >&2; exit 2; }
[[ -n "$OUT" ]] && { mkdir -p "$OUT" && rm -f "$OUT"/*.peak; }
STOP_KB=""; [[ -n "$STOP_GB" ]] && STOP_KB=$(awk -v g="$STOP_GB" 'BEGIN { printf "%d", g * 1048576 }')

declare -A PEAK=()     # store -> peak KB ("total" for the sum)
sample() {   # sample <pid>: one ps; per store and in total, the RSS (KB) of the kvevaluator processes under <pid>
  ps -eo pid=,ppid=,rss=,comm=,args= | awk -v root="$1" '
    { ppid[$1] = $2; rss[$1] = $3; comm[$1] = $4; line[$1] = $0 }
    END {
      for (p in ppid) {
        if (comm[p] !~ /^kvevaluator/) continue
        q = ppid[p]; under = (p == root); u = ""
        while (q in ppid && q > 1) {           # up to the root: the nearest grid_bodies above names the store
          if (u == "" && line[q] ~ /kvevaluator[.]measure[.]grid_bodies/) {
            n = split(line[q], a, " "); sd = ""
            for (i = 1; i < n; i++) {
              if (a[i] == "--cells" || a[i] == "--format") u = a[i + 1]
              if (a[i] == "--spec-dir") { sd = a[i + 1]; sub(/\/+$/, "", sd); sub(/.*\//, "", sd) }
            }
            if (u == "") u = sd            # a whole spec set: named by its dir
          }
          if (q == root) { under = 1; break }
          q = ppid[q]
        }
        if (!under) continue
        total += rss[p]
        if (u != "") kb[u] += rss[p]
      }
      for (u in kb) print u, kb[u]
      print "total", total + 0
    }'
}
gb() { awk -v k="$1" 'BEGIN { printf "%.1f", k / 1048576 }'; }
descendants() { local c; for c in $(pgrep -P "$1" 2>/dev/null); do echo "$c"; descendants "$c"; done; }
stop_tree() {   # stop_tree <pid>: TERM to every process under it, then to it
  local d; d=$(descendants "$1"); [[ -n "$d" ]] && kill -TERM $d 2>/dev/null; kill -TERM "$1" 2>/dev/null; true
}

"$@" &
pid=$!
trap 'stop_tree $pid' INT TERM
stopped=0; rc=""
while [[ -z "$rc" ]]; do
  while read -r u kb; do
    [[ -z "$u" ]] && continue
    if (( kb > ${PEAK[$u]:-0} )); then
      PEAK[$u]=$kb
      [[ -n "$OUT" ]] && gb "$kb" > "$OUT/$u.peak"
    fi
    if [[ $u == total && -n "$STOP_KB" ]] && (( kb > STOP_KB )); then
      echo "[watch-memory] STOP: the servers' RSS $(gb "$kb") GB passed --stop-gb $STOP_GB: stopping the command" >&2
      stopped=1; stop_tree "$pid"; break 2
    fi
  done < <(sample "$pid")
  sleep "$EVERY" & nap=$!
  ended=""; st=0; wait -n -p ended "$pid" "$nap" || st=$?    # the interval, or the command's end if sooner
  if [[ "$ended" == "$pid" ]]; then rc=$st; kill "$nap" 2>/dev/null; wait "$nap" 2>/dev/null; fi
done
if [[ -z "$rc" ]]; then rc=0; wait "$pid" || rc=$?; fi
for u in $(printf '%s\n' "${!PEAK[@]}" | grep -vx total | sort); do echo "[watch-memory] $u peak_rss_gb=$(gb "${PEAK[$u]}")"; done
echo "[watch-memory] total peak_rss_gb=$(gb "${PEAK[total]:-0}") rc=$rc$( ((stopped)) && echo ' STOPPED_BY_RSS')"
((stopped)) && exit 9
exit "$rc"
