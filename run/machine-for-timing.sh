#!/usr/bin/env bash
# run/machine-for-timing.sh -- the machine set up for a latency capture, one reproducible script (every latency driver calls it).
#   run/machine-for-timing.sh prepare --state <run dir> [--numa-balancing off|keep] [--allow-foreign]
#       sync; kernel.numa_balancing -> 0 (unless keep) and vm.swappiness -> 1, the values before recorded ONCE in
#       <run dir>/machine_before.json (a resumed run sets them again, the first record kept); a live engine or another
#       evaluator server running is warned, said in the row and marked foreign_load in <run dir>/stale.jsonl (kvevaluator.stale;
#       an earlier run's notes moved aside first), which the latency driver puts on its records; --allow-foreign is a no-op,
#       kept so old commands run; refuses (exit 1) when a sysctl fails
#   run/machine-for-timing.sh drop --state <run dir> [--label L]   sync + drop_caches 3 (exit 1 when it fails)
#   run/machine-for-timing.sh restore --state <run dir>    puts back what prepare recorded (the record moved aside after)
#   run/machine-for-timing.sh check --state <run dir> [--label L]  the state now, nothing changed
# Every call appends one row to <run dir>/machine_state.csv: utc, host, action, label, kernel, CPU governor and mean MHz
# (when readable), numa_balancing, swappiness, load1, MemFree MB per NUMA node, the evaluator servers, live engines and
# tmux sessions running, free GB on the run dir's filesystem, result. The changes need passwordless sudo
# (sudo -n). MACHINE_TEST_DIR=<dir> (a test): its files numa_balancing, swappiness, drop_caches stand in for the
# /proc ones, and nothing needs sudo.
set -u
case "${1:-}" in -h|--help) sed -n '2,/^set -u$/p' "$0" | sed '$d'; exit 0;; esac
ACTION=${1:?prepare | drop | restore | check}; shift
STATE=""; LABEL=""; BAL=off
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
while [ $# -gt 0 ]; do
  case "$1" in
    --state) STATE="${2:?}"; shift 2 ;;
    --label) LABEL="${2:?}"; shift 2 ;;
    --numa-balancing) BAL="${2:?}"; shift 2 ;;
    --allow-foreign) shift ;;   # a no-op: foreign processes are warned and marked
    *) echo "run/machine-for-timing.sh: unknown flag $1" >&2; exit 2 ;;
  esac
done
[ -n "$STATE" ] || { echo "run/machine-for-timing.sh: --state <run dir> is required" >&2; exit 2; }
case "$BAL" in off|keep) ;; *) echo "--numa-balancing off|keep, got $BAL" >&2; exit 2 ;; esac
mkdir -p "$STATE"
CSV=$STATE/machine_state.csv; BEFORE=$STATE/machine_before.json
T=${MACHINE_TEST_DIR:-}
p_bal=${T:+$T/numa_balancing}; p_bal=${p_bal:-/proc/sys/kernel/numa_balancing}
p_swap=${T:+$T/swappiness}; p_swap=${p_swap:-/proc/sys/vm/swappiness}

setsys() {  # setsys <kernel.numa_balancing|vm.swappiness> <value>
  if [ -n "$T" ]; then echo "$2" > "$T/${1#*.}"; else sudo -n sysctl -w "$1=$2" > /dev/null; fi
}
nodes=$(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | sed 's#.*/node##' | sort -n)
servers() { ps -eo comm | grep -cx kvevaluator || true; }
# dgraph(-<build>): the kvstats, profile, histogram... builds run as dgraph-kvstats etc. (engines/dgraph/dgraph-binaries.conf)
engines() { ps -eo comm | grep -E '^(dgraph(-[a-z0-9]+)?|nebula-graphd|nebula-storaged|nebula-metad|arangod)$' | sort | uniq -c | awk '{printf "%s%s x%s", (NR > 1 ? ";" : ""), $2, $1}'; }
row() {  # row <result>: one line of the run's machine state
  local gov mhz free tm
  gov=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a)
  mhz=$(cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null | awk '{s += $1; n++} END {print n ? int(s / n / 1000) : "n/a"}')
  free=$(for n in $nodes; do awk '/MemFree/ {printf "%d,", $4 / 1024}' /sys/devices/system/node/node$n/meminfo; done)
  tm=$(tmux ls -F '#S' 2>/dev/null | paste -sd ';' -)
  [ -s "$CSV" ] || echo "utc,host,action,label,kernel,governor,cpu_mhz_mean,numa_balancing,swappiness,load1,$(for n in $nodes; do echo -n "memfree_mb_node$n,"; done)evaluator_servers,live_engines,tmux_sessions,free_gb,result" > "$CSV"
  echo "$(date -u +%FT%TZ),$(hostname -s),$ACTION,${LABEL//,/;},$(uname -r),$gov,$mhz,$(cat "$p_bal"),$(cat "$p_swap"),$(cut -d' ' -f1 /proc/loadavg),${free}$(servers),$(engines),${tm},$(df -BG --output=avail "$STATE" | tail -1 | tr -dc 0-9),${1//,/;}" >> "$CSV"
  echo "machine $ACTION${LABEL:+ ($LABEL)} $(date -u +%T): numa_balancing $(cat "$p_bal"), swappiness $(cat "$p_swap"), load $(cut -d' ' -f1 /proc/loadavg), MemFree MB per node ${free%,}, evaluator servers $(servers), live engines [$(engines)]: $1"
}

case "$ACTION" in
  prepare)
    sync
    eng=$(engines); srv=$(servers); foreign=""
    # this run's marks (kvevaluator.stale) in the run dir: an earlier run's moved aside; the venv from run/env.sh, in a
    # subshell (env.sh changes the directory)
    st=$(cd "$STATE" && pwd)
    ( source "$HERE/env.sh" && "$PYTHON" -m kvevaluator.stale start --into "$st" ) || { row "FAILED: kvevaluator.stale start"; exit 1; }
    if [ -n "$eng" ] || [ "$srv" -gt 0 ]; then
      foreign="live engines [$eng], evaluator servers $srv, load1 $(cut -d' ' -f1 /proc/loadavg) at prepare"
      ( source "$HERE/env.sh" && "$PYTHON" -m kvevaluator.stale note --into "$st" foreign_load "$foreign" ) \
        || { row "FAILED: kvevaluator.stale note"; exit 1; }
    fi
    if [ ! -s "$BEFORE" ]; then
      echo "{\"numa_balancing\": $(cat "$p_bal"), \"swappiness\": $(cat "$p_swap"), \"recorded\": \"$(date -u +%FT%TZ)\", \"host\": \"$(hostname -s)\", \"stand_in\": \"$T\"}" > "$BEFORE"
    fi
    if [ "$BAL" = off ] && [ "$(cat "$p_bal")" != 0 ]; then
      setsys kernel.numa_balancing 0 || { row "FAILED: sysctl kernel.numa_balancing=0"; exit 1; }; fi
    if [ "$(cat "$p_swap")" != 1 ]; then
      setsys vm.swappiness 1 || { row "FAILED: sysctl vm.swappiness=1"; exit 1; }; fi
    row "prepared (before: $(tr -d '\n' < "$BEFORE"))${foreign:+; WARNING foreign load, marked foreign_load: $foreign}"
    ;;
  drop)
    sync
    if [ -n "$T" ]; then echo 3 > "$T/drop_caches"; else echo 3 | sudo -n tee /proc/sys/vm/drop_caches > /dev/null; fi \
      || { row "FAILED: drop_caches"; exit 1; }
    row "page cache dropped"
    ;;
  restore)
    if [ ! -s "$BEFORE" ]; then row "nothing recorded to restore"; exit 0; fi
    # a record is restored only in the mode and on the host that wrote it: a test's record (stand-in files) never sets the
    # real sysctls, nor the reverse (a test's swappiness 60 was applied to the real machine for 19 s)
    rec_t=$(grep -o '"stand_in": "[^"]*"' "$BEFORE" | sed 's/.*: "//; s/"$//'); rec_h=$(grep -o '"host": "[^"]*"' "$BEFORE" | sed 's/.*: "//; s/"$//')
    if [ "$rec_t" != "$T" ] || [ "$rec_h" != "$(hostname -s)" ]; then
      row "REFUSED: the record was written on ${rec_h:-?} with stand-in [${rec_t}], this call is on $(hostname -s) with [${T}]"; exit 1; fi
    b=$(grep -o '"numa_balancing": [0-9]*' "$BEFORE" | grep -o '[0-9]*$'); s=$(grep -o '"swappiness": [0-9]*' "$BEFORE" | grep -o '[0-9]*$')
    ok=1
    [ "$(cat "$p_bal")" = "$b" ] || setsys kernel.numa_balancing "$b" || ok=0
    [ "$(cat "$p_swap")" = "$s" ] || setsys vm.swappiness "$s" || ok=0
    if [ $ok = 1 ]; then mv "$BEFORE" "$BEFORE.restored-$(date -u +%Y%m%dT%H%M%S)"; row "restored (numa_balancing $b, swappiness $s)"
    else row "RESTORE FAILED: wanted numa_balancing $b, swappiness $s (sudo -n sysctl -w by hand)"; exit 1; fi
    ;;
  check) row "checked" ;;
  *) echo "run/machine-for-timing.sh: unknown action $ACTION (prepare | drop | restore | check)" >&2; exit 2 ;;
esac
