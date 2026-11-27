"""The mix questions the paper reads, each written once in experiments/mix_benefit/mix_questions.json: its dataset, the
dir its rows go to, and its search-mix calls. `kvcost search-mix --question NAME --dataset <its dataset>=S [--from TREE]`
runs one (python/kvcost/cli/search.py); the paper's figures name the questions they need.

A question: dataset (the one it searches), rows (its dir under mix-search/), what, calls, and optionally source
("predicted": its calls read the default tier's predicted set, predicted/<dataset>/<scale>/<set>/, count_source) and larger (searched at the run's second scale of its dataset: the
same rows at another scale). A call is {workload, flags} (a workload id or file and search-mix's flags, the settings
written out: --settings Sp,Co,Ch1k,Ch4k,Ch16k,Ch64k,Ch256k). The command adds, per call: --from the tree read when one
is given (each layout by output_tree.layout_capture in its captures/ place), and a flag given as {scale: value,
"default": value} at the searched scale; every question's rows go to <output dir>/summary/<its dataset>/<its
scale>/mix-search/<rows>/mixes-of-<settings>[-by-direction].json, beside them its .csv and the mixes/ the rows hold.
Every row must say proven, but those searched with --search two-step (JOB's combined mix), --max-nodes and --search
local, as the paper's. Each row's times hold the two-step's two stages (the run-time table reads them there). A
question's mix is a layout argument elsewhere: mix:<question>/<model>[-<limit>] (results.mix_spec).
kvcost measure-latency Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k takes questions by name and times the mixes their rows hold (cli/latency.py TIMED).
"""
import json

from kvevaluator.paths import ROOT

TABLE = ROOT / "experiments" / "mix_benefit" / "mix_questions.json"


def questions():
    with open(TABLE) as f:
        return json.load(f)


def get(name):
    qs = questions()
    if name not in qs:
        raise SystemExit(f"--question {name}: one of {', '.join(qs)} ({TABLE.relative_to(ROOT)})")
    return qs[name]


def count_source(q):
    """the count source a question's calls read: None (measured), or for "source": "predicted" the default tier's
    predicted set (kvcost.cli.predict.default_set, the tier kvevaluator.predict.tier1.DEFAULT_TIER names); a question
    names no set or tier itself, so its predicted mix is costed on the tier every prediction reads by default"""
    src = q.get("source")
    if src is None:
        return None
    if src != "predicted":
        raise SystemExit(f"{TABLE.relative_to(ROOT)}: source {src!r}: \"predicted\" (the default tier's set) or none")
    from kvcost.cli.predict import default_set
    return default_set()


def workload_id(q):
    """the id of a question's workload (its first call's: an id, or a workload file's id), which names its timed mixes"""
    wl = q["calls"][0]["workload"]
    if not wl.endswith(".json"):
        return wl
    with open(ROOT / wl) as f:
        w = json.load(f)
    return w.get("id") or w["name"]


def flags_at(flags, scale):
    """a call's flags at a scale: a {scale: value, "default": value} by its value"""
    out = []
    for f in flags:
        if isinstance(f, dict):
            out.append(f.get(str(scale), f["default"]))
        else:
            out.append(f)
    return out
