"""LinkBench substitution params -- node-id anchors per query, selected by ROLE so the study spans the
access-pattern space (getNode = random typical nodes; getLinkList = degree-stratified; composed =
moderate-degree, where the node-read can dominate the walk). Anchors are generated from the data by
gen_linkbench_anchors.py (node|degree), one file per scale TAG in the data dir. The scale TAG (e.g. "n1m") stands
in for the LDBC "sf" axis: LinkBench scales by node count (maxid1), not scale factor.
"""
import os

from kvevaluator.datasets.linkbench.loader import data_dir as _lb_data_dir  # noqa: F401  (re-exported convenience)
from kvevaluator.paths import ROOT
from kvevaluator.datasets import data_in, fetch_hint

_EXP = str(ROOT)
# The param sets LinkBench's queries can read: the degree anchors, a sample of its own request log (quick: each request
# type's first requests; quick-next: the next ones, an independent second sample), or the whole log (log). A set is an
# identity axis of a capture (its <params> dir, output_tree.params_dir: the first set's dir is reported/, each other
# set's its name); a writer takes it by flag (--param-set), as LDBC's (datasets/ldbc/params.py PARAM_SETS).
PARAM_SETS = ("anchors", "quick", "quick-next", "log")
# the one default: the set read when none is named (None, everywhere: check_param_set, output_tree.params_dir, kvcost
# --params through its LinkBench adapter): the whole request log, as published
DEFAULT_PARAM_SET = "log"
SAMPLES = PARAM_SETS[1:]     # the sets read from the request log: one dir each under the params dir (gen_linkbench_params)


def check_param_set(param_set):
    """a LinkBench param set as code passes it: for None, the one default (DEFAULT_PARAM_SET, log); else its name;
    another is refused"""
    if param_set is None:
        return DEFAULT_PARAM_SET
    if param_set not in PARAM_SETS:
        raise SystemExit(f"param set {param_set!r}: not one of LinkBench's ({', '.join(PARAM_SETS)}) -- REFUSED")
    return param_set


def anchors_path(tag):
    """node|degree anchors for a scale TAG, in its data dir (gen_linkbench_anchors.py)."""
    return str(data_in("linkbench", tag) / "anchors.txt")


def _rows(tag):
    with open(anchors_path(tag)) as f:
        header = f.readline().rstrip("\n").split("|")
        return [dict(zip(header, ln.rstrip("\n").split("|"))) for ln in f]


def _rank_spaced(items, k):
    if k >= len(items):
        return items
    if k == 1:                                     # one param: the first, as p1 is at any k
        return items[:1]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]


def request_params_path(tag, sample, qname):
    """One sample (quick, quick-next or log) of a query's params, from LinkBench's own seeded request log (gen_linkbench_params.py)."""
    return str(data_in("linkbench", tag) / "params" / sample / f"{qname}.jsonl")


def query_params(tag, qname, count=None, param_set=None):
    """Arg dicts for the first `count` params of a query (all when None). TWO sources, chosen by
    `param_set` (PARAM_SETS):
      anchors           -- degree-stratified node anchors (anchors.txt): the characterization run, "how does a
                           read's cost depend on list length" (spreads over degrees, so hubs are present);
      quick | quick-next -- a sample of LinkBench's OWN request stream (its op mix and access distributions,
                           seeded): the workload mix. The write queries exist only in the request log's sets;
      log (default)     -- the whole request log, as published (None: DEFAULT_PARAM_SET)."""
    import json
    sample = check_param_set(param_set)
    if sample != "anchors":
        path = request_params_path(tag, sample, qname)
        if not os.path.exists(path):
            raise SystemExit(f"linkbench: no request params {path} -- `{fetch_hint('linkbench', tag)}` makes the data and "
                             f"the params (checked against the tracked inputs/params/linkbench/{tag}/linkbench/params.sha256.txt)")
        with open(path) as f:
            rows = [json.loads(line) for line in f]   # a data blob stays {"$bytes": ...}: the binder decodes it
        return rows[:count] if count else rows
    rows = _rows(tag)
    ids = [r["node"] for r in rows]
    if qname == "getnode":                       # typical nodes: a rank-spaced spread across degrees
        picked = _rank_spaced(ids, count or len(ids))
    elif qname.startswith(("getlinklist_", "countlink_")):      # degree-stratified: big..small, spread across the dist
        picked = _rank_spaced(ids, count or len(ids))
    elif qname == "composed_getnode":            # the middle degree band (node-read can dominate the walk)
        lo, hi = len(ids) // 4, 3 * len(ids) // 4
        band = ids[lo:hi] or ids
        picked = band[:count] if count else band
    else:
        raise SystemExit(f"linkbench: {qname!r} has no anchor params (writes and getlink need request params: "
                         f"--params quick or quick-next)")
    return [{"node": nid} for nid in picked]


def request_log_path(tag):
    """The whole request log in log order ({seq, query, row, args, ran_as, note}, gen_linkbench_params.py): the ordered
    request file a mixed run reads.
    """
    return str(data_in("linkbench", tag) / "params" / "request_log.jsonl")


def request_log(tag):
    """the ordered request file of a mixed read + write run (measure.workload_plans.request_log), checked present"""
    path = request_log_path(tag)
    if not os.path.exists(path):
        raise SystemExit(f"linkbench: no request file {path} -- `{fetch_hint('linkbench', tag)}` makes the data and "
                         f"the params (checked against the tracked inputs/params/linkbench/{tag}/linkbench/params.sha256.txt)")
    return path


def sequence_meaning(entries):
    """What each request of an ordered run MEANS once the writes before it have been applied, decided from the order
    alone (the same on every store). entries: [(query, args)] in run order; returns [(ran_as, note)], one per entry,
    note "" when nothing changed. A write's query name was decided against the LOADED graph (gen_linkbench_params.py);
    in sequence, four things change:
      add_of_existing          an addlink of a (type, src, dst) an earlier request added or updated: LinkBench's addLink
                               is an upsert, so it RUNS the type's updatelink plan (a format may refuse a second insert).
                               A pair exists once loaded, added or updated; a hide keeps an existing link and a hide of
                               a missing one writes nothing (queries/writes.py), so a deletelink never makes a pair exist;
      delete_of_sequence_added a deletelink of a pair an earlier request added;
      update_of_deleted_node   an updatenode / deletenode of a node an earlier deletenode removed;
      link_of_deleted_node     a link write whose src or dst an earlier deletenode removed (LinkBench keeps such links).
    Reads pass unchanged (ran_as = query)."""
    links, dead, out = set(), set(), []           # (type, src, dst) added or updated so far; nodes deleted so far
    for q, args in entries:
        ran, notes = q, []
        if q.startswith(("addlink_", "updatelink_", "deletelink_")):
            t = q.rsplit("_", 1)[1]
            key = (t, args["src"], args["dst"])
            if q.startswith("addlink"):
                if key in links:
                    ran = f"updatelink_{t}"
                    notes.append("add_of_existing")
            elif q.startswith("deletelink") and key in links:
                notes.append("delete_of_sequence_added")
            if not q.startswith("deletelink"):    # a delete of a missing pair writes nothing: the pair stays missing
                links.add(key)
            if args["src"] in dead or args["dst"] in dead:
                notes.append("link_of_deleted_node")
        elif q in ("updatenode", "deletenode"):
            if args["node"] in dead:
                notes.append("update_of_deleted_node")
            if q == "deletenode":
                dead.add(args["node"])
        out.append((ran, "+".join(notes)))
    return out
