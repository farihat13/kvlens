"""The datasets the layout pricer prices (DATASETS, the --dataset flags) and their workloads: each one's workload files,
axis and families, so the pricers and the analysis scripts stay dataset-neutral. Capture paths: output_tree. It also
holds where experiments/layout_choice writes its analyses (out_root,
workload_dir, add_root_arg): those paths are layout_choice's output layout, read by the studies that draw from it. How
figures name these things: labels.py.

A dataset = its workloads (one file each in the dirs its manifest's workload_files names: workloads()) + its axis + its
families. A family is where a workload item's query was measured:
  ldbc       (axis: the scale factor sf)  interactive (the LDBC IC/IS reads) + microbench (reads) + writes (the write
             primitives + LDBC IU); bi, an optional family: a load adds it only when asked, and prices it alone
  linkbench  (axis: LinkBench's scale tag; its param set from --params: anchors, quick, quick-next, log)
             linkbench (LinkBench's reads) + linkbench_writes (its write ops)
  imdb       (axis: IMDb's data tag, sample1pct | full)  job (the Join Order Benchmark's 113 queries; reads only)
Each workload item is (family, query, how often, params).
"""
import glob
import json
import os
import types
from kvevaluator.paths import INPUTS_DIR, ROOT, figure_dir, refuse_results

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.output_tree import Tree, params_dir

from kvevaluator import datasets as _D
from kvevaluator.capture import workload_file as WF
from kvevaluator import workloads as _W

DATASETS = tuple(_D.names())     # the datasets table (kvevaluator/datasets/*/manifest.json)
# Each suite's families, read only: a stage that prices another set names it in its load (compose.Composer.families,
# built with families() below)
FAMILIES = types.MappingProxyType({d: types.MappingProxyType(_W.families(d)) for d in _D.names()})
# LDBC BI runs on the LDBC store and is captured under <grid>/bi (experiments/meta/run-bi-cells.sh). It is NOT a family
# of the ldbc suite for good: every layout-choice stage would then ask for a BI capture in every grid. A stage that
# prices BI names it in its load's families: beside the others (families("ldbc", add=BI)) or alone (BI: a BI capture
# root holds bi/ alone).
BI = (("bi", "read"),)
OPTIONAL = {d: _W.families(d, optional=True) for d in _D.names() if _W.families(d, optional=True)}   # families a load of the suite may add


def families(dataset, drop=(), add=()):
    """the suite's (family, kind) pairs in order, less `drop`, then `add` ((family, kind), ...): a compose.Composer.families"""
    out = tuple((f, k) for f, k in FAMILIES[dataset].items() if f not in drop)
    return out + tuple(x for x in add if x[0] not in dict(out))


def families_read(dataset, ws):
    """the (family, kind) pairs the workloads `ws` (dicts with "items") read: the benchmark's in order, then its optional
    ones (LDBC's BI); a compose.Composer.families"""
    used = {fam for w in ws for fam, *_ in w["items"]}
    return tuple(x for x in families(dataset, add=tuple(OPTIONAL.get(dataset, {}).items())) if x[0] in used)


def kind_of(family):
    """'read' or 'write': what a family's queries do"""
    for s in DATASETS:
        fams = {**FAMILIES[s], **OPTIONAL.get(s, {})}
        if family in fams:
            return fams[family]
    raise KeyError(family)


# The plan PICK rule a stage prices and draws under (candidates.pick_body's criterion) is an identity axis of every
# output of the study, so it is a directory level of out_root -- the default run lands in .../wops_bytes4k/, the
# ops-only pick in .../wops/, and neither overwrites the other. Every layout-choice CLI takes
# --pick-by (add_pick_by_arg, read as a.criterion) and passes it on: to the pricer in compose.Composer, to the paths here and the labels (labels.py).


def criterion_name(criterion):
    """the identity slug of a pick rule: a named rule's name (a spec is its own name); None = the default rule"""
    from kvevaluator.plan_choice.optimizer.criteria import resolve_criterion
    return resolve_criterion(criterion)[0]


def add_pick_by_arg(ap):
    """--pick-by (read as a.criterion), the one flag every layout-choice CLI takes: the plan PICK rule
    (candidates.pick_body) that the pricers pick by and that names the output level the plots and budget read"""
    from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, NAMED
    ap.add_argument("--pick-by", dest="criterion", default=None, metavar="RULE",
                    help=f"the rule a read query's plan is picked by (candidates.pick_body): a named rule "
                         f"{sorted(NAMED)} or a spec; default {DEFAULT_CRITERION}; `wops` = the ops-only pick, "
                         f"for a before/after diff. Outputs live under <study root>/<pick rule>/")


PICK_ON = ("held-out", "in-sample")


def add_pick_on_arg(ap, help=None):
    """--pick-on held-out|in-sample (read as a.pick_on; None: the dataset's default, pick_on_of)"""
    ap.add_argument("--pick-on", dest="pick_on", default=None, choices=PICK_ON,
                    help=help or "the params a plan is picked on: held-out (the default where the dataset has a "
                                 "held-out set) or in-sample (the reported params; the held-out rows are not read; "
                                 "LinkBench's default: it has no held-out set)")


def pick_on_of(a, held_out_set=True):
    """--pick-on as given, else the dataset's default: held-out where it has a held-out set, else in-sample"""
    return getattr(a, "pick_on", None) or ("held-out" if held_out_set else "in-sample")


def add_chunk_view_arg(ap):
    """--chunk-view: what a write's touch of a family's last chunk is charged (compose.chunk_view); a non-default view is
    part of the output's name (cli/search.py search_name)"""
    from kvcost.layout_cost.compose import CHUNK_VIEWS
    ap.add_argument("--chunk-view", default=None, choices=CHUNK_VIEWS,
                    help="a write's touch of a family's last chunk: half = charged at the chunk target / 2, measured = "
                         "the bytes the capture found (python/kvcost/layout_cost/README.md, \"The chunk view\"); default "
                         "measured for the bill models, half otherwise (compose.default_chunk_view)")


def chunk_view_name(stem, view):
    """an output's name under the chunk view `view`: the default view (half; None, not named) adds nothing, another
    adds _<view>, so the two views' outputs sit side by side"""
    from kvcost.layout_cost.compose import CHUNK_VIEWS
    return stem if view in (None, CHUNK_VIEWS[0]) else f"{stem}_{view}"


def add_params_arg(ap, help=None):
    """--params, read as a.param_set: which param set (output_tree.params_dir; a set some workload's params module lists:
    LDBC Interactive's ldbc or uniform, LinkBench's anchors, quick, quick-next or log), part of a capture's identity"""
    from kvevaluator.workloads import param_set_names
    ap.add_argument("--params", dest="param_set", default=None, choices=param_set_names(), metavar="SET",
                    help=help or "the param set whose captures are read: LDBC Interactive's ldbc or uniform; LinkBench's "
                                 "anchors, quick (each request type's first requests), quick-next (the next ones: a second "
                                 "sample) or log (the whole request log); default the workload's default set (LinkBench: log; LDBC: ldbc)")


def out_root(dataset, at, criterion, param_set=None, root=None):
    """where the layout-choice analysis writes: summary/<dataset>/<scale>/layout-choice/<params>/<criterion> in the tree
    at `root` (default the output dir)"""
    ps = params_dir(_W.names(kind="read", dataset=dataset)[0], param_set)
    return str(Tree(root).summary(dataset, at, "layout-choice", ps, criterion_name(criterion)))


WORKLOADS_DIR = INPUTS_DIR / "workloads"     # one file per workload: the one copy (workloads/make_workloads.py)


def workloads_dirs(dataset, at):
    """the dirs of a dataset's workload files (its manifest's workload_files; LinkBench's per scale: its request log's
    counts)"""
    return [ROOT / d.format(scale=at) for d in _D.get(dataset).workload_files]


def dataset_in_run(mix, run):
    """the dataset of a run (kvevaluator.sizes.resolve's dict) whose workload files hold the workload `mix` (an id or a
    name; kvevaluator.capture.workload_file.resolve's rule) at its scale in the run, or None: the run leaves its dataset
    out"""
    hits = WF.datasets_holding(mix, run)
    if len(hits) > 1:
        raise SystemExit(f"workload {mix!r}: held by {hits}")
    return hits[0] if hits else None


def _tuples(x):
    return tuple(_tuples(v) for v in x) if isinstance(x, list) else x


def read_workload(path):
    """(name, workload) of a workload file: items as (family, query, frequency, params) tuples, params a list of
    1-based param indexes or None; why_numbers' specs and sweep_marks' pairs as tuples, as the readers index them"""
    with open(path) as f:
        w = json.load(f)
    if "id" not in w and "name" not in w:
        raise SystemExit(f"{path}: a workload file needs a name (or an id)")
    name = w.pop("name", None) or w["id"]
    w.setdefault("id", name)                       # a file someone writes may give a name only
    w["items"] = [(it["family"], it["query"], it["frequency"], it.get("params")) for it in w["items"]]
    if "why_numbers" in w:
        w["why_numbers"] = {k: _tuples(v) for k, v in w["why_numbers"].items()}
    if "sweep_marks" in w:
        w["sweep_marks"] = [tuple(m) for m in w["sweep_marks"]]
    return name, w


def workloads(dataset, at, optional=False):
    """{name: workload} of a dataset, read from its workload files in id order (W01-W15, I01, I02, L01-L04, J01); a workload that
    reads an optional family (LDBC's BI: B01) only with optional=True, as a load adds that family only when asked"""
    dirs = workloads_dirs(dataset, at)
    paths = sorted(p for d in dirs for p in glob.glob(str(d / "*.json")))
    if not paths:
        more = f" --scale {at}" if dataset == "linkbench" else ""
        raise SystemExit(f"no workload files in {', '.join(map(str, dirs))}: python3 "
                         f"python/kvcost/layout_cost/workloads/make_workloads.py --dataset {dataset}{more}")
    ws = dict(read_workload(p) for p in paths)
    if not optional:
        opt = set(OPTIONAL.get(dataset, {}))
        ws = {n: w for n, w in ws.items() if not any(fam in opt for fam, *_ in w["items"])}
    return dict(sorted(ws.items(), key=lambda kv: kv[1]["id"]))


def select(dataset, at, only=None):
    """the suite's workloads (its top-level files), or only those `only` names -- a comma list of ids (i01) or names
    (ldbc_mix), each resolved by kvevaluator.capture.workload_file.resolve (an equal-share file by its own id)"""
    if not only:
        return workloads(dataset, at)
    picked = [read_workload(WF.resolve(x, dataset, at)) for x in (t.strip() for t in only.split(",") if t.strip())]
    return dict(sorted(dict(picked).items(), key=lambda kv: kv[1]["id"]))


def add_workload_arg(ap):
    """--workload: run a stage on some workloads only (a new workload, a quick check). Files that cover every
    workload (workload_costs, budget_picks, the index README, the cross-workload plots, sensitivity.csv) are written by
    a full run only."""
    ap.add_argument("--workload", default=None,
                    help="only these workloads: a comma list of ids (i01) or names (ldbc_mix); default all")


def queries(dataset):
    """{query: what it does, in plain words}"""
    if dataset == "linkbench":
        from kvcost.layout_cost.workloads.linkbench import QUERIES
    elif dataset == "imdb":
        from kvcost.layout_cost.workloads.job import QUERIES
    else:
        from kvcost.layout_cost.workloads.designed import QUERIES
    return QUERIES


def params_text(dataset):
    """{family: the params behind "mean over its params"}"""
    if dataset == "linkbench":
        from kvcost.layout_cost.workloads.linkbench import PARAMS
    elif dataset == "imdb":
        from kvcost.layout_cost.workloads.job import PARAMS
    else:
        from kvcost.layout_cost.workloads.designed import PARAMS
    return PARAMS


# -- naming shared by every suite's workloads ------------------------------------------------------------------
def kind(w):
    """'r' (reads only), 'w' (writes only) or 'rw' (both), from the families of its items"""
    reads = any(kind_of(family) == "read" for family, *_ in w["items"])
    writes = any(kind_of(family) == "write" for family, *_ in w["items"])
    return ("r" if reads else "") + ("w" if writes else "")


def workload_dir(dataset, at, name, w, root=None, *, criterion, param_set=None):
    """<out root>/<id>-<kind>-<name> (e.g. output/summary/ldbc/sf3/layout-choice/wops_bytes4k/w14-rw-hubs-vs-leaves): one folder per workload;
    `root` = another analysis dir (e.g. under paper-results/) instead of the working one"""
    return os.path.join(root or out_root(dataset, at, criterion, param_set), f"{w['id']}-{kind(w)}-{name.replace('_', '-')}")


def add_root_arg(ap):
    """--root: draw from another analysis dir, e.g. under paper-results/, instead of the working output"""
    ap.add_argument("--root", default=None, help="the analysis dir to read (default: the working output's <criterion> "
                                                 "level; e.g. paper-results/summary/ldbc/sf3/layout-choice/reported/wops_bytes4k); "
                                                 "the figures go beside it, or under the output dir for a fetched snapshot")


_PLOTS_OUT = None   # the drawing script's --out (use_plots_out), read by plots_dir in this process and its forked workers


def add_out_arg(ap):
    """--out: where a drawing script's figures go instead of beside the analysis (plots_dir)"""
    ap.add_argument("--out", default=None, help="the dir the figures go under: <out>/<analysis dir's name>/plots "
                                                "(default: <analysis dir>/plots, under the output dir for one in paper-results/)")


def use_plots_out(out):
    """set --out for plots_dir; refused under paper-results/"""
    global _PLOTS_OUT
    _PLOTS_OUT = refuse_results(out) if out else None


def plots_dir(d):
    """where the figures of the analysis dir `d` (a workload's dir or the analysis root) go: <--out>/<d's name>/plots,
    else <d>/plots, moved under the output dir when d is in paper-results/ (read, never written into)"""
    if _PLOTS_OUT:
        return os.path.join(_PLOTS_OUT, os.path.basename(os.path.normpath(d)), "plots")
    return figure_dir(os.path.join(d, "plots"))
