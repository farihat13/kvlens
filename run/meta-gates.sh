#!/usr/bin/env bash
# run/meta-gates.sh -- the configurable meta format's per-step gates on ONE binary, provenance first:
#   0. the binary's git sha, dirty flag and whether the sha is on a remote branch; a dirty binary is refused unless
#      --allow-dirty (a control or a work-in-progress check: its results then do not count as a gate)
#      A binary whose C++ (cpp/kvevaluator/, docs aside) differs from HEAD's is refused unless --allow-stale-cpp <name>
#      (tests/gates/check_binary_cpp_current.py; the inner just gates apply the same rule)
#   0a. the store root's lock, <store root>/sf<SF>/.gate.lock (run/gate-lock.sh): a second round on its stores is refused
#      at once (exit 3), not partway through on a store's LOCK
#   0b. the store preflight (tests/gates/meta/check_round_stores.py): every spec store the round reads is what this binary
#      writes for its spec, else ALL stale ones are listed and the round stops before its first stream (a missing one
#      passes: the round builds it)
#   0b2. the store prep: `just gate-prep <sf>` ingests every missing store of the gates, so a fresh tree needs no hand
#      step; a stale one is named and refused. Seconds when nothing is missing
#   1. ctest                                            2. the corners (run/meta-vs-formats.sh)
#   3. just cpp-diff                                    4. just write-gate <sf>
#   5. check_write_effects.py: the tested binary's pass once, then one pass per known-bad control in --controls (each
#      must be caught)
#   6. check_far_labels.py (+ --control)                7. check_symmetry.py
#   8. check_chunk_landings.py --control (LDBC, microbench; adjacency walks, then --props: Chunked props reads)
#   9. check_label_consumers.py --control (LDBC, microbench)
#  10. the per-direction mixes (inputs/specs/mixes_per_direction: an edge type's out and in lists in two
#      layouts): check_far_labels.py (+ --control), check_chunk_landings.py --control on their Chunked families, and
#      check_mix_families.py (each (edge type, direction) family == the uniform cell of its layout, key by key, with
#      its control). Gates 5 and 7 hold them as cells too (their SPEC_DIRS)
#  11. the composition proof's assumptions (the paper's "Choosing the best KV Format"),
#      on a capture this round makes itself (kvcost measure --fresh into the round's logs dir: the nine cells, the
#      built mixes, the per-direction mixes; LDBC with held-out and writes, and the microbench):
#      check_row_order.py --control (the same rows in the same order on every store), composed_vs_measured.py (the
#      composed cost == the built store's, row by row, and the same plan; its three planted differences must each exit
#      1), check_plan_locality.py, check_three_cells_compose_six.py (+ --plant), check_mix_vs_built.py (also
#      --by-direction),
#      check_mix_families.py on the built mixes; and check_row_order_after_write.py (+ --control, and on the
#      known-bad binary $ROW_ORDER_CONTROL, which must be caught)
#  13. check_bi_plans_run.py: every BI plan body runs, one param, on every store it is meant for (the four named formats
#      its variants' `for` names, the nine cells where the grid runs it); any step error fails
#  14. check_cpp_vs_python.py --families bi --format arangodb --nparam 1: ArangoDB's BI counts against its Python oracle
#  15. check_captured_bodies_row_sha.py: a run row's own body_sha wins over its name resolved at the capture's tree
#      (candidates.captured_bodies; 7 cases on HEAD's BI plan files, no store), in stream z
#  16. check_latency_fresh.py's offline part (LATENCY_FRESH_OFFLINE=1): a writes capture stamped before the write
#      counting version is stale, a reads capture stays fresh (no store), in stream z
#  17. check_store_config.py: a tiny timed capture per timed regime (grid_latency's flags) runs with the settings
#      inputs/contract/store_config.json decides; its --plant (the old cold flags, twice the steady cache,
#      a 16 KB-block store) and the known-bad binaries $STORE_CONFIG_CONTROL (iterator readahead off) and
#      $STORE_FILL_CONTROL (reads do not fill the cache) must each be caught; in stream z, on its own store copy
#  19. compose's chunk-size options on the paper's SF3 captures (paper-results, no store):
#      check_compose_unit_sizes.py (a unit or size it cannot price is refused), check_compose_missing_part.py (a missing
#      part row is refused, never priced as 0), check_chunk_size_body_sha.py (9 cases, synthetic rows); in stream z
#  20. on the round's own capture, in stream k: check_measure_only_anchor.py (a measure-only engine first fit moves the
#      pick, on the held-out, in-sample and composed paths) and check_kvcost_uniform_identity.py (+ --control: the
#      views library's uniform rows == compose.py on the nine cells), check_query_candidates.py (+ --control: what
#      compose.query_candidates returns is what query_pick picks from), check_kvcost_controls.py (six planted defects caught:
#      the composer, the pricing, cost-layouts --mix) and check_kvcost_mix_from_diagonal.py (cost-layouts --mix from the
#      three diagonal cells, the six composed == from the nine)
#  21. just lint (stream z, no store): every lint and its plants; check_layers among them (every package imports only its
#      layer and below, kvevaluator's units keep the axes apart, the hand-naming ratchet)
#  21b. check_example_workload.py (stream z, no store): the README's example workload is predicted on the nine layouts and
#      ranked, as its commands say
#  22. check_statistics_rebuild.py (stream z, no store): LDBC's committed statistics at this scale equal a rebuild from its
#      record stream (python/kvevaluator/datasets/ldbc/statistics/sf<SF>.json)
#  23. run/predict-gate.sh (stream p): predict mode's exact gate, each KV step priced from its true cardinalities equal to
#      the run's counts on every lane, and its planted wrong rules each caught; on its own store copies
# Each gate prints its own counts (cells / steps compared); this script prints rc per gate and a summary line.
# The gates run in thirteen streams side by side, but for g, then m, then k: each of those reads the one before (the list below, at "The gates"); zt runs alone after all of them; the logs print stream by stream at the end.
# A ROUND IS THIS ONE COMMAND: it already runs `just cpp-diff` and `just write-gate`, so never run those two beside it
# on the same binary (that costs 20-35 min per round).
#   Usage:  run/meta-gates.sh [--sf 0.1] --controls DIR [--allow-dirty] [--allow-stale-cpp NAME] [--only x,v,w,y,y2,y3,y4,c,z,g,m,k,p,zt] [--serial]
#                               [--ignore-memory] [--ignore-disk] [--preflight]
# --preflight runs the round's refusals only (the store root's lock, the binary, memory, disk, stale stores) and exits 0
# before the store prep; the controls are not checked (run/gate-full.sh builds them later). Prints META_GATES_PREFLIGHT_OK.
# --only runs some streams (a chain that ran the others itself; a check while editing): a PARTIAL result, never a round.
# Stream m reads g's capture and k reads both: without g, --capture DIR names an earlier round's capture
# (its logs dir + /capture); m then writes into it.
# Three streams run only when --only names them (the gate's analysis-code level, run/gate-full.sh): zn (stream z's gates
# that open no store), ps (p's `predict sizes`) and d (kvcost measure --check-only and kvcost compose on --capture DIR: the check and
# compose steps of g's and m's six captures, on a COPY of an earlier full round's capture; not with g or m). Stream z
# runs zn's gates and p runs ps's, so a full round runs each once. --controls is needed only with z, w or y2.
# The round prints "=== capture dir: DIR" (the capture g and m made, or --capture's): a full gate records it.
# --serial runs the streams one after another (a machine that also runs a latency capture).
# Memory: side by side, cpp-diff and write-gate each hold about a dozen Python processes with the sf0.1 graph and one
# oracle store (2.1 GB each, measured); a full round took about 40 GB at its peak. The round prints the
# available memory at its start and is REFUSED below what its streams need (50 GB with x and v, 30 GB with one of them,
# 10 GB otherwise; --serial: 30 GB; 5 GB more with x, its JOB lane); --ignore-memory runs it anyway.
# Disk: the round prints the free space of every filesystem it writes to and is REFUSED below what its streams need
# there (16 GB for a full sf0.1 round on one disk; the numbers and their source are at the check); --ignore-disk runs
# it anyway. --pretend-free-gb N tests the refusal: the check then takes N GB as free on every filesystem.
# The binary is KVEVAL_CPP_BIN, else the build dir's; the two must report the same sha. ctest runs the tests of the build
# that made the binary (its build cache entry, run/build_cpp.py --entry; the build dir for a build in place). --controls DIR holds one binary per patch in experiments/meta/controls/, named by the
# patch stem (e.g. DIR/write_effects_dangling_view): the tested sha + that patch (controls/README.md builds them).
# A control reads its stores' schema from the tree it was built in: the round refuses one whose tree is gone.
# A gate result counts only if the binary is clean and its sha is a pushed commit.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"

ALL="z x v w y y2 y3 y4 c p g m k zt"   # every stream, once: the default, the --only check and the round-or-partial verdict read it
NAMED_ONLY="zn ps yc d"   # streams run only when --only names them (their gates are inside z, p, y, and g and m)
SF=0.1; CONTROLS=""; ALLOW_DIRTY=0; ALLOW_STALE_CPP=""; ONLY="$ALL"; SERIAL=0; CAPTURE=""; IGNORE_MEMORY=0; IGNORE_DISK=0; PRETEND_FREE_GB=""; PREFLIGHT=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)          SF="${2:?--sf needs a value}"; shift 2 ;;
    --controls)    CONTROLS="$(realpath "${2:?--controls needs a directory}")"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --allow-stale-cpp) ALLOW_STALE_CPP="${2:?--allow-stale-cpp needs a control name}"; shift 2 ;;
    --only)        ONLY="$(echo "${2:?--only needs a list}" | tr ',' ' ')"; shift 2 ;;
    --serial)      SERIAL=1; shift ;;
    --capture)     CAPTURE="$(realpath "${2:?--capture needs a directory}")"; shift 2 ;;
    --ignore-memory) IGNORE_MEMORY=1; shift ;;
    --ignore-disk) IGNORE_DISK=1; shift ;;
    --pretend-free-gb) PRETEND_FREE_GB="${2:?--pretend-free-gb needs a number}"; shift 2 ;;
    --preflight)   PREFLIGHT=1; shift ;;
    -h|--help)     sed -n '2,/^set -uo/p' "$0" | sed '$d'; exit 0 ;;
    *)             echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
CONTROL_PATCHES=(write_effects_dangling_view write_effects_far_label_zero write_effects_empty_list_kept)
ROW_ORDER_CONTROL=row_order_condensed_reversed   # the row-order gate's known-bad binary (also built by run/meta-build-controls.sh)
STORE_CONFIG_CONTROL=store_readahead_off   # the store-config gate's known-bad binary (also built by run/meta-build-controls.sh)
STORE_FILL_CONTROL=store_fill_cache_off   # its second known-bad binary (also built by run/meta-build-controls.sh)
for st in $ONLY; do [[ " $ALL $NAMED_ONLY " == *" $st "* ]] || { echo "--only: no stream '$st' (streams: ${ALL// /, }, ${NAMED_ONLY// /, })" >&2; exit 2; }; done
NEEDS_CONTROLS=0   # the streams that run a known-bad binary: z (store config), w (write effects), y2 (row order)
for st in z w y2; do [[ " $ONLY " == *" $st "* ]] && NEEDS_CONTROLS=1; done
[[ -n "$CONTROLS" || $PREFLIGHT -eq 1 || $NEEDS_CONTROLS -eq 0 ]] || { echo "--controls DIR is required (one binary per patch: ${CONTROL_PATCHES[*]})" >&2; exit 2; }
# one round per store root: a second round started beside this one is refused at once (run/gate-lock.sh)
source "$(dirname "${BASH_SOURCE[0]}")/gate-lock.sh"
gate_lock "$SF" meta-gates

BIN="$CPP_BIN"                   # KVEVAL_CPP_BIN (env or .env), else the build dir's
prov() {   # "<sha> <dirty>" from a binary's --version
  "$1" --version | "$PYTHON" -c 'import json,sys; v=json.load(sys.stdin); print(v["binary_git_sha"], str(v["binary_dirty"]).lower())'
}
read -r SHA DIRTY < <(prov "$BIN")
read -r BSHA BDIRTY < <(prov "$BUILD_DIR/cpp/kvevaluator")
PUSHED=$(git branch -r --contains "$SHA" 2>/dev/null | tr -d ' ' | paste -sd, -)
echo "=== binary: $BIN"
echo "    sha $SHA  dirty $DIRTY  on remote: ${PUSHED:-NONE}"
CTEST_DIR=$("$PYTHON" run/build_cpp.py --entry "$BIN") || { echo "REFUSED: no build dir for $BIN's unit tests"; exit 3; }
echo "    ctest build: sha $BSHA  dirty $BDIRTY  in $CTEST_DIR"
[[ "$SHA" == "$BSHA" && "$DIRTY" == "$BDIRTY" ]] || { echo "REFUSED: KVEVAL_CPP_BIN and $BUILD_DIR/cpp differ: ctest would test another binary"; exit 3; }
if [[ "$DIRTY" != false ]]; then
  [[ $ALLOW_DIRTY -eq 1 ]] || { echo "REFUSED: a dirty binary (--allow-dirty runs it anyway; its results do not count as a gate)"; exit 3; }
  echo "    WARNING: dirty binary, run with --allow-dirty: these results do NOT count as a gate"
  export KVEVAL_ALLOW_DIRTY="${KVEVAL_ALLOW_DIRTY:-meta-gates --allow-dirty}"   # the inner gates read the same control
fi
[[ -n "$PUSHED" ]] || echo "    NOTE: $SHA is on no remote branch yet: the results count once it is pushed"
# the binary must have HEAD's C++ (binary.binary_cpp_verdict); --allow-stale-cpp NAME runs it as a named control, and
# the inner gates (just cpp-diff, just write-gate: gate_preflight.py) read the same name from the environment
[[ -z "$ALLOW_STALE_CPP" ]] || export KVEVAL_ALLOW_STALE_CPP="$ALLOW_STALE_CPP"
"$PYTHON" tests/gates/check_binary_cpp_current.py --binary "$BIN" | sed 's/^/    /'
[[ ${PIPESTATUS[0]} -eq 0 ]] || { echo "REFUSED: the binary's C++ is not HEAD's (above)"; exit 3; }
for c in "${CONTROL_PATCHES[@]}" "$ROW_ORDER_CONTROL" "$STORE_CONFIG_CONTROL" "$STORE_FILL_CONTROL"; do
  [[ $PREFLIGHT -eq 0 && $NEEDS_CONTROLS -eq 1 ]] || break   # --preflight: the controls are built after it
  [[ -x "$CONTROLS/$c" ]] || { echo "REFUSED: no control binary $CONTROLS/$c"; exit 3; }
  read -r CSHA CDIRTY < <(prov "$CONTROLS/$c")
  echo "    control $c: sha $CSHA dirty $CDIRTY$([[ "$CSHA" == "$SHA" ]] || echo "  (NOT the tested sha)")"
done
# a control resolves its stores' schema from the tree it was built in: that tree must still exist (10-20)
if [[ $PREFLIGHT -eq 0 && $NEEDS_CONTROLS -eq 1 ]]; then
  "$PYTHON" tests/gates/check_control_tree.py "${CONTROL_PATCHES[@]/#/$CONTROLS/}" "$CONTROLS/$ROW_ORDER_CONTROL" "$CONTROLS/$STORE_CONFIG_CONTROL" "$CONTROLS/$STORE_FILL_CONTROL" | sed 's/^/    /'
  [[ ${PIPESTATUS[0]} -eq 0 ]] || { echo "REFUSED: a control binary's build tree is gone or differs (above)"; exit 3; }
fi
export KVEVAL_CPP_BIN="$BIN"

# The gates, in streams that share no store, side by side: the round's wall time is the longest stream's (g, then m,
# then k count as one: their times add up along that chain).
#   x   the named formats' stores: cpp-diff; its label-probe part opens every sf<SF> store, so it reads a copy of
#       each configurable-format store (meta-*), which the streams below own
#   v   write-gate, then the corners' writes checks, on hard-link COPIES of those stores made here for this round
#       (tests/gates/store_copies.py: the same SST files; a store has one opener at a time, and cpp-diff holds
#       the originals)
#   y   the configurable format's cell stores: far-labels, symmetry
#   y2  chunk-landings and the per-direction mixes' checks; y3, y4  label-consumers (LDBC, microbench): each on its own
#       set of copies of the cell stores (the cells' stores are ingested first if missing, several at a time)
#   w   write-effects: every store it opens is ingested fresh into its own dir
#   c   the corners' LDBC checks: their stores are their own (output/checks/meta-vs-formats/ldbc/sf<SF>/)
#   z   ctest (all but test_latency_stability: stream zt), caps-parity, the record checks; step-record, and the BI plans-run smoke then ArangoDB's BI vs its oracle,
#       each on its own copies
#   g, m   the round's own capture, on one more set of copies (two disjoint groups of stores): g the nine cells;
#       then m the built mixes and the per-direction mixes
#   k   after g and m: the composition checks on that capture (they open no store, but for check_mix_families.py,
#       which reads the same copies once the captures have ended)
#   p   predict mode's exact gate (run/predict-gate.sh): the uniform cells and its fixture cells, on copies of their own
#   zt  test_latency_stability, alone, once every other stream has ended: it times a 59 KB copy fresh against after a
#       600 MB allocation history on one pinned CPU, and the streams' first minutes (run queue ~30, ~60k context
#       switches/s) pushed its median over the 10% bound in 3 of 19 runs beside a round (0 of 56 after them, 0 of 40
#       under 7 to 64 CPU spinners). -V keeps its ratios in the log on a pass too
# THIS IS THE ONLY LIST of the meta gates: a chain that needs a part of it calls --only, it does not copy the lines.
CHAIN_TAG=META_GATES; CHAIN_LOGS=$("$PYTHON" -m kvevaluator.output_tree logs --name meta-gates --name "$(date -u +%Y%m%dT%H%M%S)") || exit 1
CHAIN_KEEP_GOING=1    # the gates do not depend on each other: every one runs and reports, the verdict is taken at the end
CHAIN_BUDGET_S=1800   # a full round's target on a quiet machine; above it the summary says OVER BUDGET (a warning)
source run/chain-stages.sh
exec 3>&1
gate() {   # gate [-v REGEX | -c FLOOR REGEX] <name> <command...>: one stage (run/chain-stages.sh); its rc line also goes to the live output
  stage "$@" | sed "s/^=== /    ended (stream $STREAM): /" >&3
}
stream_zt() {
  # the verdict: rc 0 and the test's own last line with 4+ checks and 0 fails; --no-tests=error: a renamed test fails, not passes empty
  gate -v '\[test_latency_stability\] checks=([4-9]|[1-9][0-9]+) fails=0$' "latency stability" \
    "$PYTHON" run/build_cpp.py --ctest "$BIN" -- -R '^test_latency_stability$' --no-tests=error -V
  # the kvcost walkthrough (python/kvcost/cli/README.md's worked example, LDBC sf0.1) gives its answers, writes only to the tree's
  # kinds; after the streams, since its check-mix builds and opens stores in the shared output tree
  if [[ "$SF" == "0.1" ]]; then gate -v '^check_walkthrough OK' "kvcost walkthrough" "$PYTHON" tests/gates/check_walkthrough.py; fi
}
stream_z() {
  gate "ctest" "$PYTHON" run/build_cpp.py --ctest "$BIN" -- -E '^test_latency_stability$'
  gate "caps-parity" "$PYTHON" tests/unit/check_caps_parity.py   # each format's Python caps == the binary's (also inside cpp-diff)
  gate "lint" env KVEVAL_LINT_TRACKED_ONLY=1 just lint   # the commit's files, not the tree's scratch; every lint and its plants (check_layers among them: the layers, kvevaluator's units, the hand-naming ratchet); no store
  # LDBC's committed statistics are what build_stats makes from this scale's record stream (no store)
  gate -v 'PASS$' "statistics rebuild" "$PYTHON" tests/gates/check_statistics_rebuild.py --sf "$SF"
  stream_zn
  z_stores
}
stream_zn() {   # z's gates that open no store and need no binary of their own (the analysis-code level runs these alone)
  gate "contract-files" "$PYTHON" tests/unit/check_contract_files.py   # the shared data files (inputs/contract) both sides read
  # the twin-count guard's part f (who reads a counting store's .cost: it scans python/kvcost and experiments/); the
  # whole guard runs in cpp-diff and write-gate, which the analysis-code level drops
  gate -v '0 not listed$' "twin count readers" "$PYTHON" tests/gates/check_twin_count_guard.py --readers-only
  # the README's example workload (inputs/workloads/README.md, "Add your own workload"): predicted and ranked, no store
  gate -v '^example workload PASS' "example workload" "$PYTHON" tests/gates/check_example_workload.py --sf "$SF"
  gate -v '^PLAN OPS OK' "plan ops supported" "$PYTHON" tests/unit/check_plan_ops_supported.py   # every selected body's ops run on its format (no store)
  gate "record carry-over" "$PYTHON" tests/gates/meta/check_record_carry_over.py   # grid_pick_record: a rewrite at sf3 keeps the small scales' entries
  gate "record single-body" "$PYTHON" tests/gates/meta/check_record_single_body.py   # a one-body query (LinkBench) needs no pick; a multi-body one missing from the record raises
  gate -v '^[0-9]+ cases, 0 failed$' "captured-bodies row sha" "$PYTHON" tests/unit/check_captured_bodies_row_sha.py   # a row's own body_sha wins over its name at the capture's tree (9 cases, no store)
  # a cell id is read by one parser (kvcost.plotting.cell_ids): a size on either part, the old names kept, a non-cell refused
  gate -v '^CELL NAMES OK' "cell names" "$PYTHON" tests/unit/check_cell_names.py
  gate -v '^CONTROL CAUGHT' "cell names control" "$PYTHON" tests/unit/check_cell_names.py --control
  # a layout's or mix's stored size from a frozen tally library, the dollars and the SLO choice (synthetic tallies, no store)
  gate -v '^[0-9]+ cases, 0 failed$' "stored size" "$PYTHON" tests/unit/check_stored_size.py
  gate -v 'control caught: PASS$' "stored size control" "$PYTHON" tests/unit/check_stored_size.py --control
  LATENCY_FRESH_OFFLINE=1 gate -v '^LATENCY FRESH OFFLINE OK$' "latency fresh offline" "$PYTHON" tests/gates/check_latency_fresh.py   # a writes capture before the write counting version is stale, reads stay fresh (no store)
  # compose's chunk-size options, on the paper's SF3 captures (paper-results, no store): an unpriceable unit or size is
  # refused, a missing part row is refused (never a silent 0), a sized capture's rows carry their body sha. The sized
  # Chunked cells sit in the grid's own place
  local sgrid; sgrid="$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale 3 --root "$("$PYTHON" -m kvevaluator.paths paper_results_dir)")"
  gate -v '^RESULT: PASS' "compose unit sizes" "$PYTHON" tests/gates/check_compose_unit_sizes.py --grid "$sgrid" \
    --size 1k --size 4k
  gate -v '^RESULT: PASS' "compose missing part" "$PYTHON" tests/gates/check_compose_missing_part.py --grid "$sgrid" \
    --size 1k --query ic2
  gate -c 9 '^[0-9]+ cases, 0 failed$' "chunk size body sha" "$PYTHON" tests/unit/check_chunk_size_body_sha.py
}
z_stores() {   # z's gates on its own store copies and the known-bad binaries
  # the step record's free columns: histogram entries = operations and buckets bracket the lanes, P=1 = P=8, injections caught
  # (on its own hard-link copies of its three stores: streams x and y hold the originals)
  KVEVAL_STORE_COPIES="$Z_COPIES" gate -v '^STEP RECORD OK' "step-record" "$PYTHON" tests/gates/check_step_record.py --sf "$SF"
  # the timed servers' NUMA binding, on the Chunked/Chunked copy (n/a on a 1-node machine or with balancing off)
  KVEVAL_STORE_COPIES="$Z_COPIES" gate "numa binding" "$PYTHON" tests/gates/check_numa_binding.py
  # every BI plan body runs on every store it is meant for, one param (bi10 had no label; ArangoDB refused it)
  KVEVAL_STORE_COPIES="$B_COPIES" gate -v '^\[bi-plans-run\] .* -> OK$' "bi plans run" "$PYTHON" tests/gates/check_bi_plans_run.py --sf "$SF"
  # ArangoDB's BI counts against its Python oracle, one param (cpp-diff's BI lanes are nebula and dgraph, so
  # an ArangoDB disagreement would go unseen), on the same copies after the smoke
  KVEVAL_STORE_COPIES="$B_COPIES" gate -v '^[0-9]+ ok, 0 mismatch' "arangodb BI vs oracle" "$PYTHON" tests/gates/check_cpp_vs_python.py \
    --sf "$SF" --families bi --format arangodb --store arangodb --pack --skip-bytes --nparam 1
  # a timed run records the settings inputs/contract/store_config.json decides (readahead at RocksDB's default, cold and steady direct I/O,
  # no per-call timers, 4 KB blocks, reads fill the cache, the steady cache at the SF's rule; a steady write capture too);
  # the plants (the old cold flags, twice the cache, a 16 KB-block store, a write timed warm, a hand-given write cache) and the readahead-off and no-fill binaries must each be caught (on the
  # step-record copies)
  KVEVAL_STORE_COPIES="$Z_COPIES" gate -v '^PASS: every timed regime' "store config" "$PYTHON" tests/gates/check_store_config.py --sf "$SF"
  KVEVAL_STORE_COPIES="$Z_COPIES" gate -v '^CONTROL PLANT: caught' "store config plant" "$PYTHON" tests/gates/check_store_config.py --sf "$SF" --plant
  KVEVAL_STORE_COPIES="$Z_COPIES" KVEVAL_CPP_BIN="$CONTROLS/$STORE_CONFIG_CONTROL" gate -v '^CONTROL BINARY: caught' "store config control binary" \
    "$PYTHON" tests/gates/check_store_config.py --sf "$SF" --expect-fail
  KVEVAL_STORE_COPIES="$Z_COPIES" KVEVAL_CPP_BIN="$CONTROLS/$STORE_FILL_CONTROL" gate -v '^CONTROL BINARY: caught' "store config control binary (fill cache)" \
    "$PYTHON" tests/gates/check_store_config.py --sf "$SF" --expect-fail fill_cache
}
stream_x() {
  KVEVAL_STORE_COPIES="$X_COPIES" gate -v '^cpp-diff: [0-9]+ parts ran, 0 failed or not run' "cpp-diff" just cpp-diff
}
stream_v() {
  export KVEVAL_STORE_COPIES="$V_COPIES"
  gate -v '^WRITE GATE GREEN' "write-gate" just write-gate "$SF"
  gate "corners writes" run/meta-vs-formats.sh --sf "$SF" --families writes
}
stream_w() {
  # the tested binary's pass once, then one pass per known-bad control (each must be caught); the stores side by side
  # caught = each control's own signature (CONTROL_SIGNATURES); a crash or another failure is CONTROL VOID
  gate -v "^\[write-effects\] controls: ${#CONTROL_PATCHES[@]} of ${#CONTROL_PATCHES[@]} caught by their signature" \
    "write-effects (${#CONTROL_PATCHES[@]} controls)" "$PYTHON" tests/gates/meta/check_write_effects.py --sf "$SF" --binary "$BIN" \
    "${CONTROL_ARGS[@]}"
}
stream_y() {
  gate "far-labels" "$PYTHON" tests/gates/meta/check_far_labels.py --sf "$SF"
  gate "far-labels control" "$PYTHON" tests/gates/meta/check_far_labels.py --sf "$SF" --control
  gate "symmetry" "$PYTHON" tests/gates/meta/check_symmetry.py --sf "$SF"
  # capture reuse: an old-form store beside its old capture is not fresh (reads two cell manifests; opens no store)
  gate "stale store not reused" "$PYTHON" tests/gates/meta/check_stale_store_not_reused.py --sf "$SF"
  # capture reuse: a plan-file edit that moves no body and no pick keeps a capture; a moved body, let or pick does not
  gate -v '^GRID BODY KEY OK' "grid body key" "$PYTHON" tests/gates/meta/check_grid_body_key.py --sf "$SF"
  # a capture whose bodies split on the answer is refused (a planted body's rows cut by one), one that agrees is written
  gate -v '^check_answer_vote: OK' "answer vote" "$PYTHON" tests/gates/meta/check_answer_vote.py --sf "$SF"
  # the SF30 rule: which bodies run is recomputed from the SF3 capture on every run; each run is reused by its own key
  gate -v '^check_sf30_reuse: OK' "sf30 reuse" "$PYTHON" tests/gates/meta/check_sf30_reuse.py
  stream_yc
  # a cell killed mid-run resumes from its journal: finished runs kept, none twice, a not-run row now admitted runs
  gate -v '^check_journal_resume: OK' "journal resume" "$PYTHON" tests/gates/meta/check_journal_resume.py
  # the grid measures a body on a named store exactly where the generator calls it legal (caps + the format's ops)
  gate -v 'OK$' "grid legality" "$PYTHON" tests/gates/meta/check_grid_legality.py
  # one answer to "is this store usable" (stores.store_staleness) in every caller; a held LOCK is busy, not stale
  gate -v '^STORE STALENESS OK' "store staleness" "$PYTHON" tests/gates/check_store_staleness.py --sf "$SF"
  # a half-built store (an ingest stopped midway) is moved aside and rebuilt, never reused; a complete one is reused
  gate -v '^HALF-BUILT OK' "half-built store" "$PYTHON" tests/gates/check_half_built_store.py --sf "$SF"
  # a store grid_bodies cannot open (held by another process) fails the cell: rc 1, no capture to reuse
  gate -v '^check_store_error_fails_cell: OK' "store error fails the cell" "$PYTHON" tests/gates/meta/check_store_error_fails_cell.py --sf "$SF"
  # the writes captures' key: what a write cell runs and the param rows it runs, not plan or param files
  gate -v '^WRITES BODY KEY OK' "writes body key" "$PYTHON" tests/gates/meta/check_writes_body_key.py --sf "$SF"
}
stream_yc() {   # y's chooser gate alone: it runs python/kvcost/layout_cost/grid_pick_record.py, which imports kvcost (the analysis-code level runs it)
  # the chooser on a named store (grid_pick_record --formats): a carried entry is no anchor, a decided one keeps the margin
  KVEVAL_STORE_COPIES="$C_COPIES" gate -v '^check_chooser_named: OK' "chooser on a named store" "$PYTHON" tests/gates/meta/check_chooser_named.py
}
stream_y2() {
  export KVEVAL_STORE_COPIES="${Y_COPIES[0]}"
  gate "chunk-landings interactive" "$PYTHON" tests/gates/meta/check_chunk_landings.py --sf "$SF" --control   # its default queries
  gate "chunk-landings microbench" "$PYTHON" tests/gates/meta/check_chunk_landings.py --sf "$SF" --control --family microbench \
    --queries chain,chain3,closure   # the microbench's walks (its closure tells the closure rules apart at sf0.1)
  for wl in interactive microbench; do
    gate "chunk-landings props $wl" "$PYTHON" tests/gates/meta/check_chunk_landings.py --sf "$SF" --control --props --family "$wl"
  done
  gate "far-labels per-direction" "$PYTHON" tests/gates/meta/check_far_labels.py --sf "$SF" --spec-dir "$PD"
  gate "far-labels per-direction control" "$PYTHON" tests/gates/meta/check_far_labels.py --sf "$SF" --spec-dir "$PD" --control
  gate "chunk-landings per-direction" "$PYTHON" tests/gates/meta/check_chunk_landings.py --sf "$SF" --control --spec-dir "$PD" \
    --cells w16_b2_per_direction_20261010,knows_out_chunked_in_condensed,hastag_out_chunked_in_split --queries ic5,ic12,ic9,is2,is6,ic1,is3,ic6
  gate "mix-families per-direction" "$PYTHON" tests/gates/layout_choice/check_mix_families.py --sf "$SF" --spec-dir "$PD"
  # rows and their order after a write inside one plan, on every cell store; then the known-bad binary, which must be caught
  gate "row-order after a write" "$PYTHON" tests/gates/meta/check_row_order_after_write.py --sf "$SF" --control
  gate -v 'CONTROL BINARY: caught by its signature' \
    "row-order after a write: the control binary is caught" env KVEVAL_CPP_BIN="$CONTROLS/$ROW_ORDER_CONTROL" \
    "$PYTHON" tests/gates/meta/check_row_order_after_write.py --sf "$SF" --expect-fail
}
stream_y3() {
  export KVEVAL_STORE_COPIES="${Y_COPIES[1]}"
  gate "label-consumers interactive" "$PYTHON" tests/gates/meta/check_label_consumers.py --sf "$SF" --family interactive --control
}
stream_y4() {
  export KVEVAL_STORE_COPIES="${Y_COPIES[2]}"
  gate "label-consumers microbench" "$PYTHON" tests/gates/meta/check_label_consumers.py --sf "$SF" --family microbench --control
}
stream_p() {   # predict mode's exact gate, on the copies of its stores made before the streams start
  export KVEVAL_STORE_COPIES="$P_COPIES"
  gate -v '^predict-gate: DONE' "predict gate" run/predict-gate.sh --sf "$SF"
  stream_ps
}
stream_ps() {   # p's gate on kvcost alone (no store)
  gate "predict sizes" "$PYTHON" tests/gates/check_predict_sizes.py   # kvcost predict on Ch4k-Ch4k beside the nine, and the any-size search (no store)
  gate "predicted mix" "$PYTHON" tests/gates/check_predicted_mix.py   # a mix composed from predicted part rows equals it predicted whole (no store)
}
stream_c() {
  gate "corners interactive" run/meta-vs-formats.sh --sf "$SF" --families interactive
}
capture() {   # capture <gate name> <kvcost measure flags...>: one spec set's fresh capture into the round's own root
  local name="$1"; shift
  gate -v '^==> \[kvcost measure\] DONE' "$name" in_tree "$CAPTURE" env KVEVAL_STORE_COPIES="$K_COPIES" \
    "$PYTHON" -m kvcost.cli.cli measure --dataset "ldbc=$SF" --fresh "$@"
}
compose() {   # compose <gate name>: the layouts not measured, composed from the round's capture (--check: the nine measured)
  gate -v '^==> \[kvcost compose\] .* (written|unchanged|replaced)' "$1" \
    in_tree "$CAPTURE" "$PYTHON" -m kvcost.cli.cli compose --dataset "ldbc=$SF"
}
NINE=(Sp-Sp Sp-Co Sp-Ch64k Co-Sp Co-Co Co-Ch64k Ch64k-Sp Ch64k-Co Ch64k-Ch64k)   # the grid's nine layouts at 64 KB, each measured
stream_g() {
  capture "capture: the nine cells, interactive" --workload i01 "${NINE[@]}"   # nine measured: the composition checks below need them
  capture "capture: the nine cells, microbench" --family microbench "${NINE[@]}"
  compose "compose: the nine cells (checked against the measured six)"
}
stream_m() {
  capture "capture: mixes, interactive" --workload i01 "$MIXES"
  capture "capture: mixes, microbench" --family microbench "$MIXES"
  # the per-direction mixes have no row in the pick record (never built at SF3): the grid check picks them on their own
  # capture and marks them (pick_in_sample); composed_vs_measured.py below decides both sides by this capture's held-out rule
  capture "capture: mixes_per_direction, interactive" --workload i01 "$PD"
  capture "capture: mixes_per_direction, microbench" --family microbench "$PD"
}
derive() {   # derive <gate name> <kvcost measure flags...>: the grid check of one capture, on --capture's copy
  local name="$1"; shift
  gate -v '^==> \[kvcost measure\] DONE' "$name" in_tree "$CAPTURE" "$PYTHON" -m kvcost.cli.cli measure \
    --dataset "ldbc=$SF" --check-only "$@"
}
stream_d() {   # g's and m's captures, their measured rows reused: only what kvcost makes of them is made again
  derive "derive: the nine cells, interactive" --workload i01 "${NINE[@]}"
  derive "derive: the nine cells, microbench" --family microbench "${NINE[@]}"
  compose "derive: compose the nine cells"
  derive "derive: mixes, interactive" --workload i01 "$MIXES"
  derive "derive: mixes, microbench" --family microbench "$MIXES"
  derive "derive: mixes_per_direction, interactive" --workload i01 "$PD"
  derive "derive: mixes_per_direction, microbench" --family microbench "$PD"
}
must_exit_1() {   # a planted difference: the check must fail with its own exit code 1 (not 0, and not a crash)
  local rc=0; "$@" || rc=$?
  echo "[must exit 1] rc=$rc"; [[ $rc -eq 1 ]]
}
stream_k() {
  local cvm=("$PYTHON" experiments/mix_benefit/composed_vs_measured.py --sf "$SF" --grid "$GRID_ROOT"
             --held-out-rule-for-unrecorded) out="$CHAIN_LOGS/composition"
  export KVEVAL_STORE_COPIES="$K_COPIES"
  gate "row-order" "$PYTHON" tests/gates/meta/check_row_order.py --sf "$SF" --root "$GRID_ROOT" --control
  gate "composed == measured" "${cvm[@]}" --out "$out/composed-vs-measured"
  for plant in count pick one-sided; do
    gate "composed == measured: a planted $plant difference is caught" must_exit_1 "${cvm[@]}" --out "$out/plant-$plant" --plant "$plant"
  done
  gate "plan locality" "$PYTHON" tests/gates/mix_benefit/check_plan_locality.py --picks "$out/composed-vs-measured/picks.csv"
  gate "three cells compose six" "$PYTHON" tests/gates/mix_benefit/check_three_cells_compose_six.py --grid "$GRID_ROOT" --out "$out/three-cells"
  gate "three cells compose six: a planted difference is caught" must_exit_1 "$PYTHON" tests/gates/mix_benefit/check_three_cells_compose_six.py \
    --grid "$GRID_ROOT" --out "$out/three-cells-plant" --plant
  # the saved mix cost cache (search.set_cache): loaded = built fresh, a changed cost or source misses (the nine cells; no store)
  gate -c 8 '^[0-9]+ cases, 0 failed$' "mix cost cache" "$PYTHON" tests/gates/layout_choice/check_cost_cache.py --sf "$SF" --grid "$GRID_ROOT" \
    --cache "$out/mix-cost-cache" --jobs 8
  gate "mix cost cache: a key blind to the query's data serves stale costs" must_exit_1 "$PYTHON" \
    tests/gates/layout_choice/check_cost_cache.py --sf "$SF" --grid "$GRID_ROOT" --cache "$out/mix-cost-cache-control" --jobs 8 --control-no-data-key
  gate "mix cost cache: a key blind to the pricing settings serves another setting's costs" must_exit_1 "$PYTHON" \
    tests/gates/layout_choice/check_cost_cache.py --sf "$SF" --grid "$GRID_ROOT" --cache "$out/mix-cost-cache-control-pricing" --jobs 8 --control-no-composer-key
  gate "mix == composed (check_mix_vs_built)" "$PYTHON" tests/gates/check_mix_vs_built.py --sf "$SF" --grid "$GRID_ROOT"
  # the same stores priced on per-direction units: a per type row of the pick record must bind to them (below the
  # deciding scale; the control switches that binding off and must then differ)
  gate "mix == composed, on per-direction units" "$PYTHON" tests/gates/check_mix_vs_built.py --sf "$SF" --grid "$GRID_ROOT" --by-direction
  gate "mix-families mixes" "$PYTHON" tests/gates/layout_choice/check_mix_families.py --sf "$SF" --spec-dir "$MIXES" --no-direction-control
  # a measure-only engine first fit (planted) moves the pick on all three paths; kvcost's uniform views == compose.py
  gate -v '^measure-only anchor control: PASS$' "measure-only anchor" "$PYTHON" tests/unit/check_measure_only_anchor.py --sf "$SF" --grid "$GRID_ROOT"
  gate -v '^PASS$' "kvcost uniform identity" "$PYTHON" tests/gates/check_kvcost_uniform_identity.py --dataset ldbc --sf "$SF" --root "$GRID_ROOT"
  gate -v '^control caught: PASS$' "kvcost uniform identity control" "$PYTHON" tests/gates/check_kvcost_uniform_identity.py \
    --dataset ldbc --sf "$SF" --root "$GRID_ROOT" --control
  # compose.query_candidates hands out what query_pick picks from (its control: an inflated held-out vector is caught)
  gate -v '^PASS$' "compose query candidates" "$PYTHON" tests/unit/check_query_candidates.py --sf "$SF" --grid "$GRID_ROOT"
  gate -v '^control caught: PASS$' "compose query candidates control" "$PYTHON" tests/unit/check_query_candidates.py \
    --sf "$SF" --grid "$GRID_ROOT" --control
  # kvcost's negative controls (six planted defects, each must be caught) and a mix priced from the three diagonal cells
  # == the same mix from the nine measured cells (its controls: a missing diagonal cell refused, a planted mark kept)
  gate -v ': PASS$' "kvcost controls" "$PYTHON" tests/gates/check_kvcost_controls.py --sf "$SF" --grid "$GRID_ROOT"
  gate -v '^PASS$' "the diagonal's mix price (kvcost cost-layouts --mix)" "$PYTHON" tests/gates/check_kvcost_mix_from_diagonal.py --dataset ldbc --sf "$SF" \
    --mix "knows lists=Ch64k" --base Co-Sp --grid "$GRID_ROOT"
  # the layout search on this capture (no store): the one mix cost cache builder, the search without it, search-mix's edge
  # paths, and the vectorised fill against the scalar one (one option set: 33 s; both take 268 s)
  gate "mix cost cache builder" "$PYTHON" tests/gates/check_cost_cache_builder.py --grid "$GRID_ROOT" --sf "$SF" --workload w01,i01
  gate "no cost cache == cost cache" "$PYTHON" tests/gates/check_no_cost_cache_equals_cost_cache.py --dataset ldbc --sf "$SF" \
    --from "$GRID_ROOT" --workload i01
  gate "mix search edges" "$PYTHON" tests/gates/check_mix_search_edges.py --sf "$SF" --from "$GRID_ROOT"
  gate "mix cost cache fill" "$PYTHON" tests/gates/check_cost_cache_fill.py --grid "$GRID_ROOT" --sf "$SF" --option-sets "per type"
}
CONTROL_ARGS=(); for c in "${CONTROL_PATCHES[@]}"; do CONTROL_ARGS+=(--control "$CONTROLS/$c"); done
PD=inputs/specs/mixes_per_direction
MIXES=inputs/specs/mixes
K_DIRS=(--spec-dir inputs/specs/grid --spec-dir "$MIXES" --spec-dir "$PD")
if [[ " $ONLY " == *" d "* && ( " $ONLY " == *" g "* || " $ONLY " == *" m "* ) ]]; then
  echo "stream d derives an earlier round's capture: not with g or m, which make one" >&2; exit 2
fi
if [[ " $ONLY " == *" g "* ]]; then
  [[ -z "$CAPTURE" ]] || { echo "--capture names an existing capture: not with stream g, which makes one" >&2; exit 2; }
  CAPTURE="$(realpath -m "$CHAIN_LOGS")/capture"   # CHAIN_LOGS is absolute when the output dir is outside the repo
elif [[ " $ONLY " == *" m "* || " $ONLY " == *" k "* || " $ONLY " == *" d "* ]]; then
  [[ -d "$CAPTURE" ]] || { echo "the streams m, d, k without g need --capture DIR (the capture of an earlier round)" >&2; exit 2; }
fi
[[ -z "$CAPTURE" ]] || echo "=== capture dir: $CAPTURE"
# the round's measured layouts, as the readers take them: <capture>/captures/ldbc/sf<SF>
[[ -z "$CAPTURE" ]] || GRID_ROOT=$("$PYTHON" -m kvevaluator.output_tree captures-dir --dataset ldbc --scale "$SF" --root "$CAPTURE") || exit 1
CELL_DIRS=(--spec-dir inputs/specs/grid --spec-dir inputs/specs/grid_chunked_own --spec-dir "$PD")
wants() { [[ " $ONLY " == *" $1 "* ]]; }
echo "=== streams: ${ONLY// /, }$([[ $SERIAL -eq 1 ]] && echo ' (one after another: --serial)')"
chain_begin "$BIN"
# memory: what the streams started side by side need, against what is available now
NEED_GB=10
if wants x || wants v; then NEED_GB=30; fi
if wants x && wants v && [[ $SERIAL -eq 0 ]]; then NEED_GB=50; fi
if wants x; then NEED_GB=$((NEED_GB + 5)); fi   # cpp-diff's job lane: one Python twin of JOB's sample at a time, 4.5 GB peak
AVAIL_GB=$(awk '/^MemAvailable:/ {printf "%d", $2 / 1048576}' /proc/meminfo)
echo "=== memory: $AVAIL_GB GB available, these streams need about $NEED_GB GB at their peak"
if [[ $AVAIL_GB -lt $NEED_GB ]]; then
  [[ $IGNORE_MEMORY -eq 1 ]] || { echo "REFUSED: $AVAIL_GB GB available, about $NEED_GB GB needed (--serial or --only need less; --ignore-memory runs it anyway)"; exit 3; }
  echo "    WARNING: below the need, run with --ignore-memory"
fi
# disk: what the streams write, summed per filesystem (the stores, the write-effects scratch and the corners' output
# may be on different disks), against what is free there now. Measured at sf0.1, rounded up:
#   5 GB  the stores of the named formats and the cells (du of output/stores/ldbc/sf0.1 after a full round: 4.9 GB,
#         51 stores; the round's copies are hard links); counted in full, as if no store were ingested yet
#   7 GB  stream w's scratch, removed at its end (its peak, du every 5 s over one run with 3 controls: 6.2 GB)
#   2 GB  the corners' own stores and output, streams c and v (du of output/checks/meta-vs-formats/ldbc/sf0.1: 1.3 GB)
declare -A DISK_NEED
disk_need() {   # disk_need <GB> <dir>: add to the need of the filesystem that holds <dir> (or its nearest existing parent)
  local d m; d=$(realpath -m "$2"); while [[ ! -e "$d" ]]; do d=$(dirname "$d"); done
  m=$(df --output=target "$d" | tail -1)
  DISK_NEED[$m]=$(( ${DISK_NEED[$m]:-0} + $1 ))
}
STORE_ROOT=$("$PYTHON" -c "from kvevaluator.paths import store_dir; print(store_dir())") || exit 1   # every store lands here (KVEVAL_STORE_DIR)
if wants z || wants x || wants v || wants y || wants y2 || wants y3 || wants y4 || wants yc || wants p; then disk_need 5 "$STORE_ROOT"; fi
#   2 GB  the stores of the built mixes and the round's capture (0.1 GB), streams g, m, k
if wants g || wants m || wants k; then disk_need 2 "$STORE_ROOT"; fi
if wants w; then disk_need 7 "$("$PYTHON" -m kvevaluator.output_tree root)"; fi
#  12 GB  JOB's data (<data>/datasets/imdb, or KVEVAL_IMDB_DIR: the dump, the full tag the sample is cut from, the sample; du 11.6 GB on
#         and 3 GB its three sample stores (du 2.4 GB with the record stream), stream x's job lane; counted in full
if wants x; then disk_need 12 "$("$PYTHON" -c 'from kvevaluator.paths import imdb_dir; print(imdb_dir())')"; disk_need 3 "$STORE_ROOT"; fi
SF_DIR=$("$PYTHON" -m kvevaluator.output_tree summary --dataset ldbc --scale "$SF") || exit 1   # the LDBC studies' dir at this scale
if wants c || wants v; then disk_need 2 "$SF_DIR"; fi
DISK_SHORT=0
for m in $(printf '%s\n' "${!DISK_NEED[@]}" | sort); do
  FREE_GB=${PRETEND_FREE_GB:-$(df -BG --output=avail "$m" | tail -1 | tr -dc '0-9')}
  echo "=== disk: $FREE_GB GB free on $m, this round needs about ${DISK_NEED[$m]} GB$([[ -z "$PRETEND_FREE_GB" ]] || echo ' (--pretend-free-gb: not the real free space)')"
  [[ $FREE_GB -ge ${DISK_NEED[$m]} ]] || DISK_SHORT=1
done
if [[ $DISK_SHORT -eq 1 ]]; then
  [[ $IGNORE_DISK -eq 1 ]] || { echo "REFUSED: a filesystem above has less free space than this round needs (--only needs less; --ignore-disk runs it anyway)"; exit 3; }
  echo "    WARNING: below the need, run with --ignore-disk"
fi
# Every spec store the round reads, against what this binary writes for its spec: ALL stale ones named at once, here,
# not one gate at a time (five stale stores cost three rounds). Manifests only: no store is opened.
"$PYTHON" tests/gates/meta/check_round_stores.py --sf "$SF" "${CELL_DIRS[@]}" "${K_DIRS[@]}" --corners \
  || { echo "REFUSED: stale stores (listed above): move each aside, the round builds it again"; exit 3; }
if [[ $PREFLIGHT -eq 1 ]]; then echo "META_GATES_PREFLIGHT_OK"; exit 0; fi
# The stores a fresh tree lacks, made here instead of refusing minutes later: every missing gate store
# (gate_preflight.py --gate all --prep). A stale store is refused by name, never re-ingested here.
if wants x || wants v || wants z; then
  mkdir -p "$CHAIN_LOGS"
  t0=$SECONDS
  just gate-prep "$SF" > "$CHAIN_LOGS/gate-prep.log" 2>&1
  rc=$?
  n=$(grep -c '^\[gate-prep\] ingesting' "$CHAIN_LOGS/gate-prep.log")
  echo "=== prep: just gate-prep $SF: $n stores ingested, $((SECONDS - t0)) s (rc=$rc; $CHAIN_LOGS/gate-prep.log)"
  [[ $rc -eq 0 ]] || { grep -E 'STALE|INCOMPLETE|binary' "$CHAIN_LOGS/gate-prep.log"; echo "REFUSED: the store prep failed (above; a stale store: move it aside, the round builds it again)"; exit 3; }
fi
# The store copies of this round, made before any stream opens a store (a copy needs its store's lock for a moment)
# and removed on every exit.
V_COPIES=""; Y_COPIES=(); Z_COPIES=""; K_COPIES=""; B_COPIES=""; C_COPIES=""; P_COPIES=""; X_COPIES=""
trap '[[ -z "$V_COPIES$Z_COPIES$K_COPIES$B_COPIES$C_COPIES$P_COPIES$X_COPIES${Y_COPIES[*]:-}" ]] || "$PYTHON" tests/gates/store_copies.py remove $V_COPIES $Z_COPIES $K_COPIES $B_COPIES $C_COPIES $P_COPIES $X_COPIES "${Y_COPIES[@]}"' EXIT
if wants z; then   # the step-record gate's stores (check_step_record.py STORES): two cells and Per-type CSR; the store-config gate's
  "$PYTHON" -m kvevaluator.measure.ensure_stores --sf "$SF" --jobs 2 --cells props_chunked_adj_chunked,props_split_adj_split | tail -1 > /dev/null || true
  Z_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $("$PYTHON" tests/gates/check_step_record.py --names) $("$PYTHON" tests/gates/check_store_config.py --names)) \
    || { echo "REFUSED: the store copies for the step-record gate were not made (an ingest error above names its fix; with none, just gate-prep $SF)"; exit 3; }
  # the BI plans-run smoke's stores (check_bi_plans_run.py --names): the four named formats and the nine cells
  "$PYTHON" -m kvevaluator.measure.ensure_stores --sf "$SF" --jobs 6 --spec-dir inputs/specs/grid | tail -1 > /dev/null || true
  B_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $("$PYTHON" tests/gates/check_bi_plans_run.py --sf "$SF" --names)) \
    || { echo "REFUSED: the store copies for the BI plans-run smoke were not made (just gate-prep $SF)"; exit 3; }
fi
if wants v; then
  V_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $("$PYTHON" tests/gates/gate_preflight.py --gate write-gate --sf "$SF" --names)) \
    || { echo "REFUSED: the store copies for stream v were not made (just gate-prep $SF)"; exit 3; }
fi
if wants y || wants y2 || wants y3 || wants y4; then
  # the cells' stores: built here if missing (several at a time), so no two streams ingest one store
  "$PYTHON" -m kvevaluator.measure.ensure_stores --sf "$SF" --jobs 6 "${CELL_DIRS[@]}" | tail -1 \
    || { echo "REFUSED: a cell store could not be ingested"; exit 3; }
  mapfile -t Y_COPIES < <("$PYTHON" tests/gates/store_copies.py make --sf "$SF" --sets 3 \
    $("$PYTHON" -m kvevaluator.measure.ensure_stores --sf "$SF" --names "${CELL_DIRS[@]}"))
  [[ ${#Y_COPIES[@]} -eq 3 ]] || { echo "REFUSED: the copies of the cell stores were not made"; exit 3; }
fi
if wants y || wants yc; then
  # the chooser gate's named store (check_chooser_named.py --names), which other streams open too
  C_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $("$PYTHON" tests/gates/meta/check_chooser_named.py --names)) \
    || { echo "REFUSED: the store copy for the chooser gate was not made (just gate-prep $SF)"; exit 3; }
fi
if wants p; then   # predict-gate's stores (its --stores: the uniform cells and its fixture cells), built if missing, copied
  mkdir -p "$CHAIN_LOGS"
  P_NAMES=$(run/predict-gate.sh --sf "$SF" --stores 2> "$CHAIN_LOGS/predict-gate-stores.log") \
    || { echo "REFUSED: predict-gate's stores could not be built ($CHAIN_LOGS/predict-gate-stores.log)"; exit 3; }
  P_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $P_NAMES) \
    || { echo "REFUSED: the store copies for stream p were not made"; exit 3; }
fi
if wants g || wants m || wants k; then
  # the capture's stores (the nine cells, the mixes): built here if missing, then one set of copies
  "$PYTHON" -m kvevaluator.measure.ensure_stores --sf "$SF" --jobs 6 "${K_DIRS[@]}" | tail -1 \
    || { echo "REFUSED: a store of the capture could not be ingested"; exit 3; }
  K_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $("$PYTHON" -m kvevaluator.measure.ensure_stores --sf "$SF" --names "${K_DIRS[@]}")) \
    || { echo "REFUSED: the copies of the capture's stores were not made"; exit 3; }
fi
if wants x; then   # last, so every store the round built is listed: the meta-* stores cpp-diff's label-probe part opens
  X_NAMES=$("$PYTHON" tests/gates/check_label_probe_distinct.py --sf "$SF" --names) \
    || { echo "REFUSED: the label-probe check could not list its stores"; exit 3; }
  X_NAMES=$(tr ' ' '\n' <<< "$X_NAMES" | grep '^meta-' | tr '\n' ' ')
  [[ -z "$X_NAMES" ]] || X_COPIES=$("$PYTHON" tests/gates/store_copies.py make --sf "$SF" $X_NAMES) \
    || { echo "REFUSED: the copies of the meta stores for stream x were not made"; exit 3; }
fi
# the gates this round must run: the streams' own gate calls, listed by a dry pass (run/chain-stages.sh CHAIN_EXPECT). A
# gate with no row at the end, or a stream whose shell ended with a non-zero rc, fails the round.
export CHAIN_EXPECT="$CHAIN_LOGS/expected-gates.txt"
for st in $ALL $NAMED_ONLY; do
  wants "$st" || continue
  ( CHAIN_DRY=1 STREAM=$st "stream_$st" ) || { echo "REFUSED: the gates of stream $st could not be listed"; exit 3; }
done
echo "=== gates expected: $(wc -l < "$CHAIN_EXPECT")"
declare -A PID STREAM_RC
run_stream() { ( STREAM=$1 CHAIN_STAGES="$CHAIN_LOGS/stages-$1.tsv" "stream_$1" ); }
composition_chain() {   # g, then m (or d), then k: each reads the one before; rc = the worst of the streams' own
  local worst=0 rc
  if wants g; then rc=0; run_stream g || rc=$?; [[ $rc -eq 0 ]] || worst=$rc; fi
  if wants m; then rc=0; run_stream m || rc=$?; [[ $rc -eq 0 ]] || worst=$rc; fi
  if wants d; then rc=0; run_stream d || rc=$?; [[ $rc -eq 0 ]] || worst=$rc; fi
  if wants k; then rc=0; run_stream k || rc=$?; [[ $rc -eq 0 ]] || worst=$rc; fi
  return "$worst"
}
for st in z x v w y y2 y3 y4 c p zn ps yc; do
  wants "$st" || continue
  if [[ $SERIAL -eq 1 ]]; then
    rc=0; ( STREAM=$st CHAIN_STAGES="$CHAIN_LOGS/stages-$st.tsv" "stream_$st" ) || rc=$?
    STREAM_RC[$st]=$rc
  else ( STREAM=$st CHAIN_STAGES="$CHAIN_LOGS/stages-$st.tsv" "stream_$st" ) & PID[$st]=$!; fi
done
if wants g || wants m || wants d || wants k; then   # the chain's rc is reported on its last stream's row
  if [[ $SERIAL -eq 1 ]]; then rc=0; composition_chain || rc=$?; STREAM_RC[k]=$rc
  else composition_chain & PID[k]=$!; fi
fi
for st in "${!PID[@]}"; do rc=0; wait "${PID[$st]}" || rc=$?; STREAM_RC[$st]=$rc; done
if wants zt; then rc=0; run_stream zt || rc=$?; STREAM_RC[zt]=$rc; fi   # alone: its timing must not share the machine with the streams
# every gate's own output, stream by stream, then the table (name, rc, seconds), the budget line and the verdict
FILES=()
for st in $ALL $NAMED_ONLY; do
  wants "$st" && [[ -f "$CHAIN_LOGS/stages-$st.tsv" ]] || continue
  FILES+=("$CHAIN_LOGS/stages-$st.tsv")
  while IFS=$'\t' read -r name rc secs log; do
    echo "=== $name"; cat "$log"; echo "=== rc=$rc $name (${secs}s)"
  done < "$CHAIN_LOGS/stages-$st.tsv"
done
for st in $ALL $NAMED_ONLY; do   # a stream that ended badly is a failing row of its own (its gates' rows may all be green)
  [[ "${STREAM_RC[$st]:-0}" == 0 ]] && continue
  src=${STREAM_RC[$st]}; [[ $st != k ]] || st="g, m, d, k (one chain)"
  printf 'stream %s ended\t%s\t0\t-\n' "$st" "$src" >> "$CHAIN_LOGS/stages-streams.tsv"
  echo "=== stream $st ended with rc=$src"
done
[[ ! -f "$CHAIN_LOGS/stages-streams.tsv" ]] || FILES+=("$CHAIN_LOGS/stages-streams.tsv")
# no two openers of one store: a RocksDB LOCK refusal in any gate's log fails the round and names the log, also where
# the gate itself went on (a skip, a retry) or passed. The streams above share no store; this shows it held
mapfile -t SCANNED < <(find "$CHAIN_LOGS" -name '*.log' -type f | sort)
mapfile -t LOCKED < <(grep -l 'While lock file' "${SCANNED[@]}" /dev/null)
if [[ ${#SCANNED[@]} -eq 0 || ${#LOCKED[@]} -gt 0 ]]; then lrc=1; else lrc=0; fi
printf 'no store LOCK held by another opener (%s logs scanned, %s with a LOCK refusal)\t%s\t0\t-\n' \
  "${#SCANNED[@]}" "${#LOCKED[@]}" "$lrc" > "$CHAIN_LOGS/stages-locks.tsv"
echo "=== store locks: ${#SCANNED[@]} logs scanned, ${#LOCKED[@]} with a LOCK refusal (\"While lock file\")"
for f in "${LOCKED[@]}"; do echo "    LOCK REFUSED in $f: $(grep -m1 -o 'open [^:]*' "$f")"; done
FILES+=("$CHAIN_LOGS/stages-locks.tsv")
echo "=== summary (sf$SF, binary $SHA dirty $DIRTY, on remote: ${PUSHED:-NONE})"
if [[ "$ONLY" != "$ALL" ]]; then
  CHAIN_BUDGET_S="" CHAIN_TAG=META_GATES_PARTIAL chain_end "${FILES[@]}"; rc=$?
  echo "    PARTIAL: only the streams ${ONLY// /, } ran (--only): not a full round"
else
  chain_end "${FILES[@]}"; rc=$?
fi
exit $rc
