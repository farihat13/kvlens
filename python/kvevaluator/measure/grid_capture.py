"""The grid's captures, as the measurement side reads and writes them: the read workloads the grid measures (the
neutral table no workload owns: each workload's schema, its axis (LDBC: the scale factor sf; LinkBench: its node-count
scale tag), its query list, its params), where a spec set's capture lives, how a cell's store is built and checked,
which candidates are valid on a cell, the capture's run, held-out and step rows, and the pick record's path and reader
(inputs/picks/picks.csv: the bodies a picks-only run measures). The pick itself is python/kvcost/layout_cost/picks.py.
"""
import glob
import json
import os
import sys
from kvevaluator import stale
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvevaluator.plan_choice.candidates import workload_queries  # (the query / twin table, re-exported for the grid's drivers)
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture
from kvevaluator.capture.spec_sets import format_of
from kvevaluator.plan_choice.cells import store_caps, workload_schema
from kvevaluator import workloads as W

WORKLOADS = tuple(W.names(kind="read"))   # the read workloads the grid measures (the workloads table)
OWN_DATA = tuple(w for w in WORKLOADS if W.get(w).own_data)   # workloads with their own data axis: --sf is then the data tag
SCHEMAS = {w: W.get(w).schema for w in WORKLOADS}   # the microbench and BI run on LDBC's store, so its schema
from kvevaluator.layouts import (CHUNK_SIZES_KB, GRID, GRID_CHUNK_KB, SEVEN, SPECS)  # noqa: F401  (the layout table: the spec sets, the chunk sizes, the cell sets a grid run measures)


def store_names_unique(spec_dirs):
    """a store is named by its spec file's stem (a capture; its store by the expanded spec, stores.cpp_store_name), so two spec files with
    one stem in different dirs would share one store and one capture name: refused, both named, before anything is opened
    (mixes_per_direction/w16_b2.json beside mixes_w16/w16_b2.json opened the I01 arm's store as the fixture)"""
    seen = {}
    for d in spec_dirs:
        for p in sorted(glob.glob(os.path.join(d, "*.json"))):
            stem = os.path.splitext(os.path.basename(p))[0]
            if stem in seen and os.path.abspath(seen[stem]) != os.path.abspath(p):
                raise SystemExit(f"two spec files map to one store name {stem!r}: {os.path.relpath(seen[stem], EXP)} and "
                                 f"{os.path.relpath(p, EXP)} -- rename one (a name says what it is)")
            seen[stem] = p


def recorded_spec_path(path):
    """a spec file or spec dir as a capture recorded it (relative to the experiment dir, or absolute): that path when it
    exists, else the same set (and file) under inputs/specs/ (an older capture names the set's old dir; the sets kept
    their names)."""
    p = path if os.path.isabs(path) else os.path.join(EXP, path)
    if os.path.exists(p):
        return p
    head, tail = os.path.split(os.path.normpath(p))
    moved = os.path.join(SPECS, os.path.basename(head), tail) if tail.endswith(".json") else os.path.join(SPECS, tail)
    return moved if os.path.exists(moved) else p


def schema_file(workload):
    return os.path.join(EXP, SCHEMAS[workload])


def query_params(workload, sf, q, nparam, param_set=None):
    """the arg dicts a query runs on: its first `nparam` params (fewer when its family has fewer rows, e.g.
    the no-param microbench queries run once); `param_set` as workload_plans.query_params"""
    if workload not in WORKLOADS:
        raise ValueError(f"unknown workload {workload!r}")
    from kvevaluator.measure import workload_plans
    return workload_plans.query_params(workload, sf, q, nparam, param_set)


def parse_slice(queries, params, out, known_queries=None):
    """a driver's --queries / --params as (query list or None, sorted 1-based param indices or None). A slice
    writes only part of a cell's rows, so it must go to its own capture root (--out): refused without one, so
    it can never replace a full capture's CSVs. Each kept row keeps its ORIGINAL param_idx (a slice joins its
    baseline row by row)."""
    qs = [q for q in (queries or "").split(",") if q] or None
    ps = sorted({int(x) for x in (params or "").split(",") if x}) or None
    if (qs or ps) and out is None:
        raise SystemExit("--queries / --params measure a SLICE: give it its own capture root with --out")
    if ps and ps[0] < 1:
        raise SystemExit(f"--params are 1-based param indices (got {params})")
    if qs and known_queries is not None:
        unknown = [q for q in qs if q not in known_queries]
        if unknown:
            raise SystemExit(f"--queries: {unknown} are not queries of this workload")
    return qs, ps


# A NAMED FORMAT measured like a cell (grid_bodies --format): its "spec path" is the marker format:<name>; it is filed in
# the workload's capture beside the layouts, by the store's own name (dgraph-c512k, nebula)


def request_format(spec_path):
    """the format a request names: a named store's base format (a chunk arm's chunk is in its store), else meta"""
    from kvevaluator.plan_choice.optimizer.arms import base_format
    return base_format(format_of(spec_path)) if format_of(spec_path) else "meta"


def cell_spec(spec_path):
    """the meta spec file a request on this store passes (the spec= of stores.store_dir): the cell's spec, None for a named store"""
    return None if format_of(spec_path) else spec_path


def spec_json(spec_path):
    return {"format": format_of(spec_path)} if format_of(spec_path) else json.load(open(spec_path))


def format_store(sf, workload, fmt, build=True):
    """a named format's store on the workload's dataset (stores.ensure_store): a dataset with its own record stream
    at its knobs; LDBC's store (the one ldbc, bi and microbench share) at the chunk a chunk arm names
    (pertype_csr-c512k) or the format's default. `workload` is a read workload (a write workload measures on its
    dataset's read workload's store: grid_writes.grid_workload)."""
    from kvevaluator.measure import stores
    if W.get(workload).kind != "read":
        raise SystemExit(f"a named store is opened through a read workload (asked: {workload}, kind {W.get(workload).kind})")
    if W.get(workload).own_data:
        ds = W.get(workload).dataset
        return stores.ensure_store(ds, sf, fmt) if build else stores.format_store_dir(ds, sf, fmt)
    from kvevaluator.plan_choice.optimizer.arms import split_arm
    base, chunk = split_arm(fmt)
    chunk = chunk if chunk is not None else stores.store_chunk(base)
    if build:
        return stores.ensure_store(W.get(workload).dataset, sf, base, chunk=chunk)
    return stores.format_store_dir(W.get(workload).dataset, sf, base, chunk=chunk)


def meta_store(sf, workload, spec_path, build=True):
    """the path of a cell's meta store on the workload's dataset (built on first use when `build`;
    stores.ensure_store)"""
    from kvevaluator.measure import stores
    if format_of(spec_path):
        return format_store(sf, workload, format_of(spec_path), build)
    if build:
        return stores.ensure_store(W.get(workload).dataset, sf, "meta", spec=spec_path)
    return stores.format_store_dir(W.get(workload).dataset, sf, "meta", spec=spec_path)


def store_is_current(store, spec_path, workload, layout=True):
    """Is the store usable as what THIS binary writes for the cell's spec? A capture is reused only then
    (grid_bodies.fresh, grid_writes.fresh): its recorded store hash equals the manifest's even when both are from
    before a change to what the format stores for the same spec (own placement's adjacency chunk; the old
    stores lack `own_chunk`), and such a store is refused when opened, so its capture must not count as fresh.
    The one check, stores.store_staleness (key-layout stamp, settled, index + filter layout, the spec's config; a
    named format's store, not a spec file: no spec check). layout=False skips the index + filter layout: a counts
    capture records the layout it was measured on itself, so a whole store's capture stays fresh against that store
    (grid_bodies.fresh, grid_writes.store_identity; ruling A)."""
    from kvevaluator.capture import store_identity
    from kvevaluator.measure import stores
    state, _ = stores.store_staleness(store, None if format_of(spec_path) else spec_path, workload, layout=layout)
    return state == store_identity.USABLE


def invalid_on_cell(workload, spec_path):
    """{query: {candidate: the validity caps it lacks}} on one cell: the candidates that may answer wrong on its
    layout (kvevaluator.plan_choice.candidates.invalid_candidates over the cell's meta capabilities, per label). grid_bodies
    never measures them; check_grid asserts no chosen plan is one. A workload of single plans (LinkBench): {}."""
    if W.get(workload).single_plans:
        return {}
    from kvevaluator.plan_choice.candidates import invalid_candidates
    from kvevaluator.formats.caps import caps_for, caps_resolver
    LDBC_SCHEMA = workload_schema(workload)       # the workload's dataset's schema (the name is the LDBC branch's)
    if format_of(spec_path):                                  # a named format (or chunk arm): its own capabilities
        from kvevaluator.plan_choice.optimizer.arms import caps_of, resolver_of
        fmt = format_of(spec_path)
        caps, of_label = caps_of(fmt, LDBC_SCHEMA), resolver_of(fmt, LDBC_SCHEMA)
        out = {q: bad for q in workload_queries(workload)[0]
               if (bad := invalid_candidates(workload, q, caps, of_label))}
        if not W.get(workload).own_data:         # the CSV-ingested dataset's formats (LDBC)
            for q, gaps in _op_gaps(workload, fmt).items():
                for name, ops in gaps.items():
                    out.setdefault(q, {})[name] = sorted(set(out.get(q, {}).get(name, [])) | set(ops))
        return out
    # meta's capabilities come from the cell's spec
    caps, of_label = caps_for("meta", LDBC_SCHEMA, spec=spec_path), caps_resolver("meta", LDBC_SCHEMA, spec=spec_path)
    return {q: bad for q in workload_queries(workload)[0]
            if (bad := invalid_candidates(workload, q, caps, of_label))}


def _op_gaps(workload, fmt):
    """{query: {candidate: ["op:<name>", ...]}} on a named store: the candidates with an op its C++ format does not run
    (asked of the binary, plan_choice/generate.StoreCaps.runs: the same rule as the generator's legality and the agreement
    gate's). A meta cell runs every op: not asked"""
    from kvevaluator.plan_choice import generate as G
    from kvevaluator.plan_choice.candidates import candidates
    from kvevaluator.plans.ir.bundle import expand_fragments
    from kvevaluator.plan_choice.optimizer.arms import split_arm, store_chunk_of
    sc = G.StoreCaps(frozenset(), {}, {}, G.Store(fmt, split_arm(fmt)[0], None, store_chunk_of(fmt)))
    out = {}
    for q in workload_queries(workload)[0]:
        for name, ir in candidates(workload, q)[1]:
            plan = ir if "steps" in ir else next(iter(ir["plans"].values()))     # the forced body (candidates.forced: one body)
            ops = {st["op"] for st in expand_fragments(plan, ir.get("fragments", [])).get("steps", [])}
            if lack := sc.runs(ops):
                out.setdefault(q, {})[name] = [f"op:{o}" for o in lack]
    return out


def measure_only_on_cell(workload, q, spec_path):
    """the bodies of q's plan file that are measured but never picked on THIS cell: a body whose `measure_only_where`
    ({cap: [labels]}, plan_choice/generate.py: the out-keys edge probe, where the two-list probe's caps hold) is
    measure-only on a cell where every named cap holds on every named label. Joined with the bundle-wide
    `measure_only` bodies (kvevaluator.plan_choice.candidates.measure_only_bodies) into the run rows' one `measure_only` flag."""
    if workload in OWN_DATA or format_of(spec_path):
        return set()
    from kvevaluator.plan_choice.candidates import plan_files
    ir = json.load(open(plan_files(workload, q)[0]))
    where = {b: m["measure_only_where"] for b, m in (ir.get("bodies") or {}).items() if m.get("measure_only_where")}
    if not where:
        return set()
    _caps, of_label = store_caps(workload, spec_path)
    return {b for b, need in where.items() if all(c in of_label(l) for c, labels in need.items() for l in labels)}


def candidate_drift(workload, spec_path, runs, tree=None, inv=None, pipeline=None):
    """Has the capture of one cell drifted from HEAD's plan files? Two directions, both silent otherwise:
    (a) `unmeasured`: {query: [HEAD candidates the capture never ran]} -- the capture predates the plan file, so its
        pick may miss a cheaper body (candidates.unmeasured_candidates);
    (b) `refused`: {query: [captured variants that ran ok but whose body HEAD now marks invalid on this cell]} -- the
        capture would let choose() price a body HEAD refuses (invalid_on_cell at HEAD, names resolved by body sha at
        the capture's commit, candidates.resolve_body).
    Both empty = the capture and HEAD agree on this cell's candidate set. The checks and the layout-choice driver FAIL
    on either (an explicit opt-out flag says so); choose() only warns on (a), which needs no capabilities.
    `inv`: invalid_on_cell(workload, spec_path) when the caller already has it. `pipeline`: the capture's plan pipeline
    (candidates.capture_plan_pipeline; None judges against HEAD)."""
    from kvevaluator.plan_choice.candidates import resolve_body, unmeasured_candidates
    if inv is None:
        inv = invalid_on_cell(workload, spec_path)
    out = {"unmeasured": {}, "refused": {}}
    by_q = {}
    for r in runs:
        by_q.setdefault(r["query"], []).append(r)
    for q, rs in sorted(by_q.items()):
        u = unmeasured_candidates(rs, workload, q, tree, pipeline)
        if u:
            out["unmeasured"][q] = u
        bad = []
        for name in sorted({r["body"] for r in rs if r["ok"] == "1"}):
            try:
                stem, body = resolve_body(workload, q, name, tree)
            except KeyError as e:
                # a body HEAD no longer has (retired): it is priced nowhere; said once, never silent
                if (workload, q, name) not in _SAID_UNRESOLVED:
                    _SAID_UNRESOLVED.add((workload, q, name))
                    print(f"[candidate_drift] {workload} {q}: captured variant {name!r} is no body at HEAD, not priced "
                          f"({e})", file=sys.stderr)
                continue
            head_name = body if stem == q else (stem if body is None else f"{stem}:{body}")
            if head_name in inv.get(q, {}):
                bad.append(name)
        if bad:
            out["refused"][q] = bad
    return out


_SAID_UNRESOLVED = set()


def load_runs(out_dir, cell):
    with open(Capture(out_dir).path_of("counts", cell)) as f:
        return list(capture_layout.reader(f))


from kvevaluator.plan_choice.pick_record import PICKS_FILE  # (THE pick record: the decision at the largest reported scale)


def held_out_root(out_dir):
    """the held-out capture beside a capture: <workload>/held-out next to <workload>/reported"""
    return Capture(out_dir).held_out().path


def load_held_out(out_dir, cell):
    """the cell's run rows on the held-out params, or None when the capture has no held-out set (none measured, or a
    family that has none: Capture.has_held_out)"""
    cap = Capture(out_dir)
    if not cap.has_held_out():
        return None
    p = cap.held_out().path_of("counts", cell)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return list(capture_layout.reader(f))


def capture_sf(out_dir, cell):
    """the scale of a capture, from its run record; loud when the record is missing (a capture with no record is not one)"""
    p = Capture(out_dir).record(cell)
    if not os.path.exists(p):
        raise SystemExit(f"{cell}: no run record at {p} (a capture without its runs/<cell>.json is not a capture)")
    with open(p) as f:
        return str(json.load(f).get("sf", ""))


def spec_sha(spec_path):
    """the sha of a spec file's canonical JSON (the identity a record's mix entry is tied to: a re-solved mix under the
    same name must not inherit an old pick)"""
    with open(spec_path) as f:
        return spec_sha_of(json.load(f))


def spec_sha_of(spec):
    import hashlib
    return hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def drifted_record_bodies(ent, workload):
    """{query: (kind, detail)} of the record's entries whose body is not that body of HEAD's plan files: a count-equal
    successor (under_successor), another body or no sha to tell (body_moved); nothing is noted (a gate checks it is
    empty). A pick whose body name HEAD no longer has is refused: no body of that name runs."""
    from kvevaluator.plan_choice.candidates import candidate_bodies, successor_sha
    out = {}
    for q, d in ent.items():
        now = candidate_bodies(workload, q).get(d.get("body"))
        if now is None:
            raise SystemExit(f"{q}: the record's pick {d.get('body')!r} is no body of HEAD's plan files: nothing of that "
                             f"name can run; rewrite the record at the deciding scale")
        if not d.get("body_sha"):
            out[q] = ("body_moved", f"the record's pick {d['body']!r} carries no body sha (no sha): pinned by name")
        elif now != d["body_sha"]:
            kind = "under_successor" if successor_sha(workload, q, d["body_sha"]) == now else "body_moved"
            out[q] = (kind, f"the record's pick {d['body']!r} was body {d['body_sha']}, today's {now} is used")
    return out


def check_record_bodies(cell, ent, workload="interactive"):
    """the record's entries whose body moved since the record (drifted_record_bodies), each warned and marked
    (kvevaluator.stale); {query: kind}. One function for every reader of the record"""
    moved = drifted_record_bodies(ent, workload)
    for q, (kind, detail) in sorted(moved.items()):
        stale.note(kind, f"{cell} {q}: {detail}")
    return {q: kind for q, (kind, _detail) in moved.items()}


def record_cell_entries(cell, queries, path=PICKS_FILE, scale=None, workload="interactive"):
    """{query: the record's entry for this cell (body, body_sha, ...)}: at `scale`, that scale's own entry where the
    record has one (pick_record.entry, CONVENTIONS 19's exception), else the deciding scale's; with no scale, the deciding
    scale's at ANY scale (recorded_picks answers None at the deciding one). Loud when the record has no row for the cell
    or no pick for one of `queries`; a recorded body that moved since is marked (check_record_bodies). `path`: another record's
    table (a control's). `workload`: the record's rows of that workload."""
    from kvevaluator.plan_choice import pick_record
    rec = pick_record.load(path)
    ent = rec.cell(workload, cell) if rec.has_cell(workload, cell) else None
    if ent is None:
        raise SystemExit(f"{cell}: no row in {os.path.relpath(path)}: a picks-only run needs the cell's recorded picks")
    if scale is not None:
        ent = {q: pick_record.entry(workload, cell, q, scale, rec) for q in ent}
    missing = [q for q in queries if q not in ent]
    if missing:
        raise SystemExit(f"{cell}: {os.path.relpath(path)} has no pick for {missing}")
    check_record_bodies(cell, ent, workload)
    return {q: ent[q] for q in queries}


def record_cell_picks(cell, queries, path=PICKS_FILE, scale=None, workload="interactive"):
    """{query: the variant the record picks for this cell} (record_cell_entries): the bodies a picks-only run measures
    (grid_bodies --picks) and predict mode prices (predict_requests --picks)"""
    return {q: e["body"] for q, e in record_cell_entries(cell, queries, path, scale, workload).items()}


def capture_workload(out_dir, cell):
    p = Capture(out_dir).record(cell)
    if not os.path.exists(p):
        raise SystemExit(f"{cell}: no run record at {p} (a capture without its counts/<cell>.json is not a capture: measure it)")
    with open(p) as f:
        return json.load(f).get("workload", "")


def capture_tree_of(out_dir, cell):
    from kvevaluator.plan_choice.candidates import capture_tree
    return capture_tree(out_dir, cell)


def capture_pipeline_of(out_dir, cell):
    from kvevaluator.plan_choice.candidates import capture_plan_pipeline
    return capture_plan_pipeline(out_dir, cell)


def load_steps(out_dir, cell):
    with open(Capture(out_dir).path_of("steps", cell)) as f:
        return list(capture_layout.reader(f))
