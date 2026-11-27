#!/usr/bin/env bash
# run/predict-accuracy.sh -- predict mode's accuracy check for meta: each KV
# step of the traced runs, priced from statistics, against the measured run, per step, judged by the accuracy bar.
#   0. the traced requests (python/kvevaluator/measure/predict_requests.py --picks: each (cell, query) on the body the
#      pick record names, inputs/picks/picks.csv) -> <out>/requests/
#   1. the statistics (python -m kvevaluator.predict.build_stats: kvevaluator graph-stats over the record stream,
#      format-free, no store): stats.json and the anchor statistics beside it, anchor_stats.json (LDBC: every Person's
#      own degrees and recency; job / linkbench: the vids the params bind, made per run); built when missing
#   2. the tally per layout: kvevaluator tally (the ingest encoder into a counting sink, no store) on the record stream;
#      at sf0.1 also checked against each store's census, group by group (they must be equal)
#   3. kvevaluator predict --accuracy --threads <jobs>, with the C++ cardinality layer's env (cpp/kvevaluator/src/predict/;
#      its rules' fixtures: tests/unit/check_predict_rules.py) -> steps.csv (per KV step: measured; truth = the exact
#      gate's rules on the step's truth card, which must equal measured on every lane, the check line "exact gate on these
#      steps: 0 of <L> lanes differ"; then a row per level of cpp/kvevaluator/src/predict/terms.h kLevels, each the
#      estimate's formulas with the term kinds the level names from that truth card and the rest from the statistics at
#      the default tier (kvevaluator.predict.tier1.DEFAULT_TIER: stats-tier1.json): true_chunks, true_terms, true_counts,
#      predicted (predict mode proper) and predicted_anchor (plus the anchor statistics, conditioned on the query's
#      parameter); the levels kLevels marks sampled are kvcost predict's, not this pass's), levels.json (every level),
#      terms.csv (every term each level
#      read, with its source), not_estimated.txt (the steps the estimate does not cover yet, each with its reason)
#   4. kvcost predict-accuracy --per-step <out> (kvcost.predict_accuracy.report prices the lanes) -> ratios.csv, summary.md
# Output: <output>/summary/<dataset>/<scale>/predict-accuracy/ (python/kvevaluator/output_tree.py); the statistics in
# <output>/stats/<dataset>/<scale>/. The stores must exist (run/predict-gate.sh builds them).
#   run/predict-accuracy.sh
# Flags: --sf (default 0.1); --cells a,b (the three uniform cells; any of the 49's seven, kvevaluator.layouts.SEVEN);
#        --queries a,b (default ic2,ic9,is3); --nparam N (default 5); --tier 1|2 (the statistics the predicted levels
#        read: default kvevaluator.predict.tier1.DEFAULT_TIER; written to <out>/statistics.json); --jobs N (the pass's threads, one store each at a
#        time, and the tallies built at once; default run_measure.stores_at_once; the output is the same at any N);
#        --family interactive|bi|microbench|writes (default interactive; the plans, params and pick-record section; output
#        then in predict-accuracy[-captures]-<workload>/, the tallies shared with interactive's dir;
#        writes (a write family of the registry: workloads.names(kind="write")): every write plan (the write gate's
#        params, run in shadow; --write-params capture: each plan's first --nparam rows on the pre-request store, the rows
#        kvcost predict binds), no cardinality layer, every level (a write's truth card with the request's replay), also
#        reported by kvcost.predict_accuracy.write_report -> write_summary.md, write_ratios.csv;
#        job / linkbench: their own dataset, --sf its data tag (job: sample1pct; linkbench: n1m), every plan by default
#        (job: the 113 queries, one run each; linkbench: its 8 reads; the anchors serve every read but getlink, which
#        needs --param-set quick|quick-next, output then in predict-accuracy-params-<set>/; --queries writes: its 9 writes, at
#        --param-set quick|quick-next, the write path and report as for --family writes, in ...-writes/), statistics from build_stats
#        --family, output in their dataset's summary dir);
#        --reuse-stale (captures mode: keep a statistics file made from another record stream, schema or spec, marked
#        reused_stale in <out>/stale.jsonl); --allow-dirty (a no-op: a dirty binary is warned and marked binary_unclean in <out>/stale.jsonl, which the
#        summary folds in); --rules-off a,b (cardinality rules to turn off, to see each
#        one's effect: the names `kvevaluator predict --rule-names` prints, the switches of
#        cpp/kvevaluator/src/predict/cardinality.h);
#        output then in predict-accuracy-off-<a>-<b>/); -h.
#        Existing statistics are reused (--fresh-stats rebuilds them); predict refuses ones built from another schema.
#        --captures <output>/captures/ldbc/sf<SF>: predict mode against a measured capture instead of traced runs: no store is
#        opened (requests only name the capture's stores), then kvcost predict (the predicted captures, levels predicted
#        and predicted_anchor, under <out>/predicted[-anchor]/; each layout's tally linked as tally-by-name/meta-<sha12>.json)
#        and the join to the measured capture of --workload (python -m kvcost.predict_accuracy.join: --captures names the
#        place dir or the capture), reported by kvcost predict-accuracy --per-step; output in predict-accuracy-captures/). Existing tallies are reused when made from the same record
#        stream (a tally: for the same spec; their binary is printed); --fresh-stats rebuilds them. --out DIR: the
#        outputs go there (the tallies stay shared). --fresh: replace predicted captures made from other statistics or params (the
#        statistics kept). --param-set uniform: LDBC Interactive on the uniform param set (datasets/ldbc/params.py
#        PARAM_SETS): the requests bind it, the measured capture's uniform params, outputs in ...-params-uniform/ (or
#        --out); ldbc names the default set.
# Expected run time (10 queries x 5 params x 3 uniform cells; binary 5ebf48a, this machine):
#   sf0.1, --family writes (20 write plans x 5 params x 3 cells, 225 requests): ~30 s, of it the census checks 25 s
#    
#   sf0.1, traced runs: ~35 s (inputs 26 s, of it the census checks; predict --accuracy a few s); the statistics 6 s
#   sf0.1, --captures, every query with a pick on the three uniform cells: ~25 s per workload with --fresh-stats
#    
#   sf3, --captures: ~16 min with --fresh-stats (the statistics 236 s; tally 285 / 239 / 254 s for Split / Condensed / Chunked, one after
#     another); 8 s when they are reused (predict --plans and the join take seconds); --family bi / microbench with
#     them reused: 7 s / 6 s at sf3, 1 s each at sf0.1 (binary 768edd5 + this change)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the repo root: a manifest's schema path is relative to it

SF=0.1
CELLS=props_split_adj_split,props_condensed_adj_condensed,props_chunked_adj_chunked
QUERIES=ic2,ic9,is3
WORKLOAD=interactive
NPARAM=5
RULES_OFF=
CAPTURES=
OUT_ARG=
FRESH_STATS=0
REUSE_STALE=0   # --reuse-stale: keep a statistics file made from another stream, schema or spec, marked reused_stale
FRESH_PRED=0
PARAM_SET=
JOBS=
TIER=      # the statistics tier: empty = kvevaluator.predict.tier1.DEFAULT_TIER
WRITE_PARAMS=   # a write family's rows: predict_requests --write-params (empty: its default)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)      SF="${2:?--sf needs a value}"; shift 2 ;;
    --cells)   CELLS="${2:?--cells needs a comma list}"; shift 2 ;;
    --queries) QUERIES="${2:?--queries needs a comma list}"; shift 2 ;;
    --family) WORKLOAD="${2:?--family needs interactive, bi, microbench or a write family}"; shift 2 ;;
    --nparam)  NPARAM="${2:?--nparam needs a value}"; shift 2 ;;
    --jobs)    JOBS="${2:?--jobs needs a count}"; shift 2 ;;
    --tier)    TIER="${2:?--tier needs 1 or 2}"; shift 2 ;;
    --write-params) WRITE_PARAMS="${2:?--write-params needs sequence or capture}"; shift 2 ;;
    --allow-dirty) shift ;;   # a no-op: a dirty binary is warned and marked binary_unclean
    --rules-off) RULES_OFF="${2:?--rules-off needs a comma list}"; shift 2 ;;
    --captures) CAPTURES="$(cd "${2:?--captures needs a capture dir}" && pwd)"; shift 2 ;;
    --fresh-stats) FRESH_STATS=1; shift ;;
    --reuse-stale) REUSE_STALE=1; shift ;;
    --out) OUT_ARG="${2:?--out needs a dir}"; shift 2 ;;
    --fresh) FRESH_PRED=1; shift ;;
    --param-set) PARAM_SET="${2:?--param-set needs a set name}"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "predict-accuracy: unknown flag $1" >&2; exit 2 ;;
  esac
done
# the tallies depend on the record stream and the layout, not the workload: one copy in SDIR, read by every workload's
# run; a workload other than LDBC Interactive writes its own outputs beside it (-<workload>). The dataset: the
# workload's (job, linkbench: --sf is its data tag); its statistics and record stream are the shared tree's.
SCHEMA_JSON=$("$PYTHON" -m kvevaluator.datasets schema-path ldbc) || exit 1
read -r DATASET RECORDS FULL_STATS ANCHOR <<< "$("$PYTHON" -c 'import sys; from kvevaluator import workloads
from kvevaluator.measure.records import records_cache_path
from kvevaluator.predict.build_stats import anchor_path, stats_file
ds = workloads.get(sys.argv[1]).dataset
print(ds, records_cache_path(ds, sys.argv[2]), stats_file(ds, sys.argv[2]), anchor_path(stats_file(ds, sys.argv[2])))' "$WORKLOAD" "$SF")" || exit 1
summary() { "$PYTHON" -m kvevaluator.output_tree summary --dataset "$DATASET" --scale "$SF" --analysis "$1"; }
[[ -n "$JOBS" ]] || JOBS=$("$PYTHON" -c 'import sys; from kvevaluator.measure import run_measure
print(run_measure.stores_at_once(None, 1, sys.argv[1], sys.argv[2])[0])' "$DATASET" "$SF")
ANCHOR_ARGS=()                              # own-data datasets: the vids the params bind (below)
count() { tr ',' '\n' <<< "$1" | grep -c .; }
OWN=0
WRITE_STUDY=0                               # the write path: no cardinality layer, the write report
# a write family (the registry's kind; LDBC's writes run the write gate's sequence)
WRITE_FAMILY=$("$PYTHON" -c 'import sys; from kvevaluator import workloads
print(int(sys.argv[1] in workloads.names(kind="write")))' "$WORKLOAD") || exit 1
[[ $WRITE_FAMILY -eq 1 ]] && WRITE_STUDY=1
if [[ "$WORKLOAD" == job || "$WORKLOAD" == linkbench ]]; then
  OWN=1
  [[ -z "$CAPTURES" ]] || { echo "predict-accuracy: --captures is not wired for $WORKLOAD" >&2; exit 2; }
  SCHEMA_JSON="$ROOT/$("$PYTHON" -c "from kvevaluator import datasets; print(datasets.get(\"$DATASET\").schema)")"
  ANCHOR_ARGS=()
  # each plan as a read (r) or a write (w): a run takes one kind (the reads' cardinality layer, or the write path);
  # a read with no params in this param sample is "-" (LinkBench's getlink at the anchors), "x" when its params are
  # missing for another reason (a default list then stops; an explicit list fails at request building if it names one)
  KINDS=$("$PYTHON" -c 'import sys; from kvevaluator.measure import workload_plans as w
from kvevaluator.formats import KV_WRITE_OPS
for q in w.queries(sys.argv[1]):
    b = w.ir_for(sys.argv[1], q)
    steps = b["steps"] if "steps" in b else [x for body in b["plans"].values() for x in body["steps"]]
    kind = "w" if any(s.get("op") in KV_WRITE_OPS for s in steps) else "r"
    if kind == "r":
        try:
            w.query_params(sys.argv[1], sys.argv[2], q, 1, sys.argv[3] or None)
        except SystemExit as e:   # "-": no anchor params in this sample; "x": another params error
            kind = "-" if "has no anchor params" in str(e) else "x"
    print(q, kind)' "$WORKLOAD" "$SF" "${PARAM_SET:-}")
  if [[ "$QUERIES" == ic2,ic9,is3 ]]; then   # every read with params in this sample
    BAD=$(awk '$2 == "x" {print $1}' <<< "$KINDS" | paste -sd,)
    [[ -z "$BAD" ]] || { echo "predict-accuracy: no params for $BAD (name the reads with --queries, or make the params)" >&2; exit 2; }
    QUERIES=$(awk '$2 == "r" {print $1}' <<< "$KINDS" | paste -sd,)
    NOP=$(awk '$2 == "-" {print $1}' <<< "$KINDS" | paste -sd,)
    [[ -z "$NOP" ]] || echo "predict-accuracy: not run, no params in this sample: $NOP"
  elif [[ "$QUERIES" == writes ]]; then QUERIES=$(awk '$2 == "w" {print $1}' <<< "$KINDS" | paste -sd,); fi  # every write
  [[ -n "$QUERIES" ]] || { echo "predict-accuracy: no $WORKLOAD query of that kind to run" >&2; exit 2; }
  NW=$(tr , '\n' <<< "$QUERIES" | grep -cxF -f <(awk '$2 == "w" {print $1}' <<< "$KINDS") || true)   # "x" is a read
  NQ=$(count "$QUERIES")
  [[ $NW -eq 0 || $NW -eq $NQ ]] || { echo "predict-accuracy: $NW of $NQ queries are writes: run reads and writes apart" >&2; exit 2; }
  [[ $NW -gt 0 ]] && WRITE_STUDY=1
  [[ "$WORKLOAD" == job ]] && NPARAM=1   # a JOB query has no parameters: one run
fi
SAMPLE=""   # LinkBench's params: the anchors (default), or a request-log sample (--param-set quick|quick-next) in its own dir
[[ "$WORKLOAD" == linkbench && -n "$PARAM_SET" && "$PARAM_SET" != anchors ]] && SAMPLE="-params-$PARAM_SET"
[[ $OWN -eq 1 && $WRITE_STUDY -eq 1 ]] && SAMPLE="$SAMPLE-writes"
NAME="predict-accuracy$SAMPLE${CAPTURES:+-captures}${RULES_OFF:+-off-${RULES_OFF//,/-}}"
SDIR=$(summary "$NAME") || exit 1
OUT_NAME="$NAME"
[[ "$WORKLOAD" == interactive || $OWN -eq 1 ]] || OUT_NAME="$NAME-$WORKLOAD"
# --param-set: a non-default set (the uniform set) has its own output dir and capture params; the tallies stay shared
PARAMS=$("$PYTHON" -c 'import sys; from kvevaluator.output_tree import params_dir; print(params_dir(sys.argv[1], sys.argv[2] or None))' "$WORKLOAD" "$PARAM_SET") || exit 1
SET_ARG=()
if [[ "$WORKLOAD" == linkbench ]]; then   # its sample is in the name already: SAMPLE above
  [[ -z "$PARAM_SET" ]] || SET_ARG=(--param-set "$PARAM_SET")
elif [[ "$PARAMS" != reported ]]; then OUT_NAME="$OUT_NAME-params-$PARAM_SET"; SET_ARG=(--param-set "$PARAM_SET"); fi
OUT=$(summary "$OUT_NAME") || exit 1
[[ -z "$OUT_ARG" ]] || OUT="$(mkdir -p "$OUT_ARG" && cd "$OUT_ARG" && pwd)"   # a caller's own output dir (the tallies stay in SDIR)
REQ="$OUT/requests"
BIN=$("$CPP_BIN" --version | "$PYTHON" -c 'import json,sys; d=json.load(sys.stdin); print(d["binary_git_sha"], "dirty=" + str(d["binary_dirty"]))')
echo "predict-accuracy: binary $BIN"
[[ $WRITE_FAMILY -eq 1 && -n "$CAPTURES" ]] && { echo "predict-accuracy: a write family runs traced requests, not --captures" >&2; exit 2; }
mkdir -p "$SDIR/tally" "$OUT"
"$PYTHON" -m kvevaluator.stale start --into "$OUT" || exit 1   # an earlier run's notes moved aside (kvevaluator.stale)
# a dirty binary is not a refusal: warned and marked binary_unclean in <out>/stale.jsonl
[[ "$BIN" == *"dirty=False" ]] || "$PYTHON" -m kvevaluator.stale note --into "$OUT" binary_unclean "the binary ${BIN%% *} is dirty (${BIN##* })" || exit 1
rm -rf "$REQ"
t0=$SECONDS
# cells x the param rows each query has, up to NPARAM (a query with no parameter, e.g. the microbench's entrylabel_*, has one)
if [[ $WRITE_FAMILY -eq 1 ]]; then
  # every write plan unless --queries names some (the write gate's sequence, or --write-params capture's rows;
  # predict_requests checks its own count)
  [[ "$QUERIES" == ic2,ic9,is3 ]] && QUERIES=all
  "$PYTHON" -m kvevaluator.measure.predict_requests --family "$WORKLOAD" --sf "$SF" --cells "$CELLS" --queries "$QUERIES" \
    --nparam "$NPARAM" ${WRITE_PARAMS:+--write-params "$WRITE_PARAMS"} --out "$REQ"
elif [[ $OWN -eq 1 ]]; then
  PICKS=(--picks); [[ "$WORKLOAD" == linkbench ]] && PICKS=()   # LinkBench's queries are single plans: no pick record
  EXPECT=$("$PYTHON" -c 'import sys; from kvevaluator.measure.workload_plans import query_params as p
w, sf, qs, n, c, s = sys.argv[1:7]; print(int(c) * sum(len(p(w, sf, q, int(n), s or None)) for q in qs.split(",")))' \
    "$WORKLOAD" "$SF" "$QUERIES" "$NPARAM" "$(count "$CELLS")" "${SET_ARG[1]:-}")
  "$PYTHON" -m kvevaluator.measure.predict_requests "${PICKS[@]}" --family "$WORKLOAD" --sf "$SF" --cells "$CELLS" \
    --queries "$QUERIES" --nparam "$NPARAM" --ensure-stores --out "$REQ" --expect "$EXPECT" "${SET_ARG[@]}"
else
EXPECT=$("$PYTHON" -c 'import sys; from kvevaluator.measure.workload_plans import query_params as p
w, sf, qs, n, c, s = sys.argv[1:7]; print(int(c) * sum(len(p(w, sf, q, int(n), s or None)) for q in qs.split(",")))' \
  "$WORKLOAD" "$SF" "$QUERIES" "$NPARAM" "$(count "$CELLS")" "${SET_ARG[1]:-}")
"$PYTHON" -m kvevaluator.measure.predict_requests --picks --family "$WORKLOAD" --sf "$SF" --cells "$CELLS" --queries "$QUERIES" --nparam "$NPARAM" \
  --out "$REQ" --expect "$EXPECT" "${SET_ARG[@]}"
fi
# the statistics (and LDBC's anchor tier): built when missing or stale (another dump of the record stream, contract or graph-stats version),
# or always with --fresh-stats
IF_STALE=(--if-stale); [[ $FRESH_STATS -eq 1 ]] && IF_STALE=()
if [[ $OWN -eq 1 ]]; then
  KVEVAL_CPP_BIN="$CPP_BIN" "$PYTHON" -m kvevaluator.predict.build_stats --family "$WORKLOAD" --tag "$SF" "${IF_STALE[@]}"
else
  KVEVAL_CPP_BIN="$CPP_BIN" "$PYTHON" -m kvevaluator.predict.build_stats --sf "$SF" "${IF_STALE[@]}"
fi
# the statistics the predicted levels read: --tier's, else the default tier's (kvevaluator.predict.tier1.DEFAULT_TIER)
read -r TIER STATS <<< "$("$PYTHON" -c 'import sys; from kvevaluator.predict import tier1
t = int(sys.argv[2]) if sys.argv[2] else tier1.DEFAULT_TIER
print(t, tier1.for_tier(sys.argv[1], t))' "$FULL_STATS" "$TIER")" || exit 1
mkdir -p "$OUT"
"$PYTHON" -c 'import hashlib, json, os, sys
json.dump({"tier": int(sys.argv[1]), "path": os.path.basename(sys.argv[2]),
           "sha256": hashlib.sha256(open(sys.argv[2], "rb").read()).hexdigest()}, open(sys.argv[3], "w"), indent=1)' \
  "$TIER" "$STATS" "$OUT/statistics.json" || exit 1
echo "predict-accuracy: statistics $STATS (tier $TIER; $OUT/statistics.json)"
# reuse a statistics file in captures mode, and a tally in either mode (minutes each at sf3), unless --fresh-stats, when it
# was made from this record stream (and, for a tally, from this layout's spec: $2); say which binary made it. A traced
# run still checks a reused tally against the store's census
reuse() {
  [[ ( -n "$CAPTURES" || -n "${2:-}" ) && $FRESH_STATS -eq 0 && -s "$1" ]] || return 1
  "$PYTHON" - "$1" "$RECORDS" "${2:-}" "$SCHEMA_JSON" "$REUSE_STALE" "$OUT" "$CPP_BIN" <<'EOF'
import json, subprocess, sys
from kvevaluator import stale
d = json.load(open(sys.argv[1]))
moved = []
# the record stream by predict's own rule (kvevaluator predict --made-from: util/record_stream.h record_stream_mismatch)
r = subprocess.run([sys.argv[7], "predict", "--made-from", sys.argv[1], "--records", sys.argv[2]],
                   capture_output=True, text=True)
if r.returncode not in (0, 1):
    sys.exit(f"predict-accuracy: kvevaluator predict --made-from failed (rc {r.returncode}): {r.stderr.strip()}")
if r.returncode == 1:
    moved.append(f"made from another record stream than {sys.argv[2]} ({r.stdout.strip()})")
if not sys.argv[3] and d.get("schema_hash") != json.load(open(sys.argv[4]))["hash"]:   # graph statistics: predict refuses another schema
    moved.append(f"made from schema {str(d.get('schema_hash'))[:12]}, not {sys.argv[4]}'s")
if sys.argv[3] and d["spec"] != json.load(open(sys.argv[3])):
    moved.append(f"made for another spec than {sys.argv[3]}")
if moved and sys.argv[5] != "1":
    sys.exit(f"predict-accuracy: not reused {sys.argv[1]}: {'; '.join(moved)} (--reuse-stale keeps it)")
if moved:   # --reuse-stale: kept, marked in <out>/stale.jsonl (the summary folds it in)
    stale.note_into(sys.argv[6], "reused_stale", f"{sys.argv[1]}: kept although {'; '.join(moved)} (--reuse-stale)")
print(f"predict-accuracy: reused {sys.argv[1]} (binary {d['provenance']['binary_git_sha'][:10]})")
EOF
}
if [[ $OWN -eq 1 ]]; then   # the anchor statistics: the vids the params bind in the plans (level predicted_anchor; a study level)
  "$PYTHON" -m kvevaluator.predict.anchor_vids "$WORKLOAD" "$SF" "$QUERIES" "$NPARAM" "${SET_ARG[1]:-}" > "$SDIR/anchor_vids.txt"
  ANCHOR_ARGS=(--anchor-vids "$SDIR/anchor_vids.txt")
  echo "predict-accuracy: $(grep -c . "$SDIR/anchor_vids.txt") anchor vids from the requests' params"
fi
[[ -f "$RECORDS" ]] || { echo "predict-accuracy: no record stream $RECORDS (run/predict-gate.sh builds the stores)" >&2; exit 1; }
if [[ $OWN -eq 1 ]]; then   # this run's anchor tier (its statistics are build_stats'; graph-stats' stdout is not kept)
  ANCHOR="$SDIR/anchor_stats.json"
  reuse "$ANCHOR" || "$CPP_BIN" graph-stats --schema "$SCHEMA_JSON" "${ANCHOR_ARGS[@]}" --anchor-out "$ANCHOR" \
    --records "$RECORDS" > /dev/null
fi
[[ -f "$ANCHOR" ]] || { echo "predict-accuracy: no anchor statistics $ANCHOR (python -m kvevaluator.predict.build_stats)" >&2; exit 1; }
# each layout's tally (and, traced, its census check): up to --jobs at once, each one's verdict in $OUT/tally-rc/<name>
tally_one() {
  local store=$1 name schema
  name=$(basename "$store")
  "$PYTHON" -c 'import json,sys; m=json.load(open(sys.argv[1] + "/manifest.json")); json.dump(m["value_codec"], open(sys.argv[2], "w"))' \
    "$store" "$SDIR/tally/$name.spec.json" || return 1
  # the manifest's schema through the shared resolver (a store built before a move names the old path: moved_paths.json)
  schema=$("$PYTHON" -c 'import json,sys; from kvevaluator.capture.store_identity import manifest_schema_path as m
p = m(json.load(open(sys.argv[1] + "/manifest.json"))); print(p or sys.exit(f"predict-accuracy: no schema for {sys.argv[1]}"))' "$store") || return 1
  reuse "$SDIR/tally/$name.json" "$SDIR/tally/$name.spec.json" ||
    "$CPP_BIN" tally --schema "$schema" --spec "$SDIR/tally/$name.spec.json" --records "$RECORDS" > "$SDIR/tally/$name.json" ||
    return 1
  [[ -z "$CAPTURES" ]] || return 0   # the census check opens the store: not in captures mode
  "$CPP_BIN" census "$store" > "$SDIR/tally/$name.census.json" || return 1
  "$PYTHON" - "$SDIR/tally/$name.json" "$SDIR/tally/$name.census.json" <<'EOF'
import json, sys
t, c = (json.load(open(p)) for p in sys.argv[1:3])
key = lambda g: (g["part"], g["type"], g["direction"], g["owner"], g["detail"])
fields = ["keys", "key_bytes", "value_bytes", "payload_bytes", "empty_values"]
tg, cg = {key(g): g for g in t["groups"]}, {key(g): g for g in c["groups"]}
bad = [k for k in sorted(set(tg) | set(cg)) if k not in tg or k not in cg or any(tg[k].get(f) != cg[k].get(f) for f in fields)]
print(f"predict-accuracy: tally vs census {sys.argv[1].rsplit('/', 1)[-1]}: {len(cg)} groups, {len(bad)} differ")
sys.exit(1 if bad or not cg else 0)
EOF
}
STORES=$("$PYTHON" -c 'import json,sys,glob; print("\n".join(sorted({json.load(open(p))["store"]["dir"] for p in glob.glob(sys.argv[1] + "/*.json")})))' "$REQ")
RCDIR="$OUT/tally-rc"
mkdir -p "$RCDIR"
for store in $STORES; do
  rm -f "$RCDIR/$(basename "$store")"
  ( set +e; tally_one "$store"; echo $? > "$RCDIR/$(basename "$store")" ) &
  while [[ $(jobs -rp | wc -l) -ge $JOBS ]]; do sleep 1; done
done
wait
for store in $STORES; do   # a layout whose tally or census check failed stops the run here, by name
  rc=$(cat "$RCDIR/$(basename "$store")" 2>/dev/null || echo missing)
  [[ "$rc" == 0 ]] || { echo "predict-accuracy: the tally of $store failed (rc $rc)" >&2; exit 1; }
done
echo "predict-accuracy: inputs in $((SECONDS - t0)) s"
if [[ -n "$CAPTURES" ]]; then   # predict mode: no store; kvcost predict, then joined to the capture's step rows
  # kvcost predict names a layout's store, and so its tally, as a built store of the cell's spec is named now
  # (meta-<sha12>); a store named meta-<stem> has another name: each layout's tally linked under the new one
  mkdir -p "$SDIR/tally-by-name"
  "$PYTHON" - "$REQ" "$SDIR/tally" "$SDIR/tally-by-name" <<'EOF'
import glob, json, os, sys
from kvevaluator.measure import stores
from kvcost.predict_accuracy.requests import GRID_SPECS
req, tally, by_name = sys.argv[1:]
for f in sorted(glob.glob(os.path.join(req, "*.json"))):
    cell = os.path.basename(f).split("__")[0]
    link = os.path.join(by_name, f"{stores.meta_store_name(GRID_SPECS / f'{cell}.json')}.json")
    target = os.path.relpath(os.path.join(tally, os.path.basename(json.load(open(f))["store"]["dir"]) + ".json"), by_name)
    if os.path.islink(link) and os.readlink(link) != target:
        os.remove(link)
    if not os.path.lexists(link):
        os.symlink(target, link)
EOF
  FRESH=(); [[ $FRESH_STATS -eq 1 || $FRESH_PRED -eq 1 ]] && FRESH=(--fresh)
  DS=$("$PYTHON" -c 'import sys; from kvevaluator import workloads; print(workloads.get(sys.argv[1]).dataset)' "$WORKLOAD")
  in_tree "$OUT" "$PYTHON" -m kvcost.cli.cli predict --dataset "$DS=$SF" --family "$WORKLOAD" --queries "$QUERIES" \
    ${CELLS//,/ } --tally-dir "$SDIR/tally-by-name" --anchor-stats-file "$ANCHOR" --tier "$TIER" \
    --schema-file "$SCHEMA_JSON" --nparam "$NPARAM" --plans picked --work-dir "$OUT/predict-work" \
    ${RULES_OFF:+--no-rules "$RULES_OFF"} "${FRESH[@]}" "${SET_ARG[@]}"
  cat "$OUT"/predict-work/logs/*.err > "$OUT/accuracy.err"
  read -r PREDICTED PREDICTED_ANCHOR <<< "$("$PYTHON" -c 'import sys; from kvcost.cli.predict import TIER_SETS, capture_dirs
print(*capture_dirs(sys.argv[1], sys.argv[2], TIER_SETS[int(sys.argv[5])], sys.argv[3], sys.argv[4] or None))' \
    "$SF" "$WORKLOAD" "$OUT" "${SET_ARG[1]:-}" "$TIER")" || exit 1
  "$PYTHON" -m kvcost.predict_accuracy.join --dataset "$DS=$SF" --family "$WORKLOAD" ${CELLS//,/ } --queries "$QUERIES" \
    --param-rows "1-$NPARAM" --predicted "$PREDICTED" --predicted-anchor "$PREDICTED_ANCHOR" --measured "$CAPTURES" \
    --out "$OUT"
else
  rc=0
  "$CPP_BIN" predict --accuracy --threads "$JOBS" --stats "$STATS" --anchor-stats "$ANCHOR" --tally-dir "$SDIR/tally" \
    --terms-out "$OUT/terms.csv" --levels-out "$OUT/levels.json" ${RULES_OFF:+--rules-off "$RULES_OFF"} "$REQ"/*.json > "$OUT/steps.csv" 2> "$OUT/accuracy.err" \
    || rc=$?
  # the pass's own lines first, so a failure shows its reason (a truth that differs: rc 1; a refusal: rc 3)
  if [[ $rc -ne 0 ]]; then
    grep -v "^not estimated" "$OUT/accuracy.err" >&2 || true
    echo "predict-accuracy: FAIL (predict --accuracy rc=$rc; $OUT/accuracy.err)" >&2
    exit "$rc"
  fi
fi
grep -v "^not estimated" "$OUT/accuracy.err" || true
grep "^not estimated" "$OUT/accuracy.err" > "$OUT/not_estimated.txt" || true   # one line per step (and level), with its reason
if [[ $WRITE_STUDY -eq 1 ]]; then
  "$PYTHON" -m kvcost.predict_accuracy.write_report "$OUT" "$BIN"
  echo "predict-accuracy: DONE in $((SECONDS - t0)) s ($OUT/write_summary.md)"
else
  "$PYTHON" -m kvcost.cli.cli predict-accuracy --per-step "$OUT"
  echo "predict-accuracy: DONE in $((SECONDS - t0)) s ($OUT/summary.md)"
fi
