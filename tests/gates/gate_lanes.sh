# gate_lanes.sh -- run a gate recipe's parts in lanes, side by side (sourced by `just cpp-diff` and `just write-gate`).
#
# A recipe defines one bash function per part (part_<name>) and says which parts share a lane. Parts of one lane run
# one after another (they open the same C++ store: `serve` opens a store read-write, so two openers fail); the lanes run
# at the same time. Each part writes its own log and its own exit code; when every lane has ended the logs are printed
# in the recipe's fixed order, so the output reads as a serial run's and does not depend on which lane ended first.
# A lane stops at its first failing part (the rest of that lane is reported as NOT RUN); the other lanes still finish.
#
# The recipe sets, then calls `lanes_run <gate name>`:
#   ORDER   the parts in the order they print
#   LANE    associative array: lane name -> its parts, in the order they run
#   RUN     the lanes to run (all of them for the gate; fewer = a slice, never a gate result)
#   AFTER   parts that run after every lane ended, one after another (they open stores of several lanes); may be empty
#   SLICE   non-empty when RUN is not every lane: the AFTER parts are left out and the last line says it is no gate result
#   PARTS   the directory for the part logs (output/summary/ldbc/sf<SF>/<gate>/parts)
# GATE_LANES_SERIAL=1 runs the lanes one after another (to compare with the parallel run, or on a machine that also
# runs a latency capture).

# A part's rc is its FIRST failing command's: the part runs under `set -e`. Bash switches errexit off in everything
# called on the left of `||` / `&&` or as an `if` condition, the part's own subshell and its `set -e` included, so a
# part's rc would be its LAST command's alone. So _run_part, _run_lane and lanes_run are only ever
# called as plain commands and return 0; a part's verdict is its rc file (_part_ok). The part's subshell tests errexit
# first and fails with rc 98 where it is off, so a later caller cannot bring the hole back in silence.
_run_part() {
  local s=$SECONDS rc=0
  ( set -euo pipefail
    ( set -e; false; exit 0 ) & wait $! && { echo "gate_lanes: errexit is OFF inside part $1 (a caller runs it left of || or &&, or in an if): its rc would be its last command's only"; exit 98; }
    "part_$1" ) > "$PARTS/$1.log" 2>&1 &
  wait $! || rc=$?
  echo "$rc $((SECONDS - s))" > "$PARTS/$1.rc"
}

_part_ok() { local rc _; read -r rc _ < "$PARTS/$1.rc"; [ "$rc" = 0 ]; }

_run_lane() {
  local part
  for part in ${LANE[$1]}; do
    _run_part "$part"
    if ! _part_ok "$part"; then return 0; fi   # the lane stops here: its later parts are reported as NOT RUN
  done
}

lanes_run() {
  local gate="$1" l part t0=$SECONDS failed=0 ran=0 times="" rc secs known
  declare -A pid
  for l in $RUN; do
    [ -n "${LANE[$l]:-}" ] || { echo "$gate: no lane '$l' (lanes: ${!LANE[*]})"; return 2; }
  done
  for part in $ORDER; do   # every part printed is in exactly one lane or in AFTER: a part in neither would never run
    known=0
    for l in "${!LANE[@]}"; do case " ${LANE[$l]} " in *" $part "*) known=$((known + 1)) ;; esac; done
    case " ${AFTER:-} " in *" $part "*) known=$((known + 1)) ;; esac
    [ "$known" = 1 ] || { echo "$gate: part $part is in $known lanes (must be 1): fix the recipe"; return 2; }
  done
  mkdir -p "$PARTS"; rm -f "$PARTS"/*.log "$PARTS"/*.rc
  for l in $RUN; do
    if [ -n "${GATE_LANES_SERIAL:-}" ]; then _run_lane "$l"; else _run_lane "$l" & pid[$l]=$!; fi
  done
  for l in "${!pid[@]}"; do wait "${pid[$l]}" || true; done   # a part's verdict is its own rc file, read below
  local after="${AFTER:-}"; [ -z "${SLICE:-}" ] || after=""
  for part in $after; do _run_part "$part"; done
  for part in $ORDER; do
    local wanted=0
    for l in $RUN; do case " ${LANE[$l]} " in *" $part "*) wanted=1 ;; esac; done
    case " $after " in *" $part "*) wanted=1 ;; esac
    [ "$wanted" = 1 ] || continue
    if [ ! -f "$PARTS/$part.rc" ]; then
      echo "$gate: part $part did NOT RUN (an earlier part of its lane failed)"; failed=$((failed + 1)); continue
    fi
    read -r rc secs < "$PARTS/$part.rc"
    cat "$PARTS/$part.log"; ran=$((ran + 1)); times="$times $part ${secs}s,"
    [ "$rc" = 0 ] || { echo "$gate: part $part FAILED (rc=$rc)"; failed=$((failed + 1)); }
  done
  echo "$gate: $ran parts ran, $failed failed or not run, $((SECONDS - t0))s wall${GATE_LANES_SERIAL:+ (lanes one after another)}, load $(cut -d' ' -f1-3 /proc/loadavg);${times%,}"
  [ -z "${SLICE:-}" ] || echo "$gate: a SLICE (lanes: $RUN): not a gate result"
  [ "$failed" = 0 ]
}
