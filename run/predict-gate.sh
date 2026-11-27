#!/usr/bin/env bash
# run/predict-gate.sh -- predict mode's exact gate for the configurable format meta: each KV
# step of a traced run, priced from its TRUTH card (its cardinalities: the run's
# arguments + raw store reads), must equal that run's counts on every lane (S G N P D, the misses, get / scan key and
# value, the miss keys, used key and value, write key and value). Then each planted wrong rule must fail it.
# A read workload (--workload, default LDBC Interactive) gives the dataset, the queries, their plans and params (the
# workload registry, python/kvevaluator/workloads; predict_requests --gate-lists prints what this run takes, at the
# top of the log); its manifest's predict_gate block gives the bodies the picks never run, the untagged control, the
# edge check and the rules no request reaches. The cells: the 49's seven (kvevaluator.layouts.SEVEN).
# Writes: the dataset's write workload (LDBC: writes/ldbc, writes/microbench; the write-count gate's params), run in
# shadow; a write step's card reads the store merged with what its own rules wrote at the request's earlier steps.
#   1. the cells' stores, and the fixture cells' (python/kvevaluator/measure/ensure_stores.py, built when missing, --jobs
#      at a time): the rules the uniform cells never reach (Condensed split lists, a hop over two layouts, props in row
#      groups, small chunks; python/kvevaluator/measure/predict_requests.py --write-fixture-specs)
#   2. the traced requests -> <out>/requests/ (the cells) and <out>/requests-fixtures/:
#      the fixture cells; on the uniform cells, the plan bodies meta's capabilities never pick (a label scan, label
#      probes; <plan>@<body>), and a control run with label tags off (every label probe reads: existence probes,
#      props over candidate labels)
#      and the pick record's body for every (cell, query) (<out>/requests-picks/, param 1); the write plans on the cells,
#      the fixture cells and one own-placement cell (writes only: the read card refuses edge props in own chunks),
#      params 1-5 (<out>/requests-writes/)
#   3. kvevaluator predict --gate --plants all (every rule the binary can plant) over all of them -> <out>/gate.json and <out>/steps.csv (two rows
#      per KV step, measured and truth, every lane), ONE pass in --jobs threads (one store per thread at a time; the
#      files are the same bytes at any count): each request runs once, and its steps are priced by the gate's rules and
#      by each planted rule. Its verdict is its exit code (0 = no lane differs and every plant caught; 1 = the gate
#      differs; 3 = a refusal; 4 = a plant not caught). It prints the cases each rule priced. A rule with no case, and a
#      read operator no request holds, each print a WARN line; a rule with no case that is not known-untested (the
#      workloads' predict_gate blocks, each with why) fails the gate
#   4. each planted rule must be caught: some lane differs, with every step priced (a refusal is not a catch)
# A round takes <store root>/sf<SF>/.gate.lock first (run/gate-lock.sh): a second round on the same stores is refused at once.
# Output: <output>/summary/<dataset>/<scale>/predict-gate/<workload>/ (gate.json, steps.csv; `just results
# predict-formulas` builds its stores with --stores, and reports the same check on its own steps). A run narrowed by --cells, --queries,
# --nparam, --no-fixtures or --no-writes is a slice: it writes to predict-gate-slice/<workload>/, never over a whole
# gate, and fails by design (rules without a case, plants not caught). Prints the binary (commit, dirty) first; a dirty
# binary is refused unless --allow-dirty (its result is then a work-in-progress check, not a gate: it writes to
# predict-gate-slice/ too). sf0.1: about 1 min
# of ingest the first time, then the gate pass.
#   run/predict-gate.sh                       # IC1-12, IS1-7 on the seven + the fixtures, params 1-2, the writes
#   run/predict-gate.sh --sf 3 --jobs 8       # the paper's scale (about 1 h from no stores)
#   run/predict-gate.sh --workload bi --cells props_split_adj_split --no-fixtures --no-writes --nparam 1 --allow-dirty
# Flags: --sf (default 0.1); --workload W (a read workload, default the registry's: predict_requests.DEFAULT_FAMILY);
# --cells a,b; --queries a,b; --nparam N (default 2); --no-fixtures; --no-writes; --jobs N (stores built at once and
# the pass's threads, default 8); --allow-dirty; --stores (build every store the gate reads when missing, print their
# names one per line, and stop: run/meta-gates.sh copies them for its stream p); -h.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/../run/env.sh"
export LC_COLLATE=C   # the request files' order (the globs below), so the output's row order, is the same on every machine

SF=0.1
WORKLOAD=
CELLS=
QUERIES=
NPARAM=2
SLICE=0     # a narrowing flag makes the run a slice: its own dir
JOBS=
FIXTURES=1
WRITES=1
ALLOW_DIRTY=0
STORES_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sf)          SF="${2:?--sf needs a value}"; shift 2 ;;
    --workload)    WORKLOAD="${2:?--workload needs a read workload}"; shift 2 ;;
    --cells)       CELLS="${2:?--cells needs a comma list}"; SLICE=1; shift 2 ;;
    --queries)     QUERIES="${2:?--queries needs a comma list}"; SLICE=1; shift 2 ;;
    --nparam)      NPARAM="${2:?--nparam needs a value}"; SLICE=1; shift 2 ;;
    --no-fixtures) FIXTURES=0; SLICE=1; shift ;;
    --no-writes)   WRITES=0; SLICE=1; shift ;;
    --jobs)        JOBS="${2:?--jobs needs a count}"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --stores)      STORES_ONLY=1; shift ;;
    -h|--help)     sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "predict-gate: unknown flag $1" >&2; exit 2 ;;
  esac
done
# what this workload's gate runs, each from the place that decides it (predict_requests.gate_lists)
LISTS=$("$PYTHON" -m kvevaluator.measure.predict_requests --gate-lists ${WORKLOAD:+"$WORKLOAD"} --sf "$SF" \
        ${JOBS:+--jobs "$JOBS"}) || exit 2
lst() { sed -n "s/^$1\t//p" <<< "$LISTS"; }
WORKLOAD=$(lst workload)
DATASET=$(lst dataset)
WRITE_FAMILY=$(lst write_workload)
CELLS=${CELLS:-$(lst cells)}
QUERIES=${QUERIES:-$(lst queries)}
SPEC_DIRS=$(lst spec_dirs)             # the dirs that hold the cells' specs
BODIES=$(lst bodies)
UNTAGGED=$(lst untagged)
EDGE_CHECK=$(lst edge_check)
KNOWN_UNTESTED=$(lst known_untested)
OWN_CELL=$(lst own_cell)               # the own-placement cell the write card needs; writes only
OWN_SPECS=$(lst own_spec_dir)
JOBS=$(lst jobs)                       # the stores built at once and the pass's threads (run_measure's rule, or --jobs)
[[ -n "$WRITE_FAMILY" ]] || WRITES=0   # a dataset with no write workload
# one round per store root: a second round started beside this one is refused at once (run/gate-lock.sh)
source "$(dirname "${BASH_SOURCE[0]}")/gate-lock.sh"
if [[ $STORES_ONLY -eq 1 ]]; then gate_lock "$SF" predict-gate >&2; else gate_lock "$SF" predict-gate; fi   # --stores: stdout holds the names only

# the spec dirs as ensure_stores takes them
CELL_DIRS=()
for d in ${SPEC_DIRS//,/ }; do CELL_DIRS+=(--spec-dir "$d"); done
if [[ $STORES_ONLY -eq 1 ]]; then   # the stores this gate reads, with the flags given: built when missing (to stderr), named
  # once each: two cells can name one store (a fixture cell's spec can equal one of the seven's)
  exec > >(awk '!seen[$0]++')
  SPECS=$(mktemp -d)
  trap 'rm -rf "$SPECS"' EXIT
  ens() { "$PYTHON" -m kvevaluator.measure.ensure_stores --dataset "$DATASET" --sf "$SF" "$@" --jobs "$JOBS" >&2 &&
          "$PYTHON" -m kvevaluator.measure.ensure_stores --dataset "$DATASET" --sf "$SF" "$@" --names; }
  ens "${CELL_DIRS[@]}" --cells "$CELLS"
  if [[ $FIXTURES -eq 1 ]]; then
    "$PYTHON" -m kvevaluator.measure.predict_requests --write-fixture-specs "$SPECS/fixtures" > /dev/null
    ens --spec-dir "$SPECS/fixtures"
  fi
  if [[ $WRITES -eq 1 && $FIXTURES -eq 1 ]]; then
    ens --spec-dir "$OWN_SPECS" --cells "$OWN_CELL"
    "$PYTHON" -m kvevaluator.measure.predict_requests --write-write-fixture-specs "$SPECS/fixtures-writes" > /dev/null
    ens --spec-dir "$SPECS/fixtures-writes"
  fi
  exit 0
fi

ver=$("$CPP_BIN" --version)
sha=$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["binary_git_sha"])' <<< "$ver")
dirty=$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["binary_dirty"])' <<< "$ver")
echo "predict-gate: binary $CPP_BIN at $sha dirty=$dirty"
if [[ "$dirty" != "False" && $ALLOW_DIRTY -eq 0 ]]; then
  echo "predict-gate: the binary is dirty; build a clean commit, or --allow-dirty for a work-in-progress check" >&2
  exit 1
fi

GATE_DIR=predict-gate
[[ $SLICE -eq 1 || "$dirty" != "False" ]] && GATE_DIR=predict-gate-slice   # a slice or a dirty binary's run: not a gate
OUT=$("$PYTHON" -m kvevaluator.output_tree summary --dataset "$DATASET" --scale "$SF" --analysis "$GATE_DIR" \
      --subject "$WORKLOAD") || exit 1
mkdir -p "$OUT"
echo "predict-gate: $WORKLOAD on $DATASET at $SF$([[ $GATE_DIR == predict-gate-slice ]] && echo ' (a slice or a dirty binary: not a gate)') -> $OUT"
echo "predict-gate: $(lst jobs_why) (stores built, the pass's threads)"
echo "predict-gate: cells $CELLS"
echo "predict-gate: queries $QUERIES, params 1-$NPARAM; bodies ${BODIES:-none}; untagged ${UNTAGGED:-none}; edge check ${EDGE_CHECK:-none}"
echo "predict-gate: writes ${WRITE_FAMILY:-none}$([[ $WRITES -eq 1 ]] || echo ' (off)'); fixtures $([[ $FIXTURES -eq 1 ]] && echo on || echo off); known untested: ${KNOWN_UNTESTED:-none}"
# this run's files replace the last run's together: none of them is left beside a run that stops early
rm -rf "$OUT/requests" "$OUT/requests-fixtures" "$OUT/requests-fixtures-edge" "$OUT/requests-picks" "$OUT/requests-writes"
rm -f "$OUT/gate.json" "$OUT/gate.err" "$OUT/steps.csv"
t0=$SECONDS
count() { tr ',' '\n' <<< "$1" | grep -c .; }
ENS=("$PYTHON" -m kvevaluator.measure.ensure_stores --dataset "$DATASET" --sf "$SF" --jobs "$JOBS")
REQ=("$PYTHON" -m kvevaluator.measure.predict_requests --family "$WORKLOAD" --sf "$SF")
"${ENS[@]}" "${CELL_DIRS[@]}" --cells "$CELLS"
"${REQ[@]}" --cells "$CELLS" --queries "$QUERIES" --spec-dirs "$SPEC_DIRS" \
  --nparam "$NPARAM" --out "$OUT/requests" --expect $(( $(count "$CELLS") * $(count "$QUERIES") * NPARAM ))
REQS=("$OUT"/requests/*.json)
if [[ $FIXTURES -eq 1 ]]; then
  FCELLS=$("$PYTHON" -m kvevaluator.measure.predict_requests --write-fixture-specs "$OUT/fixture-specs")
  "${ENS[@]}" --spec-dir "$OUT/fixture-specs"
  # a fixture cell has no pick-record entry: every legal body of each query runs (the count is checked inside)
  "${REQ[@]}" --cells "$FCELLS" --queries "$QUERIES" \
    --nparam "$NPARAM" --out "$OUT/requests-fixtures" --spec-dirs "$OUT/fixture-specs"
  if [[ -n "$BODIES" ]]; then
    "${REQ[@]}" --cells "$CELLS" --queries "$BODIES" --spec-dirs "$SPEC_DIRS" \
      --nparam 1 --out "$OUT/requests-fixtures" --expect $(( $(count "$CELLS") * $(count "$BODIES") ))
  fi
  if [[ -n "$UNTAGGED" ]]; then
    "${REQ[@]}" --cells "$CELLS" --queries "$UNTAGGED" --untagged --spec-dirs "$SPEC_DIRS" \
      --nparam 1 --out "$OUT/requests-fixtures" --expect $(( $(count "$CELLS") * $(count "$UNTAGGED") ))
  fi
  if [[ -n "$EDGE_CHECK" ]]; then   # params 1-5 on every cell
    "${REQ[@]}" --cells "$CELLS,$FCELLS" --queries "$EDGE_CHECK" \
      --nparam 5 --out "$OUT/requests-fixtures-edge" --spec-dirs "$SPEC_DIRS,$OUT/fixture-specs"
    REQS+=("$OUT"/requests-fixtures-edge/*.json)
  fi
  # the pick record's body for every (cell, query), where it differs from the capabilities' pick (param 1)
  "${REQ[@]}" --picks --cells "$CELLS" --queries "$QUERIES" --spec-dirs "$SPEC_DIRS" \
    --nparam 1 --out "$OUT/requests-picks"
  REQS+=("$OUT"/requests-picks/*.json)
  REQS+=("$OUT"/requests-fixtures/*.json)
fi
if [[ $WRITES -eq 1 ]]; then
  WCELLS=$CELLS
  if [[ $FIXTURES -eq 1 ]]; then
    "${ENS[@]}" --spec-dir "$OWN_SPECS" --cells "$OWN_CELL"
    # the write-only fixtures (the Per-type CSR corner, with a sorted index)
    WFCELLS=$("$PYTHON" -m kvevaluator.measure.predict_requests --write-write-fixture-specs "$OUT/fixture-specs-writes")
    "${ENS[@]}" --spec-dir "$OUT/fixture-specs-writes"
    WCELLS="$CELLS,$FCELLS,$OWN_CELL,$WFCELLS"
  fi
  # the fixture cells' specs are in the gate's own dirs, not the grid's
  "$PYTHON" -m kvevaluator.measure.predict_requests --family "$WRITE_FAMILY" --sf "$SF" --cells "$WCELLS" --queries all \
    --nparam 5 --out "$OUT/requests-writes" --spec-dirs "$SPEC_DIRS,$OUT/fixture-specs,$OUT/fixture-specs-writes,$OWN_SPECS"
  REQS+=("$OUT"/requests-writes/*.json)
fi
echo "predict-gate: ${#REQS[@]} requests; stores and requests in $((SECONDS - t0)) s"

t1=$SECONDS
set +e
"$CPP_BIN" predict --gate --plants all --threads "$JOBS" --steps-out "$OUT/steps.csv" \
  "${REQS[@]}" > "$OUT/gate.json" 2> "$OUT/gate.err"
rc=$?
set -e
cat "$OUT/gate.err"
echo "predict-gate: gate rc=$rc in $((SECONDS - t1)) s ($OUT/gate.json)"
# rc 4 = the gate passed but a plant was not caught: the rule checks below still run, the plants verdict comes last
[[ $rc -eq 0 || $rc -eq 4 ]] || { echo "predict-gate: FAIL (the exact gate, rc=$rc)" >&2; exit 1; }
"$PYTHON" - "$OUT/gate.json" "$KNOWN_UNTESTED" "${REQS[@]}" <<'EOF'
import json, sys
g = json.load(open(sys.argv[1]))   # the rule list is the binary's (Meta::predict_rules), not this script's
known, reqs = set(sys.argv[2].split()), sys.argv[3:]
n = len(g["untested"]) + len(g["coverage"])
print(f"predict-gate: rules with cases {len(g['coverage'])}/{n}; untested: {', '.join(g['untested']) or 'none'}")
# the operators the requests' plans hold (every body), against the contract's (inputs/contract/kv_ops.json)
from kvevaluator import contract
OPS = [o["name"] for o in contract.kv_ops() if o["kind"] == "read"]
WOPS = [o["name"] for o in contract.kv_ops() if o["kind"] == "write"]
seen = set()
for p in reqs:
    plan = json.load(open(p))["plan"]
    for body in (plan["plans"].values() if "plans" in plan else [plan]):
        seen |= {s.get("op") for s in body["steps"]}
none = [o for o in OPS if o not in seen]
print(f"predict-gate: read operators with requests {len(OPS) - len(none)}/{len(OPS)}; none: {', '.join(none) or 'none'}")
wseen = [o for o in WOPS if o in seen]
if wseen:   # --no-writes sends none
    wnone = [o for o in WOPS if o not in seen]
    print(f"predict-gate: write operators with requests {len(wseen)}/{len(WOPS)}; none: {', '.join(wnone) or 'none'}")
    none += wnone
for r in g["untested"]:
    print(f"predict-gate: WARN untested rule {r}" + ("" if r in known else " (not on the known list)"))
for o in none:
    print(f"predict-gate: WARN no request holds the operator {o}")
gained = sorted(known - set(g["untested"]))
if gained:
    print(f"predict-gate: the known-untested rules {', '.join(gained)} now have cases: take them off the list")
new = sorted(set(g["untested"]) - known)
sys.exit(f"predict-gate: FAIL: untested rules not on the known list: {', '.join(new)}" if new else 0)
EOF

# the plants' verdicts from the same gate.json: every rule the binary can plant (--plants all), each caught
read -r caught planted < <("$PYTHON" - "$OUT/gate.json" <<'EOF'
import json, sys
got = json.load(open(sys.argv[1]))["plants"]
for p in got:
    if not p["caught"]:
        print(f"predict-gate: plant {p['plant']} NOT caught ({p['refused'] or '0 lanes differ'})", file=sys.stderr)
print(sum(p["caught"] for p in got), len(got))
EOF
)
echo "predict-gate: plants caught $caught/$planted"
[[ $planted -gt 0 && $caught -eq $planted && $rc -eq 0 ]] || { echo "predict-gate: FAIL (a planted rule passed, rc=$rc)" >&2; exit 1; }
totals=$("$PYTHON" -c 'import json, sys; g = json.load(open(sys.argv[1]))
print(g["steps_compared"], "KV steps,", g["lanes_compared"], "lanes,", g["lanes_differ"], "differ")' "$OUT/gate.json")
echo "predict-gate: DONE (binary $sha, ${#REQS[@]} requests, $totals, $(( SECONDS - t0 )) s)"
