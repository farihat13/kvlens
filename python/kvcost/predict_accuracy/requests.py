"""The requests predict mode prices (`kvevaluator predict --plans`), per layout: every body of each query's plan that is
legal on the layout (pick_record.legal_bodies; measure-only bodies left out), or the body the pick record names
(kvcost predict --plans picked), params 1..N. A layout is a cell name and its spec (<spec dir>/<cell>.json). The binary finds a layout's
tally by the store name its requests carry (`<tally dir>/<store name>.json`); that name is the one a built store of the
spec has, meta-<sha12> (stores.cpp_store_name: the expanded spec's hash), so a layout's tally is
`<tally dir>/meta-<sha12>.json`; one whose spec expands to another is refused. Under <work>: specs/<cell>.json,
requests/<cell>/<cell>__<plan>[@<body>]__<i>.json, and store-names/<dataset>/<scale>/meta-<sha12> (an empty dir the
requests name, never opened). Runs nothing; measure/predict_requests.py writes each request."""
import json
import os

from kvcost.layout_cost import benchmarks
from kvevaluator.measure import stores, predict_requests, workload_plans
from kvevaluator.plan_choice import cells
from kvevaluator.datasets import schema_path
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice import pick_record

GRID_SPECS = ROOT / "inputs" / "specs" / "grid"


def check_tally(path, spec_path, schema_json):
    """refuse a tally made for another layout than the spec (its spec expands to another)"""
    with open(path) as f:
        have = json.load(f)["spec"]
    with open(spec_path) as f:
        want = json.load(f)
    if have != want and stores.canonical_meta_spec(have, schema_json) != stores.canonical_meta_spec(want, schema_json):
        raise SystemExit(f"predict requests: {path} is the tally of another spec than {spec_path}: refused")


def legal_queries(workload, spec_path, queries):
    """[plan or plan@body]: every legal body of each plan on the layout whose spec this is"""
    caps, of_label = cells.store_caps(workload, spec_path)
    out = []
    for q in queries:
        ir = workload_plans.ir_for(workload, q)
        if "plans" not in ir:
            out.append(q)
            continue
        legal = pick_record.legal_bodies(ir, caps, of_label)
        if not legal:
            raise SystemExit(f"predict requests: {q} has no legal body on {spec_path}")
        out += [f"{q}@{b}" for b in legal]
    return out


def left_out(workload, spec_path, queries):
    """{plan: {body: why}}: the bodies of each plan legal_queries does not send on the layout (a measured capture runs
    every candidate, so a predicted record names these for the composer's drift check: picks.choose_cell)"""
    caps, of_label = cells.store_caps(workload, spec_path)
    out = {}
    for q in queries:
        ir = workload_plans.ir_for(workload, q)
        if "plans" not in ir:
            continue
        legal = set(pick_record.legal_bodies(ir, caps, of_label))
        meta = ir.get("bodies") or {}
        why = {b: "measure-only" if meta.get(b, {}).get("measure_only") else "not legal on the layout"
               for b in ir["plans"] if b not in legal}
        if why:
            out[q] = dict(sorted(why.items()))
    return out


_WRITE = {}     # write()'s arguments: what _write_cell reads (set before a fork)


def _write_cell(c):
    """one layout's spec, store name and requests under write()'s <work>: (its request dir, the (query, body) pairs
    sent, the requests written). In a forked process of write() or in its own"""
    w = _WRITE
    specs, work, dataset = w["specs"], w["work"], w["dataset"]
    spec_dir = os.path.join(work, "specs")
    store_root = os.path.join(work, "store-names")
    src = specs[c] if isinstance(specs, dict) else os.path.join(str(specs), f"{c}.json")
    if not os.path.exists(src):
        raise SystemExit(f"predict requests: no spec {src}")
    spec_path = os.path.join(spec_dir, f"{c}.json")
    with open(src) as f:
        spec = json.load(f)
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=1, sort_keys=True)
    name = stores.meta_store_name(spec_path, dataset)     # the dataset's store name (against its schema)
    t = os.path.join(w["tally_dir"], f"{name}.json")
    if not os.path.exists(t):
        raise SystemExit(f"predict requests: no tally {t} for {c} (python -m kvevaluator.predict.tally_library, "
                         f"or kvevaluator tally)")
    check_tally(t, spec_path, w["schema_json"])
    os.makedirs(os.path.join(stores.scale_root(dataset, w["sf"], store_root), name), exist_ok=True)
    req = os.path.join(work, "requests", c)
    os.makedirs(req, exist_ok=True)
    for old in os.listdir(req):
        os.remove(os.path.join(req, old))
    if w["bodies"] == "all":
        qs, picks = legal_queries(w["workload"], spec_path, w["queries"]), []
    else:
        qs, picks = list(w["queries"]), ["--picks"]
    # writes: the measured writes capture's param rows (grid_writes), so a predicted write matches it row for row
    rows = ["--write-params", "capture"] if benchmarks.kind_of(w["workload"]) == "write" else []
    if w["param_set"]:
        rows += ["--param-set", w["param_set"]]
    predict_requests.main([*picks, *rows, "--family", w["workload"], "--sf", w["sf"], "--cells", c, "--queries",
                           ",".join(qs), "--nparam", str(w["nparam"]), "--spec-dirs", spec_dir, "--store-root",
                           store_root, "--out", req])
    written = os.listdir(req)
    for f in written:   # the binary keys the tally on this name
        store = os.path.basename(json.load(open(os.path.join(req, f)))["store"]["dir"])
        if store != name:
            raise SystemExit(f"predict requests: {f} names store {store}, not {name}: its tally would not be found")
    return req, len(qs), len(written)


def write(sf, workload, cells, queries, nparam, tally_dir, work, bodies="all", specs=GRID_SPECS, schema_json=None,
          param_set=None, dataset=None, jobs=1):
    """{cell: its request dir}; each cell's dir holds only this call's requests; `specs`: a dir of <cell>.json, or
    {cell: spec file}; `param_set`: the params they bind (None: the workload's default set); `dataset`: whose stores
    the requests name (None: the workload's own; an empty dir per store under <work>/store-names: predict opens none);
    `jobs`: the layouts written at once, a process each (the same files)"""
    from kvevaluator import workloads
    from kvevaluator.measure.run_measure import at_a_time
    dataset = dataset or workloads.get(workload).dataset
    os.makedirs(os.path.join(work, "specs"), exist_ok=True)
    _WRITE.update(sf=sf, workload=workload, queries=queries, nparam=nparam, tally_dir=tally_dir, work=work, bodies=bodies,
                  specs=specs, schema_json=schema_json or schema_path(dataset), param_set=param_set, dataset=dataset)
    done = at_a_time(_write_cell, list(cells), jobs)
    out = {}
    for c, (req, pairs, written) in zip(cells, done):
        out[c] = req
        print(f"[kvcost predict] {c}: {pairs} (query, body) pairs, {written} requests", flush=True)
    return out
