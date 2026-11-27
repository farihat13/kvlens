#!/bin/bash
# run/kvstats.sh — capture EVERYTHING the C++ evaluator measures for one workload: KV op counts
# (SEEK/GET/NEXT + PUT/DEL), KV bytes (every channel: read/write/used lanes + the absence misses,
# per query AND per step -- the read-amplification source comes out of the same execution), WARM
# and COLD latency -- raw CSVs first (capture != analyse), then the figures from those CSVs.
#
# The capture ACCUMULATES per STORE (measure/kvstats_layout): every file is per store slug
# (<format>[-<schema>][-c<N>k][-<codec>]) and a run REPLACES exactly the cells it measured, so an
# SF is measured once -- add a format later, refresh a subset of queries, and nothing measured
# before is re-run or overwritten.
#
#   run/kvstats.sh                                  # SF from $SF (.env): interactive, dgraph+nebula, 5 params
#   run/kvstats.sh 0.3                              # positional SF
#   run/kvstats.sh --sf 0.1 --family microbench   # the access-path primitives
#   run/kvstats.sh --sf 0.1 --family writes       # the write primitives + LDBC inserts (IU1-8); DURABLE write latency
#   run/kvstats.sh --sf 3 --formats arangodb        # ADD a store to an existing sf3 capture (dgraph, nebula untouched)
#   run/kvstats.sh --sf 0.1 --queries ic4,ic10      # refresh two queries' cells of the default stores
#   run/kvstats.sh --sf 0.1 --schema label_only     # the label_only design's stores + plan family
#   run/kvstats.sh --sf 0.1 --formats pertype_csr --chunk 16k --codec zstd   # a chunk/codec arm (its own slug)
#   run/kvstats.sh --sf 0.1 --no-measure            # figures from the captured CSVs (no store opened)
#   run/kvstats.sh --sf 0.1 --plots-only --show dgraph,nebula,arangodb   # a selection's figures
#
# Flags: --sf <SF> | bare SF | $SF; --family interactive|microbench|writes (default interactive, ONE per run);
#        --formats a,b,c (default dgraph,nebula); --schema canonical|label_only|<name> (default canonical);
#        --codec <the decided codec> (inputs/contract/store_config.json's rocksdb.compression, the default and the only one); --chunk <N>k, e.g. 4k|64k|512k|4096k (the pertype formats' store,
#        default 64k, exported as KVEVAL_PERTYPE_CSR_CHUNK so every stage opens the same store; given
#        explicitly with dgraph in --formats it also exports dgraph's opt-in knob, KVEVAL_DGRAPH_CHUNK, so the run
#        captures the chunked arm -- dgraph-c512k = real dgraph's posting split; unset, dgraph keeps its bare
#        unbounded store);
#        --nparam N (default 5); --queries q1,q2 | all (the latency stages' queries; default: the workload's suite; `all` =
#        every committed plan of the workload, twins included -- the kv_amp study's scope; the counts stage always
#        measures the suite); --metrics counts,latency (default both; counts = kvcost measure on the formats: grid_bodies (reads) or grid_writes
#        --format (writes) into the formats capture: every body of every query, the per-step and per-part rows); --warm k,m
#        (warmup,measured; default 1,3) --cold n (default 3); --regimes warm,cold|warm|cold (default both); --fresh (re-measure
#        cells the capture already holds under the same identity -- the default REUSES them, see measure/latency_fresh); --reuse-stale (also keep a stage
#        whose rows' identity moved in its store, params, plan or counting parts; the run record marked reused_stale); --write-latency durable|overlay (writes only;
#        default durable); --show s1,s2 (the slugs the figures draw; default = the baselines dgraph,nebula
#        already in the capture + this run's stores, so a one-format capture is compared, never drawn alone);
#        --no-check (kept for --queries all: the helper plans fof, friends, knows answer differently per format);
#        --root DIR (the output tree every capture, timing and run record goes below; default the output dir; the
#        stores and store-size.csv stay the output dir's);
#        --no-measure | --plots-only; --no-plots (measure only: a driver that draws its own figures); -h.
# A STOPPED run is resumed: the counts pass writes each query when it ends, the run record exists from the start
# (status running -> ok | failed at <stage> | interrupted at <stage>), and the same command run again measures only
# the queries not yet captured under the current identity (a FAILED run's rows are re-measured).
# A latency capture timed under another request protocol (one request per plan; its run metas say so,
# measure/capture_protocol) is not reused: it is moved aside (measure/latency_fresh) and re-measured.
# --schema and --codec other than canonical / the decided codec are refused; the counts driver measures a format's store at its knobs (--chunk only).
# Stages: guard stores (never re-ingests) -> counts+bytes per query + per step (grid_bodies / grid_writes --format, into
#         <root>/captures/ldbc/sf<SF>/<workload>/reported: reused when fresh) -> warm-p0 (cpplatency perf_level 0) -> cold-p2 (cpplatency perf_level 2, direct I/O: PerfContext
#         block reads and cache hits, no per-call timer; cold cache every iteration; inputs/contract/store_config.json. Before
#         cold-p3, buffered at perf_level 3. --cold-breakdown: cold-p3 instead, as it was (buffered, perf_level 3),
#         declared as a breakdown pass, for a reader of the timers: kvcost_breakdown) -> the run record
#         -> plots (experiments/core_suite/plot_kvstats.py --show).
# Output (python/kvevaluator/output_tree.py): the counts in the workload's capture (above); the timings in
#         <root>/timings/ldbc/sf<SF>/<workload>/{<slug>/warm-p0/steps.csv, <slug>/cold-p2/steps.csv,
#         <slug>/durable-p1/steps.csv, runs/<run-id>.json}; the figures in
#         <root>/summary/ldbc/sf<SF>/named-formats/<workload>/plots[-<selection>]/
#         -- every CSV row carries `run`; runs/<run-id>.json is the invocation's provenance.
# WRITES: counts/bytes run against the executor's shadow overlay (grid_writes; the store never mutates). LATENCY
#         defaults to the DURABLE measurement (measure/cppwlatency.py): a disposable hard-link copy
#         (ARENA, measure/arenas.py) of each store, the whole write suite applied for real to it as one recorded sequence,
#         each cell's engine write = apply wall minus a paired warm shadow control; --breakdown adds a
#         second fresh-arena pass at perf_level 3. A write sequence is measured as a whole: its
#         durable-p1/<slug>/ file is replaced per run, not upserted (the cells depend on the sequence).
#         --write-latency overlay = the cheap alternative: cpplatency's wall = RMW reads + in-memory
#         overlay PUTs, NOT durable latency (warm-p0/ + cold-p2/ as for reads).
# Prereqs: run/build-cpp.sh; stores under output/stores/ldbc/sf<SF>/ (run/ingest-cpp-stores.sh).
# sf1+ takes a long time per format (the cold pass evicts caches every iteration): tmux + a .log.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"

WORKLOAD="interactive"
FORMATS="dgraph,nebula"
SCHEMA="canonical"
CODEC="$("$PYTHON" -c 'from kvevaluator.contract import store_build; print(store_build("compression"))')"   # store_config.json
DECIDED_CODEC="$CODEC"
NPARAM=5
QUERIES=""
METRICS="counts,latency"
WARM="1,3"
COLD="3"
REGIMES="warm,cold"             # the latency regimes to capture (--regimes cold: cold-p2 only)
COLD_DIR="cold-p2"; COLD_FLAGS=(--perf-level 2)   # --cold-breakdown: cold-p3, the per-call timers on purpose
FRESH=0                         # --fresh: re-measure even cells the capture already holds under the same identity
REUSE_STALE=0                   # --reuse-stale: also keep a stage's rows whose identity moved (store, params, plan, counting), marked reused_stale
WRITE_LATENCY="durable"
CHUNK="64k"
CHUNK_GIVEN=0                   # --chunk was passed: only then does dgraph get its chunk knob
SHOW=""
NOCHECK=()
MEASURE=1
PLOTS=1
ROOT=""                         # --root: another output tree (default the output dir)
ROOT_ARG=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)         SF="${2:?--sf needs a value}"; shift 2 ;;
    --family)   WORKLOAD="${2:?--family needs interactive|microbench|writes}"; shift 2 ;;
    --formats)    FORMATS="${2:?--formats needs a comma list}"; shift 2 ;;
    --schema)     SCHEMA="${2:?--schema needs a name}"; shift 2 ;;
    --codec)      CODEC="${2:?--codec needs a name}"; shift 2 ;;
    --nparam)     NPARAM="${2:?--nparam needs a value}"; shift 2 ;;
    --queries)    QUERIES="${2:?--queries needs a comma list}"; shift 2 ;;
    --metrics)    METRICS="${2:?--metrics needs a comma list}"; shift 2 ;;
    --warm)       WARM="${2:?--warm needs k,m}"; shift 2 ;;
    --cold)       COLD="${2:?--cold needs n}"; shift 2 ;;
    --regimes)    REGIMES="${2:?--regimes needs warm,cold | warm | cold}"; shift 2 ;;
    --cold-breakdown) COLD_DIR="cold-p3"; COLD_FLAGS=(--perf-level 3 --breakdown-pass --buffered-reads); export KVEVAL_COLD_BREAKDOWN=1; shift ;;
    --fresh)      FRESH=1; shift ;;
    --reuse-stale) REUSE_STALE=1; shift ;;
    --write-latency) WRITE_LATENCY="${2:?--write-latency needs durable|overlay}"; shift 2 ;;
    --chunk)      CHUNK="${2:?--chunk needs <N>k, e.g. 4k|16k|64k|512k|4096k}"; CHUNK_GIVEN=1; shift 2 ;;
    --show)       SHOW="${2:?--show needs a comma list of slugs}"; shift 2 ;;
    --no-check)   NOCHECK=(--no-check); shift ;;
    --no-measure|--plots-only) MEASURE=0; shift ;;
    --no-plots)   PLOTS=0; shift ;;
    --root)       ROOT="$(realpath -m "${2:?--root needs a directory}")"; ROOT_ARG=(--root "$ROOT"); shift 2 ;;
    -h|--help)    awk 'NR==1{next} /^#/{sub(/^# ?/,"");print;next} {exit}' "$0"; exit 0 ;;
    -*)           echo "unknown flag: $1" >&2; exit 1 ;;
    *)            SF="$1"; shift ;;   # bare positional -> SF
  esac
done
: "${SF:?set SF (positional, --sf, or .env)}"
case "$WORKLOAD" in interactive|microbench|writes) ;; *) echo "--family must be interactive, microbench or writes, got: $WORKLOAD" >&2; exit 1 ;; esac
for m in ${METRICS//,/ }; do
  case "$m" in counts|latency) ;; amp) echo "--metrics amp is gone: the counts pass writes the per-step channels (steps/<slug>.csv)" >&2; exit 1 ;;
    *) echo "--metrics names counts, latency; got: $m" >&2; exit 1 ;; esac
done
case "$WRITE_LATENCY" in durable|overlay) ;; *) echo "--write-latency must be durable or overlay, got: $WRITE_LATENCY" >&2; exit 1 ;; esac
has() { [[ ",$METRICS," == *",$1,"* ]]; }
# any <N>k (KiB): the chunk-size ladder runs 4096k and 32768k; every stage reads the knob in bytes
case "$CHUNK" in [1-9]*k) [[ "${CHUNK%k}" =~ ^[0-9]+$ ]] || { echo "--chunk must be <N>k, got: $CHUNK" >&2; exit 1; } ;; *) echo "--chunk must be <N>k (e.g. 64k, 512k, 4096k), got: $CHUNK" >&2; exit 1 ;; esac
if [[ "$SCHEMA" != canonical || "$CODEC" != "$DECIDED_CODEC" ]]; then
  echo "--schema $SCHEMA / --codec $CODEC: not options of this driver; the counts are grid_bodies --format on a format's store" >&2; exit 1; fi
if (( CHUNK_GIVEN )) && [[ "$FORMATS" != *pertype_csr* && "$FORMATS" != *dgraph* ]]; then
  echo "--chunk applies to the pertype_csr formats and dgraph's knob; --formats $FORMATS has none" >&2; exit 1; fi
if [[ "$WRITE_LATENCY" != durable && "$WORKLOAD" != writes ]]; then
  echo "--write-latency applies to --family writes only" >&2; exit 1; fi
if [[ "$QUERIES" == all && ${#NOCHECK[@]} -eq 0 ]]; then
  echo "--queries all needs --no-check: the helper plans fof/friends/knows answer differently per format (their cross-format check would fail the run after writing its rows)" >&2; exit 1; fi
export SF
export KVEVAL_PERTYPE_CSR_CHUNK=$(( ${CHUNK%k} * 1024 ))   # the one chunk knob every stage reads
if (( CHUNK_GIVEN )) && [[ "$FORMATS" == *dgraph* ]]; then  # the opt-in knob: only an explicit --chunk sets it
  export KVEVAL_DGRAPH_CHUNK=$(( ${CHUNK%k} * 1024 ))
fi

# the workload's timings dir, its run records (runs), and one store slug's timings at a regime (output_tree.timing_in)
timings() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import Tree
print(Tree(sys.argv[1] or None).timings(*sys.argv[2:]))' "$ROOT" "$WORKLOAD" "$SF" "$@"; }
OUT="$(timings)"
RUNS_DIR="$(timings runs)"
timing() { "$PYTHON" -c 'import sys; from kvevaluator.output_tree import timing_in; print(timing_in(*sys.argv[1:]))' "$OUT" "$@"; }
RUN_ID="$(date +%Y%m%dT%H%M%S)"
# one run per second: a back-to-back run (e.g. every cell reused, under a second) waits for a fresh
# id instead of colliding with the previous run record (a run-id is never reused)
while [[ -e "$RUNS_DIR/${RUN_ID}.json" ]]; do sleep 1; RUN_ID="$(date +%Y%m%dT%H%M%S)"; done
t0=$(date +%s)
log() { echo "[run-kvstats $(date +%H:%M:%S) +$(( $(date +%s) - t0 ))s] $*"; }
# the store slug of a format under this run's schema/codec/chunk (the bridge's one naming rule)
slug_of() { "$PYTHON" -c "from kvevaluator.measure.kvstats_layout import slug; from kvevaluator.measure.stores import store_chunk; print(slug('$1', '$SCHEMA', '$CODEC', store_chunk('$1')))"; }
SLUGS=""
for fmt in ${FORMATS//,/ }; do SLUGS="${SLUGS:+$SLUGS,}$(slug_of "$fmt")"; done
SHOW_GIVEN="$SHOW"
if [[ "$QUERIES" == all ]]; then
  QUERIES="$("$PYTHON" -c "from kvevaluator.measure import workload_plans as w; print(','.join(w.all_plans('$WORKLOAD')))")"
fi
LQ=(); [[ -n "$QUERIES" ]] && LQ=(--queries ${QUERIES//,/ })

# the run record is written when a measuring run STARTS (status running) and again on EVERY exit (status ok |
# failed at <stage> | interrupted at <stage>), so rows stamped with this run-id always have their provenance: the
# counts pass writes each query as it ends, and a later run reuses the finished queries of a STOPPED run
# (measure/latency_fresh._vouches: running / interrupted vouch for counts rows; failed vouches for nothing)
STAGE="start"; STATUS="ok"
write_run_record() {
  local rc=$? phase="${1:-exit}"
  (( MEASURE )) || return 0
  if [[ "$phase" == start ]]; then local STATUS="running"
  elif (( rc >= 128 )); then STATUS="interrupted at $STAGE (signal $((rc - 128)))"
  elif [[ $rc -ne 0 ]]; then STATUS="failed at $STAGE (exit $rc)"; fi
  "$PYTHON" - "$SF" "$WORKLOAD" "$RUN_ID" "$SLUGS" "$FORMATS" "$SCHEMA" "$CODEC" "$CHUNK" "$NPARAM" "$QUERIES" "$METRICS" "$WARM" "$COLD" "$WRITE_LATENCY" "$t0" "$STATUS" "$REGIMES" "${FRESH_JSON:-}" "$FRESH" "$phase" "$ROOT" <<'EOF2'
import json, os, subprocess, sys, time
sf, wl, run_id, slugs, fmts, schema, codec, chunk, nparam, queries, metrics, warm, cold, wlat, t0, status, regimes, fresh_json, fresh, phase, root = sys.argv[1:]
root = root or None
from kvevaluator.measure.stores import store_dir
from kvevaluator.capture.store_identity import store_props_codec
from kvevaluator.formats.props_codec import props_codec, PROPS_CODEC_DEFAULT
def _props_codec(slug):
    """The store's edge-prop codec from its manifest (named | schema), None for a format without the knob."""
    try:
        return store_props_codec(store_dir("ldbc", sf, slug))
    except OSError:
        return None
def _store_bytes(slug):
    """The store's size on disk at capture time (every file under its RocksDB dir)."""
    d = store_dir("ldbc", sf, slug); total = 0
    for dirpath, _dirs, files in os.walk(d):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return total
plan = json.load(open(fresh_json)) if fresh_json and os.path.exists(fresh_json) else {}
from kvevaluator.measure.kvstats_layout import write_run
from kvevaluator.measure.grid_bodies import FREE_ROWS as COUNTS_FREE_ROWS
from kvevaluator.measure import latency_fresh as F
from kvevaluator.capture.binary import binary_path
git = subprocess.run(["git", "log", "-1", "--format=%H"], capture_output=True, text=True).stdout.strip()
dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout.strip())
binary = subprocess.run([binary_path(), "--version"], capture_output=True, text=True)
rec = {"status": status, "sf": sf, "workload": wl, "stores": slugs.split(","), "formats": fmts.split(","), "schema": schema,
       "codec": codec, "chunk": chunk, "nparam": int(nparam), "queries": queries.split(",") if queries else "suite",
       "store_bytes": {sl: _store_bytes(sl) for sl in slugs.split(",") if sl},   # the ingested store's size on disk
       # the property codec each store holds, from its manifest (a bare slug does not name it)
       "props_codec": {sl: _props_codec(sl) for sl in slugs.split(",") if sl and _props_codec(sl)},
       "metrics": metrics.split(","), "regimes": regimes.split(","), "warm": warm, "cold": cold, "git_commit": git, "git_dirty": dirty,
       "counts_free_rows": COUNTS_FREE_ROWS,   # the counts run frees a step's rows after their last reader; latency runs never do
       # the capture identity per slug (measure/latency_fresh): what a later run compares to skip a fresh cell
       "identity": {s: v["identity"] for s, v in plan.items()},
       "reused": {s: v["skip"] for s, v in plan.items() if v["skip"]}, "forced_fresh": fresh == "1",
       # latency captures timed under another request protocol, moved aside before this run (measure/capture_protocol)
       "moved_aside": {s: v["moved_aside"] for s, v in plan.items() if v.get("moved_aside")},
       "binary": (binary.stdout or binary.stderr).strip(), "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
       "wall_s": int(time.time()) - int(t0),
       "command": ("" if props_codec() == PROPS_CODEC_DEFAULT else f"KVEVAL_PROPS_CODEC={props_codec()} ")
                  + f"run/kvstats.sh --sf {sf} --family {wl} --formats {fmts} --schema {schema} --codec {codec} "
                  f"--chunk {chunk} --nparam {nparam} --metrics {metrics} --warm {warm} --cold {cold}"
                  + (f" --regimes {regimes}" if regimes != "warm,cold" else "") + (" --fresh" if fresh == "1" else "")
                  + (f" --queries {queries}" if queries else "") + (f" --write-latency {wlat}" if wl == "writes" else "")
                  + (f" --root {root}" if root else ""),
       "files": {"counts": "the workload's capture: captures/ldbc/sf<SF>/<workload>/reported/{counts,steps,parts}/<store>.csv "
                           "(grid_bodies / grid_writes --format; each file's record, counts/<store>.json, holds its run's provenance)",
                 "warm_latency": "<slug>/warm-p0/steps.csv (+ per-cell JSON; perf_level 0)",
                 "cold_latency": "<slug>/cold-p2/steps.csv (perf_level 2, direct I/O: PerfContext block reads and cache hits, no per-call timer; with --cold-breakdown <slug>/cold-p3/: buffered, perf_level 3, the timers on purpose)"}}
# reused stages whose rows a body since rewritten count-equal made (kvevaluator.stale: under_successor)
if stale_entries := F.stale_of_plan(plan):
    from kvevaluator import stale as _marks
    with _marks.scope():          # only the plan's entries: a kept stage's marks, and its adoptions in `adopted`
        rec = _marks.fold(rec, extra=stale_entries)
if wl == "writes":
    rec["write_latency"] = wlat
    rec["files"]["write_latency"] = ("<slug>/durable-p1/steps.csv: regime headline (perf 1) and breakdown (perf 3, fresh arena); "
                                     "per step wall_ns (apply) and shadow_wall_ns (paired warm control) -- the engine write = "
                                     "wall_ns - shadow_wall_ns" if wlat == "durable" else
                                     "warm-p0/ + cold-p2/: RMW reads + in-memory overlay PUTs, NOT durable latency")
p = write_run(sf, wl, run_id, rec, finish=(phase == "exit"), root=root)
print(f"[run-kvstats] run record ({status}) -> {os.path.relpath(p)}")
if phase == "start":
    sys.exit(0)
# also persist the sizes into the COMMITTED store-size.csv (portable across machines, unlike runs/
# which is gitignored): the store-size figure then redraws on a fresh clone with no store ingested
from kvevaluator.measure.kvstats_layout import store_size_csv, upsert_store_sizes
from kvevaluator.measure.stores import store_dir as _store_dir
from kvevaluator.measure import store_layout as _SL


def _layout(sl):   # the store's index + filter layout: its SST bytes depend on it
    try:
        with open(os.path.join(_store_dir("ldbc", sf, sl), "manifest.json")) as f:
            return _SL.of_manifest(json.load(f))
    except OSError:
        return ""


sz = {sl: {"total_bytes": b, "index_filter_blocks": _layout(sl), "recorded": run_id}
      for sl, b in rec["store_bytes"].items() if b}
if sz:
    upsert_store_sizes(sf, wl, sz)
    print(f"[run-kvstats] store sizes -> {os.path.relpath(store_size_csv(sf, wl))}")
EOF2
}
trap write_run_record EXIT
# a stop by a signal exits with 128 + its number, so the record says "interrupted" (without these, bash ran the EXIT
# trap with status 0 on a hangup and a stopped run was recorded as ok)
trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM

if (( MEASURE )); then
  # guard: never re-ingest silently (an sf30 store is hours; the shared-state rule)
  for fmt in ${FORMATS//,/ }; do
    d="$("$PYTHON" -m kvevaluator.measure.stores root ldbc "$SF")/$(slug_of "$fmt")"
    # the command that builds this store, for both hints below: the same flags that named its slug
    ingest="run/ingest-cpp-stores.sh --sf $SF --formats $fmt --schema $SCHEMA --codec $CODEC"
    if (( CHUNK_GIVEN )); then ingest+=" --chunk $CHUNK"; fi   # an if: a failing $(...) in an assignment ends the script (set -e)
    [[ -d "$d" ]] || { echo "missing C++ store $d -- build it: $ingest" >&2; exit 1; }
    # a store from before a key-layout bump: the binary refuses it at open, say so now with the rebuild
    "$PYTHON" -c "from kvevaluator.capture.store_identity import check_key_layout; check_key_layout('$d', '$ingest --rebuild')" || exit 1
  done
  mkdir -p "$OUT"
  # SKIP-IF-FRESH (the default; --fresh re-measures): a stage of a slug is reused when every
  # requested query's rows come from ok runs stamped with the same identity -- store fingerprint
  # (key layout included), param sha + nparam, committed plan-IR sha, the binary's counting versions
  # (measure/latency_fresh). The plan names each skip; the run record stamps the identity.
  FRESH_JSON="$(mktemp)"
  "$PYTHON" - "$SF" "$WORKLOAD" "$SLUGS" "$QUERIES" "$NPARAM" "$SCHEMA" "$METRICS" "$REGIMES" "$WRITE_LATENCY" "$FRESH" "$FRESH_JSON" "$FORMATS" "$PLOTS" "$SHOW_GIVEN" "$REUSE_STALE" "$ROOT" <<'EOF3'
import json, os, sys
sf, wl, slugs, queries, nparam, schema, metrics, regimes, wlat, fresh, out, fmts, plots, show, reuse_stale, root = sys.argv[1:]
root = root or None
from kvevaluator.measure import latency_fresh as F; from kvevaluator.measure import kvstats_layout as L; from kvevaluator.measure import workload_plans as W
qs = queries.split(",") if queries else W.queries(wl)
stages = []          # the latency stages (the counts stage keeps its own reuse: grid_bodies / grid_writes)
if "latency" in metrics.split(","):
    stages += ["durable"] if (wl == "writes" and wlat == "durable") else [r for r in ("warm", "cold") if r in regimes.split(",")]
# a latency capture timed under another request protocol (one request per plan) is moved aside,
# so this run writes a capture of one protocol (measure/capture_protocol; the readers refuse a mixed set)
# ... and when the figures this run draws would mix protocols, they are drawn labelled (plot_kvstats) and the run
# record marked protocol_mixed (the plan carries it to the record step)
latency = [st for st in stages if st in F.LATENCY_STAGES]
mixed = []
if plots == "1" and latency:
    measured = {(sl, st) for sl in slugs.split(",") for st in latency}
    for sel in ([show.split(",")] if show else [L.baseline_show(sf, wl, root=root), sorted(set(L.all_show(sf, wl, root=root)) | set(slugs.split(",")))]):
        why = F.plot_mix_after_run(sf, wl, sel, measured, root=root)
        if why:
            mixed.append(f"the figures of {','.join(sel)} mix request protocols after this run: {why}")
            print(f"[kvstats] WARNING {mixed[-1]}: drawn labelled; re-measure those stores (add them to --formats) "
                  "for one protocol", file=sys.stderr)
moved = {}
for slug in slugs.split(","):
    for st in latency:
        path = F.move_aside_other_protocol(sf, wl, slug, st, root=root)
        if path:
            moved.setdefault(slug, {})[st] = path
            left = sorted({r["query"] for r in L.read(os.path.join(path, "steps.csv"))} - set(qs))
            print(f"[kvstats] {slug} {st}: the capture was timed under another request protocol -- moved to {path}; "
                  "this run measures its queries again" + (f"; {len(left)} queries it held are not in this run and "
                  f"are now only in the moved capture: {','.join(left)}" if left else ""))
plan = F.plan(sf, wl, slugs.split(","), qs, nparam, stages, schema, formats=fmts.split(","), reuse_stale=reuse_stale == "1",
              root=root)
for slug, v in plan.items():
    v["moved_aside"] = moved.get(slug, {})
    v["protocol_mixed"] = mixed
for slug, v in plan.items():
    for st in stages:
        n = len(v["fresh"][st])
        if fresh == "1":
            v["skip"] = []
            if n:
                print(f"[kvstats] --fresh: {slug} {st} ({n}/{len(qs)} queries fresh) -- re-measured anyway")
        elif st in v["skip"]:
            kept = v["reused_stale"].get(st, {})
            print(f"[kvstats] fresh: {slug} {st} ({n}/{len(qs)} queries captured under the current identity"
                  + (f"; {len(kept)} kept by --reuse-stale, their identity moved: {','.join(sorted(kept))}" if kept else "")
                  + ") -- reused; --fresh re-measures")
        elif n:
            print(f"[kvstats] partial: {slug} {st} ({n}/{len(qs)} fresh) -- re-measured")
json.dump(plan, open(out, "w"))
EOF3
  skip() { "$PYTHON" -c "import json,sys; p=json.load(open('$FRESH_JSON')); sys.exit(0 if '$2' in p.get('$1',{}).get('skip',[]) else 1)"; }
  write_run_record start
  log "kvstats sf$SF workload=$WORKLOAD stores=$SLUGS nparam=$NPARAM metrics=$METRICS run=$RUN_ID -> $OUT"

  if has counts; then
    STAGE="counts"
    # a format's store at this run's chunk: a chunk arm names it (dgraph-c512k, pertype_csr-c16k)
    ARMS=""
    for fmt in ${FORMATS//,/ }; do
      arm="$fmt"; if (( CHUNK_GIVEN )) && [[ "$fmt" == dgraph || "$fmt" == pertype_csr ]]; then arm="$fmt-c$CHUNK"; fi
      ARMS="${ARMS:+$ARMS,}$arm"
    done
    COUNTS_FRESH=(); (( FRESH )) && COUNTS_FRESH=(--fresh)
    [[ -n "$QUERIES" ]] && log "counts: the whole suite is measured (or reused); --queries narrows the latency stages only"
    # kvcost measure on the named formats: the reported params only (no held-out capture), grid_bodies for a read query
    # set and grid_writes for writes; it marks each store's record by its binary rule
    log "counts+bytes per query + per step, every body (kvcost measure --family $WORKLOAD ${ARMS//,/ }) -> the formats capture"
    "$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --family "$WORKLOAD" ${ARMS//,/ } --nparam "$NPARAM" \
        --pick-on in-sample "${COUNTS_FRESH[@]}" "${ROOT_ARG[@]}"
  fi
  if has latency && [[ "$WORKLOAD" == writes && "$WRITE_LATENCY" == durable ]]; then
    STAGE="durable-p1"
    for fmt in ${FORMATS//,/ }; do
      s="$(slug_of "$fmt")"
      skip "$s" durable && continue
      log "durable write latency $s (cppwlatency: link-copy arena, apply minus shadow; + breakdown pass at perf 3)"
      "$PYTHON" -m kvevaluator.measure.cppwlatency --sf "$SF" --arms "$s" --nparam "$NPARAM" \
          --breakdown --out "$(timing "$s" durable-p1)" "${LQ[@]}"
    done
  elif has latency; then
    STAGE="warm-p0"
    for fmt in $([[ ",$REGIMES," == *,warm,* ]] && echo ${FORMATS//,/ }); do
      s="$(slug_of "$fmt")"
      skip "$s" warm && continue
      log "warm-p0 $s (--warm $WARM, perf_level 0)"
      "$PYTHON" -m kvevaluator.measure.cpplatency --sf "$SF" --family "$WORKLOAD" --format "$fmt" --schema "$SCHEMA" \
          --arms "$s" --nparam "$NPARAM" --warm "$WARM" --cold '' --perf-level 0 \
          --out "$(timing "$s" warm-p0)" --accumulate "$RUN_ID" "${LQ[@]}"
    done
    STAGE="$COLD_DIR"
    for fmt in $([[ ",$REGIMES," == *,cold,* ]] && echo ${FORMATS//,/ }); do
      s="$(slug_of "$fmt")"
      skip "$s" cold && continue
      log "$COLD_DIR $s (--cold $COLD, ${COLD_FLAGS[*]})"
      "$PYTHON" -m kvevaluator.measure.cpplatency --sf "$SF" --family "$WORKLOAD" --format "$fmt" --schema "$SCHEMA" \
          --arms "$s" --nparam "$NPARAM" --warm '' --cold "$COLD" "${COLD_FLAGS[@]}" \
          --out "$(timing "$s" "$COLD_DIR")" --accumulate "$RUN_ID" "${LQ[@]}"
    done
  fi
  STAGE="plots"
else
  [[ -d "$OUT" ]] || { echo "no captured CSVs at $OUT -- run without --no-measure first" >&2; exit 1; }
  log "--no-measure: figures from the captured CSVs under $OUT"
fi

# the default selection: the baselines PRESENT in the capture + this run's slugs (a lone
# format is compared, never drawn alone); an explicit --show draws exactly what it names
# a capture publishes exactly TWO selections (the rule): the engine baseline
# (dgraph, nebula, arangodb as present -> plots / plots-arangodb / plots-only-...) and every slug it
# holds (-> plots-all); an explicit --show draws that one selection instead
if (( ! PLOTS )); then
  log "--no-plots: CSVs only"
elif [[ -n "$SHOW_GIVEN" ]]; then
  log "plots for $SHOW"
  "$PYTHON" experiments/core_suite/plot_kvstats.py --sf "$SF" --family "$WORKLOAD" --show "$SHOW" "${ROOT_ARG[@]}"
else
  for sel in baseline all; do
    SHOW="$("$PYTHON" -c 'import sys; from kvevaluator.measure import kvstats_layout as L
print(",".join(getattr(L, sys.argv[1] + "_show")(sys.argv[2], sys.argv[3], root=sys.argv[4] or None)))' "$sel" "$SF" "$WORKLOAD" "$ROOT")"
    [[ -n "$SHOW" ]] || continue
    log "plots ($sel selection) for $SHOW"
    "$PYTHON" experiments/core_suite/plot_kvstats.py --sf "$SF" --family "$WORKLOAD" --show "$SHOW" "${ROOT_ARG[@]}"
  done
fi
echo "[$(basename "$0")] DONE${SF:+ (sf$SF)}"
