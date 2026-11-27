#!/usr/bin/env python3
"""Measure a write workload on each design-space grid cell, the write side of grid_bodies.py: `writes` (the write
primitives + LDBC IU1-IU8, the default) or `linkbench` (its write ops, params from its request log: --param-set quick|quick-next|log;
--sf is then the scale tag).

Per cell, every write plan runs on the cell's store through one `kvevaluator serve`, in the executor's shadow overlay
(the store never changes). One execution writes three row sets: counts (per query and param: S / G / N for the
read-modify-write reads, P / D, write key / value bytes), steps, and parts (each step's counts split by the store part
each op touched: what prices a per-type layout, since one write touches several parts). The files go to the workload's
capture (kvevaluator.output_tree); re-running a cell replaces its counts file. Columns: capture/capture_columns
(WRITE_RUN_COLS, WRITE_STEP_COLS, WRITE_PART_COLS).

  python3 -m kvevaluator.measure.grid_writes --sf 3 [--family writes|linkbench] [--nparam 5] [--cells a,b]
                                          [--fresh] [--spec-dir DIR] [--root DIR [--queries q1,q2] [--params 1,4]]
                                          [--format dgraph,nebula]
--queries / --params (1-based, each row keeping its index) measure a slice and need --root. --format measures named
formats instead of the cells, each filed by its store's name beside the layouts.
"""
import argparse
import csv
import fcntl
import hashlib
import json
import os
import sys
import time

from kvevaluator.measure.grid_capture import GRID, cell_spec, meta_store, parse_slice, request_format, schema_file, store_is_current
from kvevaluator.capture.spec_sets import format_spec
from kvevaluator.capture.spec_sets import named_cells
from kvevaluator.output_tree import Tree, checked_param_set
from kvevaluator import stale
from kvevaluator.capture.capture_layout import write_columns, write_rows
from kvevaluator.measure import counting
from kvevaluator.capture import step_record
from kvevaluator import workloads as _W
from kvevaluator.datasets.linkbench import params as lb_params
from kvevaluator.paths import ROOT

EXP = str(ROOT)
FORMAT = "meta"   # every grid cell's store is a meta store (meta_store): its counting versions key a capture


# what a write capture HOLDS, bumped when the write code or workload changes it; what a write is CHARGED is the
# binary's write counting versions (measure/counting.py). Records keep it under "measurement_version".
CAPTURE_SHAPE = 11  # 11: the last-chunk columns on the step, part and counts rows (a shape-10 file cannot be upgraded); 10: the grid shape (capture_columns; upgrade reads 9 in place)


def grid_workload(workload):
    """the read-side workload whose axis and capture root a write workload shares: its dataset's first read workload"""
    return _W.names(kind="read", dataset=_W.get(workload).dataset)[0]


def writes_capture(sf, workload="writes", root=None, param_set=None):
    """the write workload's capture (kvevaluator.output_tree)"""
    return Tree(root).capture(workload, sf, param_set)


def kvstats_files(sf, cell, workload="writes", root=None, param_set=None):
    """(steps csv, parts csv) of a cell's writes capture"""
    cap = writes_capture(sf, workload, root, param_set)
    return cap.path_of("steps", cell), cap.path_of("parts", cell)


def cell_lock(cap, cell):
    """one process per write cell (two lanes measuring one cell would race on its files): an exclusive lock held until
    the cell's record is written, refused loudly when another process holds it. The lock file is kept, in the
    capture's .locks/"""
    d = os.path.join(cap.path, ".locks")
    os.makedirs(d, exist_ok=True)
    lock = open(os.path.join(d, f".{cell}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise SystemExit(f"[grid_writes] {cell}: another process is measuring this cell into {cap.path} "
                         f"(its lock {d}/.{cell}.lock is held): one process per cell")
    return lock


# ---- the rows of one write run
# P/D/write_bytes are the write's own; its read-modify-write reads are S/G/N and the read lanes. scan_key_bytes /
# get_key_bytes are HIT key bytes; the absence misses (S_miss/G_miss/N_miss, the miss key bytes) are their own columns.
# Columns no run fills here stay blank (the shape is kept for the readers; agree / order / predicted / rocks: n/a).
P = 1              # the executor's worker threads per run (the logical counts do not depend on it)
FREE_ROWS = True   # run.free_rows: a step's rows are freed after their last reader (peak memory only)


def step_rows(qname, pidx, body, resp):
    """The WRITE_STEP_COLS rows of one response."""
    return [{"query": qname, "param_idx": pidx, "body": body, "step_idx": i, "step": s["name"], "op": s["op"],
             **step_record.fields(s, ""), **step_record.last_chunk_fields(s), **step_record.write_units_fields(s)}
            for i, s in enumerate(resp["steps"])]


def part_rows(qname, pidx, body, resp):
    """The WRITE_PART_COLS rows of one response ([] when the format names no parts)."""
    out = []
    for i, s in enumerate(resp["steps"]):
        for part, c in sorted(s.get("parts", {}).items()):
            r = {"query": qname, "param_idx": pidx, "body": body, "step_idx": i, "step": s["name"], "op": s["op"],
                 "part": part}
            for k in ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss"):
                r[k] = c[k]
            r.update(c["bytes"])
            r.update(step_record.hist_fields(c))
            r.update(step_record.decoded_fields(c.get("decoded"), c))
            r.update(step_record.last_chunk_fields(c))
            r.update(step_record.write_units_fields(c))
            out.append(r)
    return out


def result_hash(final):
    """a fingerprint of the result rows in their normalized (sorted) form"""
    from kvevaluator.measure.report import normalize
    return hashlib.blake2b(repr(normalize(final)).encode(), digest_size=16).hexdigest()


def bound_plan(ir, args, body):
    """the plan the C++ side ran, as Python Steps with params and lets bound: the bare plan, or the body the response
    names"""
    from kvevaluator.plans.ir.bundle import expand_fragments
    from kvevaluator.plans.ir.interp import bind_ir
    if "steps" in ir:
        plan = ir
    elif body is not None and body in ir.get("plans", {}):
        plan = dict(ir["plans"][body])
    else:
        raise SystemExit(f"the response names body {body!r}, not a plan of this file")
    flat = dict(expand_fragments(plan, ir.get("fragments", [])))
    for k in ("params", "let"):           # the file-level declarations travel with the body
        if k in ir and k not in flat:
            flat[k] = ir[k]
    return flat, bind_ir(flat, args)


def payload_of(steps):
    """a write's logical payload: 8 B per id + its props (the denominator of wa)"""
    from kvevaluator.schema.graph import value_bytes
    payload = 0
    for st in steps:
        for name, v in st.args.items():
            if name in ("vid", "src", "dst"):
                payload += 8
            elif name in ("props", "value"):
                payload += value_bytes(v)
    return payload


def measure_write(sf, workload, qname, pidx, args, db, srv, cell, spec=None):
    """one (query, param) on one store: (counts row, step rows, part rows). The steps' parts are asked for always (a
    format without a part scheme returns none). `spec`: a meta cell's spec file."""
    from kvevaluator.measure.plan_runs import ir_to_run
    from kvevaluator.plans.ir.bundle import body_sha
    from kvevaluator.schema.graph import value_bytes
    ir = ir_to_run(workload, qname, db, srv.store_dir, spec=spec)
    resp = srv.run(ir, args, {"P": P, "perf_level": 2, "free_rows": FREE_ROWS, "parts": True})
    if resp.get("error"):
        raise SystemExit(f"cpp {qname} p{pidx} {db}: {resp['error']['where']}: {resp['error']['what']}")
    tot, out_bytes = {}, {}
    for s in resp["steps"]:
        for k in ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss"):
            tot[k] = tot.get(k, 0) + s[k]
        for k, v in s["bytes"].items():
            tot[k] = tot.get(k, 0) + v
        out_bytes[s["name"]] = s["out_bytes"]
    final = resp["rows"]
    prov = resp.get("provenance", {})
    flat, steps = bound_plan(ir, args, prov.get("body"))
    if prov.get("body_sha") is not None:
        want = body_sha(ir, prov.get("body") if "plans" in ir else None)
        if prov["body_sha"] != want:
            raise SystemExit(f"cpp {qname} p{pidx} {db}: the binary ran body {prov['body_sha']}, the file here lowers "
                             f"to {want} -- the tree and the binary disagree on the plan")
    if out_bytes[flat["output"]] != value_bytes(final):      # the executor's sizing and Python's agree on the answer
        raise SystemExit(f"cpp {qname} p{pidx} {db}: executor out_bytes {out_bytes[flat['output']]} "
                         f"!= python value_bytes {value_bytes(final)} on the final rows")
    g = lambda k: tot.get(k, 0)
    body = prov.get("body") or qname
    row = {"sf": sf, "cell": cell, "query": qname, "param_idx": pidx, "body": body, "measure_only": 0, "ok": 1,
           "error": "", **{k: g(k) for k in ("S", "G", "N", "S_miss", "G_miss", "N_miss")},
           **{k: g(k) for k in step_record.READ_BYTE_LANES}, "read_bytes": sum(g(k) for k in step_record.READ_BYTE_LANES),
           "rows": len(final), "result_hash": result_hash(final),
           "answer_key": "", "body_sha": prov.get("body_sha") or "", "run_key": "",   # a write plan has one body
           "wall_ns": (resp.get("totals") or {}).get("wall_ns", ""), "measured_as": "",
           "P": g("P"), "D": g("D"), "write_key": g("write_key"), "write_value": g("write_value"),
           "payload_bytes": payload_of(steps) if g("write_key") + g("write_value") else ""}
    srows, prows = step_rows(qname, pidx, body, resp), part_rows(qname, pidx, body, resp)
    for col in step_record.LAST_CHUNK:      # the run's last-chunk touches: its steps' summed ("" when not measured)
        have = [r[col] for r in srows if r[col] != ""]
        row[col] = sum(have) if have else ""
    return row, srows, prows


def upsert_rows(path, kind, rows, run_id):
    """rows into a cell's steps or parts file, per query: a query these rows carry replaces its old rows, the others
    are kept (a slice re-measures some queries of a cell); every row carries `run`"""
    from kvevaluator.measure import kvstats_layout as L
    L.upsert(path, rows, L.KEYS[kind], run_id, columns=[c for c in write_columns(kind) if c != "run"],
             scope=L.SCOPE[kind])


def check_parts(sf, cell, spec_dir=GRID, workload="writes", root=None, named=False, param_set=None):
    """re-check, on the written CSVs, that every step's parts sum exactly to the step's counts (the executor
    asserts it as it counts; this reads back what reached disk)"""
    from kvevaluator.measure.kvstats_layout import parts_sum_mismatches
    steps_path, parts_path = kvstats_files(sf, cell, workload, root, param_set)
    steps = write_rows(steps_path)[2]
    # a named format names no store parts: its capture has no parts file
    if named and not os.path.exists(parts_path):
        return len(steps), 0
    parts = write_rows(parts_path)[2]
    bad = parts_sum_mismatches(steps, parts)
    unknown = [p for p in parts if p.get("part") == "unknown"]   # the sum closes over 'unknown' too: check it apart
    if unknown:
        raise SystemExit(f"[grid_writes] {cell}: {len(unknown)} part rows decoded to 'unknown': {unknown[:2]}")
    if bad:
        raise SystemExit(f"[grid_writes] {cell}: {len(bad)} steps whose parts do not sum to the step: {bad[:3]}")
    return len(steps), len(parts)


def store_identity(sf, cell, spec_dir=GRID, workload="writes", spec_path=None):
    """[key layout, value codec hash, index + filter layout, what the store holds (store_content_identity:
    the schema, the record stream, the key and ingest counts)] of the cell's store manifest, or None when it is not
    built yet or is not what this binary builds for the spec (a capture then is never fresh: fresh() refuses None)"""
    spec_path = spec_path or os.path.join(spec_dir, f"{cell}.json")      # a cell's spec, or a named format's
    store = meta_store(sf, grid_workload(workload), spec_path, build=False)
    mpath = os.path.join(store, "manifest.json")
    if not os.path.exists(mpath):
        return None
    m = json.load(open(mpath))
    if not store_is_current(store, spec_path, grid_workload(workload), layout=False):
        return None      # a store this binary would not build for the spec: its capture is not fresh
    from kvevaluator.measure import store_layout
    from kvevaluator.capture.store_identity import store_content_identity
    return [m.get("key_codec"), m.get("value_codec_hash"), store_layout.of_manifest(m), store_content_identity(m)]


def write_queries(workload):
    """the write queries a cell runs: the write workload's query list (its manifest's, or its queries module's suite)"""
    return _W.query_list(workload)


def inputs_fingerprint(sf, workload="writes", nparam=5, params=None, queries=None, param_set=None):
    """sha of what a cell's writes depend on besides the store and the code: per write query what its committed IR can
    run (kvevaluator.plans.ir.bundle.run_identity: body shas, bind / let / params in file order, selectors; not the file's bytes, since
    and the param rows the cell runs (the first `nparam`, or a slice's 1-based `params`; not whole param
    files); for LinkBench also its schema and its params source (the LDBC writes' schema is the one the store was built
    with: store_identity carries its content sha). Write plans carry no per-cell selection today (every one is bare); a bundle's selectors are hashed
    whole, which can only re-measure more."""
    from kvevaluator.measure import plan_runs
    from kvevaluator.plans.ir.bundle import run_identity
    from kvevaluator.measure.workload_plans import query_params
    h = hashlib.sha256()
    if _W.get(workload).own_data:
        from kvevaluator import datasets
        ds = datasets.get(_W.get(workload).dataset)
        path = os.path.join(EXP, ds.schema)
        h.update(ds.capture_key_schema.encode())                 # the schema's fixed name, not its path
        with open(path, "rb") as f:
            h.update(f.read())
        h.update(lb_params.check_param_set(param_set).encode())
    for q in (queries or write_queries(workload)):          # a slice hashes its own queries only
        if params:
            allp = query_params(workload, sf, q, max(params), param_set)
            rows = [allp[i - 1] for i in sorted(params) if i - 1 < len(allp)]
        else:
            rows = query_params(workload, sf, q, nparam, param_set)
        h.update(json.dumps([q, run_identity(plan_runs.ir_for(workload, q)), sorted(params) if params else nparam, rows],
                            sort_keys=True).encode())
    return h.hexdigest()[:16]


def fresh(sf, cell, out, nparam, spec_dir=GRID, workload="writes", root=None, slice_=(None, None), spec_path=None,
          param_set=None):
    """reuse a cell's CSV only if it was measured on the SAME store, with the same param rows and what the plans run
    (inputs_fingerprint), the same params count, the same capture shape (CAPTURE_SHAPE) and the binary's write
    counting versions (never keyed on the git SHA, which flips every commit)"""
    named = bool(spec_path) and spec_path.startswith("format:")
    side = out[:-len(".csv")] + ".json"
    if not (os.path.exists(out) and os.path.exists(side)):
        return False
    files = list(kvstats_files(sf, cell, workload, root, param_set))
    if not all(os.path.exists(p) for p in (files[:1] if named else files)):   # a named format: no parts file
        return False
    rec = json.load(open(side))
    ident = store_identity(sf, cell, spec_dir, workload, spec_path)
    fmt = request_format(spec_path) if spec_path else FORMAT       # a named store's base format, else meta
    return (ident is not None                   # no store, or a stale one: never fresh (a record of None == None was)
            and rec.get("measurement_version") == CAPTURE_SHAPE and rec.get("nparam") == nparam
            and counting.counting_fresh(rec, fmt, "write", "grid_writes")[0]   # the C++ write path's counting
            and rec.get("slice", {"queries": None, "params": None}) == {"queries": slice_[0], "params": slice_[1]}
            and rec.get("inputs") == inputs_fingerprint(sf, workload, nparam, slice_[1], slice_[0], param_set)
            and rec.get("store") == ident)


def measure_cell(sf, workload, cell, store, fmt, spec_dir, nparam, slice_, out, spec=None, param_set=None):
    """every write query of one cell (or named store) through one serve: the counts file written whole, the steps and
    parts upserted per query. The record is written by the caller. `spec`: a meta cell's spec file"""
    from kvevaluator.measure import kvstats_layout as L
    from kvevaluator.measure.server import CppServer
    from kvevaluator.measure.workload_plans import query_params
    rows, run_id = [], L.new_run_id()
    cap = writes_capture(sf, workload, slice_[2], param_set)
    steps_path, parts_path = cap.path_of("steps", cell), cap.path_of("parts", cell)
    for path in (steps_path, parts_path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with CppServer(store, fmt, schema=schema_file(grid_workload(workload))) as srv:
        for q in (slice_[0] or write_queries(workload)):
            if slice_[1]:
                every = query_params(workload, sf, q, 0, param_set)
                picked = [(i, every[i - 1]) for i in slice_[1] if 1 <= i <= len(every)]
            else:
                picked = list(enumerate(query_params(workload, sf, q, nparam, param_set), 1))
            srows, prows = [], []
            for pidx, args in picked:
                row, s_, p_ = measure_write(sf, workload, q, pidx, args, fmt, srv, cell, spec)
                rows.append(row); srows.extend(s_); prows.extend(p_)
            if srows:
                upsert_rows(steps_path, "steps", srows, run_id)
            if prows:
                upsert_rows(parts_path, "parts", prows, run_id)
    if not rows:
        raise SystemExit(f"[grid_writes] {cell}: no (query, param) selected -- nothing was measured")
    tmp = out + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=write_columns("counts"))
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, out)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", required=True, help="the scale factor (writes) or LinkBench's scale tag (linkbench)")
    ap.add_argument("--family", dest="workload", default="writes", choices=_W.names(kind="write"))
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--cells", default="", help="comma list (default: every <spec-dir>/*.json)")
    ap.add_argument("--format", default="", help="comma list of NAMED formats (dgraph, nebula, arangodb, a chunk arm "
                    "such as dgraph-c512k) measured instead of the cells, filed by the store's name beside the layouts")
    ap.add_argument("--fresh", action="store_true", help="re-measure cells that already have a counts CSV")
    ap.add_argument("--spec-dir", default=GRID, help="the spec set (default: the grid, inputs/specs/grid)")
    ap.add_argument("--root", default=None, help="the output tree to write into (default: the output dir); a slice "
                    "needs its own")
    ap.add_argument("--queries", default="", help="comma list: only these write queries (a slice; needs --root)")
    ap.add_argument("--params", default="", help="comma list of 1-based param indices (a slice; needs --root)")
    ap.add_argument("--param-set", default=None, help="the param set the writes bind, one the workload's params module "
                    "lists (LinkBench: anchors, quick, quick-next, log); default its first")
    a = ap.parse_args()
    a.param_set = checked_param_set(a.workload, a.param_set)
    spec_dir = os.path.abspath(a.spec_dir)
    # a slice names write queries only: the key hashes the write queries (a LinkBench read named here would run unkeyed)
    slice_ = parse_slice(a.queries, a.params, a.root, write_queries(a.workload))
    root, named = a.root, bool(a.format)
    if a.format:
        jobs = [(fmt, format_spec(fmt)) for fmt in a.format.split(",")]
    else:
        cells = named_cells(spec_dir, a.cells, "grid_writes")
        if not cells:
            raise SystemExit(f"[grid_writes] no *.json specs in {spec_dir}")
        jobs = [(None, os.path.join(spec_dir, f"{c}.json")) for c in cells]
    out_dir = writes_capture(a.sf, a.workload, root, a.param_set).dir("counts")
    os.makedirs(out_dir, exist_ok=True)
    from kvevaluator.capture.binary import binary_provenance
    from kvevaluator.measure.report import git_provenance
    for fmt, spec_path in jobs:
        store = meta_store(a.sf, grid_workload(a.workload), spec_path)
        cell = os.path.basename(store) if fmt else os.path.basename(spec_path)[:-len(".json")]
        out = os.path.join(out_dir, f"{cell}.csv")
        if not a.fresh and fresh(a.sf, cell, out, a.nparam, spec_dir, a.workload, root, slice_, spec_path,
                                 param_set=a.param_set):
            print(f"[grid_writes] {cell}: already measured on this store with these params -- reused (--fresh re-measures)")
            continue
        cap = writes_capture(a.sf, a.workload, root, a.param_set)
        lock = cell_lock(cap, cell)
        t0 = time.time()
        side = out[:-len(".csv")] + ".json"
        if os.path.exists(side):
            # invalidate the old provenance BEFORE measuring: a run that fails half-way must never leave a record that
            # still reads as fresh next to a partial capture
            with open(side, "w") as f:
                json.dump({"cell": cell, "sf": a.sf, "status": "measuring (an earlier run did not finish)"}, f)
        with stale.scope():        # this cell's notes go to its own record (kvevaluator.stale)
            rows = measure_cell(a.sf, a.workload, cell, store, request_format(spec_path), spec_dir, a.nparam,
                                (*slice_, root), out, cell_spec(spec_path), a.param_set)
            nsteps, nparts = check_parts(a.sf, cell, spec_dir, a.workload, root, named, a.param_set)
            sha, dirty = git_provenance()
            with open(side, "w") as f:
                json.dump(stale.fold({"git_commit": sha, "git_dirty": dirty, "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "sf": a.sf, "backend": "cpp", "workload": a.workload, "variant": "canonical",
                           "dbs": [request_format(spec_path)], "queries": len({r["query"] for r in rows}),
                           "rows": len(rows), "param_sha": None, "P": P, "free_rows": FREE_ROWS,
                           "cell": cell, "nparam": a.nparam, "measurement_version": CAPTURE_SHAPE,
                           "counting": counting.stamp(request_format(spec_path)),
                           "slice": {"queries": slice_[0], "params": slice_[1]},
                           "inputs": inputs_fingerprint(a.sf, a.workload, a.nparam, slice_[1], slice_[0], a.param_set),
                           "store": store_identity(a.sf, cell, spec_dir, a.workload, spec_path),
                           "wall_s": round(time.time() - t0, 1), "binary": binary_provenance()},
                                     os.path.dirname(os.path.dirname(out)), unit=cell), f, indent=2)   # + the capture dir's notes
        lock.close()
        print(f"[grid_writes] {cell}: {time.time() - t0:.0f}s -> {out} ({len(rows)} rows, {nsteps} steps, "
              f"{nparts} part rows, parts sum exactly)")


if __name__ == "__main__":
    sys.exit(main())
