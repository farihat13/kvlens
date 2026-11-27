"""Write the run requests predict mode's exact gate replays: one per (meta cell, query, param), traced (trace_args
and parts on, P = 1), into --out as <cell>__<query>__<param>.json. --family names whose plans, params and pick-record
section (default interactive: LDBC; also bi, microbench: the same LDBC data). Runs nothing (run/predict-gate.sh runs them).
A query may name one body of its plan bundle as <plan>@<body> (that body runs whatever the store's capabilities would
pick: the gate's fixtures for operators the picked bodies never reach); the file is then <cell>__<plan>@<body>__<i>.json.
A query named alone runs the body the pick record names for the cell; on a cell with no entry (a fixture cell) every
legal body runs, each filed as <plan>@<body>. The count written is checked against cells x bodies sent x params.
--untagged runs with no charged read putting a label on its rows (run.label_tags off, a control: a label probe then
reads, unless the row carries an index's label: an index read, a label scan and top-k label their output always);
the files are named <cell>__<query>__<i>__untagged.json. --expect N refuses a count of requests other than N. --write-fixture-specs DIR instead writes the gate's fixture cells:
the rules sf0.1's uniform cells never reach (Condensed split lists, a hop over two layouts, props in row groups,
small chunks), and prints their names. --gate-lists WORKLOAD prints, one `name<TAB>value` per line, what
run/predict-gate.sh runs for that read workload (gate_lists).

    python3 -m kvevaluator.measure.predict_requests --sf 0.1 --cells a,b --queries ic2,ic9 --nparam 5 --out DIR
    python3 -m kvevaluator.measure.predict_requests --family bi --sf 0.1 --cells a,b --queries bi4,bi6 --picks --out DIR
    python3 -m kvevaluator.measure.predict_requests --write-fixture-specs DIR
    python3 -m kvevaluator.measure.predict_requests --gate-lists
"""
import argparse
import json
import os

from kvevaluator.measure import server, stores, grid_capture, grid_writes, workload_plans
from kvevaluator.plan_choice import cells
from kvevaluator.paths import ROOT

SPECS = ROOT / "inputs" / "specs"
DEFAULT_FAMILY = "interactive"   # whose plans a request runs when none is named, and the read workload the exact gate runs


def write_fixture_specs(into):
    """the fixture cells, each named for the rule it reaches; returns their names"""
    os.makedirs(into, exist_ok=True)
    out = {}
    d = json.load(open(SPECS / "grid" / "props_condensed_adj_condensed.json"))
    d["adjacency"]["split"] = 512                        # Condensed lists past 512 B are stored as parts
    out["predict_condensed_split512"] = d
    d = json.load(open(SPECS / "grid" / "props_chunked_adj_chunked.json"))
    d["vertex_props"]["owners"]["pack"] = 1024           # small chunks: more chunk boundaries per step
    d["adjacency"]["owners"]["pack"] = 1024
    out["predict_chunked_pack1k"] = d
    # a hop whose two directions are Chunked and Condensed (two passes); props in hot / cold row groups
    out["knows_out_chunked_in_condensed"] = json.load(open(SPECS / "mixes_per_direction" / "knows_out_chunked_in_condensed.json"))
    # with the cell above, every layout on each side of a mixed write (an edge update, a vertex delete's incident edges)
    out["knows_out_condensed_in_split"] = json.load(open(SPECS / "mixes_per_direction" / "knows_out_condensed_in_split.json"))
    d = json.load(open(SPECS / "mixes_per_direction" / "knows_out_split_in_condensed.json"))
    d["adjacency_by_etype_dir"]["knows"]["in"] = out["knows_out_chunked_in_condensed"]["adjacency_by_etype_dir"]["knows"]["out"]
    out["knows_out_split_in_chunked"] = d
    out["props_grouped_hot_cold_adj_condensed"] = json.load(open(SPECS / "grid_grouped" / "props_grouped_hot_cold_adj_condensed.json"))
    _write_specs(into, out)


def write_write_fixture_specs(into):
    """the fixture cells only the write requests run on; returns their names"""
    os.makedirs(into, exist_ok=True)
    # the Per-type CSR corner (meta's own chunk: edge props in their own chunks, ordinals in the adjacency chunk), with a
    # sorted index: the write card refuses a posting index by name
    d = json.load(open(SPECS / "corners" / "corner_pertype_csr.json"))
    d["index"] = json.load(open(SPECS / "grid" / "props_chunked_adj_chunked.json"))["index"]
    _write_specs(into, {"predict_own_sorted_index": d})


UNTAGGED = "untagged"   # the name's last part on a request run with label tags off


def request_name(cell, query, param, untagged=False):
    """a request file's name, without .json: <cell>__<query>__<param>[__untagged] (a query may be <plan>@<body>)"""
    return f"{cell}__{query}__{param}" + (f"__{UNTAGGED}" if untagged else "")


def parse_request_name(name):
    """request_name's parts: {cell, query, param, untagged}; ValueError for another name"""
    parts = name.split("__")
    untagged = parts[-1] == UNTAGGED
    if untagged:
        parts = parts[:-1]
    if len(parts) < 3 or not parts[-1].isdigit():
        raise ValueError(f"{name!r} is not a request name (<cell>__<query>__<param>[__{UNTAGGED}])")
    return {"cell": parts[0], "query": "__".join(parts[1:-1]), "param": int(parts[-1]), "untagged": untagged}


def gate_lists(workload=DEFAULT_FAMILY, jobs=None, scale=None):
    """what predict mode's exact gate runs for a read workload, each from the one place that decides it: the dataset and
    the queries (the workload registry), the write workload of that dataset (its kind) and its own-placement cell, the
    cells (the 49's seven, layouts.SEVEN) and the spec dirs that hold them (layouts.cell_runs), the workload's own
    fixture bodies, untagged control and edge check, the rules no request reaches (the manifests' predict_gate blocks),
    and the units run at once (run_measure.stores_at_once: the stores built and the pass's threads; `jobs` overrides).
    {name: value}, every value a comma list but known_untested (space-separated) and jobs_why. The fixture cells
    (write_fixture_specs) and the plants are the dataset's whose write workload declares a predict_gate block (LDBC's):
    another dataset is refused"""
    from kvevaluator import layouts, workloads
    from kvevaluator.measure import run_measure
    w = workloads.get(workload)
    if w.kind != "read":
        raise SystemExit(f"predict_requests: --gate-lists takes a read workload ({', '.join(workloads.names(kind='read'))})")
    writes = [x for x in workloads.names(kind="write", dataset=w.dataset) if workloads.predict_gate(x)]
    if len(writes) != 1:
        raise SystemExit(f"predict_requests: the exact gate runs on a dataset whose write workload declares a "
                         f"predict_gate block (its fixture cells and write plants are that dataset's); {w.dataset} has "
                         f"{len(writes)}")
    own, wr = workloads.predict_gate(workload), workloads.predict_gate(writes[0])
    dirs = [str(SPECS / d) for d, _ in layouts.cell_runs(",".join(layouts.SEVEN))]
    n, why = run_measure.stores_at_once(jobs, 1, w.dataset, scale)
    return {"workload": workload, "dataset": w.dataset, "write_workload": writes[0], "cells": ",".join(layouts.SEVEN),
            "spec_dirs": ",".join(dirs), "queries": ",".join(workloads.query_list(workload)),
            **{k: ",".join(own.get(k, [])) for k in ("bodies", "untagged", "edge_check")},
            "own_cell": wr["own_cell"], "own_spec_dir": str(SPECS / wr["own_spec_dir"]),
            "known_untested": " ".join(sorted({**own.get("known_untested", {}), **wr.get("known_untested", {})})),
            "jobs": str(n), "jobs_why": why}


def _write_specs(into, specs):
    for name, spec in sorted(specs.items()):
        with open(os.path.join(into, f"{name}.json"), "w") as f:
            json.dump(spec, f, indent=1, sort_keys=True)
    print(",".join(sorted(specs)))


def _cell_spec(a, cell):
    """a cell's spec file: <cell>.json in its spec set's dir (layouts.cell_runs: the grid's, or a chunk size's set) or
    one of --spec-dirs"""
    from kvevaluator import layouts
    dirs = [str(SPECS / d) for d, _ in layouts.cell_runs(cell)] + [d for d in a.spec_dirs.split(",") if d]
    spec = next((os.path.join(d, f"{cell}.json") for d in dirs if os.path.exists(os.path.join(d, f"{cell}.json"))), None)
    if spec is None:
        raise SystemExit(f"predict_requests: no spec {cell}.json in {dirs}")
    return spec


def _ldbc_store(a, cell, spec):
    """the cell's LDBC store, below --store-root when given (store_path finds it by its spec)"""
    if a.store_root:
        return stores.store_path(stores.scale_root("ldbc", a.sf, a.store_root),
                                    f"meta-{cell}", spec)
    return stores.store_dir("ldbc", a.sf, f"meta-{cell}", spec)


def _cell_store(a, cell, spec):
    """the cell's store: on its own data axis for JOB and LinkBench (--sf = the tag; under --store-root only its name,
    none opened), else LDBC's"""
    from kvevaluator import workloads
    w = workloads.get(a.workload)
    if not w.own_data:
        return _ldbc_store(a, cell, spec)
    if a.store_root:
        return os.path.join(stores.scale_root(w.dataset, a.sf, a.store_root),
                            stores.meta_store_name(spec, w.dataset))
    return grid_capture.meta_store(a.sf, grid_writes.grid_workload(a.workload), spec, build=a.ensure_stores)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--write-fixture-specs", metavar="DIR")
    ap.add_argument("--gate-lists", metavar="WORKLOAD", nargs="?", const=DEFAULT_FAMILY, help="print what "
                    f"run/predict-gate.sh runs for this read workload (default {DEFAULT_FAMILY}), one name<TAB>value per "
                    "line (gate_lists), and stop")
    ap.add_argument("--write-write-fixture-specs", metavar="DIR", help="writes the fixture cells only the write "
                    "requests run on, prints their names")
    ap.add_argument("--sf")
    ap.add_argument("--cells", help="comma list of meta spec names (their stores must exist)")
    ap.add_argument("--family", dest="workload", default=DEFAULT_FAMILY, choices=list(workload_plans.WORKLOADS),
                    help="whose plans, params and pick-record section (all run on the LDBC data); writes: the write "
                    "plans (writes/ldbc, writes/microbench) with the write gate's params (measure/cppwrite.sequence), "
                    "each run in shadow (the store is not changed)")
    ap.add_argument("--queries", help="comma list of the workload's plan names (workloads/<workload>/plans/<q>.json)")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--param-set", default=None, help="the param set the reads bind (LDBC Interactive: ldbc, the "
                    "default, or uniform; datasets/ldbc/params.py PARAM_SETS)")
    ap.add_argument("--write-params", choices=("sequence", "capture"), default="sequence",
                    help="writes: whose param rows. sequence (the default): the write gate's order, conflict groups split "
                    "(measure/cppwrite.sequence); capture: each plan's first --nparam rows, the rows the measured writes "
                    "capture runs (grid_writes: workload_plans.query_params), for predict mode (kvcost predict)")
    ap.add_argument("--out")
    ap.add_argument("--jobs", type=int, help="with --gate-lists: the units at once (default: run_measure's rule)")
    ap.add_argument("--untagged", action="store_true", help="run.label_tags off: every label probe reads")
    ap.add_argument("--ensure-stores", action="store_true", help="build each cell's store first when missing (job, "
                    "linkbench: their own data axis, --sf the data tag; grid_capture.meta_store)")
    ap.add_argument("--expect", type=int, help="refuse unless exactly this many requests were written")
    ap.add_argument("--picks", action="store_true", help="each (cell, query) runs the body the pick record names "
                    "(inputs/picks/picks.csv), not the one the cell's capabilities pick; a record that lacks the "
                    "pair, or names a body HEAD's plan files no longer hold, is refused")
    ap.add_argument("--spec-dirs", default="", help="comma list of spec dirs a cell's spec is found in after the grid's "
                    "(its capabilities decide which bodies are legal): the fixture cells' dir")
    ap.add_argument("--store-root", help="the stores dir (<tree>/stores) whose <dataset>/<scale>/ stores the "
                    "requests name (default: this tree's); predict mode against another tree's capture opens none of them")
    a = ap.parse_args(argv)
    if a.gate_lists:
        for k, v in gate_lists(a.gate_lists, a.jobs, a.sf).items():
            print(f"{k}\t{v}")
        return
    if a.write_fixture_specs:
        write_fixture_specs(a.write_fixture_specs)
        return
    if a.write_write_fixture_specs:
        write_write_fixture_specs(a.write_write_fixture_specs)
        return
    if not (a.sf and a.cells and a.queries and a.out):
        ap.error("--sf, --cells, --queries and --out are required")
    os.makedirs(a.out, exist_ok=True)
    from kvevaluator.plan_choice import pick_record
    from kvevaluator.plan_choice.candidates import forced
    n = expected = 0
    from kvevaluator import workloads
    write_families = set(workloads.names(kind="write"))
    if a.workload in write_families - {"writes"} and a.write_params != "capture":
        # an own-data write family (linkbench_writes): each request on the measured writes capture's rows, every one
        # in shadow on the pre-request store (grid_writes); there is no write gate's sequence for it
        raise SystemExit(f"predict_requests: the write family {a.workload} takes --write-params capture (the measured "
                         f"writes capture's rows); the sequence is LDBC's writes only")
    if a.workload in write_families:
        from kvevaluator.measure.cppwrite import sequence
        if a.write_params == "sequence":
            seq = sequence(a.sf, None if a.queries == "all" else a.queries.split(","), a.nparam)
        else:   # every request sees the pre-request store (predict mode): no conflict group to split
            if a.queries == "all":
                raise SystemExit("predict_requests: --write-params capture needs the plans named (--queries)")
            # an own-data family's rows are its param set's (LinkBench: the request log); LDBC's writes have one set,
            # whatever set the reads bind (as the measured side keeps one writes capture)
            ps = a.param_set if workloads.get(a.workload).own_data else None
            seq = [(a.workload, q, idx, args, workload_plans.ir_for(a.workload, q))
                   for q in a.queries.split(",")
                   for idx, args in enumerate(workload_plans.query_params(a.workload, a.sf, q, a.nparam, ps), 1)]
        # LDBC's writes run on the store's own schema; an own-data family's on its dataset's (as grid_writes serves it)
        schema = ({"schema": str(ROOT / grid_capture.SCHEMAS[grid_writes.grid_workload(a.workload)])}
                  if workloads.get(a.workload).own_data else {})
        for cell in a.cells.split(","):
            store = _cell_store(a, cell, _cell_spec(a, cell))
            if not os.path.isdir(store):
                raise SystemExit(f"predict_requests: no store {store} (run/predict-gate.sh builds it first)")
            for _wl, q, idx, args, ir in seq:
                req = server.build_request(ir, args, store, fmt="meta", run={"P": 1, "parts": True, "trace_args": True},
                                           **schema)
                with open(os.path.join(a.out, request_name(cell, q, idx) + ".json"), "w") as f:
                    f.write(json.dumps(req, sort_keys=True))    # the same text as json.dump's, encoded in one call
                n += 1
            expected += len(seq)
        a.cells = ""                                     # the read loop below sends nothing more
    for cell in [c for c in a.cells.split(",") if c]:
        spec = _cell_spec(a, cell)
        caps, of_label = cells.store_caps(a.workload, spec)
        store = _cell_store(a, cell, spec)
        if not os.path.isdir(store):
            raise SystemExit(f"predict_requests: no store {store} (run/predict-gate.sh builds it first)")
        recorded = (grid_capture.record_cell_picks(cell, [q for q in a.queries.split(",") if "@" not in q], scale=a.sf,
                                                   workload=a.workload)
                    if a.picks else {})   # this scale's pick, as the pricing side (kvcost.layout_cost.picks) takes it
        for q in a.queries.split(","):
            plan, _, body = q.partition("@")
            full = workload_plans.ir_for(a.workload, plan)
            if not body and "plans" in full:            # a single-body plan is recorded under its own name
                body = recorded.get(plan, "")
            if body:                                     # one body, named
                if body not in full.get("plans", {}):
                    raise SystemExit(f"predict_requests: {plan} has no body {body!r}")
                runs = [(q, forced(full, body))]
            elif "plans" not in full:
                runs = [(q, full)]                       # a bare plan: one body
            else:
                # the body the pick record names for the cell (one legal body resolves itself); a cell with no entry
                # (a fixture cell) sends every legal body, each named <plan>@<body>: a check of prediction, not a pick
                legal = pick_record.legal_bodies(full, caps, of_label)
                if len(legal) == 1 or pick_record.entry(a.workload, cell, plan) is not None:
                    runs = [(q, forced(full, pick_record.body_for(a.workload, cell, plan, full, caps, of_label, a.sf)))]
                else:
                    runs = [(f"{plan}@{b}", forced(full, b)) for b in legal]
            plist = workload_plans.query_params(a.workload, a.sf, plan, a.nparam, a.param_set)
            expected += len(runs) * len(plist)
            for (name, ir), (i, p) in ((r, ip) for r in runs for ip in enumerate(plist, 1)):
                run = {"P": 1, "parts": True, "trace_args": True}
                if a.untagged:
                    run["label_tags"] = {"hops": False, "probes": False}
                req = server.build_request(ir, p, store, fmt="meta", run=run,
                                              schema=str(ROOT / grid_capture.SCHEMAS[a.workload]))
                with open(os.path.join(a.out, request_name(cell, name, i, a.untagged) + ".json"), "w") as f:
                    f.write(json.dumps(req, sort_keys=True))    # the same text as json.dump's, encoded in one call
                n += 1
    print(f"predict_requests: {n} requests in {a.out}; expected {expected} (cells x bodies sent x params)"
          + (f", --expect {a.expect}" if a.expect is not None else ""))
    if n != expected or (a.expect is not None and n != a.expect):
        raise SystemExit(f"predict_requests: {n} requests written, {a.expect if a.expect is not None else expected} expected")


if __name__ == "__main__":
    main()
