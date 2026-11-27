#!/usr/bin/env bash
# run/cpp-diff.sh -- `just cpp-diff` (the round, run/meta-gates.sh, calls it). Args: [nparam, default 5] [lanes, default all].
set -euo pipefail
case "${1:-}" in -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0;; esac   # before env.sh changes dir
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"
SF01_DIR=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale 0.1) || exit 1   # LDBC's studies at sf0.1
NPARAM="${1:-5}"
LANES="${2:-all}"
"$PYTHON" tests/gates/gate_preflight.py --gate cpp-diff --sf 0.1 --lanes "$LANES"   # the binary + the store, named, before any part
"$PYTHON" tests/lint/check_no_python_measurement.py
"$PYTHON" tests/lint/check_no_type_from_id.py                       # a vid is opaque at query time
# the per-role size cache vs a fresh walk (a classifier edit without a kKeyRolesVersion bump); kvsize first, so a
# fresh machine has caches to check
"$PYTHON" -m kvevaluator.measure.kvsize --sf 0.1 > /dev/null
"$PYTHON" tests/gates/check_size_roles_cache.py --sf 0.1
"$PYTHON" cpp/kvevaluator/tests/gen_expr_fixtures.py --check   # the C++ expr parity fixtures match python/kvevaluator/plans/ir/expr.py
# the capability sets the Python side selects plans and grid picks from == what the binary grants, per format,
# design, label and edge type (store-free, ~2 s): a drift would move picks with no count mismatch
"$PYTHON" tests/unit/check_caps_parity.py
# a Python twin's counts are refused where they are not its format's (Per-type CSR under the label rule); rows pass.
# 104 s on the Python oracle, no C++ store: a lane of its own (guard)
part_twin_guard() {
  out=$("$PYTHON" tests/gates/check_twin_count_guard.py 2>&1) || { printf '%s\n' "$out" | grep -v '^\[ldbc\]'; exit 1; }; printf '%s\n' "$out" | tail -1
}
part_pertype_rows() {
  "$PYTHON" tests/gates/check_cpp_vs_python.py --nparam $NPARAM \
    --report $SF01_DIR/cpp-evaluator/diff-sf0.1-n$NPARAM.json
}
# Per-type CSR's COUNTS on the steps its Python twin does not disown (runner.run_plan refuses the others: the label
# rule, a chunk with a far-label block, the index layout): S / G / N, the misses and the value bytes of every
# unrefused step must equal the C++ evaluator's. This is what makes "an unrefused twin count is the format's" true;
# the tool prints how many steps it compared and fails on 0 (7,858 of 8,815 over 472 cells, 219 s;
# with the twin's label mark switched off 7 of 8 control cells fail)
part_pertype_counts() {
  export KVEVAL_STORE_COPIES="${COPIES[0]}"   # Per-type CSR's store, first copy
  "$PYTHON" tests/gates/check_cpp_vs_python.py --nparam $NPARAM --compare-counts \
    --report $SF01_DIR/cpp-evaluator/diff-pertype-unrefused-counts-sf0.1-n$NPARAM.json | tail -3
}
# the edge-exists body with two separate reads (out at the anchor, then out at the candidates), which no store
# pins: forced on both sides, counts and bytes compared -- a chunk two reads of one step touch is charged once.
# Label tags off for this check: it compares the chunk-cache rule, and the Python twin carries no labels on rows
# (the C++ reads do; with them on, is7's hops read fewer families than the twin's)
# --twin-counts old-label-rule: the twin's counts on these hops are refused by default (run_plan's guard); here
# the C++ side runs the twin's rule and the chunks read hold no label block, so the comparison is like for like
part_edge_exists() {
  export KVEVAL_STORE_COPIES="${COPIES[1]}"   # Per-type CSR's store, second copy
  KVEVAL_LABEL_TAGS=hops=off,probes=off "$PYTHON" tests/gates/check_cpp_vs_python.py --nparam 35 --queries is7 edge_probe --force-body edge_probe_out --compare-counts --twin-counts old-label-rule \
    --report $SF01_DIR/cpp-evaluator/diff-edge-exists-two-reads-sf0.1-n35.json
}
# the BI workload (every plan file, 2 params) on the two formats whose Python twin runs it in about a minute at
# sf0.1; the full BI differential (5 params, arangodb too, pertype_csr's ~1.5 h Python chase) is the pre-merge
# check named in python/kvevaluator/workloads/bi/README.md "Run"
part_bi_nebula() {
  export KVEVAL_STORE_COPIES="${COPIES[0]}"   # NebulaGraph's store, a copy (the counts part reads the original)
  "$PYTHON" tests/gates/check_cpp_vs_python.py --families bi --format nebula --store nebula --pack --skip-bytes --nparam 2 \
    --report $SF01_DIR/cpp-evaluator/diff-bi-nebula-sf0.1-n2.json
}
part_bi_dgraph() {
  "$PYTHON" tests/gates/check_cpp_vs_python.py --families bi --format dgraph --store dgraph --pack --skip-bytes --nparam 2 \
    --report $SF01_DIR/cpp-evaluator/diff-bi-dgraph-sf0.1-n2.json
}
# COUNTS, C++ vs the Python oracle, on the formats whose Python twin runs the same index layout (the pertype_csr
# run above compares rows, order and bytes only: its store uses the posting index, counts_checked = false).
# LDBC + microbench, every plan file; --skip-bytes because the Python byte model differs from the C++ codec on
# these. About 4 min for the three at sf0.1.
counts_of() {
  "$PYTHON" tests/gates/check_cpp_vs_python.py --format "$1" --store "$1" --pack --skip-bytes --nparam $NPARAM \
    --report "$SF01_DIR/cpp-evaluator/diff-counts-$1-sf0.1-n$NPARAM.json"
}
part_counts_dgraph() { counts_of dgraph; }; part_counts_nebula() { counts_of nebula; }; part_counts_arangodb() { counts_of arangodb; }
# the engine twins (engine fidelity) vs their Python oracles: every recorded engine-plan cell at sf0.1, then the
# per-step branch cases -- check_cpp_vs_python loops over the compared formats only, so a change to a dgraph /
# nebula / arangodb read path the twins inherit is caught here
part_engine_plans() {
  "$PYTHON" tests/gates/check_engine_plans.py --check tests/fixtures/engine_plans_python_884bbfeb61.json --backend cpp
}
part_engine_ops() {
  "$PYTHON" tests/gates/check_engine_ops.py
}
# the label row scan with a keep set on Per-type CSR, C++ against its Python twin (hand plans: no plan file uses keep yet)
part_label_scan_keep() {
  "$PYTHON" tests/gates/check_label_scan_keep.py
}
# the label probe costs one probe per DISTINCT vid on every format: a one-step fixture plan, a repeated input
# against the same vids once, on every sf0.1 store but the parked schemas' (2 s); then its control, which must be caught
part_label_probe_distinct() {
  "$PYTHON" tests/gates/check_label_probe_distinct.py
  "$PYTHON" tests/gates/check_label_probe_distinct.py --control | grep -v "^  FAIL"   # the planted failure
}
# the plan IR round trip on the Python oracle (70 s): the template the resolver picked (the
# optimizer's pick, per-SF pins, a twin's transform), serialized, read back and bound, gives the same counts and
# rows as the plan production runs; dgraph + nebula, every LDBC registry query
part_planir() {
  "$PYTHON" tests/unit/check_planir_ldbc.py --sf 0.1 | sed -n '/^  abstract sf/,$p'   # the summary, every skip, the verdict
  # the same round trip on the microbench's plans, and the demo graph's (with the IR's own unit checks): each
  # prints its cells run
  "$PYTHON" tests/unit/check_microbench_planir.py --sf 0.1 | sed -n '/^  microbench sf/,$p'
  "$PYTHON" tests/unit/check_planir.py | sed -n '/^  round-trip:/,$p'
}
# Per-type CSR's COUNTS with labels on rows (the differential above compares rows on that format, never counts):
# every chunk-probe step replayed from the store's chunk keys and the labels its inputs carry == the measured
# S / S_miss / scan bytes (ldbc + microbench + bi; controls in its docstring).
# --stored: the store keeps the far end's label per adjacency entry (the default)
part_probe_counts() {
  export KVEVAL_STORE_COPIES="${COPIES[1]}"   # Per-type CSR's store, second copy
  "$PYTHON" tests/gates/check_probe_counts.py --sf 0.1 --store pertype_csr-c64k --nparam $NPARAM --stored \
    --out $SF01_DIR/label-rule/probe-counts-gate | tail -1
}
# LinkBench data blobs (the bytes type): stored, read, written and answered at their raw length in every format
# that runs LinkBench, a str blob refused where an input predates the type; then C++ vs the Python oracle on the
# LinkBench reads (counts, rows, order). Their own tiny stores (output/checks/linkbench-blob-bytes/), ~20 s
part_linkbench_blob_bytes() {
  "$PYTHON" tests/gates/check_linkbench_blob_bytes.py | grep -E "FAIL|^binary|cases, "   # every failing case, the binary, the summary
}
part_linkbench_counts() {
  "$PYTHON" tests/gates/check_cpp_vs_python_linkbench.py | grep -E "MISMATCH|^binary| mismatch over "
  # getlink answers every id of its request, against links.jsonl, on the three engine formats and the nine meta cells (~35 s)
  "$PYTHON" tests/gates/check_linkbench_getlink_ids.py --backend cpp | grep -E "FAIL|ERROR|^binary| cases over "
}
# JOB (IMDb as plans; the workload that builds vids from stored columns) against its Python twins: the round's 16
# queries (check_job_cpp_vs_python.py GATE_QUERIES), each query's first body, rows and S / G / N per step, on the
# three engine formats at the sample tag, one after another (4.5 GB each). Its own stores ($OUTPUT_DIR/stores/imdb-rows/)
# and data, made by gate-prep.
part_job() {
  local f out line ok=0 bad=0 want=0 n=0
  for f in dgraph nebula arangodb; do
    out=$("$PYTHON" tests/gates/check_job_cpp_vs_python.py --gate --format "$f" 2>&1) || { printf '%s\n' "$out"; exit 1; }
    printf '%s\n' "$out" | grep -E '^\[job cpp-vs-python\]|MISMATCH'
    line=$(printf '%s\n' "$out" | grep '^\[job cpp-vs-python\]')
    ok=$((ok + $(sed -E 's/.*: ([0-9]+) ok, .*/\1/' <<< "$line"))); bad=$((bad + $(sed -E 's/.* ([0-9]+) mismatch.*/\1/' <<< "$line")))
    n=$(sed -E 's/.* of ([0-9]+) expected.*/\1/' <<< "$line"); want=$((want + n))
  done
  echo "job: $n queries x 3 formats = $ok cells, $bad mismatch (of $want expected)"
  [ "$ok" -gt 0 ] && [ "$ok" = "$want" ] && [ "$bad" = 0 ]
}
# the parts in the order they print, and the lane (the C++ store) of each
ORDER="twin_guard pertype_rows pertype_counts edge_exists bi_nebula bi_dgraph counts_dgraph counts_nebula counts_arangodb engine_plans engine_ops label_scan_keep label_probe_distinct planir probe_counts linkbench_blob_bytes linkbench_counts job"
# Per-type CSR's four parts and NebulaGraph's two would be the longest lanes (618 s and 298 s one after another), so
# three of them read a hard-link copy of their store, made here for this run and removed at its end
# (tests/gates/store_copies.py: the same SST files; the parts name the store as before, so the reports are the same)
mapfile -t COPIES < <("$PYTHON" tests/gates/store_copies.py make --sf 0.1 --sets 2 pertype_csr-c64k nebula)
[ "${#COPIES[@]}" = 2 ] || { echo "cpp-diff: the store copies were not made"; exit 1; }
trap '"$PYTHON" tests/gates/store_copies.py remove "${COPIES[@]}"' EXIT
declare -A LANE=([guard]="twin_guard" [pertype]="pertype_rows" [pertype_copy1]="pertype_counts" [pertype_copy2]="edge_exists probe_counts"
                 [nebula]="counts_nebula" [nebula_copy1]="bi_nebula"
                 [dgraph]="bi_dgraph counts_dgraph" [arangodb]="counts_arangodb" [engine]="engine_plans engine_ops"
                 [planir]="planir"
                 [linkbench]="linkbench_blob_bytes linkbench_counts" [job]="job")
AFTER="label_scan_keep label_probe_distinct"   # they open Per-type CSR's and ArangoDB's stores (the second every store): after the lanes
if [ "$LANES" = all ]; then RUN="${!LANE[*]}"; SLICE=""; else RUN=$(echo "$LANES" | tr ',' ' '); SLICE=1; fi
PARTS=$SF01_DIR/cpp-evaluator/parts
source tests/gates/gate_lanes.sh
lanes_run cpp-diff
