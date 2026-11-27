#!/usr/bin/env bash
# run-w16-mix-latency.sh -- I01's picked mixes at five byte limits, built and TIMED beside the nine uniform layouts
# (its rule is pre-registered: experiments/layout_choice/w16_latency_score.py).
# Stages (one at a time, --stage; each prints its wall):
#   specs    experiments/layout_choice/pick_specs.py: the four mix specs from a layout-choice snapshot, checked against its
#            budget_picks.csv (never typed) -> inputs/specs/mixes_w16/ + mixes_w16-picks.json
#   counts   the nine cells + the four mixes measured (kvcost measure; stores built on first use), check_grid (answers ==
#            the nine), check_mix_vs_built (measured == predicted on every measure): a mix that fails is not timed
#   latency  python/kvcost/latency/grid_latency.py per cell, ONE regime at a time in the order --regimes gives (steady first:
#            the headline; a later regime can be cut), the pre-registered order (order(): the five stores first, each mix
#            before its best single, the three repeat cells again at the end under <regime>-repeat); two output roots
#            (grid_latency keeps one counts capture per root): <out>/grid/interactive/<regime>/meta-<cell>/ and
#            <out>/mixes/interactive/<regime>/meta-<stem>/. Before the first store: the preflight (the binary's sha + dirty flag,
#            the tree's sha, the record checked against the captures, every store's 19 picks, the machine's facts; a moved
#            input is marked, below); before each store its SST facts, the load and foreign evaluator processes; after each (store, regime) grid_latency's
#            gates (counts against the capture: a difference marks the timing; cells per pass). A stage writes <out>/DONE-<stage>[-<regimes>]
#            only when it ran to the end (never under --dry-run); a failure stops the chain (set -e).
#   ic10-bodies  IC10 alone, both bodies (the point-read body and the picked scan body), WARM regime, two copies each in the
#            order point, scan, point, scan, on every store whose pick is the scan (item 9 of the prereg) ->
#            <out>/ic10-bodies/<body>/{grid,mixes}/interactive/warm-p0[-copy2]/meta-<cell>/
#   writes   python/kvevaluator/measure/cppwlatency.py per store: the write suite applied once on a disposable arena (IU1-8 at the
#            apply wall; 4 GB memtable, no flush). --write-cache steady (the default): the reads' steady
#            settings (inputs/contract/store_config.json: the SF's steady cache, direct I/O), two runs per store, each on its own
#            arena: A, the 4 legs per write (practice, two controls, apply: each write's own blocks warmed by its practice
#            leg; the engine-write split) -> <out>/writes/steady-settings-<MB>mb-4leg-p1-noflush/meta-<cell>/; then B, the
#            writes after a steady read warm-up (the store's steady read capture from --stage latency warms the cache
#            until its hit rate settles, then each write once, apply only; its counts must equal A's) ->
#            <out>/writes/after-steady-warmup-<MB>mb-p1-noflush/meta-<cell>/ (+ warmup-passes.csv); --write-warmup none: A only.
#            --write-kvtimer N (with B): then the KV-timer passes (the RocksDB share): B's protocol at perf level 2 + the
#            KV timer (each RocksDB call timed at the API boundary, as the reads' steady-<MB>mb-kvtimer passes), each pass
#            on its own arena after its own read warm-up, its counts held to B's -> <out>/writes/after-steady-warmup-<MB>mb-
#            kvtimer-noflush/meta-<cell>/run-<k>/, k = 0..N-1. Passes accumulate: a run adds only the run-<k> dirs missing
#            below N (a later --write-kvtimer 10 after 5 adds 5); a run-<k> left without its meta (a pass that stopped)
#            is moved aside to run-<k>.incomplete-<time> and re-run. The share: experiments/layout_choice/write_rocksdb_share.py.
#            --write-cache warm: an 8 GB buffered cache, 4 legs -> <out>/writes/durable-p1-noflush/...
#            --timings-root DIR: B reads each store's steady read capture from run/latency-cells.sh --root DIR's timings
#            (DIR/timings/ldbc/sf<SF>/interactive/meta-<cell>/steady-<MB>mb-p<L>/) and lands beside them, in
#            DIR/timings/ldbc/sf<SF>/writes/meta-<cell>/after-steady-warmup-<MB>mb-p1-noflush/, where
#            paper/derive/latency_steady_ldbc.py reads it (A stays in <out>/writes/)
#   score    experiments/layout_choice/w16_latency_score.py: noise (the repeats -> tau_w) then score (the words) per regime
#            -> <out>/analysis/<regime>/{noise.json,w16_latency.csv,w16_words.csv}
#   plot     experiments/layout_choice/plot_w16_mix_latency.py, plot_w16_engine_arms.py, plot_w16_stores.py (per query, per group,
#            by mix) -> <out>/analysis/plots/
#   experiments/layout_choice/run-w16-mix-latency.sh --sf 0.1 --stage counts
#   experiments/layout_choice/run-w16-mix-latency.sh --sf 3 --stage latency --regimes steady --cells props_split_adj_split      # the pilot cell
#   experiments/layout_choice/run-w16-mix-latency.sh --sf 3 --stage latency --regimes steady,warm,cold                         # the chain
#   experiments/layout_choice/run-w16-mix-latency.sh --sf 3 --stage ic10-bodies                                                # IC10's two bodies, warm
# Flags: --sf <SF> | $SF; --stage specs|counts|latency|ic10-bodies|writes|score|plot (required); --regimes steady,warm,cold (default
#        steady); --cells a,b (default: the pre-registered order, from mixes_w16-picks.json + the grid; a mix by its spec
#        stem); --no-repeats; --steady CACHE_MB,PASSES (default: SF x 0.1 GiB -> 307 at sf3, 3072 at sf30; 3 passes);
#        --cold N (default 1); --write-cache steady|warm (the writes stage's read settings, default steady);
#        --write-warmup reads|none (with --write-cache steady: B, the writes after the read warm-up, beside A; default reads);
#        --timings-root DIR (the writes stage: B beside run/latency-cells.sh --root DIR's timings, above);
#        --write-kvtimer N (the writes stage, with B: N KV-timer passes per store in all, the missing ones run; default 0,
#        none);
#        --write-spec-dir DIR (repeatable; the writes stage's B: the dirs a cell's spec file is looked up in, instead of
#        $MIXDIR for a mix and inputs/specs/grid* for a single layout; e.g. a spec set kept beside a snapshot);
#        --steady-perf-level L (default 2: the walls of record, no per-operation timer; 3 = the
#        captures named steady-<MB>mb-p3, e.g. to --stage score an older root); --breakdown (each steady run
#        also writes its breakdown capture, steady-<MB>mb-breakdown: one more pass at perf 3 + the KV timer, for
#        the latency-levels figures);
#        --out DIR (default output/summary/ldbc/sf<SF>/i01-mix-latency); --snapshot DIR (specs: the layout-choice
#        dir the picks come from, REQUIRED for that stage: no default, a default would rewrite the specs from a stale solve);
#        --engine-arms fmt=cell,... (default dgraph=props_split_adj_condensed: an engine FORMAT timed
#        on its own store with its registry pins right after the cell nearest its layout, steady and warm only; '' = none;
#        the nebula arm is added later with nebula=props_condensed_adj_split); --pins-root mixes=DIR | sets=DIR | formats=DIR |
#        workload=WL=DIR (repeatable; the sf3 preflight's grid_pick_record --check roots beside the grid, replacing the default:
#        the microbench, BI and JOB captures; e.g. a snapshot's roots the pass did not re-measure); --drop-caches (the OS page cache dropped before each
#        timed store, run/machine-for-timing.sh drop: a row per drop in <out>/machine_state.csv; a failed drop stops the stage); --dry-run; --allow-foreign (a
#        no-op, kept so old commands run: another kvevaluator process alive is warned and marked foreign_load); -h. Long stages: tmux + a .log.
# Marks (kvevaluator.stale): a dirty or unstamped binary, or one whose C++ build inputs are not HEAD's (binary_unclean, with
#        cpp_diff_files), a pick record that differs from the captures (body_moved) and another kvevaluator process before a
#        store (foreign_load) are warned and noted into the record dirs (<out>/grid, <out>/mixes, <out>/ic10-bodies/<body>/<sub>,
#        <out>/writes/<pass>/meta-<cell>: <dir>/stale.jsonl); the run goes on. A gate's refusal stays one.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../../run/env.sh"

STAGE=""; REGIMES="steady"; CELLS=""; REPEATS=1; STEADY=""; COLD=1; OUT=""; DRY=0; TIMINGS_ROOT=""
STEADY_PERF=2; BREAKDOWN=(); PINS_ROOTS=(); DROP=0; WRITE_CACHE=steady; WRITE_WARMUP=reads; WRITE_SPEC_DIRS=(); WRITE_KVTIMER=0
ENGINE_ARMS="dgraph=props_split_adj_condensed"
SNAPSHOT=""
MIXDIR="inputs/specs/mixes_w16"; PICKS="inputs/specs/mixes_w16-picks.json"
JOB_SCALE=full                                   # the JOB capture the record's job section was decided on
capture() { "$PYTHON" -m kvevaluator.output_tree capture --family "$1" --scale "$2"; }   # every capture path comes from there
grid_counts() { capture interactive "$SF"; }                                     # the nine cells' and the mixes' counts capture
counts_file() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import Capture; print(Capture(sys.argv[1]).path_of("counts", sys.argv[2]))' "$1" "$2"; }
captures_place() { "$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale "$1"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)       SF="${2:?}"; shift 2 ;;
    --stage)    STAGE="${2:?}"; shift 2 ;;
    --regimes)  REGIMES="${2:?}"; shift 2 ;;
    --cells)    CELLS="${2:?}"; shift 2 ;;
    --no-repeats) REPEATS=0; shift ;;
    --steady)   STEADY="${2:?}"; shift 2 ;;
    --cold)     COLD="${2:?}"; shift 2 ;;
    --steady-perf-level) STEADY_PERF="${2:?}"; shift 2 ;;
    --breakdown) BREAKDOWN=(--steady-breakdown); shift ;;
    --out)      OUT="${2:?}"; shift 2 ;;
    --snapshot) SNAPSHOT="${2:?}"; shift 2 ;;
    --engine-arms) ENGINE_ARMS="$2"; shift 2 ;;
    --pins-root) case "${2:?}" in mixes=*|sets=*|formats=*|workload=*) PINS_ROOTS+=("--${2%%=*}" "${2#*=}") ;; *) echo "--pins-root mixes=DIR | sets=DIR | formats=DIR | workload=WL=DIR, got $2" >&2; exit 1 ;; esac; shift 2 ;;
    --drop-caches) DROP=1; shift ;;
    --write-cache) WRITE_CACHE="${2:?}"; shift 2 ;;
    --write-warmup) WRITE_WARMUP="${2:?}"; shift 2 ;;
    --write-spec-dir) WRITE_SPEC_DIRS+=("${2:?}"); shift 2 ;;
    --write-kvtimer) WRITE_KVTIMER="${2:?}"; shift 2 ;;
    --timings-root) TIMINGS_ROOT="$(realpath -m "${2:?}")"; shift 2 ;;
    --dry-run)  DRY=1; shift ;;
    --allow-foreign) shift ;;   # a no-op: another kvevaluator process alive is warned and marked foreign_load
    -h|--help)  awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    *)          echo "unknown flag: $1" >&2; exit 1 ;;
  esac
done
[ -n "$STAGE" ] || { echo "--stage is required" >&2; exit 1; }
case "$WRITE_CACHE" in steady|warm) ;; *) echo "--write-cache steady|warm, got $WRITE_CACHE" >&2; exit 1 ;; esac
case "$WRITE_WARMUP" in reads|none) ;; *) echo "--write-warmup reads|none, got $WRITE_WARMUP" >&2; exit 1 ;; esac
[[ "$WRITE_KVTIMER" =~ ^[0-9]+$ ]] || { echo "--write-kvtimer N (a count), got $WRITE_KVTIMER" >&2; exit 1; }
if [ "$WRITE_KVTIMER" -gt 0 ] && { [ "$WRITE_CACHE" != steady ] || [ "$WRITE_WARMUP" != reads ]; }; then
  echo "--write-kvtimer goes with --write-cache steady and --write-warmup reads (its passes are B's protocol)" >&2; exit 1
fi
OUT="${OUT:-$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF" --analysis i01-mix-latency)}"
# the steady cache: 10% of the dataset's nominal size, SF x 0.1 GiB, the same absolute bytes for every store (the rule
# the SF30 steady grid ran at 3,072 MB); 3 measured passes
[ -n "$STEADY" ] || STEADY="$("$PYTHON" -c "import sys; from kvevaluator.measure.store_config import steady_cache_mib; print(f'{steady_cache_mib(sys.argv[1])},3')" "$SF")"   # inputs/contract/store_config.json's rule
GRID_CELLS=(props_split_adj_split props_split_adj_condensed props_split_adj_chunked props_condensed_adj_split
            props_condensed_adj_condensed props_condensed_adj_chunked props_chunked_adj_split props_chunked_adj_condensed
            props_chunked_adj_chunked)
REPEAT_CELLS=(props_condensed_adj_split w16_b1 w16_b3)   # the noise reference: the reference single (first in the chain), a mix, the chunked-heavy mix (w16_latency_score.REPEAT_CELLS)

run() { echo "+ $*"; [ $DRY = 1 ] || "$@"; }
# A moved input found here (kvevaluator.stale) is not a refusal: one warning line, then a note into the dir of every record
# this run writes after it (notes_into, right before the tool that writes there: grid_latency's --out root, cppwlatency's
# --out), whose writer folds <dir>/stale.jsonl in. NOTES hold the run's notes (the binary, the record check), STORE_NOTES
# the next store's (foreign processes before it).
NOTES=(); STORE_NOTES=(); declare -A STARTED=() WRITTEN=()
mark() { echo "[stale] WARNING $1: $2"; NOTES+=("$1"$'\t'"$2"); }
mark_store() { echo "[stale] WARNING $1: $2"; STORE_NOTES+=("$1"$'\t'"$2"); }
notes_into() {  # notes_into <dir> [<unit>]: the run's notes so far into <dir>/stale.jsonl (on its first use in this run, an
  # earlier run's moved aside); with a unit (the cell or engine arm grid_latency writes next), the next store's notes into
  # that unit's own stale-<unit>.jsonl, started afresh for each store, so they mark that store's record only
  local d=$1 u=${2:-} n
  [ $DRY = 1 ] && return 0
  mkdir -p "$d"
  [ -n "${STARTED[$d]:-}" ] || { "$PYTHON" -m kvevaluator.stale start --into "$d"; STARTED[$d]=1; }
  local dir_notes=("${NOTES[@]}")
  [ -n "$u" ] || dir_notes+=("${STORE_NOTES[@]}")   # no unit (cppwlatency's --out, one dir per store): the store's notes too
  for n in "${dir_notes[@]}"; do
    [ -n "${WRITTEN[$d$'\t'$n]:-}" ] && continue
    "$PYTHON" -m kvevaluator.stale note --into "$d" "${n%%$'\t'*}" "${n#*$'\t'}" 2> /dev/null || { echo "stale note into $d failed" >&2; exit 1; }
    WRITTEN[$d$'\t'$n]=1
  done
  if [ -n "$u" ]; then
    "$PYTHON" -m kvevaluator.stale start --into "$d" --unit "$u"
    for n in "${STORE_NOTES[@]}"; do
      "$PYTHON" -m kvevaluator.stale note --into "$d" --unit "$u" "${n%%$'\t'*}" "${n#*$'\t'}" 2> /dev/null || { echo "stale note into $d ($u) failed" >&2; exit 1; }
    done
  fi
}
# the preflight before any timed store: the binary (dirty, unstamped, or C++ build inputs other than HEAD's: warned and
# marked binary_unclean, with cpp_diff_files), the tree, the record against the captures (at the deciding scale --check,
# elsewhere --small-scale; a difference is warned and marked body_moved; not for the writes stage, which reads no pick),
# every store's 19 picks, the machine's facts
preflight() {
  local cells="$1" out l
  echo "=== preflight $(date -u +%FT%TZ) sf$SF stage $STAGE"
  out=$("$PYTHON" - <<'PYEOF'
import json, subprocess, sys
from kvevaluator.capture.binary import binary_cpp_verdict, binary_path, binary_state, build_input_paths
from kvevaluator.paths import ROOT, checkout_of
v = json.loads(subprocess.run([binary_path(), "--version"], capture_output=True, text=True).stdout)
tree = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
dirty = [l for l in subprocess.run(["git", "status", "--short"], capture_output=True, text=True).stdout.splitlines() if not l.startswith("??")]
sha, flag, state = v.get("binary_git_sha"), v.get("binary_dirty"), binary_state(v)
print(f"binary {binary_path()}: sha {sha} dirty {flag} ({state}); tree HEAD {tree}, {len(dirty)} modified tracked files")
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
  # the record check's whole output goes to a file; its verdict is its own exit code (never read through a pipe); the
  # writes stage reads no pick, so it checks no record
  local pins_log rc=0
  if [ "$STAGE" != writes ]; then
    mkdir -p "$OUT/preflight"; pins_log="$OUT/preflight/grid_pick_record-$STAGE-$(date -u +%Y%m%dT%H%M%SZ).log"
    if [ "$SF" = 3 ]; then
      # every capture the record was decided on (the pick record: every layout and named store of the Interactive capture,
      # the microbench, BI and JOB; its mixes are kept by reference), or --check reports the others as "a record row with
      # no capture"
      R="$(captures_place 3)"
      if [ ${#PINS_ROOTS[@]} = 0 ]; then
        PINS_ROOTS+=(--family "microbench=$(capture microbench 3)" --family "bi=$(capture bi 3)"
                     --family "job=$(capture job "$JOB_SCALE")")
      fi
      echo "grid_pick_record --check roots: --grid $R ${PINS_ROOTS[*]}"
      "$PYTHON" -m kvcost.layout_cost.grid_pick_record --sf 3 --grid "$R" "${PINS_ROOTS[@]}" --check > "$pins_log" 2>&1 || rc=$?
    else
      "$PYTHON" -m kvcost.layout_cost.grid_pick_record --sf "$SF" --grid "$(captures_place "$SF")" --mixes "$(grid_counts)" --small-scale > "$pins_log" 2>&1 || rc=$?
    fi
    grep -v "^\[" "$pins_log" | grep -m1 -E "compared|^sf|valid" || true
    if [ "$rc" != 0 ]; then   # grid_pick_record --check on its own stays a refusal; here the run goes on, marked
      echo "grid_pick_record rc $rc ($pins_log); its first differing rows:"
      grep -E "^    " "$pins_log" | head -20 || true
      tail -3 "$pins_log"
      mark body_moved "the pick record differs from the captures: grid_pick_record rc $rc, $(grep -cE "^    " "$pins_log" || true) differing rows ($pins_log)"
    else
      echo "grid_pick_record rc 0 ($pins_log)"
    fi
  fi
  if [ "$STAGE" = writes ]; then
    # the write suite runs no read plan, so no pick is read; a store of any spec set (grid_chunked_*, lat_*) is timed
    echo "--- the picks per store: none (the writes stage runs the write suite, no read plan)"
  else
  echo "--- the picks per store, from the counts capture under the record's rule; equal to the record when grid_pick_record passed above (not an independent source; otherwise marked body_moved); a query absent here = the committed IR is its only body:"
  "$PYTHON" - "$SF" $cells <<'PYEOF'
import sys
from kvcost.latency import grid_latency as GL
from kvevaluator.output_tree import Tree
sf, cells = sys.argv[1], sys.argv[2:]
counts = Tree().capture("interactive", sf).path
for cell in cells:
    p = GL.picks_of(counts, cell, "interactive")
    if len(p) != 19:
        sys.exit(f"{cell}: {len(p)} picks, not 19")
    print(f"  {cell:30s} " + " ".join(f"{q}={v}" for q, v in sorted(p.items()) if v != "(pin)"))
PYEOF
  fi
  echo "--- machine: governor $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo n/a); intel no_turbo $(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null || echo n/a); cpufreq boost $(cat /sys/devices/system/cpu/cpufreq/boost 2>/dev/null || echo n/a); NUMA nodes $(lscpu 2>/dev/null | grep -i 'numa node(s)' | awk '{print $NF}'); swappiness $(cat /proc/sys/vm/swappiness); page cache $(awk '/^Cached:/{printf "%.0f GB", $2/1048576}' /proc/meminfo); available $(awk '/MemAvailable/{printf "%.0f GB", $2/1048576}' /proc/meminfo)"
  machine_now "the first store"
}
# before a store is opened (machine_now <store>): another kvevaluator process alive on the machine (its reads and CPU would
# be in our clock) is warned, a row in <out>/machine_state.csv (the load, the processes) and marked foreign_load on that
# store's records; load1 above 2.0 is a WARNING line (printed, not a mark)
machine_now() {
  local n load1
  n=$(ps -eo args | grep -c '[k]vevaluator ' || true); load1=$(cut -d' ' -f1 /proc/loadavg)   # grep -c exits 1 on a count of 0: never let set -e read that as a failure
  echo "--- now $(date -u +%T): load $(cut -d' ' -f1-3 /proc/loadavg); kvevaluator processes on the machine: $n"
  STORE_NOTES=()
  if [ "$n" -gt 0 ]; then
    ps -eo pid,etimes,args | grep '[k]vevaluator ' | cut -c1-160 || true
    run/machine-for-timing.sh check --state "$OUT" --label "$n kvevaluator processes before $1"
    mark_store foreign_load "$n kvevaluator process(es) alive, load1 $load1, before $1 ($OUT/machine_state.csv)"
  fi
  awk -v l="$load1" 'BEGIN{ if (l+0 > 2.0) print "WARNING: load1 " l " is above 2.0 before this store" }'
}
# a store's SST facts before it is timed (sst-props: read-only table properties, before the server opens it; the LSM state is
# the same for every store: SstFileWriter ingest, no compaction since)
store_root() { "$PYTHON" -m kvevaluator.measure.stores root ldbc "$SF"; }   # the sf's stores dir (stores.scale_root)
store_facts() {
  local dir=$1
  if [ $DROP = 1 ]; then   # before the store's server: no store inherits another's pages (a failed drop stops the stage)
    run/machine-for-timing.sh drop --state "$OUT" --label "$(basename "$dir") $STAGE ${regime:-}" || exit 1
  fi
  "$PYTHON" - "$dir" <<'PYEOF'
import json, os, subprocess, sys
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.stores import store_path
d = store_path(os.path.dirname(sys.argv[1]), os.path.basename(sys.argv[1]))   # meta-<cell>: its store, also one named by hash
props = [json.loads(l) for l in subprocess.run([binary_path(), "sst-props", d], capture_output=True, text=True).stdout.splitlines() if l.startswith("{")]
m = json.load(open(os.path.join(d, "manifest.json"))) if os.path.exists(os.path.join(d, "manifest.json")) else {}
print(f"  store {os.path.relpath(d)}: {len(props)} SSTs, {sum(p['file_bytes'] for p in props)/1e9:.2f} GB (data {sum(p['data_bytes'] for p in props)/1e9:.2f} GB, index {sum(p['index_bytes'] for p in props)/1e6:.0f} MB, filter {sum(p['filter_bytes'] for p in props)/1e6:.0f} MB); key codec {m.get('key_codec')}; index_filter_blocks {m.get('index_filter_blocks', 'whole')}")
PYEOF
}
done_file() { [ $DRY = 1 ] && { echo "(dry run: no DONE file)"; return; }; mkdir -p "$OUT"; echo "$(date -u +%FT%TZ) $STAGE $* $(git rev-parse HEAD)" > "$OUT/DONE-$STAGE${1:+-$1}"; echo "DONE file: $OUT/DONE-$STAGE${1:+-$1}"; }
t0=$SECONDS
stage_done() { echo "[run-w16-mix-latency] $STAGE sf$SF DONE ($((SECONDS - t0))s)"; }

# the pre-registered cell order (prereg item 4): the four cells and the Dgraph format arm first
# (the arm follows its cell, see arm_after), then each mix right before its best single within B (a drift must not separate
# the pair; a single already timed is not repeated), then the remaining singles, then the three repeat cells (the noise
# reference: Condensed/Split is the chain's first cell and its last). The mixes come from the picks file, never typed here.
order() {
  "$PYTHON" - "$PICKS" <<'PYEOF'
import json, sys
p = json.load(open(sys.argv[1]))
mixes = p["specs"]                                              # the distinct spec stems, e.g. w16_b05 w16_b1 w16_b2 w16_b3
best = {k: v["best_single_within_B"]["cell"] for k, v in p["picks"].items()}
out = ["props_condensed_adj_split", "props_split_adj_condensed", "props_chunked_adj_chunked", "props_condensed_adj_chunked"]
for m, b in [("w16_b3", best.get("3")), ("w16_b1", best.get("1")), ("w16_b05", best.get("0.5")), ("w16_b2", best.get("2"))]:
    if m in mixes:
        out.append(m)
        if b and b not in out:
            out.append(b)
singles = ["props_split_adj_split", "props_split_adj_chunked", "props_chunked_adj_split", "props_condensed_adj_condensed",
           "props_chunked_adj_condensed"]
out += [c for c in singles if c not in out] + [m for m in mixes if m not in out]
print(" ".join(out))
PYEOF
}
is_mix() { [[ "$1" == w16_* ]]; }
arm_after() { local c=$1 kv; for kv in ${ENGINE_ARMS//,/ }; do [[ "${kv#*=}" == "$c" ]] && echo "${kv%%=*}"; done; }   # the engine arm timed right after cell c
engine_formats() { local kv; for kv in ${ENGINE_ARMS//,/ }; do echo -n "${kv%%=*},"; done; }

case "$STAGE" in
  specs)
    [ -n "$SNAPSHOT" ] || { echo "--stage specs needs --snapshot <the layout-choice dir of the solve the picks come from> (no default: a stale solve would rewrite the specs)" >&2; exit 1; }
    run "$PYTHON" experiments/layout_choice/pick_specs.py --sf "$SF" --workload i01 --root "$SNAPSHOT" --out "$MIXDIR" \
        --check-picks "$SNAPSHOT/budget_picks.csv"
    ;;
  counts)
    run "$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --workload i01 Sp-Sp Co-Co Ch64k-Ch64k Sp-Co Sp-Ch64k Co-Sp Co-Ch64k Ch64k-Sp Ch64k-Co   # the latency stage times every grid cell's store
    run "$PYTHON" -m kvcost.cli.cli compose --dataset "ldbc=$SF"
    run "$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --workload i01 "$MIXDIR"
    if [ -n "$ENGINE_ARMS" ]; then   # the engine arms' counts (every body; the gate reads the pick record's), the variant gate's other side
      arms="$(engine_formats)"        # an arm is a store slug: a chunk arm (dgraph-c512k) names its store
      # the reads on the reported params, as kvcost measure-latency measures every store's counts (no held-out capture:
      # the latency stage times the pick record's body)
      run "$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --family interactive --pick-on in-sample ${arms//,/ }
    fi
    run "$PYTHON" tests/gates/check_grid.py --sf "$SF" --spec-dir "$MIXDIR"
    run "$PYTHON" tests/gates/check_mix_vs_built.py --sf "$SF" --spec-dir "$MIXDIR"
    ;;
  latency)
    [ -n "$CELLS" ] && cells="${CELLS//,/ }" || cells="$(order)"
    echo "order: $cells$([ $REPEATS = 1 ] && [ -z "$CELLS" ] && echo " | repeats: ${REPEAT_CELLS[*]}")"
    [ $DRY = 1 ] || preflight "$cells"
    for regime in ${REGIMES//,/ }; do
      extra=()
      case "$regime" in
        steady) extra=(--regimes steady --steady "$STEADY" --steady-perf-level "$STEADY_PERF" "${BREAKDOWN[@]}") ;;
        warm)   extra=(--regimes warm --warm 1,3) ;;
        cold)   extra=(--regimes cold --cold "$COLD") ;;
        *) echo "unknown regime $regime" >&2; exit 1 ;;
      esac
      for cell in $cells; do
        counts="$(grid_counts)"
        if is_mix "$cell"; then spec=(--spec-dir "$MIXDIR"); root="$OUT/mixes"
        else spec=(); root="$OUT/grid"; fi
        [ -f "$(counts_file "$counts" "$cell")" ] || [ $DRY = 1 ] || { echo "no counts capture for $cell at sf$SF ($counts): run --stage counts first" >&2; exit 2; }
        [ $DRY = 1 ] || { store_facts "$(store_root)/meta-$cell"; machine_now "meta-$cell"; }
        notes_into "$root" "$cell"
        run "$PYTHON" -m kvcost.latency.grid_latency --sf "$SF" --counts "$counts" --cells "$cell" --out "$root" "${spec[@]}" "${extra[@]}" --require-clean
        for arm in $(arm_after "$cell"); do    # the engine format nearest this cell, right after it; steady and warm only
          [ "$regime" = cold ] && continue
          [ -f "$(counts_file "$(grid_counts)" "$arm")" ] || [ $DRY = 1 ] || { echo "no counts capture for the $arm arm at sf$SF: run --stage counts first" >&2; exit 2; }
          [ $DRY = 1 ] || { store_facts "$(store_root)/$arm"; machine_now "$arm"; }
          notes_into "$OUT/grid" "$arm"
          run "$PYTHON" -m kvcost.latency.grid_latency --sf "$SF" --counts "$(grid_counts)" --cells "" --engine-arms "$arm" \
              --out "$OUT/grid" "${extra[@]}" --require-clean
        done
      done
      if [ $REPEATS = 1 ] && [ -z "$CELLS" ]; then
        for cell in "${REPEAT_CELLS[@]}"; do
          counts="$(grid_counts)"
          if is_mix "$cell"; then spec=(--spec-dir "$MIXDIR"); root="$OUT/mixes"
          else spec=(); root="$OUT/grid"; fi
          [ $DRY = 1 ] || { store_facts "$(store_root)/meta-$cell"; machine_now "meta-$cell"; }
          notes_into "$root" "$cell"
          run "$PYTHON" -m kvcost.latency.grid_latency --sf "$SF" --counts "$counts" --cells "$cell" --out "$root" "${spec[@]}" "${extra[@]}" --suffix=-repeat --require-clean
        done
      fi
    done
    done_file "${REGIMES//,/-}"
    ;;
  ic10-bodies)
    # IC10 alone, both bodies, WARM (one query's five params fit the steady cache after a pass: steady would be warm with
    # extra steps), two copies each, point / scan / point / scan per store, on every store whose pick is the scan body
    # per store BOTH bodies (w16_latency_score.py ic10-plan): the scan body is its recorded pick (inputs/picks/picks.csv),
    # the point body the planner's own body on that store (v1's selector over its spec: the anchor the record's ratio is
    # taken against; filter_index+fetch_inline on Condensed and Chunked props, filter_probe+fetch_deferred on Split props);
    # a store whose pick is not the scan is skipped. The plan is written first (the score's expected stores)
    [ -n "$CELLS" ] && cells="${CELLS//,/ }" || cells="$(order)"
    plan="$OUT/ic10-bodies/bodies.csv"; [ $DRY = 1 ] && plan="$(mktemp)"
    "$PYTHON" experiments/layout_choice/w16_latency_score.py ic10-plan --cells "${cells// /,}" --spec-dir "$MIXDIR" --out "$plan" || exit 1
    bodies=$(tail -n +2 "$plan" | tr ',' ' '); [ $DRY = 1 ] && rm -f "$plan"   # a dry run keeps no plan
    scan_cells=$(echo "$bodies" | awk '{print $1}' | tr '\n' ' ')
    echo "stores whose IC10 pick is the scan (the record, decided at sf$("$PYTHON" -c "from kvevaluator.plan_choice import pick_record; print(pick_record.load().decided_at)")); point = the planner's body on that store, scan = its pick:"
    echo "$bodies" | awk 'NF {printf "  %s: point=%s scan=%s\n", $1, $2, $3}'
    [ -n "$scan_cells" ] || { echo "no store picks the scan at sf$SF: nothing to time" >&2; exit 2; }
    [ $DRY = 1 ] || preflight "$scan_cells"
    for cell in $scan_cells; do
      POINT=$(echo "$bodies" | awk -v c="$cell" '$1==c{print $2}'); SCAN=$(echo "$bodies" | awk -v c="$cell" '$1==c{print $3}')
      echo "$cell: point=$POINT scan=$SCAN"
      counts="$(grid_counts)"
      if is_mix "$cell"; then spec=(--spec-dir "$MIXDIR"); sub=mixes
      else spec=(); sub=grid; fi
      [ $DRY = 1 ] || { store_facts "$(store_root)/meta-$cell"; machine_now "meta-$cell"; }
      for copy in "" "-copy2"; do
        for body in point scan; do
          [ $body = point ] && b="$POINT" || b="$SCAN"
          notes_into "$OUT/ic10-bodies/$body/$sub" "$cell"
          run "$PYTHON" -m kvcost.latency.grid_latency --sf "$SF" --counts "$counts" --cells "$cell" --out "$OUT/ic10-bodies/$body/$sub" "${spec[@]}" \
              --regimes warm --warm 1,3 --queries ic10 --pick "ic10=$b" --suffix="$copy" --require-clean
        done
      done
    done
    done_file
    ;;
  writes)
    # a write capture kept on resume must be timed as this run would time it (one batch per arena):
    # an older one-request-per-leg capture is refused, never mixed in (the reads' check is grid_latency's)
    kept_writes() { "$PYTHON" -m kvevaluator.measure.capture_protocol --expect batch "$1" \
        || { echo "$1 cannot be resumed: move it aside (or pick another --out) and re-run" >&2; exit 1; }; }
    [ -n "$CELLS" ] && cells="${CELLS//,/ }" || cells="$(order)"
    [ $DRY = 1 ] || preflight "$cells"
    # the regime dirs. --write-cache steady: A = the 4-leg protocol under the steady settings (each write's own blocks
    # warmed by its practice leg, the cache not warmed by the reads); B = the writes after a steady read warm-up (the
    # store's steady read capture warms the cache, then each write once, apply only), its counts held to A's
    if [ "$WRITE_CACHE" = steady ]; then
      mb="$("$PYTHON" -c "import sys; from kvevaluator.measure.store_config import steady_cache_mib; print(steady_cache_mib(sys.argv[1]))" "$SF")"
      wdir="steady-settings-${mb}mb-4leg-p1-noflush"
      bdir="after-steady-warmup-${mb}mb-p1-noflush"
      kdir="$("$PYTHON" -c "import sys; from kvevaluator.measure.cppwlatency import kvtimer_dir; print(kvtimer_dir(sys.argv[1]))" "$bdir")"   # B's KV-timer passes (--write-kvtimer): the one rule
    else
      wdir=durable-p1-noflush; bdir=""
    fi
    [ "$WRITE_WARMUP" = reads ] || bdir=""
    # the cell's steady read capture: the one <out>/<root>/interactive/steady-<MB>mb-p<L>[-repeat]/meta-<cell>/ (the latency
    # stage's grid/ and mixes/ roots, or a root grid_latency wrote for another spec set)
    timings_of() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import Tree; print(Tree(sys.argv[1]).timings(sys.argv[2], sys.argv[3]))' "$TIMINGS_ROOT" "$1" "$SF"; }
    if [ -n "$TIMINGS_ROOT" ]; then T_READS="$(timings_of interactive)"; T_WRITES="$(timings_of writes)"; fi
    b_dir() { [ -n "$TIMINGS_ROOT" ] && echo "$T_WRITES/meta-$1/$bdir$2" || echo "$OUT/writes/$bdir$2/meta-$1"; }   # B's dir
    read_capture() {
      local hits=() d
      if [ -n "$TIMINGS_ROOT" ]; then echo "$T_READS/meta-$1/steady-${STEADY%%,*}mb-p$STEADY_PERF$2"; return; fi
      for d in "$OUT"/*/interactive; do
        [ -d "$d/steady-${STEADY%%,*}mb-p$STEADY_PERF$2/meta-$1" ] && hits+=("$d/steady-${STEADY%%,*}mb-p$STEADY_PERF$2/meta-$1")
      done
      [ ${#hits[@]} -le 1 ] || { echo "steady read capture of $1: ${#hits[@]} dirs match: ${hits[*]}" >&2; exit 2; }
      echo "${hits[0]:-$OUT/grid/interactive/steady-${STEADY%%,*}mb-p$STEADY_PERF$2/meta-$1}"
    }
    # the cell's spec file: a mix's in $MIXDIR, a single layout's in the one inputs/specs/grid* set that holds it
    # (--write-spec-dir: only the dirs given); cppwlatency checks that the file is the spec of the cell's store
    spec_file() {
      local hits=() h d
      if [ ${#WRITE_SPEC_DIRS[@]} -gt 0 ]; then
        for d in "${WRITE_SPEC_DIRS[@]}"; do [ -f "$d/$1.json" ] && hits+=("$d/$1.json"); done
      elif is_mix "$1"; then
        echo "$MIXDIR/$1.json"; return
      else
        hits=(inputs/specs/grid*/"$1".json)
      fi
      [ ${#hits[@]} -gt 0 ] && [ -f "${hits[0]}" ] || { echo "spec of $1: no $1.json in ${WRITE_SPEC_DIRS[*]:-inputs/specs/grid*}" >&2; exit 2; }
      for h in "${hits[@]}"; do   # a spec copied into several grid sets: the copies must be byte-identical
        cmp -s "$h" "${hits[0]}" || { echo "spec of $1: ${hits[*]} differ" >&2; exit 2; }
      done
      echo "${hits[0]}"
    }
    write_cell() {   # write_cell <cell> <''|-repeat>: A, then B on its own arena
      local cell=$1 sfx=$2 d="$OUT/writes/$wdir$2/meta-$1" b reads spec
      b="$(b_dir "$1" "$2")"
      if [ -f "$d/steps.csv" ]; then
        kept_writes "$d"; echo "  $cell writes$sfx: already measured ($d); a re-measure needs another --out"
      else
        [ $DRY = 1 ] || { store_facts "$(store_root)/meta-$cell"; machine_now "meta-$cell"; }
        notes_into "$d"
        run "$PYTHON" -m kvevaluator.measure.cppwlatency --sf "$SF" --arms "meta-$cell" --pass headline --cache "$WRITE_CACHE" --write-buffer-mb 4096 \
            --require-no-flush --warmup-write --out "$d"
      fi
      [ -n "$bdir" ] || return 0
      if [ -f "$b/steps.csv" ]; then
        kept_writes "$b"; echo "  $cell writes after the warm-up$sfx: already measured ($b)"; return 0
      fi
      reads="$(read_capture "$cell" "$sfx")"
      spec="$(spec_file "$cell")"
      [ -f "$reads/steps.csv.meta.json" ] || [ $DRY = 1 ] || { echo "no steady read capture for $cell: $reads (run --stage latency --regimes steady first, or --write-warmup none)" >&2; exit 2; }
      [ $DRY = 1 ] || { store_facts "$(store_root)/meta-$cell"; machine_now "meta-$cell"; }
      notes_into "$b"
      run "$PYTHON" -m kvevaluator.measure.cppwlatency --sf "$SF" --arms "meta-$cell" --pass headline --cache steady --write-buffer-mb 4096 \
          --require-no-flush --warmup-write --warm-reads "$reads" --spec "$spec" --same-counts-as "$d" --out "$b"
    }
    timer_cell() {   # timer_cell <cell>: B's KV-timer passes run-0 .. run-<N-1>, only the missing ones (passes accumulate)
      local cell=$1 b k="$OUT/writes/$kdir/meta-$1" reads spec i r have=0
      b="$(b_dir "$1" "")"
      [ "$WRITE_KVTIMER" -gt 0 ] || return 0
      [ -f "$b/steps.csv.meta.json" ] || [ $DRY = 1 ] || { echo "no B run for $cell ($b): its KV-timer passes hold their counts to it" >&2; exit 2; }
      reads="$(read_capture "$cell" "")"
      spec="$(spec_file "$cell")"
      for ((i = 0; i < WRITE_KVTIMER; i++)); do
        r="$k/run-$i"
        if [ -f "$r/steps.csv.meta.json" ]; then
          kept_writes "$r"; have=$((have + 1)); continue
        fi
        if [ -e "$r" ] && [ $DRY = 0 ]; then   # a pass that stopped before its meta: kept aside, never mixed in
          mv "$r" "$r.incomplete-$(date -u +%Y%m%dT%H%M%SZ)"; echo "  $cell kvtimer run-$i: incomplete, moved aside"
        fi
        [ $DRY = 1 ] || { store_facts "$(store_root)/meta-$cell"; machine_now "meta-$cell"; }
        notes_into "$r"
        run "$PYTHON" -m kvevaluator.measure.cppwlatency --sf "$SF" --arms "meta-$cell" --pass kvtimer --cache steady --write-buffer-mb 4096 \
            --require-no-flush --warmup-write --warm-reads "$reads" --spec "$spec" --same-counts-as "$b" --out "$r"
      done
      echo "  $cell kvtimer: $WRITE_KVTIMER passes ($have already measured, $((WRITE_KVTIMER - have)) run now) in $k"
    }
    for cell in $cells; do
      write_cell "$cell" ""
      timer_cell "$cell"      # the KV-timer passes on the main cells only: the repeats are the walls' noise reference
    done
    if [ $REPEATS = 1 ] && [ -z "$CELLS" ]; then
      for cell in "${REPEAT_CELLS[@]}"; do
        write_cell "$cell" -repeat
      done
    fi
    done_file
    ;;
  score)
    for regime in ${REGIMES//,/ }; do
      case "$regime" in
        steady) rdir="steady-${STEADY%%,*}mb-p$STEADY_PERF" ;;
        warm)   rdir="warm-p0" ;;
        # grid_latency's cold dir: cold-p2, a dated root's cold-p3 before (kvstats_layout.cold_regime)
        cold)   rdir="$("$PYTHON" -c 'import os, sys; from kvevaluator.measure import kvstats_layout as L; print(L.cold_regime(*[os.path.join(r, "interactive") for r in sys.argv[1:]]) or L.COLD)' "$OUT" "$OUT"/*/)" ;;
      esac
      cells_csv="$(IFS=,; echo "${REPEAT_CELLS[*]}")"
      run "$PYTHON" experiments/layout_choice/w16_latency_score.py noise --picks "$PICKS" --latency-root "$OUT" --regime "$rdir" \
          --cells "$cells_csv" --out "$OUT/analysis/$rdir"
      nw=()
      if [ -d "$OUT/writes/durable-p1-noflush-repeat" ]; then
        run "$PYTHON" experiments/layout_choice/w16_latency_score.py noise --writes-only --picks "$PICKS" --latency-root "$OUT" \
            --regime "$rdir" --cells "$cells_csv" --out "$OUT/analysis/$rdir"
        nw=(--noise-writes "$OUT/analysis/$rdir/noise-writes.json")
      fi
      run "$PYTHON" experiments/layout_choice/w16_latency_score.py score --picks "$PICKS" --latency-root "$OUT" --regime "$rdir" \
          --noise "$OUT/analysis/$rdir/noise-$rdir.json" "${nw[@]}" --out "$OUT/analysis/$rdir" --all-arms
      # the engine-format arms (steady and warm only; the registered pairs are the scorer's, no flags): the same noise file
      [ "$regime" = cold ] || run "$PYTHON" experiments/layout_choice/w16_latency_score.py engine --latency-root "$OUT" --regime "$rdir" \
          --noise "$OUT/analysis/$rdir/noise-$rdir.json" --out "$OUT/analysis/$rdir"
      # AFTER the words (steady only; the other regimes have no passes): the pass-to-pass wall spread per store against the
      # floor (the floor's premise); a store above it is named beside its word rows (a column), no word changes
      run "$PYTHON" experiments/layout_choice/w16_latency_score.py passes --latency-root "$OUT" --regime "$rdir" \
          --noise "$OUT/analysis/$rdir/noise-$rdir.json" --out "$OUT/analysis/$rdir"
    done
    # IC10's two bodies (the ic10-bodies stage; once, its own regime): the expected stores come from the record
    [ -d "$OUT/ic10-bodies" ] && run "$PYTHON" experiments/layout_choice/w16_latency_score.py ic10 --latency-root "$OUT" --out "$OUT/analysis/ic10-bodies" \
        || echo "  no $OUT/ic10-bodies: the ic10-bodies stage has not run (its verdict is missing, not held)"
    ;;
  plot)
    run "$PYTHON" experiments/layout_choice/plot_w16_mix_latency.py --sf "$SF" --root "$OUT" --picks "$PICKS"
    run "$PYTHON" experiments/layout_choice/plot_w16_engine_arms.py --sf "$SF" --root "$OUT"
    run "$PYTHON" experiments/layout_choice/plot_w16_stores.py --sf "$SF" --root "$OUT" --picks "$PICKS"   # per query, per group, by mix
    ;;
  *) echo "unknown stage $STAGE" >&2; exit 1 ;;
esac
stage_done
