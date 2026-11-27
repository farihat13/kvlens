"""The candidate bodies of every read query: each query's logical plan x the access-rule catalog (rules.py), or its
hand registry where it has no logical plan, then each twin transform on every base body it fits; one entry per body
(body_sha), each with the validity caps it needs (`requires`, per label in `on`). The one source of candidates."""
import glob
import json
import os
from dataclasses import dataclass, field

from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path
from kvevaluator.plans.ir import plan_to_ir
from kvevaluator.plans.plan import Plan
from kvevaluator.plans.ir.bundle import PROPERTY_INDEX_OPS, body_sha
from kvevaluator.plans.ir.capabilities import OP_REQUIRES, VALIDITY_CAPS, VALIDITY_IMPLIED
from kvevaluator.formats.caps import caps_for, caps_resolver, edge_caps_of
from kvevaluator.plans.ir.lowering import lower_ir_steps

SPECS = os.path.join(str(ROOT), "inputs", "specs")
SPEC_GRID = os.path.join(SPECS, "grid")
# the ChK-ChK chunk cells measured beside the 9 grid cells: each set's diagonal cell (the pack target is in the spec)
META_CHUNK_SETS = ("grid_chunked_1k", "grid_chunked_4k", "grid_chunked_16k", "grid_chunked_256k")
# the named formats each workload runs; parked designs (label_only) are not here, so no body is generated for them
NAMED = {"interactive": ["dgraph", "nebula", "arangodb", "pertype_csr", "dgraph-c512k", "pertype_csr-c512k"],
         "bi": ["dgraph", "nebula", "arangodb", "pertype_csr"],
         "microbench": ["dgraph", "nebula", "arangodb", "pertype_csr"]}

# No body is dropped on sf0.1 evidence: relative costs change with scale (a scan or an index walk
# grows with the data, a point read does not), so a body dear at sf0.1 can be the cheapest at SF3. Every legal body is a
# candidate at the scale it is chosen at; the sf0.1 ratio of each new body to its query's cheapest is reported, never
# acted on. A body is pruned only from measurements at the scale of use. DROPPED stays empty: generate() refuses one.
DROPPED = {}

@dataclass(frozen=True)
class Store:
    """one store a body may run on: a named format (or chunk arm), or a meta cell (its spec, its chunk)"""
    name: str
    fmt: str
    spec: str = None
    chunk: int = None


def stores(workload):
    """every store `workload` runs on: its named formats and arms, the 9 meta grid cells and the ChK-ChK chunk cells"""
    from kvevaluator.plan_choice.optimizer.arms import split_arm, store_chunk_of
    out = []
    for n in NAMED[workload]:
        out.append(Store(n, split_arm(n)[0], None, store_chunk_of(n)))
    paths = sorted(glob.glob(os.path.join(SPEC_GRID, "*.json")))
    for d in META_CHUNK_SETS:       # the Chunked / Chunked diagonal cell of each set (grid_chunked_4k holds 4 more)
        paths += sorted(glob.glob(os.path.join(SPECS, d, "props_chunked_adj_chunked_c*.json")))
    for path in paths:
        out.append(Store(os.path.splitext(os.path.basename(path))[0], "meta", path, None))
    return out


@dataclass
class StoreCaps:
    caps: frozenset
    of_label: dict           # label or edge type -> caps (resolved eagerly: the meta spec is read once)
    edge: dict               # edge type -> adjacency caps
    store: Store = None
    ops: dict = field(default_factory=dict)   # op -> "kv" / "flow" / "neither": what the store's C++ format runs

    def runs(self, ops):
        """the ops of `ops` this store's format does not run (asked of the binary once per op: `kvevaluator caps
        --ops`, the C++ format's own op list; the one source, as tests/unit/check_plan_ops_supported.py reads it)"""
        missing = sorted(set(ops) - set(self.ops))
        if missing:
            self.ops.update(_binary_ops(self.store, missing))
        return [o for o in sorted(set(ops)) if self.ops[o] == "neither"]


def _binary_ops(store, ops):
    import subprocess
    from kvevaluator.capture.binary import binary_path
    schema = schema_path("ldbc")
    cmd = [binary_path(), "caps", "--schema", schema, "--format", store.fmt, "--ops", ",".join(ops)]
    config = json.load(open(store.spec)) if store.spec else ({"chunk_target": store.chunk} if store.chunk else None)
    if config:
        cmd += ["--codec-config", json.dumps(config)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"{store.name}: `kvevaluator caps --ops` failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return json.loads(r.stdout)["ops"]


def store_caps(store, schema, labels):
    """the store's caps, its caps per label / edge type (for `labels`), its edge caps per edge type"""
    caps = frozenset(caps_for(store.fmt, schema, chunk=store.chunk, spec=store.spec))
    resolve = caps_resolver(store.fmt, schema, chunk=store.chunk, spec=store.spec)
    edges = edge_caps_of(store.fmt, schema, chunk=store.chunk, spec=store.spec)
    return StoreCaps(caps, {l: frozenset(resolve(l)) for l in labels},
                     {e: frozenset(edges(e)) for e in labels}, store)


@dataclass
class Body:
    """one candidate body: what it runs, what it needs, where it came from"""
    steps: list
    output: dict
    requires: dict = field(default_factory=dict)   # validity cap -> sorted labels it is checked on ([] = store-wide)
    sources: list = field(default_factory=list)    # how it was made: "<rule label>", "<twin>(<base>)", "hand", "+probe"
    measure_only: bool = False
    reason: str = None
    name: str = None
    measure_only_where: dict = field(default_factory=dict)   # cap -> edge types: measure-only on a store holding them

    @property
    def sha(self):
        return body_sha({"steps": self.steps, "output": self.output})


def _labels(s):
    ls = s.get("label", s.get("labels"))
    return [ls] if isinstance(ls, str) else list(ls or ())


def _refs(v):
    """the step names a step's arguments reference ({"ref": name}, at any depth)"""
    if isinstance(v, dict):
        return ({v["ref"]} if set(v) == {"ref"} else set()).union(*[_refs(x) for x in v.values()]) if v else set()
    if isinstance(v, list):
        return set().union(*[_refs(x) for x in v]) if v else set()
    return set()


def dangling_refs(steps, output=None):
    """[(step, the names it references that no EARLIER step defines)] (and the output's): a body that cannot run"""
    seen, bad = set(), []
    for s in steps:
        miss = _refs({k: v for k, v in s.items() if k != "name"}) - seen
        if miss:
            bad.append((s.get("name"), sorted(miss)))
        seen.add(s.get("name"))
    if output is not None and _refs(output) - seen:
        bad.append(("output", sorted(_refs(output) - seen)))
    return bad


def _downstream(steps, name):
    """the steps that read `name`'s output, directly or through other steps"""
    reached, out = {name}, []
    for s in steps:
        if _refs({k: v for k, v in s.items() if k != "name"}) & reached:
            reached.add(s["name"])
            out.append(s)
    return out


TYPE_RESTRICTING_OPS = frozenset({"read_filter_by_label_index", "read_filter_by_label_probe"})
# the index reads that ignore labels on a shared index (capabilities.VALIDITY_CAPS: per_type_index's evidence): the ops
# whose `requires` names per_type_index (inputs/contract/kv_ops.json); an equality read (read_index_eq) is not among them
UNTYPED_INDEX_OPS = frozenset(op for op, req in OP_REQUIRES.items() if "per_type_index" in req)


# the ops whose vertices are those of their first input (rows, items or vids), filtered or reordered
_SAME_VERTICES = frozenset({"dedup", "filter", "filter_in", "filter_not_in", "top_k", "rename", "project", "derive",
                            "limit", "sort", "flag_in"})
_LABEL_OPS = frozenset({"read_vertices_by_label", "read_filter_by_label_index", "read_filter_by_label_probe",
                        "filter_by_label", "read_index_eq", "read_index_range", "index_range_buckets",
                        "read_top_k_by_index"})


def possible_labels(steps, schema):
    """step name -> the labels its vertices can carry (a frozenset), from the schema's edge endpoints; a step not
    followed (an op not listed here, a param) is absent: unknown. A neighbour read gives its edge type's other
    endpoints; a pair read's `src` column keeps its sources' labels (pluck). An index read is taken at its own labels:
    whether its rows hold others is _type_restricted's question. Known limit: a pluck takes the column by its name, so
    a rename that moves `src` into `_vid` is not followed (no plan file does it)."""
    lab, src = {}, {}

    def of(v):
        r = _refs(v) if v is not None else set()
        return lab.get(next(iter(r))) if len(r) == 1 else None
    for s in steps:
        op, name = s.get("op"), s.get("name")
        got = None
        if op in _LABEL_OPS and _labels(s):
            got = frozenset(_labels(s))
        elif op == "read_neighbors" and s.get("etype") in schema.edge_endpoints:
            eps, have = schema.edge_endpoints[s["etype"]], of(s.get("srcs"))
            out = {d for a, d in eps if have is None or a in have} if s.get("dir") in ("out", "both") else set()
            if s.get("dir") in ("in", "both"):
                out |= {a for a, d in eps if have is None or d in have}
            got = frozenset(out)
            if have is not None:
                src[name] = have
        elif op == "pluck":
            r = _refs(s.get("rows"))
            r = next(iter(r)) if len(r) == 1 else None
            got = src.get(r) if s.get("field") == "src" else lab.get(r) if s.get("field") in ("dst", "_vid") else None
        elif op == "read_vertex_props":
            have = of(s.get("vids"))
            got = have & frozenset(_labels(s)) if have is not None and _labels(s) else have
        elif op == "intersect":
            sides = [of(v) for k, v in s.items() if k != "name" and _refs(v)]
            known = [x for x in sides if x is not None]
            got = frozenset.intersection(*known) if known else None
        elif op in _SAME_VERTICES:
            got = of(s.get("rows", s.get("items", s.get("vids"))))
        if got is not None:
            lab[name] = got
    return lab


def _declaring(schema, labels, props):
    """the labels among `labels` that declare one of `props`"""
    return {l for l in labels if set(props) & set(schema.vertex_props.get(l, ()))}


def _sinks(steps, output):
    if output is not None:
        return {output} if isinstance(output, str) else _refs(output)
    read = set().union(*[_refs({k: v for k, v in d.items() if k != "name"}) for d in steps])
    return {d["name"] for d in steps} - read


def _other_labels_reach_output(steps, s, extra, schema, output=None):
    """do vertices of the labels `extra`, let into step `s`'s rows, reach the output: followed step by step, they leave
    at a neighbour read over an edge type none of them has in that direction (they have no neighbours there), at a
    type filter, and at an intersection with a set that cannot hold them (possible_labels); every other step that reads
    them carries them on"""
    lab = possible_labels(steps, schema)
    carried = {s["name"]: set(extra)}            # step -> the extra labels its rows may still hold
    for d in steps:
        args = {k: v for k, v in d.items() if k != "name"}
        held = set().union(*[carried[r] for r in _refs(args) if r in carried]) if _refs(args) & set(carried) else None
        if not held:
            continue
        op = d.get("op")
        if op in TYPE_RESTRICTING_OPS or op == "filter_by_label":
            held = held - set(_labels(d))                 # a type filter keeps the labels it names
            if not held:
                continue
        if op == "read_neighbors" and _refs(d.get("srcs")) & set(carried):
            eps = schema.edge_endpoints.get(d.get("etype"), ())
            has = ({a for a, _b in eps} if d.get("dir") in ("out", "both") else set()) | \
                  ({b for _a, b in eps} if d.get("dir") in ("in", "both") else set())
            held = held & has
            if not held:
                continue
        if op == "intersect":
            others = [lab.get(next(iter(_refs(v)))) for k, v in args.items()
                      if _refs(v) and not (_refs(v) & set(carried)) and len(_refs(v)) == 1]
            if any(o is not None and not (held & set(o)) for o in others):
                continue
        carried[d["name"]] = held
    return bool(_sinks(steps, output) & set(carried))


def _type_restricted(steps, s, output=None, schema=None):
    """is index read `s`'s output restricted to its labels' vertices before it reaches the output: every path from `s`
    to the output (every sink when no output is given) passes a type filter, or an intersection with a set that does
    not come from `s` and holds no vertex of another label declaring the indexed prop (possible_labels: a neighbour
    list of Post, Comment and Forum does not restrict a shared creationDate index to Post and Comment); a top-k read
    given candidate vids is restricted. A step that only reads a type filter's answer beside the unrestricted rows
    (flag_in) does not restrict them. Without `schema` an intersection restricts (no labels to check)."""
    if s.get("op") == "read_top_k_by_index" and s.get("vids") is not None:
        return True
    labs = set(s.get("labels") or _labels(s))
    lab = possible_labels(steps, schema) if schema is not None else {}

    def restricts(v):
        if schema is None:
            return True
        r = _refs(v)
        other = lab.get(next(iter(r))) if len(r) == 1 else None
        return other is not None and not _declaring(schema, set(other) - labs, [s.get("prop")])
    tainted = {s["name"]}                   # the steps whose rows still hold `s`'s unrestricted vertices
    for d in steps:
        args = {k: v for k, v in d.items() if k != "name"}
        if not _refs(args) & tainted:
            continue
        if d.get("op") in TYPE_RESTRICTING_OPS:
            continue
        if d.get("op") == "intersect" and any(not (_refs(v) & tainted) and restricts(v)
                                               for k, v in args.items() if _refs(v)):
            continue
        tainted.add(d["name"])
    if output is not None:
        sinks = {output} if isinstance(output, str) else _refs(output)
    else:
        read = set().union(*[_refs({k: v for k, v in d.items() if k != "name"}) for d in steps])
        sinks = {d["name"] for d in steps} - read
    return not (sinks & tainted)


# Props reads under a label whose vertices can carry another label declaring the prop, where those vertices still reach
# the output by the steps alone (_other_labels_reach_output), reviewed by hand as dropped by the data: (workload, query,
# step) -> why. The agreement gate (tests/gates/check_bodies_agree.py) runs every such body on Dgraph beside the
# label-scoped stores.
LABEL_LEAK_REVIEWED = {     # (workload, query, step, the step it reads its vids from) -> why
    ("bi", "bi13", "pp", "p"): "p are isLocatedIn in-neighbours of Cities: LDBC places messages in Countries, so p holds "
                          "Persons and Universities, and Organisation declares no creationDate",
    ("bi", "bi17", "mp0", "M"): "a Forum in M has no replyOf parent, so it is its own root; Forums have no containerOf "
                           "in-neighbour, so the inner join mf (rooted x fr_n on _root) drops its rows",
}


def op_requires(steps, schema=None, output=None, where=None):
    """the validity caps a body's own ops need, cap -> labels (or edge types): an op that returns wrong rows or fails
    on a store without the cap (capabilities.VALIDITY_CAPS has the evidence per cap). An index read whose rows no
    later step restricts to its labels relies on the index being per type (per_type_index); unrestricted, a bucket
    walk needs the unsorted range index, and a sorted range read with no exact re-check (no later filter) the sorted
    one. `where` = (workload, query): the reviewed exceptions apply (LABEL_LEAK_REVIEWED)"""
    need = {}
    lab = possible_labels(steps, schema) if schema is not None else {}

    def add(cap, names):
        need.setdefault(cap, set()).update(names)
    for s in steps:
        op = s.get("op")
        if op == "read_vertex_props" and _labels(s) and schema is not None:
            # a props read under a label, on vertices that can carry another label declaring a read prop: a store whose
            # props are not keyed under the label (dgraph's) returns that vertex's prop (bi2: hasTag in-neighbours hold
            # Forums, whose creationDate a Post / Comment read then returns)
            r = _refs(s.get("vids"))
            have = lab.get(next(iter(r))) if len(r) == 1 else None
            extra = _declaring(schema, set(have) - set(_labels(s)), s.get("props") or []) if have is not None else set()
            if extra and (where or ()) + (s["name"], next(iter(r), None)) not in LABEL_LEAK_REVIEWED and \
                    _other_labels_reach_output(steps, s, extra, schema, output):
                add("label_scoped_props", _labels(s))
        if op == "read_filter_by_label_index":
            add("type_index", _labels(s))
        if op == "read_vertices_by_label" and (s.get("props") or s.get("keep") is not None):
            add("label_rows_one_run", _labels(s))
        if op == "read_edge_exists" and any(r.get("dir") == "in" for r in (s.get("reads") or [])):
            add("reverse_adjacency", [s["etype"]])
        if op in PROPERTY_INDEX_OPS:
            add("property_index", [])
            labs = s.get("labels") or _labels(s)
            # a shared index (dgraph's) mixes the labels that declare the property: per type only matters if another does
            others = {l for l, ps in schema.vertex_props.items() if s.get("prop") in ps} - set(labs) if schema else {1}
            if op in UNTYPED_INDEX_OPS and others and not _type_restricted(steps, s, output, schema):
                add("per_type_index", labs)
                if op == "index_range_buckets":
                    add("unsorted_range_index", labs)
            if op == "read_index_range" and not any(d.get("op") == "filter" for d in _downstream(steps, s["name"])):
                add("sorted_range_index", s.get("labels") or _labels(s))
    return need


def _merge(into, caps):
    """join two {cap: labels} requirements; a store-wide requirement (no labels) holds over a per-label one"""
    for cap, names in caps.items():
        if cap not in into:
            into[cap] = set(names)
        elif not into[cap] or not names:
            into[cap] = set()
        else:
            into[cap].update(names)
    return into


def measure_only_on(body, sc):
    """is `body` measured but never picked on this store: marked for the query, or an out-keys edge probe on a
    store that keeps both adjacency lists of that edge type"""
    return body.measure_only or bool(body.measure_only_where) and all(
        cap in sc.of_label.get(e, frozenset()) for cap, es in body.measure_only_where.items() for e in es)


def legal(body, sc):
    """what `body` lacks on a store (empty: legal there): the validity caps it needs (a cap with labels is checked on
    each label) and "op:<name>" for an op the store's format does not run"""
    lack = [f"op:{o}" for o in sc.runs({st["op"] for st in body.steps})] if sc.store is not None else []
    for cap, names in sorted(body.requires.items()):
        held = all(cap in sc.of_label.get(n, frozenset()) for n in names) if names else cap in sc.caps
        if not held:
            lack.append(cap)
    return lack


def _lowerings(plan, q, sc):
    """[(steps, output, params, let, suffix)] of a Plan lowered two ways: under the store's caps (the type filter by
    the type index where it is one posting; edge probes by the store's adjacency), and with no caps (the probe forms,
    legal wherever the plan's own validity caps hold: v1's universal fallback body; suffix "+probe" where it differs)"""
    ir = plan_to_ir(plan, name=q)
    out = []
    for caps, edge in ((set(sc.caps), lambda e: sc.edge.get(e, frozenset())), (set(), lambda e: set())):
        steps = lower_ir_steps(ir["steps"], caps, edge)
        if out and steps == out[0][0]:
            continue
        probe = any(a.get("op") == "filter_by_label" and b.get("op") == "read_filter_by_label_probe"
                    for a, b in zip(ir["steps"], steps))
        out.append((steps, ir["output"], ir.get("params"), ir.get("let") or {}, "+probe" if probe else ""))
    return out


def _validity(caps, labels=()):
    """a builder's or rule's declared caps as {validity cap: labels}: selectors dropped, their implied caps kept"""
    out = {}
    for c in caps:
        for v in ({c} & VALIDITY_CAPS) | VALIDITY_IMPLIED.get(c, frozenset()):
            out.setdefault(v, set()).update(labels)
    return out


class CandidateSet:
    """the generated candidates of one query: {sha: Body}, and the plan-level fields every body shares"""
    def __init__(self, workload, q):
        self.workload, self.q = workload, q
        self.schema = _schema(workload)
        self.bodies, self.params, self.let = {}, None, {}
        self.skipped = {}        # (source, why) -> count: a builder that cannot build from no args, a twin that does not fit

    def add(self, steps, output, params, let, requires, source):
        if self.params is None:
            self.params = params
        elif params != self.params:
            raise ValueError(f"{self.q}: params differ between bodies ({source})")
        for k, v in let.items():
            if k in self.let and self.let[k] != v:
                raise ValueError(f"{self.q}: let {k!r} differs between bodies ({source})")
            self.let[k] = v
        bad = dangling_refs(steps, output)
        if bad:
            raise ValueError(f"{self.q} ({source}): steps reference steps no earlier step defines: {bad}")
        b = Body(steps, output)
        req = _merge(_merge({}, requires), op_requires(steps, self.schema, output, (self.workload, self.q)))
        if b.sha in self.bodies:
            old = self.bodies[b.sha]
            if source not in old.sources:
                old.sources.append(source)
            _merge(old.requires, req)
            return old
        b.requires, b.sources = req, [source]
        self.bodies[b.sha] = b
        return b


def _schema(workload):
    from kvevaluator.datasets.ldbc.schema import get_schema
    return get_schema("canonical")


def _queries_module(workload):
    import importlib
    return importlib.import_module(f"kvevaluator.workloads.{workload}.queries")


def _base_plans(workload, q, schema, st, sc):
    """[(Plan, declared validity caps, source)] of q's base bodies on one store: every legal rule combination of its
    logical plan, else every builder of its hand registry entry (each format key, and kept bodies)"""
    from kvevaluator.plan_choice.optimizer import rules
    from kvevaluator.plan_choice.optimizer.rules import legal_choices
    Q = _queries_module(workload)
    logical_of = getattr(Q, "LOGICAL", {})
    out = []
    if q in logical_of:
        logical = logical_of[q]({})
        by_name = {r["name"]: r for table in (rules.RANK_RULES, rules.ENTRY_RULES, rules.FETCH_RULES,
                                              rules.MEMBERSHIP_RULES, rules.FILTER_CHOICE_RULES, rules.WINDOW_RULES)
                   for r in table}
        points = {p.out: p for p in rules.choice_points(logical)}
        for label, choice in legal_choices(logical, schema):
            need = {}
            for out_name, rule in choice.items():
                p = points[out_name]
                lab = p.args["etype"] if p.op == "membership_flag" else p.args.get("label", p.args.get("labels"))
                labs = [lab] if isinstance(lab, str) else list(lab or ())
                _merge(need, _validity(by_name.get(rule, {}).get("validity_caps", ()), labs))
            out.append((rules.expand(logical, choice, schema), need, label or q))
        return out
    return _registry_plans(Q, q, Q.REGISTRY[q])


def _fuse(Q):
    """the workload's label-scan fusion (an enumerate + props pair read as one scan of the label's rows), or None"""
    if hasattr(Q, "_fuse_plan"):
        return Q._fuse_plan
    if hasattr(Q, "_FUSED"):
        from kvevaluator.plans.ir.lowering import with_fused_label_scan
        return with_fused_label_scan
    return None


_SKIPPED = []     # the hand builders _registry_plans could not build, for the caller's report


def _registry_plans(Q, name, entry):
    """[(Plan, declared validity caps, source)] of every builder of a hand registry entry (each format key, kept
    bodies), and each one's fused-scan form where the fusion changes it (the scan carries the rows: v1 ran it on the
    Chunked layouts only; here a candidate wherever its cap holds)"""
    builders = ([("default", entry)] if not isinstance(entry, dict)
                else [(k, b) for k, b in sorted(entry.items()) if k != "_keep"]
                + [(f"keep:{k}", b) for k, (_f, b) in sorted((entry.get("_keep") or {}).items())])
    fuse = _fuse(Q)
    skipped = _SKIPPED
    fused_cap = getattr(Q, "FUSED_SCAN_REQUIRES", {}).get(name, "label_scan_carries_props")
    out = []
    for key, b in builders:
        try:
            plan = b({})
        except Exception as ex:  # -- a builder that needs bound args has no body to generate
            skipped.append((f"hand:{key}", f"no body from no args: {type(ex).__name__}"))
            continue
        if not isinstance(plan, Plan):
            skipped.append((f"hand:{key}", "not a Plan"))
            continue
        need = _validity(getattr(b, "validity_caps", ()))
        name = "hand"              # never a format key: the body's name comes from its content (_name_and_mark)
        out.append((plan, need, name))
        if fuse is not None:
            fp = fuse(plan)
            if fp is not plan and plan_to_ir(fp, name=name)["steps"] != plan_to_ir(plan, name=name)["steps"]:
                labs = sorted({l for st in plan_to_ir(fp, name=name)["steps"]
                               if st.get("op") == "read_vertices_by_label" and st.get("props") for l in _labels(st)})
                out.append((fp, _merge(_merge({}, need), _validity({fused_cap}, labs if fused_cap ==
                                                                   "label_scan_carries_props" else ())),
                            f"{name}+fused"))
    return out


def _twins(workload, q):
    """[(name, [transforms in order], requires, entry)] of q's twins: each twin and each TWIN_DERIVES_FROM chain. A
    twin with no transform is a scan twin: its type filter entered by the index (with_index_filter). `entry` is the
    twin's own registry entry where it has one (BI, the microbench), applied to its builders instead of q's bodies"""
    from kvevaluator.plans.ir.lowering import with_index_filter
    Q = _queries_module(workload)
    alts = getattr(Q, "ALTERNATIVES", {})
    tf = getattr(Q, "TWIN_TRANSFORM", {})
    treq = getattr(Q, "TWIN_REQUIRES", {})
    derives = getattr(Q, "TWIN_DERIVES_FROM", {})
    out = []
    for t in sorted(x for x, base in alts.items() if base == q):
        chain = ([derives[t]] if t in derives else []) + [t]
        own = Q.REGISTRY.get(t)
        entry = own if own is not None and own is not Q.REGISTRY.get(q) else None
        out.append((t if t not in derives else f"{t}<-{derives[t]}", [tf.get(x, with_index_filter) for x in chain],
                    _validity(set().union(*[treq.get(x, ()) for x in chain])), entry))
    return out


def generate(workload, q, store_list=None):
    """the CandidateSet of q: every base body and every twin body over every store, deduped by body_sha"""
    schema = _schema(workload)
    store_list = store_list or stores(workload)
    labels = sorted(set(schema.vertex_props) | set(schema.etypes))
    out = CandidateSet(workload, q)
    out.legal_on = {}
    _SKIPPED.clear()
    twins = _twins(workload, q)
    for st in store_list:
        sc = store_caps(st, schema, labels)
        for plan, need, source in _base_plans(workload, q, schema, st, sc):
            if not _holds(need, sc):
                continue        # the rule needs a validity cap this store lacks: not a candidate here
            for *low, suffix in _lowerings(plan, q, sc):
                out.add(*low, need, source + suffix)
            for tname, chain, treq, entry in twins:
                if entry is None:
                    _twin_body(out, st, sc, q, plan, need, source, tname, chain, treq)
        Q = _queries_module(workload)
        for tname, chain, treq, entry in twins:
            if entry is not None:
                for plan, need, source in _registry_plans(Q, tname, entry):
                    if _holds(need, sc):
                        _twin_body(out, st, sc, q, plan, need, source, tname, chain, treq)
    for key in _SKIPPED:
        out.skipped[key] = out.skipped.get(key, 0) + 1
    # where a body is legal is decided by its `requires` alone, on every store (a body built under one store's
    # lowering may be legal on others, or on none: a body legal nowhere is no candidate)
    scs = {st.name: store_caps(st, schema, labels) for st in store_list}
    out.legal_on = {sha: {n for n, sc in scs.items() if not legal(b, sc)} for sha, b in out.bodies.items()}
    out.dropped = sorted(sha for sha, where in out.legal_on.items() if not where)
    for sha in out.dropped:
        del out.bodies[sha], out.legal_on[sha]
    _name_and_mark(out, _queries_module(workload))
    if DROPPED:
        raise ValueError(f"DROPPED must stay empty (no body is dropped on sf0.1 evidence): {sorted(DROPPED)}")
    return out


def _source_order(source):
    """a body's name is its plainest source: a base body before a twin's, the store's lowering before the probe
    form, a hand body before its fused form, then the shortest"""
    return ("(" in source, "+probe" in source, "+fused" in source, len(source), source)


def _name_and_mark(out, Q):
    """name every body (its plainest source; a source shared by two bodies -- one rule lowered two ways by two
    stores' edge caps -- gets the body sha's head) and mark the measure-only ones: the query's MEASURE_ONLY entries
    by source, and an out-keys edge probe where the store keeps both adjacency lists (measure_only_where)"""
    from kvevaluator.plans.ir.lowering import READS_OUT_KEYS
    for b in out.bodies.values():
        b.sources.sort(key=_source_order)
    shared = {}
    for sha, b in out.bodies.items():
        shared.setdefault(b.sources[0], []).append(sha)
    for sha, b in out.bodies.items():          # a source two bodies share: each named by its own sha's head
        b.name = b.sources[0] if len(shared[b.sources[0]]) == 1 else f"{b.sources[0]}~{sha[:6]}"
    marks = getattr(Q, "MEASURE_ONLY", {}).get(out.q, {})
    unmatched = set(marks) - {src for b in out.bodies.values() for src in b.sources}
    if unmatched:
        raise ValueError(f"{out.q}: MEASURE_ONLY names no generated body: {sorted(unmatched)}")
    for b in out.bodies.values():
        hit = [src for src in b.sources if src in marks]
        if hit:
            b.measure_only, b.reason = True, marks[hit[0]]
        probes = sorted({st["etype"] for st in b.steps if st.get("op") == "read_edge_exists"
                         and [dict(r) for r in st.get("reads") or []] == [dict(r) for r in READS_OUT_KEYS]})
        b.measure_only_where = {"adjacency_lists": probes, "reverse_adjacency": probes} if probes else {}


def _holds(need, sc):
    """does a store hold every declared validity cap (a cap with labels: on each label)?"""
    return all((cap in sc.caps) if not ns else all(cap in sc.of_label.get(n, ()) for n in ns)
               for cap, ns in need.items())


def _twin_body(out, st, sc, q, plan, need, source, tname, chain, treq):
    """add one twin body: the transforms applied in order; a transform whose pattern is absent refuses (skipped)"""
    if not _holds(treq, sc):
        return
    try:
        tp = plan
        for t in chain:
            tp = t(tp)
    except Exception as ex:  # -- the transform's pattern is absent from this body: it refuses
        key = (f"{tname}({source})", f"refused: {type(ex).__name__}")
        out.skipped[key] = out.skipped.get(key, 0) + 1
        return
    ir = plan_to_ir(tp, name=q)
    if dangling_refs(ir["steps"], ir["output"]):    # a transform that splices by step name on a body without them
        key = (f"{tname}({source})", "does not fit: leaves a reference to a step the base does not have")
        out.skipped[key] = out.skipped.get(key, 0) + 1
        return
    for *low, suffix in _lowerings(tp, q, sc):
        out.add(*low, _merge(_merge({}, need), treq), f"{tname}({source}){suffix}")



# The plan files at V1_TREE (before generation), v1's own; the generated files carry no selectors: every run names its
# body from the pick record (whose carried entries are the selectors of pick_record.SELECTOR_TREE).
V1_TREE = "65b294f"


def plan_file(workload, q):
    """the plan file of query q: every generated body under its name, each body's `requires` / `on` (validity caps
    only; legality) and measure-only marks. No `variants`: a runner sends the body the pick record names"""
    gen = generate(workload, q)
    out = {"plan": q, "generated_by": "python/kvevaluator/plan_choice/generate.py", "params": gen.params}
    if gen.let:
        out["let"] = gen.let
    out["plans"] = {}
    out["bodies"] = {}
    for sha, b in sorted(gen.bodies.items(), key=lambda kv: kv[1].name):
        out["plans"][b.name] = {"steps": b.steps, "output": b.output}
        meta = {"sha": sha, "requires": sorted(b.requires)}
        on = {c: sorted(l) for c, l in sorted(b.requires.items()) if l}
        if on:
            meta["on"] = on
        if b.measure_only:
            meta["measure_only"], meta["reason"] = True, b.reason
        if b.measure_only_where:
            meta["measure_only_where"] = {c: sorted(e) for c, e in sorted(b.measure_only_where.items())}
        meta["sources"] = b.sources
        out["bodies"][b.name] = meta
    from kvevaluator import workloads
    # the query's question (and params_like), after `plan`: from its Python, or from the workload's tables
    return workloads.with_query_fields(workload, q, out, declared_fields(workload, q))


def declared_fields(workload, q):
    """{"question": ..., "params_like": ..., "estimate_hints": ...} as q's Python declares them (Plan(question=...,
    params_like=..., estimate_hints=...)) on its logical plan or its hand builders, or a query module's
    ESTIMATE_HINTS = {query: hints} (a logical builder returns steps, not a Plan); refused when two declare different
    values"""
    Q = _queries_module(workload)
    if q in getattr(Q, "LOGICAL", {}):
        builders = [Q.LOGICAL[q]]
    else:
        entry = getattr(Q, "REGISTRY", {}).get(q)
        builders = [] if entry is None else [entry] if not isinstance(entry, dict) else \
            [b for k, b in sorted(entry.items()) if k != "_keep"]
    out = {}
    for b in builders:
        try:
            plan = b({})
        except Exception:  # -- a builder that needs bound args declares nothing here
            continue
        for k in ("question", "params_like", "estimate_hints"):
            v = getattr(plan, k, None)
            if v is None:
                continue
            if out.get(k, v) != v:
                raise ValueError(f"{q} ({workload}): its builders declare two values of {k}: {out[k]!r} and {v!r}")
            out[k] = v
    import importlib
    import pkgutil
    modules = [Q]   # the queries module itself, and each module in it when it is a package
    if hasattr(Q, "__path__"):
        modules += [importlib.import_module(f"{Q.__name__}.{m.name}")
                    for m in sorted(pkgutil.iter_modules(Q.__path__), key=lambda m: m.name)]
    for mod in modules:
        hints = getattr(mod, "ESTIMATE_HINTS", {}).get(q)
        if hints is None:
            continue
        if out.get("estimate_hints", hints) != hints:
            raise ValueError(f"{q} ({workload}): two declarations of estimate_hints: {out['estimate_hints']!r} and {hints!r}")
        out["estimate_hints"] = hints
    return out


def write_plan_files(workload, root=None):
    """write every read query's plan file (the suite's and the registry's other reads) and remove its twins' files
    (their bodies are in the query's file now); returns (written, removed) paths"""
    from kvevaluator.plan_choice.candidates import extra_queries, plans_dir, workload_queries
    qs, alts = workload_queries(workload)
    qs = list(qs) + extra_queries(workload)
    d = plans_dir(workload) if root is None else os.path.join(root, workload, "plans")
    os.makedirs(d, exist_ok=True)
    written, removed = [], []
    for q in qs:
        path = os.path.join(d, f"{q}.json")
        with open(path, "w") as f:
            f.write(json.dumps(plan_file(workload, q), indent=2) + "\n")
        written.append(path)
    for t in sorted(alts):
        path = os.path.join(d, f"{t}.json")
        if os.path.exists(path):
            os.remove(path)
            removed.append(path)
    return written, removed

def main():
    """print every generated body (or, with --write, write the plan files), one line each: workload, query, name, sha, measure-only, requires, legal stores"""
    import argparse
    from kvevaluator.plan_choice.candidates import workload_queries
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--families", dest="workloads", default="interactive,bi,microbench")
    ap.add_argument("--queries", default=None, help="comma list (default: every read query)")
    ap.add_argument("--write", action="store_true", help="write the plan files (and remove the twins' files) instead")
    ap.add_argument("--root", default=None, help="with --write: under this root instead of python/kvevaluator/workloads/*/plans")
    a = ap.parse_args()
    if a.write:
        for wl in a.workloads.split(","):
            written, removed = write_plan_files(wl, a.root)
            print(f"{wl}: wrote {len(written)} plan files, removed {len(removed)} twin files")
        return
    for wl in a.workloads.split(","):
        n = 0
        for q in (a.queries.split(",") if a.queries else workload_queries(wl)[0]):
            gen = generate(wl, q)
            for sha, b in sorted(gen.bodies.items(), key=lambda kv: kv[1].name):
                req = ",".join(f"{c}{'@' + '+'.join(sorted(l)) if l else ''}" for c, l in sorted(b.requires.items()))
                mo = "measure_only" if b.measure_only else ("measure_only_where" if b.measure_only_where else "-")
                print(f"{wl}\t{q}\t{b.name}\t{sha}\t{mo}\t{req or '-'}\t{len(gen.legal_on[sha])}")
                n += 1
        print(f"# {wl}: {n} bodies")


if __name__ == "__main__":
    main()
