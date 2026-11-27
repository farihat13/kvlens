"""The JOB workload's query registry: 113 queries, each a bundle of bodies generated from its spec (../specs) by
../generator.py. A body is named by its entry: `enter_<alias>_by_<index|scan>`, with `_outputs_early` for the twin
that reads output-only properties with the predicate's read, or `enter_<alias>_by_label_run` (the label's own key run,
valid where the store has label_rows_one_run). The plain scan (`enter_<alias>_by_scan`) is offered only where the entry
label lacks whole_label_scan_by_default (the engine formats; never a grid cell): see SCAN_DOMINATED. Which aliases a query is entered at: the 3 with the
fewest rows passing their own tests in the full data (../entry_counts_full.csv, from the reference), plus the smallest
one an index serves when none of the three is. The first body is the one a store runs unless a capture picks another.
"""
import csv
import json
import os
import re

from kvevaluator.workloads.job import generator
from kvevaluator.workloads.job.pattern import Pattern
from kvevaluator.plans.ir.serialize import plan_to_ir
from kvevaluator.paths import ROOT

_JOB = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "job")
ENTRIES = 3
RUN = "_by_label_run"
SCAN = "_by_scan"            # the plain scan: the label's vid list, then a read per vertex (outputs late)
SCAN_CAP = "whole_label_scan_by_default"
# SCAN_DOMINATED: where the entry label holds SCAN_CAP, the label-run body is never dearer than the
# plain scan in the pick measure, wops_bytes4k (G + 1.1 S + 0.01 N + read_bytes / 4096): 16,308 of 16,308 (layout,
# query, alias) pairs at imdb full, 12 measured and 37 composed layouts, closest run / scan = 0.999 (job10c, alias cn,
# props_chunked_c256k_adj_split). Not by construction: the run crosses every row of the label, so its N and bytes can
# be higher (worst 5.6x NEXTs, job9b n; 3.8x bytes, job16a t, on props_split_adj_condensed); the per-vertex GETs it
# saves decide it. Re-checked on any capture tree by tests/gates/check_job_scan_dominated.py


def _natural(name):
    m = re.match(r"job(\d+)(\w*)", name)
    return int(m.group(1)), m.group(2)


SPECS = {n[:-5]: json.load(open(os.path.join(_JOB, "specs", n))) for n in os.listdir(os.path.join(_JOB, "specs")) if n.endswith(".json")}
QUERIES = sorted(SPECS, key=_natural)
PATTERNS = {q: Pattern(SPECS[q]) for q in QUERIES}
with open(os.path.join(_JOB, "entry_counts_full.csv"), newline="") as _f:
    ENTRY_COUNTS = {(r["query"], r["alias"], r["test"]): int(r["rows"]) for r in csv.DictReader(_f)}


def entry_aliases(q):
    """the aliases q is entered at, smallest first"""
    p = PATTERNS[q]
    ranked = sorted((a for a, e in p.elements.items() if e.kind != "edge" and e.selective()),
                    key=lambda a: (ENTRY_COUNTS[(q, a, "all")], a))
    chosen = ranked[:ENTRIES]
    if not any(generator.index_served(p.elements[a]) for a in chosen):
        chosen += [a for a in ranked[ENTRIES:] if generator.index_served(p.elements[a])][:1]
    return chosen


def bodies(q):
    """{body name: Plan}, the default first. A body that cannot be built for this query is left out (generator.CannotBuild)."""
    out, seen = {}, set()
    for alias in entry_aliases(q):
        for access, outputs in (("index", "late"), ("index", "early"), ("scan", "late"), ("scan", "early"), ("run", "late")):
            if True:
                try:
                    plan = generator.build(PATTERNS[q], alias, access, outputs, ENTRY_COUNTS)
                except generator.CannotBuild:
                    continue
                key = json.dumps(plan_to_ir(plan, name=q)["steps"], sort_keys=True)
                if key in seen:           # the early twin of a body with no output-only property is the body itself
                    continue
                seen.add(key)
                name = f"enter_{alias}_by_label_run" if access == "run" else f"enter_{alias}_by_{access}"
                out[name + ("_outputs_early" if outputs == "early" else "")] = plan
    if not out:
        raise ValueError(f"{q}: no body can be built")
    return out


def _entry_label(q, name, suffix):
    return PATTERNS[q].elements[name[len("enter_"):-len(suffix)]].label


def bundle(q):
    """the plan file's content: every body, and the variant entries that say where each body may run and which is the
    default"""
    bs = bodies(q)
    plans = {}
    for name, plan in bs.items():
        ir = plan_to_ir(plan, name=q)
        plans[name] = {"steps": ir["steps"], "output": ir["output"]}
    # The first variant that fits is the body a store runs by default. A plain-scan body is offered only where its
    # entry label LACKS whole_label_scan_by_default (candidates.body_lacks reads `lacks` as select_variant does): where
    # a whole-label read with props runs as one scan of the label's rows (every grid cell), the label-run body is that
    # read, and it is cheaper in the pick measure (SCAN_DOMINATED). The engine formats (arangodb, dgraph, nebula) do
    # not hold the cap and keep it. When the first body is a plain scan, a second default names the first body valid on
    # every store (neither a plain scan nor a label run)
    first = next(iter(bs))
    variants = []
    if first.endswith(SCAN):
        variants.append({"requires": [], "lacks": [SCAN_CAP], "on": {SCAN_CAP: [_entry_label(q, first, SCAN)]},
                         "plan": first})
        variants.append({"requires": [], "plan": next(n for n in bs if not n.endswith((SCAN, RUN)))})
    else:
        variants.append({"requires": [], "plan": first})
    # a label-run body is valid only where the entry's label is one key run: its variant entry says so
    # (candidates.invalid_candidates reads it), after the default, so first fit never selects it
    for name in bs:
        if name.endswith(RUN):
            label = _entry_label(q, name, RUN)
            variants.append({"requires": ["label_rows_one_run"], "on": {"label_rows_one_run": [label]}, "plan": name})
        elif name.endswith(SCAN) and name != first:
            variants.append({"requires": [], "lacks": [SCAN_CAP], "on": {SCAN_CAP: [_entry_label(q, name, SCAN)]},
                             "plan": name})
    return {"plan": q, "for": "all formats", "params": {}, "variants": variants, "plans": plans}


REGISTRY = {q: (lambda args, _q=q: next(iter(bodies(_q).values()))) for q in QUERIES}
LOGICAL = {}
ALTERNATIVES = {}       # no scan twin is a query of its own: a query's other bodies live in its bundle
VARIANT_REQUIRES = {}
GROUPS = [{"key": "job", "title": "Join Order Benchmark", "figure": "", "queries": list(QUERIES)}]


def _pick(entry, fmt_name):
    return entry


def plan_for(name, fmt_name, schema, args):
    from kvevaluator.plans.ir import bind
    return bind(REGISTRY[name](args), args)
