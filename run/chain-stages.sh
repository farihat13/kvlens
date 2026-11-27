# run/chain-stages.sh -- the stages of a driver chain, one way (sourced, like run/env.sh; used by run/meta-gates.sh).
#
# A chain is a list of stages. Each stage's output goes to its own log; its verdict is its command's own exit code,
# read on its own line (never through a pipe); its rc and wall time are recorded. The chain says the commit under test
# once at its top and ends with ONE verdict at the start of a line: <TAG>_DONE, or <TAG>_FAIL <stage>.
#
#   CHAIN_TAG=MY_RUN; CHAIN_LOGS=output/logs/my-run/<time>
#   source run/chain-stages.sh
#   chain_begin [binary]                    # tree commit (+ whether tracked files differ), the binary's commit, time, load
#   stage "ingest" run/ingest-cpp-stores.sh --sf 0.1
#   stage -v '^WRITE GATE GREEN' "write-gate" just write-gate 0.1     # rc 0 AND this line in its log, else a failure
#   stage -c 9 '^[0-9]+ cases, 0 failed$' "my check" python3 check.py   # and the line's first number is 9 or more
#   chain_end                               # the wall-time table, then <TAG>_DONE only when every stage passed
#
# -c FLOOR REGEX is a count with a floor, not an exact count: a check that gains a case still passes, one that ran
# fewer than FLOOR fails (a check that tested 0 cases is a failure). The count is the first number in the last line
# that matches REGEX; FLOOR is the count the day the stage was written.
# A failing stage ends the chain at once (`exit 1`, after its <TAG>_FAIL line). CHAIN_KEEP_GOING=1 runs the later
# stages too and leaves the verdict to chain_end (for checks that do not depend on each other).
# Stages side by side: run each group in a background subshell with its own CHAIN_STAGES file, wait, then give the
# files to chain_end in the order they should print.
# CHAIN_BUDGET_S: chain_end prints the total against it and OVER BUDGET above it (a warning, never a failure).
# CHAIN_EXPECT: a file of stage names, one per line: chain_end fails when one of them has no row (a stage that did
# not run is a failure, never a pass). The list comes from the chain's own stage calls, not from a second copy:
# run them once with CHAIN_DRY=1 (in a subshell), and `stage` writes its name there instead of running its command.
# A stage is a command, not a shell function that relies on `set -e`: the rc is taken with `||`, which switches
# errexit off inside a function.

: "${CHAIN_TAG:?set CHAIN_TAG before sourcing run/chain-stages.sh}"
: "${CHAIN_LOGS:?set CHAIN_LOGS (a directory for the stage logs) before sourcing run/chain-stages.sh}"
mkdir -p "$CHAIN_LOGS"
: "${CHAIN_STAGES:=$CHAIN_LOGS/stages.tsv}"
CHAIN_T0=$SECONDS

chain_begin() {   # chain_begin [binary]
  local tree dirty=""
  tree=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
  git diff --quiet HEAD 2>/dev/null || dirty=", tracked files differ from it"
  echo "=== $CHAIN_TAG: tree $tree$dirty; start $(date -u +%FT%TZ); load $(cut -d' ' -f1-3 /proc/loadavg); logs in $CHAIN_LOGS"
  if [ -n "${1:-}" ]; then echo "    binary $1: $("$1" --version | grep 'binary_' | tr -d '\n ')"; fi
}

stage() {   # stage [-v REGEX | -c FLOOR REGEX] <name> <command...>
  local verdict="" floor="" line n
  if [ "$1" = -v ]; then verdict="$2"; shift 2; fi
  if [ "$1" = -c ]; then floor="$2"; verdict="$3"; shift 3; fi
  local name="$1"; shift
  if [ -n "${CHAIN_DRY:-}" ]; then printf '%s\n' "$name" >> "${CHAIN_EXPECT:?CHAIN_DRY needs CHAIN_EXPECT}"; return 0; fi
  local log s=$SECONDS rc=0 note=""
  log="$CHAIN_LOGS/$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_').log"
  "$@" > "$log" 2>&1 || rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$verdict" ]; then
    line=$(grep -E "$verdict" "$log" | tail -n 1 || true)
    if [ -z "$line" ]; then
      rc=97; note=" (the command's rc was 0, but its log has no verdict line /$verdict/)"
    elif [ -n "$floor" ]; then
      n=$(printf '%s\n' "$line" | grep -oE '[0-9]+' | head -n 1)
      if [ "${n:-0}" -lt "$floor" ]; then
        rc=97; note=" (its verdict line counts ${n:-no number}, below the floor $floor: fewer cases ran than the stage was written for)"
      fi
    fi
  fi
  printf '%s\t%s\t%s\t%s\n' "$name" "$rc" "$((SECONDS - s))" "$log" >> "$CHAIN_STAGES"
  echo "=== rc=$rc $name ($((SECONDS - s))s)$note"
  if [ "$rc" -ne 0 ] && [ -z "${CHAIN_KEEP_GOING:-}" ]; then echo "${CHAIN_TAG}_FAIL $name"; exit 1; fi
  return 0
}

chain_end() {   # chain_end [stages file...]: the table and the verdict; returns 0 only on <TAG>_DONE
  local files=("$@") f name rc secs log n=0 total=0 first_bad="" ran=$'\n' want
  [ ${#files[@]} -gt 0 ] || files=("$CHAIN_STAGES")
  echo "=== $CHAIN_TAG wall time per stage"
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    while IFS=$'\t' read -r name rc secs log; do
      printf '    %6s s  rc=%-3s %s\n' "$secs" "$rc" "$name"
      n=$((n + 1)); total=$((total + secs)); ran="$ran$name"$'\n'
      [ "$rc" = 0 ] || [ -n "$first_bad" ] || first_bad="$name"
    done < "$f"
  done
  if [ -n "${CHAIN_EXPECT:-}" ]; then   # every expected stage has a row, or the chain fails
    [ -s "$CHAIN_EXPECT" ] || { echo "${CHAIN_TAG}_FAIL the list of expected stages is empty ($CHAIN_EXPECT)"; return 1; }
    while IFS= read -r want; do
      case "$ran" in *$'\n'"$want"$'\n'*) continue ;; esac
      printf '    %6s    NOT RUN %s\n' "-" "$want"
      [ -n "$first_bad" ] || first_bad="$want (did not run)"
    done < "$CHAIN_EXPECT"
  fi
  local wall=$((SECONDS - CHAIN_T0))
  echo "    $n stages, $total s summed, $wall s wall; end $(date -u +%FT%TZ); load $(cut -d' ' -f1-3 /proc/loadavg)"
  if [ -n "${CHAIN_BUDGET_S:-}" ]; then
    if [ "$wall" -gt "$CHAIN_BUDGET_S" ]; then echo "    OVER BUDGET: $wall s wall against a budget of $CHAIN_BUDGET_S s"
    else echo "    within budget: $wall s wall against $CHAIN_BUDGET_S s"; fi
  fi
  if [ "$n" -eq 0 ]; then echo "${CHAIN_TAG}_FAIL no stage ran"; return 1; fi
  if [ -n "$first_bad" ]; then echo "${CHAIN_TAG}_FAIL $first_bad"; return 1; fi
  echo "${CHAIN_TAG}_DONE"
}
