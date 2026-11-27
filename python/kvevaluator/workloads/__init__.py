"""The workloads: one folder each (workloads/<name>/), described by its manifest.json; this module is the one table of
them. Nothing else lists the workloads by name: a list or a branch on a workload reads a field here.

A workload is a set of queries on one dataset. Its manifest says:
  dataset     the graph it runs on (kvevaluator.datasets)
  kind        "read" or "write"
  pick_params how a read query's plan is picked: "held-out" (on params kept out of the reported ones: LDBC Interactive),
              "in-sample" (on the reported params: no held-out rows exist), "none" (writes: one plan per query)
  params_module  its params module when it is the dataset's (LDBC's, LinkBench's param files), else
              kvevaluator.workloads.<name>.params; its queries module is kvevaluator.workloads.<name>.queries
  plan_dirs   its committed plan dirs, relative to python/kvevaluator/workloads/
  formats     the workload's named formats (its default comparison set)
  queries_module  when its queries are another folder's (LinkBench's writes are in workloads/linkbench/queries)
  optional    a cost load of its dataset adds it only when asked (LDBC's BI)
  single_plans  its queries have one plan each (no candidates to pick among: LinkBench)
  label_only_dir  the plan dir an index-free design reads first (LDBC Interactive)
  study_dir   the study dir its picks table lives in (BI, JOB)
  predict_gate  what predict mode's exact gate runs beside its queries on this workload: bodies, untagged, edge_check
              (a read workload), own_cell and own_spec_dir (a write workload), and the rules no request reaches,
              known_untested {rule: why} (predict_gate(name); predict_requests.gate_lists; run/predict-gate.sh)

    get("bi")                      -> Workload
    names(kind="read")             -> the read workloads, in manifest order
    queries_module("bi")           -> the workload's queries module (imported only when asked)
    params_module("bi")            -> its params module
"""
import glob
import importlib
import functools
import json
import os
import pkgutil
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
PICK_PARAMS = ("held-out", "in-sample", "none")
KINDS = ("read", "write")


@dataclass(frozen=True)
class Workload:
    """one workload, as its manifest.json declares it"""
    name: str
    order: int
    title: str
    dataset: str
    kind: str
    pick_params: str
    plan_dirs: tuple
    formats: tuple
    params_module: str = None      # optional in a manifest
    queries_module: str = None
    optional: bool = False
    single_plans: bool = False
    label_only_dir: str = None
    study_dir: str = None
    data_step: str = None          # a module that makes what the workload needs beside its dataset's data, at a scale:
                                   # missing / ensure / check (JOB's reference answers); `just fetch-dataset` runs it

    @property
    def own_data(self):
        """True when --sf is the dataset's data tag (LinkBench, JOB), not the LDBC scale factor"""
        from kvevaluator import datasets
        return datasets.get(self.dataset).scale_axis != "sf"

    @property
    def schema(self):
        """the dataset's schema.json, relative to the repo root"""
        from kvevaluator import datasets
        return datasets.get(self.dataset).schema


def _load():
    rows = []
    for p in sorted(glob.glob(os.path.join(HERE, "*", "manifest.json"))):
        d = json.load(open(p))
        if d["name"] != os.path.basename(os.path.dirname(p)):
            raise SystemExit(f"{p}: name {d['name']!r} is not its folder's")
        if d["kind"] not in KINDS or d["pick_params"] not in PICK_PARAMS:
            raise SystemExit(f"{p}: kind {d['kind']!r} (one of {KINDS}), pick_params {d['pick_params']!r} (one of {PICK_PARAMS})")
        d["plan_dirs"], d["formats"] = tuple(d["plan_dirs"]), tuple(d["formats"])
        rows.append(Workload(**{k: d[k] for k in Workload.__dataclass_fields__ if k in d}))   # the query fields: query_list, query
    return {w.name: w for w in sorted(rows, key=lambda w: w.order)}


WORKLOADS = _load()


def names(kind=None, dataset=None, pick_params=None):
    """the workloads' names in manifest order, optionally only one kind, dataset or pick_params"""
    return [w.name for w in WORKLOADS.values()
            if (kind is None or w.kind == kind) and (dataset is None or w.dataset == dataset)
            and (pick_params is None or w.pick_params == pick_params)]


def get(name):
    if name not in WORKLOADS:
        raise ValueError(f"unknown workload {name!r} (one of {tuple(WORKLOADS)})")
    return WORKLOADS[name]


def families(dataset, optional=False):
    """{workload: "read" | "write"} of a dataset's workloads in manifest order (a dataset's families, read from the
    workload manifests; optional=True: the ones a cost load adds only when asked, LDBC's BI; False: the rest)"""
    return {w: get(w).kind for w in names(dataset=dataset) if get(w).optional == optional}


def queries_module(name):
    return importlib.import_module(get(name).queries_module or f"kvevaluator.workloads.{get(name).name}.queries")


def params_module(name):
    return importlib.import_module(get(name).params_module or f"kvevaluator.workloads.{get(name).name}.params")


# --- a workload's query files ----------------------------------------------------------------------------------------
# Interactive, BI and LinkBench keep one module per query file, each declaring its plans in dicts by kind: SIM (the
# hand plans, per format where they differ), LOGICAL (the logical plan the rules expand), LABEL_ONLY, ENGINE and
# ENGINE_DRAFT (the live engines' plans). merge_query_modules is the one place they are merged. A query with a LOGICAL
# plan and no SIM entry runs its logical plan as its hand plan too, so a query whose two are one function is
# registered once. Every file is imported, so a new query file cannot be left out. Microbench writes its REGISTRY in
# its __init__ (per-format overrides beside shared builders), JOB generates its plans from SPECS (no query files),
# and writes is one module merging two sub-registries: they keep their own.

def merge_query_modules(package, path):
    """{REGISTRY, LOGICAL, LABEL_ONLY, ENGINE_REGISTRY, ENGINE_DRAFT_REGISTRY} of every module under a queries
    package (its __name__ and __path__); a label-only or engine plan of a query with neither SIM nor LOGICAL refuses"""
    reg, logical, label_only, engine, draft = {}, {}, {}, {}, {}
    for m in pkgutil.iter_modules(path):
        mod = importlib.import_module(f"{package}.{m.name}")
        sim, logic = getattr(mod, "SIM", {}), getattr(mod, "LOGICAL", {})
        reg.update(sim)
        reg.update({q: f for q, f in logic.items() if q not in sim and q not in reg})
        logical.update(logic)
        label_only.update(getattr(mod, "LABEL_ONLY", {}))
        engine.update(getattr(mod, "ENGINE", {}))
        for q, entry in getattr(mod, "ENGINE_DRAFT", {}).items():   # a query can split: some engines validated,
            draft.setdefault(q, {}).update(entry)                    # others still draft
    orphans = sorted((set(label_only) | set(engine) | set(draft)) - set(reg))
    if orphans:
        raise ValueError(f"{package}: {', '.join(orphans)} has a label-only or engine plan but neither SIM nor LOGICAL")
    return {"REGISTRY": reg, "LOGICAL": logical, "LABEL_ONLY": label_only, "ENGINE_REGISTRY": engine,
            "ENGINE_DRAFT_REGISTRY": draft}


# --- the queries ----------------------------------------------------------------------------------------------------
# A query is one record: its name, its question in English, the file its plans are in, and where its params come from
# when it borrows another query's rows (`params_like`, e.g. LDBC's IS1-3 take IC1's person ids). The question and
# params_like are written into the query's plan file (with_query_fields), so a query someone adds declares both in its
# own file; for the benchmarks' queries they come from the workload's questions.json and manifest.

@dataclass(frozen=True)
class Query:
    """one query of a workload"""
    name: str
    workload: str
    question: str
    params_like: object          # the query whose param rows it runs on (None: its own)
    plan_file: str               # its plan file (the first of the workload's plan dirs that holds one)
    twins: tuple                 # the scan twins whose bodies are extra candidates of this query (ALTERNATIVES)
    capped: object               # its capped twin, measured beside it (LDBC: is2, is6, ic12)
    # its plans, from the queries module's registries (None: the workload has none of that kind for it)
    logical: object = None       # the logical plan the rules expand (LOGICAL)
    bodies: object = None        # the hand plan: a builder, or {"default": b, "<format>": b} (REGISTRY: SIM, else LOGICAL)
    label_only: object = None    # the plan when the only index is the vertex-label one (LABEL_ONLY)
    engine: object = None        # {format: the plan the live engine runs} (ENGINE_REGISTRY)
    engine_draft: object = None  # {format: an engine plan not yet matched to the live engine} (ENGINE_DRAFT_REGISTRY)


_QUESTIONS = {}


def questions(name):
    """{query: question} of a workload: its questions.json; JOB's are made from each query's spec"""
    if name not in _QUESTIONS:
        if name == "job":
            mod = queries_module("job")
            _QUESTIONS[name] = {q: _job_question(q, mod.SPECS[q]) for q in mod.QUERIES}
        else:
            path = os.path.join(HERE, name, "questions.json")
            _QUESTIONS[name] = json.load(open(path)) if os.path.exists(path) else {}
    return _QUESTIONS[name]


def _job_question(q, spec):
    tables = sorted(set(spec["aliases"].values()))
    preds = sum(len(v) for v in spec["predicates"].values())
    return (f"Join Order Benchmark query {q[len('job'):]}: join {len(tables)} IMDb tables ({', '.join(tables)}) on "
            f"{len(spec['joins'])} join conditions, keep the rows that pass {preds} filters, and return the smallest "
            f"value of each of its {len(spec['outputs'])} output columns.")


@functools.lru_cache(maxsize=None)
def _manifest(name):
    """a workload's manifest.json, read once (every caller reads it, none changes it)"""
    return json.load(open(os.path.join(HERE, name, "manifest.json")))


def predict_gate(name):
    """the manifest's predict_gate block ({} when it has none): a read workload's {bodies, untagged, edge_check:
    [query]}, a write workload's {own_cell, own_spec_dir}, and {known_untested: {rule: why}}
    (kvevaluator.measure.predict_requests.gate_lists reads it)"""
    return dict(_manifest(name).get("predict_gate", {}))


def query_list(name):
    """the workload's measured query list: the manifest's `queries` when it names them (LDBC Interactive), else the
    queries module's QUERIES less its scan twins (ALTERNATIVES)"""
    m = _manifest(name)
    if "queries" in m:
        return list(m["queries"])
    mod = queries_module(name)
    alts = getattr(mod, "ALTERNATIVES", {})
    return [q for q in mod.QUERIES if q not in alts]


def capped_twins(name):
    """{query: its capped twin} in the manifest's order (LDBC: is2, is6, ic12)"""
    return dict(_manifest(name).get("capped", {}))


def plan_path(name, q):
    """the query's plan file: the first of the workload's plan dirs that holds <q>.json (None: no file)"""
    from kvevaluator.paths import ROOT
    for d in get(name).plan_dirs:
        p = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", *d.split("/"), f"{q}.json")
        if os.path.exists(p):
            return p
    return None


def query_fields(name, q):
    """the fields a query's plan file carries about the query: {"question": ...} and, when declared, "params_like".
    A file that already declares them (a query someone added) keeps its own."""
    out = {}
    qs = questions(name)
    if q in qs:
        out["question"] = qs[q]
    like = _manifest(name).get("params_like", {}).get(q)
    if like:
        out["params_like"] = like
    return out


def with_query_fields(name, q, ir, declared=None):
    """the IR with the query's fields right after `plan` (a file's own fields win): the workload's tables', or
    `declared`, the ones its Python declares (Plan(question=..., params_like=...)); a field in both is refused"""
    table = query_fields(name, q)
    declared = declared or {}
    both = sorted(set(table) & set(declared))
    if both:
        raise ValueError(f"{q} ({name}): {', '.join(both)} declared twice, in its Python (Plan(...)) and in the "
                         f"workload's tables (questions.json, manifest.json params_like): keep one")
    fields = {k: v for k in ("question", "params_like", "estimate_hints") for v in [declared.get(k, table.get(k))]
              if v is not None and k not in ir}
    if not fields:
        return ir
    out = {}
    for k, v in ir.items():
        out[k] = v
        if k == "plan":
            out.update(fields)
    if "plan" not in ir:
        out = {**fields, **out}
    return out


def query(name, q):
    """the Query record of q in workload `name`: the plan file's own question / params_like first, then the tables"""
    path = plan_path(name, q)
    ir = json.load(open(path)) if path else {}
    fields = {**query_fields(name, q), **{k: ir[k] for k in ("question", "params_like") if k in ir}}
    mod = queries_module(name)
    alts = getattr(mod, "ALTERNATIVES", {})
    bodies, logical = getattr(mod, "REGISTRY", {}).get(q), getattr(mod, "LOGICAL", {}).get(q)
    if bodies is None and logical is None:
        raise KeyError(f"{name}: query {q} has neither a hand plan (SIM) nor a logical plan (LOGICAL) in its queries")
    return Query(name=q, workload=name, question=fields.get("question", ""), params_like=fields.get("params_like"),
                 plan_file=path, twins=tuple(sorted(t for t, base in alts.items() if base == q)),
                 capped=_manifest(name).get("capped", {}).get(q), logical=logical, bodies=bodies,
                 label_only=getattr(mod, "LABEL_ONLY", {}).get(q), engine=getattr(mod, "ENGINE_REGISTRY", {}).get(q),
                 engine_draft=getattr(mod, "ENGINE_DRAFT_REGISTRY", {}).get(q))


def queries(name):
    """the Query records of the workload's measured queries, in order"""
    return [query(name, q) for q in query_list(name)]


# --- files in an older commit ---------------------------------------------------------------------------------------
# In older commits the workloads' files are in python/kvevaluator/benchmarks/<package>/ (LDBC Interactive's package
# is `ldbc`). A reader of an older commit (a plan file as v1 or a capture's binary had it) tries both places.
_BEFORE_MOVE = (("python/kvevaluator/workloads/interactive/", "python/kvevaluator/benchmarks/ldbc/"),
                ("python/kvevaluator/workloads/", "python/kvevaluator/benchmarks/"))


def paths_in_old_trees(rel):
    """the repo-relative paths a workload file `rel` may have in an older commit: itself, then its place before the
    move"""
    rel = rel.replace(os.sep, "/")
    for new, old in _BEFORE_MOVE:
        if rel.startswith(new):
            return (rel, old + rel[len(new):])
    return (rel,)


def param_set_names():
    """every param set a workload's params module lists, in the workloads' order, each once (--params' choices)"""
    out = []
    for name in names():
        out += [s for s in getattr(params_module(name), "PARAM_SETS", ()) if s not in out]
    return tuple(out)
