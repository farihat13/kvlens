"""Where a live query and its params come from, per workload: shared by engine_fidelity's measure.py, its plan capture
and the engine-plan tests.

  ldbc        engines/queries/ldbc/<db>/interactive-{complex,short}-N.<ext>; LDBC's param rows and cost_eval's
              per-DB binder.
  microbench  engines/queries/microbench/<db>/<shape>.<ext>, one file per evaluator microbench shape; params = the
              evaluator's own microbench params (kvevaluator.workloads.microbench.params), so a live cell and an
              evaluator cell are the same anchor. Bound with the same per-DB rules as LDBC (nebula id prefixes,
              arango and tugraph ints, dgraph ISO dates).
  writes      engines/queries/writes/<db>/<primitive>.<ext>, one file per write primitive the live sweep runs (its
              own dir: the microbench's dir is globbed as a read registry); params = the evaluator's writes params
              (kvevaluator.workloads.writes.params); measure.py --family writes binds and runs them.
"""
from __future__ import annotations

from pathlib import Path

from kvevaluator.paths import ROOT, ldbc_holdout_file


_QDIR = ROOT / "engines" / "queries"
_ENGINE_DIR = {"dgraph": "dgraph", "nebula": "nebulagraph", "arangodb": "arangodb", "tugraph": "tugraph"}
EXT = {"dgraph": "graphql", "nebula": "gql", "arangodb": "aql", "tugraph": "cypher"}
_LDBC_KIND = {"ic": "interactive-complex", "is": "interactive-short"}
WORKLOADS = ("interactive", "microbench")

# microbench param columns -> the LDBC names the per-DB preprocessors already know
_MB_RENAME = {"srcId": "person1Id", "dstId": "person2Id", "days": "durationDays"}
_MB_PROVENANCE = {"degree", "stratum", "depth", "count", "subtree"}   # recorded, not bound
# nebula vid prefixes kvengines's LDBC table does not carry (kvengines.nebula.ldbc.preprocess_ids)
_MB_NEBULA_PREFIX = {"tagClassId": "tagclass"}


def query_path(workload: str, db: str, qname: str) -> Path:
    """db's text of a query; a query-text variant (complex_kv_reads.QUERY_VARIANTS: <query>-<variant>) is the base
    query's file with -<variant> after its stem"""
    from kvengines.complex_kv_reads import base_query
    base = base_query(qname)
    variant = qname[len(base):]
    if workload == "interactive":                         # "ic12" -> interactive-complex-12
        qt = "is" if base.lower().startswith("is") else "ic"
        return _QDIR / "ldbc" / _ENGINE_DIR[db] / f"{_LDBC_KIND[qt]}-{int(base[2:])}{variant}.{EXT[db]}"
    return _QDIR / workload / _ENGINE_DIR[db] / f"{base}{variant}.{EXT[db]}"


def queries_with_text(workload: str, db: str) -> list[str]:
    """Every query of a non-LDBC workload that has a live text for `db` (the file IS the registry)."""
    return sorted(p.stem for p in (_QDIR / workload / _ENGINE_DIR[db]).glob(f"*.{EXT[db]}"))


def raw_rows(workload: str, sf: str, qname: str) -> list[dict]:
    """the param rows of a query (a query-text variant: its base query's)"""
    from kvengines.complex_kv_reads import base_query
    qname = base_query(qname)
    if workload == "interactive":
        from kvevaluator.datasets.ldbc.params import param_rows
        return param_rows(sf, qname)
    from kvevaluator.workloads.microbench.params import param_rows
    return param_rows(sf, qname)


def db_params(workload: str, sf: str, qname: str, db: str, pidx: int, qtext: str) -> dict | None:
    """The engine bind params for param `pidx` (1-based), or None when the SF has fewer params."""
    rows = raw_rows(workload, sf, qname)
    if pidx > len(rows):
        return None
    from kvengines.cost_eval.loader import build_db_params, filter_params, parse_query_spec, prep_params
    from kvengines.complex_kv_reads import base_query
    if workload == "interactive":
        qt, num = parse_query_spec(base_query(qname))
        return build_db_params(rows[pidx - 1], db, qt, num, qtext)
    raw = {_MB_RENAME.get(k, k): v for k, v in rows[pidx - 1].items() if k not in _MB_PROVENANCE}
    bound = prep_params(raw, db, 0)
    if db == "nebula":
        bound.update({k: f"{pre}_{bound[k]}" for k, pre in _MB_NEBULA_PREFIX.items() if k in bound})
    return filter_params(bound, qtext, "@" if db == "arangodb" else "$")


def plan_args(workload: str, sf: str, qname: str, pidx: int) -> dict:
    """The evaluator's args for param `pidx` (1-based) -- what both the engine and optimal plans bind."""
    if workload == "interactive":
        from kvevaluator.datasets.ldbc.params import args_from_row, param_rows
        return args_from_row(qname, param_rows(sf, qname)[pidx - 1])
    from kvevaluator.workloads.microbench.params import query_params
    return query_params(sf, qname)[pidx - 1]


def _queries_module(workload: str):
    if workload == "interactive":
        from kvevaluator.workloads.interactive import queries
    else:
        from kvevaluator.workloads.microbench import queries
    return queries


def engine_registries(workload: str) -> dict:
    """{"suite": ENGINE_REGISTRY, "draft": ENGINE_DRAFT_REGISTRY} of the workload."""
    Q = _queries_module(workload)
    return {"suite": Q.ENGINE_REGISTRY, "draft": Q.ENGINE_DRAFT_REGISTRY}


def engine_plan(workload: str, qname: str, db: str, args: dict):
    return _queries_module(workload).engine_plan_for(qname, db, args)


def optimal_plan(workload: str, qname: str, db: str, schema, args: dict, sf: str | None = None):
    """The workload's cheapest plan for the query -- the <=-engine bound's reference. LDBC's
    plan_for resolves the pick record's body, per scale (`sf`: an entry that differs at this SF wins; None = the
    deciding scale's); the microbench keeps its hand plans as primitive measurements, so its cheapest plan is a separate
    resolver (no per-SF pins)."""
    from kvevaluator.plan_choice import resolve
    if workload == "interactive":
        return resolve.plan_for(workload, qname, db, schema, args, scale=None if sf is None else str(sf))
    return resolve.cheapest_plan_for(workload, qname, db, args, schema=schema)


def train_param(n_params: int) -> int:
    """The blind-holdout training param: the median one (3 -> 2, 5 -> 3, 6 -> 3, 7 -> 4, 1 -> 1).
    A plan's author sees only this param's captured plan and live counts; every other param is
    held out. Used by the microbench only -- LDBC's engine plans were written with every param in
    view, so its cells are all `seen`."""
    return (n_params + 1) // 2


# Shapes whose engine plans were written by someone who HAD seen held-out counts, so none of their
# cells is blind. hop1_knows: the pilot, written after its per-param live counts were printed.
SEEN_BY_AUTHOR = {"microbench": {"hop1_knows"}}


# LDBC's holdout: params the plans were not written against, measured AFTER every engine plan was
# frozen (ldbc_holdout.json: the freeze commit, the params, and the pairs edited after their holdout
# counts were seen). The standing params (1, 7, 13, ...) were in view while the plans were written:
# `seen`. A code change after the freeze does not relabel anything (validate.py only reports it): a pair
# whose PLAN changed after its holdout counts were seen must be added to `seen`.
# NOT blind for every cell: other studies' committed captures hold live counts at most of these params
# (cost_eval sf0.3, engine-20260811 sf0.1) -- the results doc says which cells.
_HOLDOUT_FILE = ldbc_holdout_file()     # the study's frozen freeze record (inputs/params/ldbc/)


def ldbc_holdout() -> dict:
    """{"frozen_at": the latest freeze commit | None, "freezes": [{commit, date, pairs}, ...] (history),
    "params": [...], "seen": [[query, db], ...]}."""
    import json
    return json.loads(_HOLDOUT_FILE.read_text())


def split_of(workload: str, sf: str, qname: str, pidx: int, db: str | None = None) -> str:
    """train / test for the microbench's blind holdout; holdout / seen for LDBC (and SEEN_BY_AUTHOR)."""
    if workload == "interactive":
        h = ldbc_holdout()
        return ("holdout" if pidx in h["params"] and [qname, db] not in h["seen"] else "seen")
    if qname in SEEN_BY_AUTHOR.get(workload, ()):
        return "seen"
    n = len(raw_rows(workload, sf, qname))
    if n == 1:
        return "train"                                 # one param: nothing to hold out
    return "train" if pidx == train_param(n) else "test"


# LDBC's message-anchored param files are stratified, 6 per reply depth over depths 0-5: these indices give one anchor
# per depth (measure.py's default; with the holdout params they are the archived captures' 12)
DEPTH_PARAMS = (1, 7, 13, 19, 25, 31)
FIDELITY = "fidelity"     # the engine-fidelity check's run; a figure's own run has its own name (complex_kv_reads.RUN)
# the paper's engine-fidelity check: LDBC Interactive at these scales, the microbench at the first (the scales whose
# live capture paper-results holds; `just results engine-fidelity`, paper/derive/engine_fidelity.py)
FIDELITY_SCALES = ("0.3", "0.1")
FIDELITY_DATASET = ",".join(f"ldbc={sc}" for sc in FIDELITY_SCALES)
# the write side: the live write sweep (mb2 MB81-89) and its compare with the formats' write counts
# (experiments/engine_fidelity/write_fidelity.py), both runs at the sweep's scale, the first fidelity scale
WRITE_SWEEP, WRITE_FIDELITY = "write-sweep", "write-fidelity"


def write_family() -> str:
    """the evaluator's write workload on the live engines' dataset: the one whose params the live write sweep binds and
    whose primitives it runs (its texts: engines/queries/<it>/<db>/)"""
    from kvevaluator import workloads
    (name,) = workloads.names(kind="write", dataset=workloads.get(WORKLOADS[0]).dataset)
    return name
# each engine's format whose write counts the compare reads (the formats the paper's write numbers name)
WRITE_STORES = {"dgraph": "dgraph-c512k", "nebula": "nebula", "arangodb": "arangodb"}


def write_sweep_path(sf: str, tree=None) -> Path:
    """the live write sweep's CSV (measure.py --family writes; write_fidelity.py --sweep reads it):
    <tree>/engines/ldbc/sf<SF>/write-sweep/write_sweep.csv (tree: the output dir unless given, capture_tree)"""
    from kvevaluator import workloads
    from kvevaluator.output_tree import Tree
    return (tree or Tree()).engines(workloads.get(write_family()).dataset, sf, WRITE_SWEEP) / "write_sweep.csv"


def capture_tree(name: str):
    """the output tree a --from names for a live capture that is only read: None for `output` (the output dir, where a
    live run writes it), else paper-results (the paper's capture) or a path"""
    from kvevaluator.output_tree import Tree
    from kvevaluator.paths import paper_results_dir
    if name == "output":
        return None
    return Tree(paper_results_dir() if name == "paper-results" else Path(name))


def out_dir(workload: str, sf: str, run: str = FIDELITY, tree=None) -> Path:
    """the live engines' raw answers and counters of a workload in a run: <tree>/engines/<dataset>/<scale>/<run>/<workload>
    (tree: the output dir unless given, capture_tree)"""
    from kvevaluator import workloads
    from kvevaluator.output_tree import Tree
    return (tree or Tree()).engines(workloads.get(workload).dataset, sf, run, workload)


def kvstats_path(workload: str, sf: str, qname: str, db: str, pidx: int, run: str = FIDELITY) -> Path:
    """One cell's raw engine counters (measure.py), next to its captured plan and stored answer."""
    return out_dir(workload, sf, run) / db / qname / f"p{pidx}" / "kvstats.json"


# The live Dgraph's uids, for the engine steps that follow Dgraph's uid order (first_k_by_uid). The
# loader assigns them, so they are a property of the loaded engine, captured with its counts
# (measure.py) into <out_dir>/dgraph/uids_<type>.csv (LDBC id, uid). Only the types such a step reads.
DGRAPH_UID_TYPES = {"Person": "person"}          # our label -> Dgraph's type name


def dgraph_uids_path(workload: str, sf: str, label: str, run: str = FIDELITY, tree=None) -> Path:
    return out_dir(workload, sf, run, tree) / "dgraph" / f"uids_{label.lower()}.csv"


def dgraph_uid_load(workload: str, sf: str, tree=None) -> str | None:
    """Which engine load the captured uid map came from (the backup's `created` stamp), or None."""
    import json
    path = out_dir(workload, sf, tree=tree) / "dgraph" / "uids.meta.json"
    return json.loads(path.read_text()).get("data_created") if path.exists() else None


def dgraph_uid_map(workload: str, sf: str, run: str = FIDELITY, tree=None) -> dict | None:
    """{vid: live Dgraph uid} from the captured files of a run, or None when any is missing."""
    import csv
    from kvevaluator.schema.vids import vid
    uid_of = {}
    for label in DGRAPH_UID_TYPES:
        path = dgraph_uids_path(workload, sf, label, run, tree)
        if not path.exists():
            return None
        with open(path) as f:
            uid_of.update({vid(label, r["id"]): int(r["uid"], 16) for r in csv.DictReader(f)})
    return uid_of


def with_uid_map(steps: list, uid_of: dict | None) -> list:
    """The bound engine steps with the uid map riding on every first_k_by_uid step (its `uid_of` arg: [vid, uid]
    pairs, sorted), the one form both evaluators read. Unchanged when no step needs it or the map is None (the
    step then fails loudly, as it should)."""
    from kvevaluator.plans.plan import Step
    if uid_of is None or not any(s.op == "first_k_by_uid" for s in steps):
        return steps
    pairs = sorted([v, u] for v, u in uid_of.items())
    return [Step(s.out, s.op, **{**s.args, "uid_of": pairs}) if s.op == "first_k_by_uid" else s for s in steps]


def answer_size(rows: list) -> int:
    """How many items an answer holds: a single `{"count": n}` row counts n (the microbench's
    count-final shapes), anything else counts its rows."""
    if len(rows) == 1 and isinstance(rows[0], dict) and set(rows[0]) == {"count"}:
        return int(rows[0]["count"])
    return len(rows)
