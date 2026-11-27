#!/usr/bin/env bash
# run/write-gate.sh -- `just write-gate` (the round, run/meta-gates.sh, calls it). Args: [sf, default 0.1] [lanes, default all].
set -euo pipefail
case "${1:-}" in -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0;; esac   # before env.sh changes dir
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"
SF="${1:-0.1}"
LANES="${2:-all}"
# GREEN means every part ran: the binary and every store a part opens are checked first, and a missing one
# fails the recipe with the command that makes it (a part that covers 0 cells is not a pass; `just gate-prep`)
"$PYTHON" tests/gates/gate_preflight.py --gate write-gate --sf $SF
# a store manifest's schema path resolves in this tree (relative, or another tree's copy rebased; ~1 s, no store)
"$PYTHON" tests/unit/check_manifest_schema_path.py
# a check's last line on success, its WHOLE stdout on failure (one line per check once hid four of five FAIL lines)
gate() { local out rc=0; out=$("$@") || rc=$?; if [ "$rc" -eq 0 ]; then printf '%s\n' "$out" | tail -1; else printf '%s\n' "$out"; fi; return "$rc"; }
echo "=== 0/7 no Python measurement on a result path (the retirement ratchet); no plan reads a type from a vid"
"$PYTHON" tests/lint/check_no_python_measurement.py
"$PYTHON" tests/lint/check_no_type_from_id.py
# every body the selector gives a store uses only ops its format runs (the binary's `caps --ops`; ~11 s, no store)
gate "$PYTHON" tests/unit/check_plan_ops_supported.py
# The parts run in LANES (tests/gates/gate_lanes.sh): the parts that open no C++ store each in a lane of their
# own (plans, suite, effects, model, selftest); the cross-language effects one lane per named format. The write-count
# oracle opens stores of several lanes, so it runs after the lanes. The output prints in the fixed order below,
# whichever lane ends first; every lane reports, also when another one failed.
# a Python twin's counts are refused where they are not its format's; 104 s on the Python oracle, so a lane of its own
part_twin_guard() {
  out=$("$PYTHON" tests/gates/check_twin_count_guard.py 2>&1) || { printf '%s\n' "$out" | grep -v '^\[ldbc\]'; exit 1; }; printf '%s\n' "$out" | tail -1
}
part_plans() {
  echo "=== 1/7 write-plan IR round-trip + drift guard"
  gate "$PYTHON" tests/unit/check_writes_planir.py
  gate "$PYTHON" tests/unit/check_plan_files.py
  gate "$PYTHON" tests/unit/check_optimizer_rules.py
}
part_suite() {
  echo "=== 2/7 the write suite on the Python oracle store: the committed IR on 4 formats;"
  echo "          predicted == actual per step, formats agree"
  gate "$PYTHON" tests/gates/check_write_queries.py --sf $SF
}
part_effects() {
  echo "=== 3/7 execution-effect checks (store contents, incl. shared-posting survival)"
  gate "$PYTHON" tests/gates/check_write_effects.py
}
part_model() {
  echo "=== 4/7 mb2 four-laws model check (engine formats)"
  gate "$PYTHON" tests/unit/check_write_model.py --sf $SF
}
part_cpp_dgraph() {
  echo "=== 5/7 cross-language write effects (every store the preflight checked)"
  gate "$PYTHON" tests/gates/check_cpp_write_effects.py --sf $SF
}
cpp_effects() { gate "$PYTHON" tests/gates/check_cpp_write_effects.py --sf $SF --format "$1" | sed "s/^/  $1: /"; }
part_cpp_nebula() { cpp_effects nebula; }; part_cpp_arangodb() { cpp_effects arangodb; }; part_cpp_pertype_csr() { cpp_effects pertype_csr; }
declare -A LANE
part_counts() {
  echo "=== 6/7 write-count ORACLE (apply-mode RocksDB tickers == the workers' counts; shadow == apply)"
  gate "$PYTHON" tests/gates/check_cpp_write_counts.py --sf $SF
}
# dgraph's chunk knob (dgraph-c64k, the fairness arm): the same oracle on the chunked store
part_counts_dgraph_c64k() {
  KVEVAL_DGRAPH_CHUNK=65536 gate "$PYTHON" tests/gates/check_cpp_write_counts.py --sf $SF --formats dgraph | sed 's/^/  dgraph-c64k: /'
}
# printed last, so the parts above report first; the only recipe that runs selftest
part_selftest() {
  echo "=== 7/7 op semantics per format on the hand-built demo graph (selftest: read + write ops, packing models)"
  gate "$PYTHON" tests/unit/selftest.py
}
ORDER="twin_guard plans suite effects model cpp_dgraph cpp_nebula cpp_arangodb cpp_pertype_csr counts counts_dgraph_c64k selftest"
LANE+=([guard]="twin_guard" [plans]="plans" [suite]="suite" [effects]="effects" [model]="model" [selftest]="selftest" [dgraph]="cpp_dgraph"
       [nebula]="cpp_nebula" [arangodb]="cpp_arangodb" [pertype_csr]="cpp_pertype_csr")
AFTER="counts counts_dgraph_c64k"
if [ "$LANES" = all ]; then RUN="${!LANE[*]}"; SLICE=""; else RUN=$(echo "$LANES" | tr ',' ' '); SLICE=1; fi
SF_DIR=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF") || exit 1   # the LDBC studies' dir at this scale
PARTS=$SF_DIR/write-gate/parts
source tests/gates/gate_lanes.sh
lanes_run write-gate
if [ -z "$SLICE" ]; then echo "WRITE GATE GREEN"; fi
