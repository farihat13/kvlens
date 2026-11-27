"""Dump each workload's parameterized plan IR (templates with {"param"} / {"let"} holes) to one JSON file per query in
its own workloads/<w>/plans/: the language-neutral query contract the C++ evaluator reads. The Python builders are
the source of truth; this serializes them, and nothing is shared across workloads.

A file is a bare plan {plan, for, params, let, steps, output} when every format agrees, else a capability bundle
{plan, params, let, variants, plans} whose variants carry `requires` (the caps a format must have), optionally
`lacks` (the caps it must not have) and `for` (the format list, for people). A non-fallback variant's `requires` comes
from the workload's VARIANT_REQUIRES (named by the query's author: it cannot be derived), plus
type_index_single_posting when the body enters the type filter by the index.

The files carry physical ops only: filter_by_label is lowered per format (queries.lower_filter), and since a format's
caps can depend on the design, every format is resolved under each of the family's reference designs; each distinct
body is a variant, selected at run time by caps_for(format, schema), or per label (caps_resolver) for a variant with
an "on" annotation. iter_all() is the one generator this dumper and the drift guard (tests/unit/check_plan_files.py)
share. The label_only family's files are frozen as committed, and nothing runs them (label_only_plan_for refuses).
Engine-fidelity plans are not in the IR.

Run:  uv run --locked python python/kvevaluator/plan_choice/dump_planir.py
"""
import json
import os
from kvevaluator import workloads as _W
import sys

from kvevaluator.plans.plan import Plan
from kvevaluator.plans.ir import plan_to_ir, NeedsExpr
from kvevaluator.plans.ir.capabilities import OP_REQUIRES, STAMPED_AT_EMIT, validity_caps
from kvevaluator.plans.ir.bundle import with_property_index_requires   # every workload's dumper stamps the same way
from kvevaluator.plan_choice.optimizer.arms import base_format, caps_of as caps_for, edge_caps_of   # a profile name: a format, or a chunk arm
from kvevaluator.workloads.interactive import queries as ldbc_q
from kvevaluator.datasets.ldbc.schema import get_schema
from kvevaluator.workloads.microbench import queries as mb_q
from kvevaluator.workloads.writes.microbench import queries as wmb_q
from kvevaluator.workloads.writes.ldbc import queries as wldbc_q
from kvevaluator.workloads.linkbench import queries as lb_q
from kvevaluator.workloads.bi import queries as bi_q
from kvevaluator.datasets.linkbench.schema import get_schema as lb_get_schema
from kvevaluator.paths import ROOT

# Fallback (dgraph) first. Every format a workload resolves plans for must appear in ITS list --
# and no more (a format a workload does not target must not, or the bundle would claim to serve
# it).
CANON_FORMATS = ["dgraph", "nebula", "arangodb", "pertype_csr"]
# LDBC also serves the paper's CHUNK ARMS: a format's store at another chunk target, with its own capability profile
# (coarse_chunks)
LDBC_FORMATS = CANON_FORMATS + ["pertype_csr-c512k", "dgraph-c512k"]
MB_FORMATS = ["dgraph", "nebula", "arangodb", "pertype_csr"]
LB_FORMATS = ["dgraph", "nebula", "arangodb"]           # LinkBench targets the three real DBs
BI_FORMATS = ["dgraph", "nebula", "arangodb", "pertype_csr"]   # BI: the engine formats on the LDBC stores

# The reference physical designs a family's bundle must serve (name -> schema). A format is
# resolved under each; the canonical family needs the type-index-free design too, since dgraph's
# type filter lowers differently there.
CANON_DESIGNS = {"canonical": get_schema("canonical"), "no_type_index": get_schema("no_type_index")}
# The no_type_index design (dgraph's type-index ablation) is NOT resolved for the posting-index
# per-type family: nothing measures them there (the optimizer's families are canonical +
# label_only), so their body under it would be a policy artifact -- the caps_default sentinel
# lowers to the probe while nebula's adopted whole-type scan stays, two bodies no capability set
# separates. Banked: measure that design
# for them (a third search family) if a study ever needs it.
NO_TYPE_INDEX_EXCLUDED = {"pertype_csr"}
# The LDBC family also serves the full_index design (dgraph's index CONTROL arm: every index per
# type, dates key-sorted -- caps per_type_index / key_sorted_index). It changes dgraph's plans only
# (the optimizer's full_index family holds dgraph's picks; the other formats' caps and stores are the same
# under it), so it is resolved
# for the dgraph side alone: the other formats' canonical profile already checks them.
LDBC_DESIGNS = {**CANON_DESIGNS, "full_index": get_schema("full_index")}
FULL_INDEX_RESOLVED = {"dgraph"}


def resolves(fmt, design):
    """Is (format, design) a profile the emitter resolves and the drift guard checks?"""
    if design == "full_index":
        return fmt in FULL_INDEX_RESOLVED
    if design != "canonical" and fmt != base_format(fmt):
        return False                             # a chunk arm's store exists for the canonical design only
    return not (design == "no_type_index" and fmt in NO_TYPE_INDEX_EXCLUDED)
LINKBENCH_DESIGNS = {"canonical": lb_get_schema("canonical")}   # LinkBench has one design
from kvevaluator.plans.ir.lowering import ANY_TYPE_CAP, TYPE_CAP


def _pdir(*parts):
    return os.path.join(str(ROOT), "python", "kvevaluator", "workloads",
                        *parts, "plans")


def _flat_pick(entry, _fmt):
    return entry                                 # writes: bare builders, no per-format override


class Workload:
    """One plan family to dump: where its builders are and how they resolve per format (its hand pick)."""
    def __init__(self, name, registry, pick, formats, plans_dir, variant_requires, designs,
                 fallback_format="dgraph", base_requires=(), query_requires=None, fused_scan_requires=None):
        self.name, self.registry, self.pick, self.formats = name, registry, pick, formats
        self.plans_dir, self.variant_requires, self.designs = plans_dir, variant_requires, designs
        self.fallback_format = fallback_format   # the format whose body is the `requires: []` variant
        self.base_requires = tuple(base_requires)   # caps EVERY variant of this family requires --
        # a family-level guard
        self.query_requires = query_requires or {}     # query -> caps its bundle requires (the registry's TWIN_REQUIRES)
        self.fused_scan_requires = fused_scan_requires or {}   # query -> the cap its fused-scan variant names in place
                                                 # of label_scan_carries_props (the registry's FUSED_SCAN_REQUIRES)


def call_pick(pick, entry, fmt, design=None, scale=None):
    """Resolve a pick for (format, design[, scale]): a pick taking the design and scale gets them; the hand picks take
    (entry, fmt) and are design- and scale-blind (their lowering happens per caps downstream)."""
    import inspect
    n = len(inspect.signature(pick).parameters)
    if n >= 4:
        return pick(entry, fmt, design, scale)
    if n >= 3:
        return pick(entry, fmt, design)
    return pick(entry, base_format(fmt))         # the hand registries know formats, not chunk arms


def pick_for(w, q):
    """The producer for (workload, query): the workload's hand pick (a generated read query never comes here)"""
    return w.pick


def plans_dir_in(w, root=None):
    """w's plans dir, or the same relative path under another root (--out-root: a scratch copy, never the committed
    one)"""
    if root is None:
        return os.path.abspath(w.plans_dir)
    workloads_root = os.path.dirname(os.path.abspath(_pdir()))      # .../evaluator/workloads
    return os.path.join(os.path.abspath(root), os.path.relpath(os.path.abspath(w.plans_dir), workloads_root))


WORKLOADS = [
    Workload("interactive", ldbc_q.REGISTRY, ldbc_q._pick, LDBC_FORMATS, _pdir("interactive"),
             ldbc_q.VARIANT_REQUIRES, LDBC_DESIGNS, query_requires=ldbc_q.TWIN_REQUIRES),
    Workload("microbench", mb_q.REGISTRY, mb_q._pick, MB_FORMATS, _pdir("microbench"),
             mb_q.VARIANT_REQUIRES, CANON_DESIGNS, query_requires=mb_q.TWIN_REQUIRES,
             fused_scan_requires=mb_q.FUSED_SCAN_REQUIRES),
    Workload("writes/microbench", wmb_q.REGISTRY, _flat_pick, CANON_FORMATS,
             _pdir("writes", "microbench"), {}, CANON_DESIGNS),
    Workload("writes/ldbc", wldbc_q.REGISTRY, _flat_pick, CANON_FORMATS, _pdir("writes", "ldbc"), {},
             CANON_DESIGNS),
    Workload("linkbench", lb_q.REGISTRY, lb_q._pick, LB_FORMATS, _pdir("linkbench"),
             lb_q.VARIANT_REQUIRES, LINKBENCH_DESIGNS),
    Workload("bi", bi_q.REGISTRY, bi_q._pick, BI_FORMATS, _pdir("bi"), bi_q.VARIANT_REQUIRES, CANON_DESIGNS,
             query_requires=bi_q.TWIN_REQUIRES, fused_scan_requires=bi_q.FUSED_SCAN_REQUIRES),
]


from kvevaluator.plans.ir.lowering import lower_ir_steps  # (queries.lower_filter on IR dicts)


def _index_caps(group):
    """The type-index cap a body entering the type filter by the index op needs: the single-
    posting cap where the group's formats hold it, else a type index of any kind (nebula's /
    the chunked layout's whole-type scan, reached by the logical filter's via="index" -- and,
    `on` a label, whatever kind the label's owner has). Empty when the body does not use the op."""
    if not any(s.get("op") == "read_filter_by_label_index" for s in group["body"]["steps"]) or not group["profiles"]:
        return set()
    common = set.intersection(*[caps for _p, caps in group["profiles"]])
    return {TYPE_CAP} if TYPE_CAP in common else {ANY_TYPE_CAP} if ANY_TYPE_CAP in common else set()


def _in_read_caps(group):
    """reverse_adjacency for a body with a read_edge_exists read of dir "in": the read is valid only where the edge
    type's in lists are kept, whatever else selects the body (check_plan_files lints the same rule on the files)."""
    return {"reverse_adjacency"} if any(r.get("dir") == "in" for s in group["body"]["steps"]
                                         if s.get("op") == "read_edge_exists" for r in s.get("reads") or ()) else set()


def _body_caps(group):
    """the caps a selected body's own ops add to its selector"""
    return _index_caps(group) | _in_read_caps(group)


def _sat(sel, caps):
    """Does a format with `caps` select a variant with selector `sel` = (requires, lacks)?"""
    req, lacks = sel
    return set(req) <= caps and not (set(lacks) & caps)


# The validity caps a body's OPS need (the op table's `requires`, inputs/contract/kv_ops.json, less the caps stamped at
# emit; capabilities.VALIDITY_CAPS' evidence, op by op; the fingerprint inventory): the one derivation the
# selector pool trusts beside the picked rules' guard and the author's annotation. The ops-derived validity of candidates.invalid_candidates itself is not fixed at the root.
OPS_VALIDITY = {op: req - STAMPED_AT_EMIT for op, req in OP_REQUIRES.items() if req - STAMPED_AT_EMIT}


def _needed(body, guard, annotated=None):
    """the validity caps this body may claim: the picked rules' guard, what its ops need (OPS_VALIDITY; a label scan
    that carries props needs label_scan_carries_props), and the query author's annotated cap"""
    need = set(guard)
    for st in body.get("steps", []):
        need |= OPS_VALIDITY.get(st["op"], set())
        if st["op"] == "read_vertices_by_label" and st.get("props"):
            need.add("label_scan_carries_props")
    if annotated:
        need.add(annotated)
    return need


def _selectable(group, annotated=None):
    """The caps a positive selector for this group may name: the group's common caps minus the VALIDITY caps its body
    does not need (_needed). A variant's `requires` is read by candidates.invalid_candidates as the body's validity
    claim (requires & VALIDITY_CAPS), so a validity cap that only SEPARATES profiles would mark the body invalid on
    every format lacking the cap although the body runs there (is2's `unsorted_range_index_at_0.1`, a
    read_top_k_by_index body, was refused on every meta cell; 8 variants in all). `lacks` is no validity claim, so it
    stays the fallback separator."""
    from kvevaluator.plans.ir.capabilities import VALIDITY_CAPS
    if not group["profiles"]:                    # the universal default (no declared profile): it names no cap
        return set()
    common = set.intersection(*[caps for _p, caps in group["profiles"]])
    if not _probed_etypes(group["body"]):        # a body that probes no edge never selects on an edge type's caps
        common -= EDGE_CAPS
    return common - (VALIDITY_CAPS - _needed(group["body"], group.get("guard", ()), annotated))


def _requires(group, all_profiles, q, variant_requires, base_requires=()):
    """The selector (requires, lacks) that picks this group's body. The query author's annotation,
    else the minimal distinguishing set (caps every profile in the group has and no other profile
    has, among the caps a selector may name: _selectable); plus the type cap when the body enters the type filter
    by the index. Whether the derived selectors actually ROUTE every profile to its own body is checked globally
    in bundle_for (the first-match order can make a loose annotation safe, so no per-group check is possible);
    a group that captures a foreign profile is tightened there by _hitting_set."""
    common = _selectable(group, variant_requires.get(q))
    if q in variant_requires and variant_requires[q] in common:
        req = {variant_requires[q]}              # the annotated cap, when this group holds it
    else:
        others = set().union(*[caps for p, caps in all_profiles if p not in group["names"]])
        req = common - others
    return (sorted(req | _body_caps(group)), [])


def _tightened(group, all_profiles, q, variant_requires):
    """A group's selector replaced by its minimal hitting set (+ the type cap its body needs);
    loud when none exists -- selection is then not expressible by capability sets."""
    hs = _hitting_set(group, all_profiles, variant_requires.get(q))
    if hs is None:
        raise ValueError(f"{q}: no capability subset distinguishes one variant's formats "
                         f"(selection not expressible by capability sets)")
    req, lacks = hs
    return (sorted(set(req) | _body_caps(group)), sorted(lacks))


def _specialized(group, all_profiles, q, variant_requires):
    """A captured group's selector made MORE SPECIFIC: its minimal hitting set plus the query's
    annotated cap when the group holds it (the variant keeps its story), so it sorts ahead of
    the looser variant that captured its profiles. None when no hitting set exists -- then the
    CAPTOR is the one to tighten (see bundle_for). The case: a format whose caps are a strict
    SUPERSET of another's wants a distinct body (pertype_csr's fused scan over nebula's
    enumerate + re-read on topk10_scan: both hold whole_row_read, only pertype_csr holds
    label_scan_carries_props, and nebula's group can never be tightened away from it)."""
    hs = _hitting_set(group, all_profiles, variant_requires.get(q))
    if hs is None:
        return None
    common = _selectable(group, variant_requires.get(q))
    req, lacks = set(hs[0]), hs[1]
    if q in variant_requires and variant_requires[q] in common:
        req.add(variant_requires[q])
    return (sorted(req | _body_caps(group)), sorted(lacks))


def _hitting_set(group, all_profiles, prefer=None):
    """The smallest selector (requires R, lacks L) that picks this group and NO outside profile:
    R a subset of the group's common caps, L a subset of the caps NO member holds, such that no
    outside profile holds all of R while missing all of L. Positive selectors first (the ic8
    flip: nebula's caps are a strict subset of arango's, so no single cap picks arango alone,
    but {type cap, whole_row_read} does); `lacks` only when no positive set works -- the case a
    cost-driven pick creates when the MORE capable format prefers the plainer body (a negative
    selector). Deterministic: smallest |R| + |L|, then fewest lacks, then sets
    carrying `prefer` (the query's annotated cap), then lexicographic. None when none works."""
    import itertools
    common = _selectable(group, prefer)          # a positive selector never claims a validity cap the body lacks
    outside = [caps for p, caps in all_profiles if p not in group["names"]]
    universe = set().union(*[caps for _p, caps in all_profiles])
    lackable = universe - set().union(*[caps for _p, caps in group["profiles"]])
    if not _probed_etypes(group["body"]):        # ... nor on the lack of one (its selector stays what it was)
        lackable -= EDGE_CAPS
    for size in range(1, len(common) + len(lackable) + 1):
        combos = []
        for nl in range(0, min(size, len(lackable)) + 1):     # fewest lacks first
            nr = size - nl
            if nr > len(common):
                continue
            for R in itertools.combinations(sorted(common), nr):
                for L in itertools.combinations(sorted(lackable), nl):
                    if not any(set(R) <= caps and not (set(L) & caps) for caps in outside):
                        combos.append((nl, 0 if prefer in R else 1, R, L))
            if combos:
                break
        if combos:
            nl, _pref, R, L = min(combos)
            return (sorted(R), sorted(L))
    return None


def _for(group, designs, requires=(), full_caps=None):
    """The human-readable format list: a format whose body is the same under every design is
    named once; one that differs is named per design (dgraph@no_type_index). With `full_caps` (the
    caps runners select with, guards included), a profile lacking one of the validity caps of
    `requires` (a twin's TWIN_REQUIRES, e.g. label_rows_one_run) is left out: it would refuse the
    body or answer wrong, so the plan must not claim it."""
    valid = validity_caps(requires)
    by_fmt = {}
    for (fmt, design), _caps in group["profiles"]:
        if full_caps is not None and not valid <= full_caps[(fmt, design)]:
            continue
        by_fmt.setdefault(fmt, []).append(design)
    names = []
    for fmt, ds in by_fmt.items():
        names.append(fmt if len(ds) == len(designs) else ", ".join(f"{fmt}@{d}" for d in ds))
    return ", ".join(names)


def lowered_body(entry, pick, q, fmt, schema, design=None, scale=None):
    """The (steps, output) body production resolves for (q, fmt) on `schema` (under `design`,
    the schema's name, for a design-aware searched pick; at `scale`, a per-SF pin) -- the reference
    the drift guard checks a bundle's selection against. None for a baked builder."""
    raw = call_pick(pick, entry, fmt, design, scale)({})
    if isinstance(raw, dict) and "steps" in raw:         # a PRE-LOWERED body (a hand override's candidate body)
        return {"steps": raw["steps"], "output": raw["output"]}
    if not isinstance(raw, Plan):
        return None
    ir = plan_to_ir(raw, name=q)
    return {"steps": lower_ir_steps(ir["steps"], (caps_for(fmt, schema) - GUARD_CAPS), edge_caps_of(fmt, schema)), "output": ir["output"]}


def _ir_of(raw, q):
    """(ir, pre_lowered): a builder's product as IR -- a Plan serialized (to lower per caps), or a pre-lowered body
    (a hand override's candidate body: `steps` already lowered under the caps of the format it was emitted for; it is
    taken as is, so a format can run a body its own caps would never lower to)"""
    if isinstance(raw, dict) and "steps" in raw:
        return raw, True
    return plan_to_ir(raw, name=q), False


# the ops that read a property index over `labels` (the per_type_index rule of _on_labels): the ops that require it
INDEX_READ_OPS = frozenset(op for op, req in OP_REQUIRES.items() if "per_type_index" in req)
INDEX_CAPS = frozenset({"per_type_index", "sorted_range_index", "unsorted_range_index"})   # checked on those labels
EDGE_CAPS = frozenset({"reverse_adjacency", "adjacency_lists"})   # per EDGE TYPE: checked on the edge types a body probes


def _probed_etypes(body):
    """the edge types a body's read_edge_exists steps probe (the names its edge-type caps are checked `on`)"""
    return {s["etype"] for s in body.get("steps", []) if s.get("op") == "read_edge_exists"}


def _on_labels(req, body, fallback_body):
    """`"on": {cap: [label,...]}` — which TYPE each required capability is exploited on, derived
    from the variant's own steps, so select_variant can check the cap against that type's OWNING
    format (assignment-aware: a store with a layout per type runs the index / whole-row variant exactly when
    the owner supports it; a single-format store resolves every label to its own caps, unchanged).
    Rules: the type cap marks the labels its index-filter steps name; whole_row_read marks the
    labels of the vertex reads that DIFFER from the fallback body (the one-phase reads the
    variant widened) -- and, when none differ (is7's materialize body: the same row reads in another plan shape), the
    labels of EVERY row read the body does, since the capability is a property of one label's props layout and a
    store-wide check wrongly loses it to a Split label the body never reads; per_type_index
    and the range caps mark the labels its index reads name (the
    index must be the type restriction / exact / bucketed on exactly those types); label_scan_carries_props marks the
    labels its fused scans name (read_vertices_by_label with props: the op reads that label's own layout); the edge-type
    caps (reverse_adjacency, adjacency_lists) mark the EDGE TYPES its read_edge_exists steps probe. A cap with
    no rule stays un-annotated -> the format-global check."""
    fb = {s["name"]: s for s in fallback_body["steps"]}
    on = {}
    for cap in req:
        labels = set()
        for s in body["steps"]:
            ls = s.get("label")
            ls = set(ls) if isinstance(ls, list) else {ls} if ls else set()
            if cap in (TYPE_CAP, ANY_TYPE_CAP) and s.get("op") == "read_filter_by_label_index":
                labels |= ls
            if (cap == "whole_row_read" and s.get("op") == "read_vertex_props"
                    and fb.get(s["name"]) != s):
                labels |= ls
            # ... and the same widened read done as a row scan with a keep set (a twin's swap of that step)
            if (cap == "whole_row_read" and s.get("op") == "read_vertices_by_label" and s.get("keep") is not None
                    and fb.get(s["name"]) != s):
                labels |= ls
            if cap in INDEX_CAPS and s.get("op") in INDEX_READ_OPS:
                labels |= set(s.get("labels") or ())
            if cap == "label_scan_carries_props" and s.get("op") == "read_vertices_by_label" and s.get("props"):
                labels |= ls
            if cap in EDGE_CAPS and s.get("op") == "read_edge_exists":
                labels.add(s["etype"])
        if cap == "whole_row_read" and not labels:      # none widened: every row read of the body (a fused scan reads rows too)
            for s in body["steps"]:
                if s.get("op") == "read_vertex_props" or (s.get("op") == "read_vertices_by_label" and (s.get("keep") is not None or s.get("props"))):
                    ls = s.get("label")
                    labels |= set(ls) if isinstance(ls, list) else {ls} if ls else set()
        if labels:
            on[cap] = sorted(labels)
    return on


# GUARD caps: a correctness condition a query NAMES (its registry's TWIN_REQUIRES, stamped as the bundle's requires),
# never a fingerprint -- stripped from every profile before selectors are derived, so adding one to a format leaves
# every other bundle byte-identical (label_scoped_props: the fused twins' "a props read under another label misses").
# label_rows_one_run: "one scan of the label's rows may run" (a twin with a read_vertices_by_label that carries props
# on a row layout, or a keep set, names it); held by every props layout of meta, so as a fingerprint it would only
# move other bundles' derived selectors (it did once: hop1_posts_props took it as a `lacks`).
# whole_label_scan_by_default: the selector of the microbench whole-label scans' fused body (FUSED_SCAN_REQUIRES), held
# where label_rows_one_run is except on arangodb; stripped for the same reason, named only through fused_scan_requires.
GUARD_CAPS = frozenset({"label_scoped_props", "label_rows_one_run", "whole_label_scan_by_default"})
FUSED_SCAN_CAP = "label_scan_carries_props"      # what the derivation names for a body with a fused label scan


# A registry entry may KEEP bodies no format selects: {"_keep": {name: (format, builder)}} -- each is lowered with that
# format's capabilities and emitted under `name` in the bundle's plans with no variant, so a re-pin that moves every
# format off a body does not drop it from the candidates the meta grid measures (grid_bodies forces every body).
KEEP_KEY = "_keep"


def _kept_bodies(q, entry, designs):
    """{name: lowered body} of an entry's kept bodies (see KEEP_KEY)"""
    out = {}
    for name, (fmt, builder) in ((entry.get(KEEP_KEY) or {}) if isinstance(entry, dict) else {}).items():
        design, schema = next((d, s) for d, s in designs.items() if resolves(fmt, d))
        ir = plan_to_ir(builder({}), name=q)
        out[name] = {"steps": lower_ir_steps(ir["steps"], (caps_for(fmt, schema) - GUARD_CAPS), edge_caps_of(fmt, schema)), "output": ir["output"]}
    return out


def bundle_for(q, entry, pick, formats, variant_requires, designs=CANON_DESIGNS,
               fallback_format="dgraph", base_requires=(), fused_scan_requires=None):
    """The one IR object for query q: a bare plan (all profiles agree) or a capability bundle.
    None if any format resolves to a baked/lambda plan (skipped, like an engine plan).
    `fused_scan_requires`: the cap this query's fused-scan variant names in place of label_scan_carries_props
    (whole_label_scan_by_default: the scan is the query's plan on more than the Chunked layouts). The
    selectors are derived as before, on label_scan_carries_props; the name is swapped in the emitted variant."""
    params, merged_let = None, {}

    def derive_caps(fmt, schema):
        # the caps the selectors are derived on. For a query with fused_scan_requires, a holder of that cap counts as a
        # holder of label_scan_carries_props here (a format may hold the wider cap without it and run the fused body:
        # ArangoDB once did, before its default was held), so the derivation and the emitted bundle are what
        # they were; the emitted variant names the wider cap
        caps = caps_for(fmt, schema)
        return (caps - GUARD_CAPS) | ({FUSED_SCAN_CAP} if fused_scan_requires and fused_scan_requires in caps else set())
    groups = []                                  # [{names, profiles, body, key}]
    all_profiles = []                            # [((fmt, design), caps)]
    body_at = {}                                 # (fmt, design) -> {None: body, tag: body}: the per-SF pins
    guard_at = {}                                # (fmt, design) -> its body's validity caps
    full_caps = {}                               # (fmt, design) -> caps_for as the runners select with (guards incl.)
    probed = set()                               # the edge types the bodies' read_edge_exists steps probe
    universal = {}                               # design -> the fallback format's guard-free body (emit: build.guard_free)
    for fmt in formats:
        builder = call_pick(pick, entry, fmt, next(iter(designs)))
        if builder is None:
            # not declared for this format (subset family: ic3's dated read is nebula-only).
            # Its capability profiles still enter all_profiles so the DECLARED variants'
            # requires distinguish against it -- otherwise a single-variant subset bundle
            # would carry only base_requires and an undeclared-but-capable format (another
            # format) would select a plan that was never meant for it.
            for design, schema in designs.items():
                if resolves(fmt, design):
                    all_profiles.append(((fmt, design), derive_caps(fmt, schema)))
            continue
        # the body is resolved PER DESIGN: a searched pick may hold a rule that is legal on one
        # design and not another (nebula's whole-type-scan filter_index needs the type index the
        # no_type_index design lacks), so each design gets the pick resolved for it; a hand
        # pick returns the same Plan for every design and its lowering happens per caps below
        for design, schema in designs.items():
            if not resolves(fmt, design):
                continue
            try:
                builder = call_pick(pick, entry, fmt, design)
                # the validity caps the picked rules need (a searched pick's filter_fused: label_scoped_props),
                # stamped into this body's selector below so no format lacking them can select it
                guard = frozenset(getattr(builder, "validity_caps", ()))
                raw = builder({})                # a Plan builds from {} (holes)
            except Exception:  # -- baked builder (reads args / entry_range_dgraph)
                return None                      # can't build from {}; skip it like a lambda
            if not isinstance(raw, Plan) and not (isinstance(raw, dict) and "steps" in raw):
                return None
            ir, pre = _ir_of(raw, q)             # may raise NeedsExpr
            if params is None:
                params = ir["params"]
            elif ir["params"] != params:
                raise ValueError(f"{q}: params differ across formats/designs at {fmt}@{design}")
            # variants may need DIFFERENT derived values (e.g. entry_range's dgraph day-bucket
            # walk), so the bundle carries the UNION of the lets -- a shared name must have a
            # shared definition, and a variant only references the ones it needs.
            for k, v in (ir.get("let") or {}).items():
                if k in merged_let and merged_let[k] != v:
                    raise ValueError(f"{q}: let {k!r} differs across format plans")
                merged_let[k] = v
            caps = derive_caps(fmt, schema)
            profile = (fmt, design)
            all_profiles.append((profile, caps))
            body = {"steps": ir["steps"] if pre else lower_ir_steps(ir["steps"], caps, edge_caps_of(fmt, schema)),
                    "output": ir["output"]}
            probed |= _probed_etypes(body)
            if fmt == fallback_format and guard and getattr(builder, "guard_free", None) is not None:
                # the picked body needs a validity cap: its guard-free twin, lowered with NO caps, is what a store
                # lacking the cap runs (the bundle's default below)
                uir, _upre = _ir_of(builder.guard_free({}), q)
                universal[design] = {"steps": lower_ir_steps(uir["steps"], set(), lambda etype: set()),
                                     "output": uir["output"]}
            key = json.dumps(body, sort_keys=True)
            for g in groups:
                if g["key"] == key:
                    g["names"].add(profile)
                    g["profiles"].append((profile, caps))
                    g["guard"] |= guard
                    break
            else:
                groups.append({"names": {profile}, "profiles": [(profile, caps)], "body": body, "key": key,
                               "guard": set(guard)})
            # the per-SF pins of this cell (builder.scales): the body at each tagged scale
            body_at[profile], guard_at[profile], full_caps[profile] = {None: body}, set(guard), caps_for(fmt, schema)
            for tag in getattr(builder, "scales", []):
                tb = call_pick(pick, entry, fmt, design, tag)
                tir, tpre = _ir_of(tb({}), q)
                if tir.get("params", params) != params:
                    raise ValueError(f"{q}: params differ at {fmt}@{design} scale {tag}")
                for k, v in (tir.get("let") or {}).items():
                    if k in merged_let and merged_let[k] != v:
                        raise ValueError(f"{q}: let {k!r} differs across format plans (scale {tag})")
                    merged_let[k] = v
                body_at[profile][tag] = {"steps": tir["steps"] if tpre else lower_ir_steps(tir["steps"], caps, edge_caps_of(fmt, schema)),
                                         "output": tir["output"]}
                if frozenset(getattr(tb, "validity_caps", ())) - set(guard):
                    raise ValueError(f"{q}: the scale-{tag} body of {fmt}@{design} needs validity caps its default "
                                     f"body does not ({sorted(frozenset(getattr(tb, 'validity_caps', ())) - set(guard))})")

    if not groups:
        return None                              # no format declares this query in the family
    if probed:
        # a body probes edges: the caps of THOSE edge types (reverse_adjacency, adjacency_lists) join each profile's
        # set for this bundle, so a selector can name them; the variant checks them `on` the edge types (_on_labels).
        # A bundle with no read_edge_exists is untouched.
        for (fmt, design), caps in all_profiles:
            caps |= set().union(*[edge_caps_of(fmt, designs[design])(e) for e in sorted(probed)])
        for (fmt, design), caps in full_caps.items():
            caps |= set().union(*[edge_caps_of(fmt, designs[design])(e) for e in sorted(probed)])
    tags = sorted({t for per in body_at.values() for t in per if t is not None})
    declared = set().union(*[g["names"] for g in groups])
    undeclared = [(pr, caps) for pr, caps in all_profiles if pr not in declared]

    def guard_leak(req, body_for):
        # emit-time twin of check_plan_files' LEAKY SELECT: a subset bundle's requires must
        # NOT be satisfiable by an undeclared format's caps -- it would select a plan never
        # meant for it (and possibly an op it cannot serve). VARIANT_REQUIRES is the
        # annotation to disambiguate when the capability sets cannot.
        for pr, caps in undeclared:
            if _sat(req, caps):
                raise ValueError(f"{q}: undeclared profile {pr} satisfies selector {req} "
                                 f"(variant for {body_for}) -- annotate VARIANT_REQUIRES or "
                                 f"declare the format")
    if len(groups) == 1 and not tags:            # a per-SF pin needs the bundle path (a tagged variant + the default)
        if (entry.get(KEEP_KEY) if isinstance(entry, dict) else None):
            raise ValueError(f"{q}: kept bodies need a multi-variant bundle (one body for every format here)")
        if base_requires or undeclared or groups[0]["guard"]:
            # every plan of a guarded family carries the requires, even a single-body one; a
            # SUBSET bundle also carries the caps that distinguish the declared format from
            # the family's undeclared ones (e.g. whole_row_read), so an
            # undeclared-but-capable format falls back instead of selecting a foreign plan
            req = sorted(set(_requires(groups[0], all_profiles, q, variant_requires)[0])
                         | set(base_requires) | groups[0]["guard"])
            guard_leak((req, []), _for(groups[0], designs))
            d = {"plan": q, "params": params}
            if merged_let:
                d["let"] = merged_let
            d["variants"] = [{"requires": req, "plan": "default",
                              "for": _for(groups[0], designs, req, full_caps)}]
            d["plans"] = {"default": groups[0]["body"]}
            return d
        d = {"plan": q, "for": "all formats", "params": params}
        if merged_let:
            d["let"] = merged_let
        d.update(groups[0]["body"])
        return d
    # Every group's selector is computed the same way; the FALLBACK (requires []) is the one
    # group no capability distinguishes -- the universal body (a probe-side type filter, or the
    # dgraph two-phase shape). When every group is distinguishable, the fallback format's body
    # under its weakest design is the fallback, as before (ic1: dgraph's shape is the default).
    # `reqs` is the DISTINGUISHING set only -- the ambiguity error and the fallback pick both
    # depend on emptiness, so the family-wide base_requires is stamped at emit time below,
    # never here (stamping first would disarm the "two indistinguishable variants" raise)
    reqs = {id(g): _requires(g, all_profiles, q, variant_requires) for g in groups}
    fb_design = min(designs, key=lambda dn: len(caps_for(fallback_format, designs[dn])))
    # The fallback is selected by any store, so it must need nothing. When the fallback format's own body needs a
    # validity cap (is7: the two-list edge probe needs the in lists), the bundle's default is that body's guard-free
    # twin (a group no declared profile is in), and the guarded body becomes an ordinary variant with its selector.
    fbg = next((g for g in groups if (fallback_format, fb_design) in g["names"]), None)
    demoted = None                               # the guarded body that was the default before it named its guard
    if fbg is not None and fbg["guard"] and fb_design in universal:
        ukey = json.dumps(universal[fb_design], sort_keys=True)
        if not any(g["key"] == ukey for g in groups):
            anyone = {"names": set(), "profiles": [], "body": universal[fb_design], "key": ukey, "guard": set()}
            groups.append(anyone)
            reqs[id(anyone)] = ([], [])
            demoted = fbg
    for attempt in (0, 1, 2):
        empties = [g for g in groups if not (reqs[id(g)][0] or reqs[id(g)][1])]
        if len(empties) > 1:
            # two no-discriminator groups: keep the fallback format's as the universal
            # default, tighten the others by a minimal hitting set
            # (the profile-less universal default, when the bundle has one, is the one to keep: every declared
            # group then gets a selector of its own)
            keep = next((g for g in empties if not g["profiles"]),
                        next((g for g in empties if (fallback_format, fb_design) in g["names"]), empties[0]))
            for g in empties:
                if g is not keep:
                    reqs[id(g)] = _tightened(g, all_profiles, q, variant_requires)
            empties = [keep]
        if empties:
            fallback = empties[0]
        else:
            fallback = next(g for g in groups if (fallback_format, fb_design) in g["names"])
        # first-match order, exactly as select_variant walks it (stable: most specific first)
        ordered = sorted([g for g in groups if g is not fallback],
                         key=lambda g: -(len(reqs[id(g)][0]) + len(reqs[id(g)][1]))) + [fallback]
        # GLOBAL routing check: every profile must reach its own body. A loose req can be
        # saved by an earlier more-specific variant (ic10's whole_row_read annotation), so
        # only a group that actually CAPTURES a foreign profile is tightened -- the case a
        # cost-driven pick creates when it separates subset-ordered cap sets (the ic8 flip:
        # nebula < arango, so arango's one_phase needs {type cap, whole_row_read}).
        captured = set()                 # captor groups to tighten
        specialized = {}                 # captured groups that can instead be made more specific
        for profile, caps in all_profiles:
            mine = next(g for g in groups if profile in g["names"])
            got = next(g for g in ordered if _sat(reqs[id(g)], caps))
            if got is not mine:
                # the captured group first: when its formats' caps are a strict superset of
                # the captor's, the captor cannot be tightened at all, but the captured group
                # can be made more specific and sort ahead (the fused-scan case). Otherwise
                # the captor is the loose one (the ic8 flip). The FALLBACK's selector is never
                # consulted (it is the last variant), so a captured fallback always tightens
                # the captor -- specializing it would change nothing (ic8 with pertype_csr
                # in the fallback: arango's annotated whole_row_read captured them; the fix
                # is arango's own index_entries_by_docid, not a selector on the default).
                sp = None if mine is fallback else _specialized(mine, all_profiles, q, variant_requires)
                if sp is not None and sp != reqs[id(mine)]:
                    specialized[id(mine)] = sp
                else:
                    captured.add(id(got))
        if not captured and not specialized:
            break
        if attempt == 2:
            raise ValueError(f"{q}: selection not expressible by capability sets")
        carved = False
        for g in groups:
            if id(g) in specialized:
                reqs[id(g)] = specialized[id(g)]
            elif id(g) in captured:
                hs = _hitting_set(g, all_profiles, variant_requires.get(q))
                if hs is None:
                    # the captor cannot be tightened: a FALLBACK member whose caps are a superset of the captor's
                    # formats' (a format pinned to another's body while a variant it would also select wants a cap it
                    # holds). Carve that
                    # member out of the fallback into its own group with the SAME body and a selector of its own,
                    # sorted ahead; the fallback keeps its `requires: []` for everyone else.
                    stolen = [(pr, caps) for pr, caps in fallback["profiles"]
                              if _sat(reqs[id(g)], caps) and pr in fallback["names"]]
                    if not stolen:
                        raise ValueError(f"{q}: no capability subset distinguishes one variant's formats "
                                         f"(selection not expressible by capability sets)")
                    carve = {"names": {pr for pr, _c in stolen}, "profiles": stolen, "body": fallback["body"],
                             "key": fallback["key"], "guard": set(fallback["guard"])}
                    sel = _hitting_set(carve, all_profiles, variant_requires.get(q))
                    if sel is None:
                        raise ValueError(f"{q}: no capability subset distinguishes one variant's formats "
                                         f"(selection not expressible by capability sets)")
                    fallback["profiles"] = [(pr, c) for pr, c in fallback["profiles"] if pr not in carve["names"]]
                    fallback["names"] -= carve["names"]
                    groups.insert(groups.index(fallback), carve)
                    reqs[id(carve)] = (sorted(set(sel[0]) | _body_caps(carve)), sorted(sel[1]))
                    carved = True
                else:
                    reqs[id(g)] = (sorted(set(hs[0]) | _body_caps(g)), sorted(hs[1]))
        if carved:
            # the carved group needs its own ordering slot: re-derive the order on the next attempt
            continue
    variants, plans = [], {}
    for g in groups:                             # plans dict keeps groups order (file bytes)
        if g is fallback:
            continue
        req = sorted(set(reqs[id(g)][0]) | set(base_requires) | g["guard"])
        lacks = list(reqs[id(g)][1])
        guard_leak((req, lacks), _for(g, designs))
        name = "_".join(req) + ("_not_" + "_".join(lacks) if lacks else "")
        if name in plans:
            raise ValueError(f"{q}: two variants would both be selected by {req} / lacks {lacks}")
        v = {"requires": req, "plan": name, "for": _for(g, designs, req, full_caps)}
        if lacks:
            v["lacks"] = lacks                    # the negative selector (bundle.py select_variant)
        on = _on_labels(req, g["body"], fallback["body"])
        if fused_scan_requires and FUSED_SCAN_CAP in req:
            # every holder of the derived cap holds the wider one, and no declared format outside the group does
            # (_check_routing below, on the runners' full capability sets)
            swap = lambda c: fused_scan_requires if c == FUSED_SCAN_CAP else c
            req = sorted(swap(c) for c in req)
            on = {swap(c): labels for c, labels in on.items()}
            v["requires"], v["plan"] = req, "_".join(req) + ("_not_" + "_".join(lacks) if lacks else "")
            name = v["plan"]
            if name in plans:
                raise ValueError(f"{q}: two variants would both be selected by {req} / lacks {lacks}")
        if on:
            v["on"] = on                         # per-label check: assignment-aware selection
        if g is demoted:
            v["_last"] = True
        variants.append(v)
        plans[name] = g["body"]
    # select_variant takes the FIRST variant whose requirements the format meets: most specific first
    variants.sort(key=lambda v: -(len(v["requires"]) + len(v.get("lacks", []))))
    # ... except the body that was the bundle's default until it named its guard: it keeps the LAST word among the
    # variants (a store another variant also fits selects that one, as it did when this body was the fallback)
    variants.sort(key=lambda v: bool(v.pop("_last", False)))
    # the fallback keeps the FAMILY requires (base_requires): a guarded family's default
    # still needs the family's capability -- it must never be selected by a format without it
    if fallback["guard"]:                        # the fallback is selected by any format: it must need nothing
        raise ValueError(f"{q}: the fallback body needs validity caps {sorted(fallback['guard'])} -- a format "
                         f"lacking them would select it")
    fb_req = sorted(base_requires)
    guard_leak((fb_req, []), _for(fallback, designs))
    # no profile at all: "any other store"; profiles, none of which can run it (validity caps): "" (only the meta cells
    # whose caps fit run it; no named format claims it)
    fb_for = _for(fallback, designs, fb_req, full_caps) if fallback["profiles"] else "any other store"
    default_entry = {"requires": fb_req, "plan": "default", "for": fb_for}
    # The default probes the same edge type as a variant selected by adjacency_lists (the two-list probe): on a store
    # that fits THAT variant the default is measured and reported, never picked (`measure_only_where`: the grid turns
    # it into the run rows' measure_only flag, grid_capture.measure_only_on_cell). Its gain there depends on the sampled
    # pairs, not on the layout (the plan-change rule; sizes over 35 params: optimizer/rules.py MEMBERSHIP_RULES). Both
    # selectors ignore the key.
    lists_variant = next((v["plan"] for v in variants
                          if set(v.get("on", {}).get("adjacency_lists", ())) & _probed_etypes(fallback["body"])), None)
    if lists_variant:
        default_entry["measure_only_where"] = lists_variant
    variants.append(default_entry)
    plans["default"] = fallback["body"]
    # per-SF pins: at each tagged scale, the profiles whose body CHANGES there are grouped by that body and each group
    # gets a `scales`-tagged variant whose selector separates it from EVERY other profile (an unchanged profile with the
    # same body is not a member: it keeps selecting its untagged variant, so a scale-blind caller still succeeds for it).
    # Tagged variants come FIRST (first fit; planir/bundle.py select_variant skips them at any other scale).
    tagged_variants = []
    for tag in tags:
        groups_t = []
        for profile, caps in all_profiles:
            if profile not in body_at or body_at[profile].get(tag, body_at[profile][None]) == body_at[profile][None]:
                continue                         # undeclared here, or its body does not change at this scale: the
                                                 # untagged variants serve it (a scale-blind caller must still succeed)
            body = body_at[profile][tag]
            key = json.dumps(body, sort_keys=True)
            for g in groups_t:
                if g["key"] == key:
                    g["names"].add(profile); g["profiles"].append((profile, caps)); g["guard"] |= guard_at[profile]
                    break
            else:
                groups_t.append({"names": {profile}, "profiles": [(profile, caps)], "body": body, "key": key,
                                 "guard": set(guard_at[profile])})
        for g in groups_t:
            req, lacks = _requires(g, all_profiles, q, variant_requires)
            outsiders = [(pr, caps) for pr, caps in all_profiles if pr not in g["names"]]
            if any(_sat((req, lacks), caps) for _pr, caps in outsiders):
                # the plain distinguishing set does not separate (it may hold only the body's index cap): the minimal
                # hitting set, positive first, then lacks -- raises when no capability subset works
                req, lacks = _tightened(g, all_profiles, q, variant_requires)
            # a tagged variant is checked before every untagged one, so it must pick NO other profile at this scale
            for pr, caps in outsiders:
                if _sat((req, lacks), caps):
                    raise ValueError(f"{q}: the scale-{tag} variant for {_for(g, designs)} would also select {pr} "
                                     f"(selection not expressible by capability sets)")
            req = sorted(set(req) | set(base_requires) | g["guard"])
            name = ("_".join(req) if req else "default") + ("_not_" + "_".join(lacks) if lacks else "") + f"_at_{tag}"
            if name in plans:
                raise ValueError(f"{q}: two scale-tagged variants collide on {name!r}")
            v = {"requires": req, "plan": name, "scales": [tag], "for": _for(g, designs, req, full_caps)}
            if lacks:
                v["lacks"] = sorted(lacks)
            on = _on_labels(req, g["body"], fallback["body"])
            if on:
                v["on"] = on
            tagged_variants.append(v)
            plans[name] = g["body"]
    # one variant per BODY across tags: the same body pinned at several smaller scales (at 0.1 and 3) is one tagged
    # variant listing both ("..._at_0.1+3"), not a copy per tag
    merged = []
    for v in tagged_variants:
        twin = next((m for m in merged if plans[m["plan"]] == plans[v["plan"]] and m["requires"] == v["requires"]
                     and m.get("lacks") == v.get("lacks") and m.get("on") == v.get("on")), None)
        if twin is None:
            merged.append(v)
            continue
        del plans[v["plan"]]
        twin["scales"] = sorted(set(twin["scales"]) | set(v["scales"]))
        new_name = twin["plan"].rsplit("_at_", 1)[0] + "_at_" + "+".join(twin["scales"])
        plans[new_name] = plans.pop(twin["plan"])
        twin["plan"] = new_name
    tagged_variants = merged
    tagged_variants.sort(key=lambda v: -(len(v["requires"]) + len(v.get("lacks", []))))
    variants = tagged_variants + variants
    for name, body in _kept_bodies(q, entry, designs).items():
        if name in plans:
            raise ValueError(f"{q}: kept body {name!r} collides with a selected variant's name")
        if body not in plans.values():           # a body some format still selects needs no keeping
            plans[name] = body
    # one name per body: a body two variants select (an untagged one and a scale-tagged one) is written once, and both
    # variants name it. The name kept: an untagged one before a scale-tagged one, then the first sorted
    kept = {}
    for name in sorted(plans, key=lambda n: ("_at_" in n, n)):
        kept.setdefault(json.dumps(plans[name], sort_keys=True), name)
    rename = {n: kept[json.dumps(b, sort_keys=True)] for n, b in plans.items()}
    rename = {n: k for n, k in rename.items() if n != k}
    for v in variants:
        v["plan"] = rename.get(v["plan"], v["plan"])
        if v.get("measure_only_where") in rename:
            v["measure_only_where"] = rename[v["measure_only_where"]]
    plans = {n: b for n, b in plans.items() if n not in rename}
    d = {"plan": q, "params": params}
    if merged_let:
        d["let"] = merged_let
    d["variants"] = variants
    d["plans"] = plans
    _check_routing(q, d, full_caps, body_at, tags)
    return d


def _check_routing(q, d, full_caps, body_at, tags):
    """The emitted bundle routes every declared profile to its own body at every scale (the default and each tagged
    one), through the SAME selector the runners use (planir/bundle.py select_variant); a profile with a per-SF pin
    must refuse a scale-blind selection (never a guess). The emit-time twin of check_plan_files' selection check."""
    from kvevaluator.plans.ir.bundle import NoVariant, select_variant
    # a GUARD cap every variant requires (a twin's TWIN_REQUIRES, e.g. label_rows_one_run: the op does not run without
    # it): a profile that lacks it has no variant here, and must be refused -- its body stays in the bundle for the
    # stores that hold the guard and otherwise select as this profile does (the configurable format's cells)
    variants = d.get("variants") or []
    guard = GUARD_CAPS & set.intersection(*[set(v.get("requires") or ()) for v in variants]) if variants else set()
    for profile, caps in full_caps.items():
        if guard - set(caps):
            try:
                select_variant(d, caps, scale=tags[-1] if tags else None)
            except NoVariant:
                continue                         # the designed refusal: the query does not exist for this profile
            raise ValueError(f"{q}: {profile} lacks {sorted(guard - set(caps))} but selects a variant")
        for scale in [None] + tags:
            want = body_at[profile].get(scale, body_at[profile][None]) if scale else body_at[profile][None]
            if scale is None and len(body_at[profile]) > 1:
                try:
                    select_variant(d, caps, scale=None)
                except ValueError:
                    continue                     # the designed refusal: this cell's pin differs by scale
                raise ValueError(f"{q}: {profile} pins per scale but a scale-blind selection did not refuse")
            got = d["plans"][select_variant(d, caps, scale=scale)]
            if got != want:
                raise ValueError(f"{q}: {profile} at scale {scale} selects another body than its pick "
                                 f"(selection not expressible by capability sets)")


def _generated(w):
    """(read queries, their twins) of a workload whose plan files plan_choice/generate.py writes, else two empty sets"""
    if w.name not in _W.names(kind="read", dataset="ldbc"):     # the workloads plan_choice/generate.py writes
        return set(), set()
    from kvevaluator.plan_choice.candidates import extra_queries, workload_queries
    qs, alts = workload_queries(w.name)
    return set(qs) | set(extra_queries(w.name)), set(alts)


def table_name(w):
    """the workloads table's name of a dumper family: ldbc/* is LDBC Interactive's, writes/* the writes'"""
    head = w.name.split("/")[0]
    return {"ldbc": "interactive"}.get(head, head)


def iter_workload(w):
    """Yield (query_name, ir_text) for one workload, in file order. A read query's file comes from plan_choice/generate.py
    (every candidate body); its twins have no file of their own (their bodies are in the query's)."""
    from kvevaluator.plan_choice import generate
    gen_q, gen_twins = _generated(w)
    for q in sorted(w.registry):
        if q in gen_q:
            yield q, json.dumps(generate.plan_file(w.name, q), indent=2) + "\n"
            continue
        if q in gen_twins:
            continue
        try:
            ir = bundle_for(q, w.registry[q], pick_for(w, q), w.formats, w.variant_requires,
                            w.designs, w.fallback_format,
                            base_requires=w.base_requires + tuple(w.query_requires.get(q, ())),
                            fused_scan_requires=w.fused_scan_requires.get(q))
        except NeedsExpr:
            continue
        if ir is None:
            continue
        yield q, json.dumps(_W.with_query_fields(table_name(w), q, with_property_index_requires(ir)), indent=2) + "\n"


def iter_all():
    """Yield (plans_dir, query_name, ir_text) across ALL workloads -- for the drift guard."""
    for w in WORKLOADS:
        for q, text in iter_workload(w):
            yield w.plans_dir, q, text


def main():
    import argparse
    ap = argparse.ArgumentParser(description="regenerate the committed plan IR files (all workloads by default)")
    ap.add_argument("--family", dest="workload", action="append", default=None, choices=[w.name for w in WORKLOADS],
                    help="only this workload's plans (repeatable), e.g. --family linkbench")
    ap.add_argument("--out-root", default=None,
                    help="write the plans dirs under this root instead of the tracked workloads/<workload>/plans/")
    a = ap.parse_args()
    only = a.workload
    total = 0
    for w in WORKLOADS:
        if only and w.name not in only:
            continue
        plans_dir = plans_dir_in(w, a.out_root)
        n = 0
        for q, text in iter_workload(w):
            os.makedirs(plans_dir, exist_ok=True)   # on the first file: a family whose builders
            with open(os.path.join(plans_dir, f"{q}.json"), "w") as f:   # are all baked leaves no dir
                f.write(text)
            n += 1
            ir = json.loads(text)
            kind = f"BUNDLE {[v['plan'] for v in ir['variants']]}" if "variants" in ir else "plan"
            print(f"  [{w.name}] {q}.json  ({kind})")
        print(f"  -> {n} IR files in {plans_dir}\n")
        total += n
    print(f"  {total} IR files across {len(only) if only else len(WORKLOADS)} workloads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
