# measure-preflight.sh -- sourced by a timing driver (run/latency-cells.sh): the
# stale marks (mark, notes_into) and the binary preflight (preflight). Needs PYTHON, BIN (the kvevaluator binary) and
# DRY (1: a dry run writes no note).
#
# A moved input found here (kvevaluator.stale) is not a refusal: one warning line, then a note into the dir of every record
# this run writes after it (notes_into, right before the tool that writes there: the cell's counts capture for grid_bodies,
# grid_latency's --out root), whose writer folds <dir>/stale.jsonl in.
NOTES=(); declare -A STARTED=() WRITTEN=()
mark() { echo "[stale] WARNING $1: $2"; NOTES+=("$1"$'\t'"$2"); }
notes_into() {  # notes_into <dir> [<unit>]: the notes so far into <dir>/stale.jsonl, or the unit's stale-<unit>.jsonl (a cell
  # in the shared counts capture: parallel --cells calls neither mark nor move each other's notes); on its first use in
  # this run, an earlier run's notes moved aside
  local d=$1 u=${2:-} n k
  [ $DRY = 1 ] && return 0
  mkdir -p "$d"; k="$d"$'\t'"$u"
  [ -n "${STARTED[$k]:-}" ] || { "$PYTHON" -m kvevaluator.stale start --into "$d" ${u:+--unit "$u"}; STARTED[$k]=1; }
  for n in "${NOTES[@]}"; do
    [ -n "${WRITTEN[$k$'\t'$n]:-}" ] && continue
    "$PYTHON" -m kvevaluator.stale note --into "$d" ${u:+--unit "$u"} "${n%%$'\t'*}" "${n#*$'\t'}" 2> /dev/null || { echo "stale note into $d failed" >&2; exit 1; }
    WRITTEN[$k$'\t'$n]=1
  done
}
# preflight [timed]: the binary: its sha and dirty flag, read from the binary; dirty, unstamped, or C++ build inputs other
# than HEAD's: warned and marked binary_unclean (with cpp_diff_files). With `timed` (before timed stores): another
# kvevaluator process alive is warned and marked foreign_load (with its count and the load)
preflight() {
  local out l
  out=$("$PYTHON" - "$BIN" <<'PYEOF'
import json, subprocess, sys
from kvevaluator.capture.binary import binary_cpp_verdict, binary_state, build_input_paths
from kvevaluator.paths import ROOT, checkout_of
v = json.loads(subprocess.run([sys.argv[1], "--version"], capture_output=True, text=True).stdout)
tree = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
sha, flag, state = v.get("binary_git_sha"), v.get("binary_dirty"), binary_state(v)
print(f"binary {sys.argv[1]}: sha {sha} dirty {flag} ({state}); tree HEAD {tree}")
# a line "STALE <kind>: <detail>" is a note for the records of this run (the driver's mark)
if state in ("dirty", "unknown"):
    print(f"STALE binary_unclean: binary {str(sha)[:10]} is {state} (its dirty flag {flag!r})")
if sha in (None, "unknown", "HEAD"):
    print(f"STALE binary_unclean: the binary's build had no git to stamp it (sha {sha}): its C++ is not compared with HEAD's")
else:
    ok, msg = binary_cpp_verdict(sha, allow="")    # the one rule for the binary's C++ inputs (.md aside); allow="": no named control
    print(f"C++ build inputs equal to HEAD's: {'YES' if ok else 'NO'}")
    if not ok:
        # the files, by binary_cpp_verdict's diff (its message names three of them)
        r = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", sha, "HEAD", "--", *build_input_paths(),
                            ":(exclude,glob)**/*.md"], capture_output=True, text=True) if checkout_of(ROOT) else None
        if r is not None and r.returncode == 0:
            print(f"STALE binary_unclean: binary {sha[:10]}: its C++ build inputs differ from HEAD's; cpp_diff_files {' '.join(r.stdout.split())}")
        else:
            print(f"STALE binary_unclean: {msg.removeprefix('REFUSED: ').split('. Rebuild')[0]}")
PYEOF
)
  while IFS= read -r l; do
    case "$l" in "STALE "*) l=${l#STALE }; mark "${l%%: *}" "${l#*: }" ;; *) echo "$l" ;; esac
  done <<< "$out"
  local others load1; others=$(pgrep -fc "kvevaluator (serve|run)" || true); load1=$(cut -d' ' -f1 /proc/loadavg)
  echo "machine: $(nproc) cores, $(free -g | awk 'NR==2{print $7}') GB available, load $(cut -d' ' -f1-3 /proc/loadavg), other kvevaluator processes: $others"
  if [ "$others" != 0 ] && [ "${1:-}" = timed ]; then
    mark foreign_load "$others other kvevaluator process(es) alive before the timed stores, load1 $load1"
  fi
}
