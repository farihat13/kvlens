"""A query's plan bound to a store and run: the plan file a (workload, query) runs, the body a store's record
picks, the steps bound to params, one cell run on an open server, and opening that server.
"""
import json
import os

from kvevaluator import workloads as _W
from kvevaluator.datasets import schema_path
from kvevaluator.measure.server import CppServer, scale_of_store_dir
from kvevaluator.capture.store_identity import store_chunk_target
from kvevaluator.measure.stores import ensure_store, store_chunk
from kvevaluator.paths import ROOT

_EXP = str(ROOT)


# the committed CANONICAL plan dirs per workload (relative to python/kvevaluator/workloads/), from the workloads table
# (each manifest's plan_dirs) -- the one map ir_for and measure/workload_plans read; the per-design ldbc families are
# prepended by ir_for
PLAN_DIRS = {w: list(_W.get(w).plan_dirs) for w in _W.names()}


def ir_for(workload, qname, label_only_family=False):
    """The committed IR file for one query (the writes workload spans two plan dirs).
    label_only_family: an index-free design's ldbc queries come from plans/label_only --
    the canonical plans' index ops would throw on the C++ side (B3)."""
    _json = json
    base = os.path.join(_EXP, "python", "kvevaluator", "workloads")
    # an index-free design: the label_only family first, then the canonical file for a query
    # that has no label_only twin -- those are the queries that use no index (the mirror of
    # queries.label_only_plan_for's fallback), so their canonical plan has nothing to throw
    first = _W.get(workload).label_only_dir if label_only_family else None
    dirs = ([first] if first else []) + PLAN_DIRS[workload]
    for d in dirs:
        path = os.path.join(base, *d.split("/"), f"{qname}.json")
        if os.path.exists(path):
            return _json.load(open(path))
    raise SystemExit(f"no IR file for {workload}/{qname} (dump with python/kvevaluator/plan_choice/dump_planir.py)")


def record_store_key(fmt_name, store_dir, spec=None):
    """a store's key in the pick record: a meta cell's spec stem (its spec file `spec`); a named format's store name at the
    store's own chunk target (pick_record.store_key: pertype_csr-c64k, dgraph, dgraph-c512k)"""
    if fmt_name == "meta":
        if not spec:
            raise SystemExit("record_store_key: a meta store needs its spec (the cell's spec file)")
        return os.path.splitext(os.path.basename(spec))[0]
    from kvevaluator.plan_choice.pick_record import store_key
    return store_key(fmt_name, store_chunk_target(store_dir))


def ir_to_run(workload, qname, fmt_name, store_dir, label_only_family=False, scale=None, spec=None):
    """the IR a runner sends for one query on one store: a generated plan file (and JOB's) reduced to the one body the
    pick record names for this store (pick_record.body_for; a query with one legal body resolves itself), so the C++
    runs what it is sent and selects nothing; a bare plan, and the parked label_only files (which keep
    their `variants`: the C++ selects), as the file is"""
    from kvevaluator.plan_choice.candidates import forced
    ir = ir_for(workload, qname, label_only_family)
    body = body_to_run(workload, qname, fmt_name, store_dir, ir, scale, spec)
    return ir if body is None else forced(ir, body)


def body_to_run(workload, qname, fmt_name, store_dir, ir=None, scale=None, spec=None):
    """the body a runner sends for one query on one store (ir_to_run), by name: the pick record's for this store at its
    scale (pick_record.body_for; a query with one legal body resolves itself); None for a file whose body the record does
    not name (a bare plan, a parked label_only file). A reader of a capture that holds every body selects the same one
    with it (kvstats_layout.load_counts)."""
    from kvevaluator.plan_choice import pick_record
    ir = ir if ir is not None else ir_for(workload, qname)
    if not pick_record.names_its_body(workload, ir):
        return None
    from kvevaluator.capture import spec_sets
    from kvevaluator.plan_choice import cells
    key = record_store_key(fmt_name, store_dir, spec)
    path = spec if fmt_name == "meta" else spec_sets.format_spec(key)
    chunk = None if fmt_name == "meta" else store_chunk_target(store_dir)
    caps, of_label = cells.store_caps(workload, path, chunk)
    scale = scale if scale is not None else scale_of_store_dir(store_dir)
    return pick_record.body_for(workload, key, qname, ir, caps, of_label, scale)


def label_only_family(schema):
    """ir_for's label_only_family for a physical design: an index-free design runs the label_only family."""
    return not (schema.indexed_cols or schema.date_cols)


def bound_steps(ir, args, fmt_name, schema, chunk="store", scale=None, spec=None):
    """The python Steps the C++ side runs for this IR: the variant its capabilities select
    (per label; `chunk` = the store's chunk target, as caps_for -- by default the chunk the
    store's name carries, store_chunk; pass None only for a chunk-blind pick), fragments expanded, params bound.
    Pairs with a response's steps by name (the executor keeps the IR's names)."""
    from kvevaluator.plans.ir.bundle import select_variant, expand_fragments
    from kvevaluator.formats.caps import caps_for, caps_resolver
    from kvevaluator.plans.ir.interp import bind_ir
    if chunk == "store":
        chunk = store_chunk(fmt_name)
    caps = caps_for(fmt_name, schema, chunk=chunk, spec=spec)
    of_label = caps_resolver(fmt_name, schema, chunk=chunk, spec=spec)
    plan = ir if "steps" in ir else dict(ir["plans"][select_variant(ir, caps, of_label, scale)])
    flat = dict(expand_fragments(plan, ir.get("fragments", [])))
    for k in ("params", "let"):
        if k in ir and k not in flat:
            flat[k] = ir[k]
    return bind_ir(flat, args)


def step_kvcost(s):
    """One response step -> the KVCost the python runner would have returned for it."""
    from kvevaluator.cost import KVCost
    b = s["bytes"]
    return KVCost(S=s["S"], G=s["G"], N=s["N"], P=s["P"], D=s["D"],
                  get_key_bytes=b["get_key"], get_value_bytes=b["get_value"],
                  scan_key_bytes=b["scan_key"], scan_value_bytes=b["scan_value"],
                  write_key_bytes=b["write_key"], write_value_bytes=b["write_value"],
                  S_miss=s["S_miss"], G_miss=s["G_miss"], N_miss=s["N_miss"],
                  seek_miss_key_bytes=b["seek_miss_key"], next_miss_key_bytes=b["next_miss_key"],
                  get_miss_key_bytes=b["get_miss_key"])


def run_cell(server, workload, qname, args, fmt_name, schema, spec=None):
    """One (query, params) cell on the C++ engine, in run_plan's shape: (outputs, kvcosts) with
    kvcosts = [(Step, None, KVCost)] (no python prediction on this side) and outputs holding
    only the final rows under the output step's name. A driver that walks run_plan's kvcosts
    can walk these unchanged."""
    scale = scale_of_store_dir(server.store_dir)
    ir = ir_to_run(workload, qname, fmt_name, server.store_dir, label_only_family(schema), scale=scale, spec=spec)
    steps = bound_steps(ir, args, fmt_name, schema, chunk=store_chunk_target(server.store_dir), scale=scale, spec=spec)
    resp = server.run(ir, args)
    if resp.get("error"):
        raise RuntimeError(f"cpp {qname} {fmt_name}: {resp['error']['where']}: {resp['error']['what']}")
    by_name = {s["name"]: s for s in resp["steps"]}
    kvcosts = [(st, None, step_kvcost(by_name[st.out])) for st in steps]
    return {steps[-1].out: resp["rows"]}, kvcosts


def run_steps(server, steps, name="plan"):
    """A bound python plan (a list of Steps, e.g. an optimizer pick) on the C++ engine, lowered to IR with its
    params baked -- run_cell's shape, for a plan that is not a committed IR file."""
    from kvevaluator.plans.ir.serialize import plan_to_ir
    resp = server.run(plan_to_ir(steps, name=name), {})
    if resp.get("error"):
        raise RuntimeError(f"cpp {name}: {resp['error']['where']}: {resp['error']['what']}")
    by_name = {s["name"]: s for s in resp["steps"]}
    kvcosts = [(st, None, step_kvcost(by_name[st.out])) for st in steps]
    return {steps[-1].out: resp["rows"]}, kvcosts


def open_server(sf, fmt_name, schema_name="canonical", codec=None, chunk=None, variant=None, spec=None):
    """A C++ session over a format's store (ingested on first use; `variant`: a data variant, records_file)."""
    path = ensure_store("ldbc", sf, fmt_name, schema_name=schema_name, codec=codec, chunk=chunk, variant=variant, spec=spec)
    kw = {}
    if schema_name != "canonical":
        kw["schema"] = schema_path("ldbc", schema_name)
    return CppServer(path, fmt_name, **kw).__enter__()
