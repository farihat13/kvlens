"""How a kvcost command finds its workload and its places: --workload (a workload file, or the id or name of one under
inputs/workloads/, kvevaluator.capture.workload_file.resolve) and --dataset's scale give a WorkloadFile; its dataset and families
give the pricer's benchmark (its adapter). Every command writes into the output tree's kinds
(python/kvevaluator/output_tree.py): its tables under summary/<dataset>/<scale>/<what>/<workload id>/ (the functions
below name each), its counts in captures/, its timings in timings/, its stores in stores/. No place is a workload's own
tree."""
import json
import os
import sys

from kvevaluator.capture import workload_file as WF
from kvevaluator.output_tree import Tree

# the summary/<dataset>/<scale>/ dirs the run commands write (search-mix's: kvcost.layout_search.results.home;
# cost-layouts': kvcost.layout_cost.layout_costs.place)
BUILT = "built-mixes"             # check-mix: mix_<sha>/mix_<sha>.json, the mix as built (named by its content)
LATENCY = "workload-latency"      # measure-latency: <workload id>/summary.csv (the raw timings: timings/)


def workload_arg(text):
    """--workload's type: a workload (a file, or the id or name of one; resolved where it is used). A family (a query set:
    kvevaluator.workloads.names()) is not a workload: it is given with --family"""
    from argparse import ArgumentTypeError
    from kvevaluator import workloads
    if text in workloads.names() and not os.path.isfile(text):
        raise ArgumentTypeError(f"{text} is a family (a query set), not a workload: --family {text}")
    return text


def reads_of(run, family):
    """the id (else name) of a workload file that holds exactly `run`'s items of the query set `family` (a workload with
    writes: its reads-only twin), or None"""
    want = sorted((it.query, it.frequency, json.dumps(it.params)) for it in run.items if it.family == family)
    for _p, w in WF.files(run.dataset, run.scale):
        items = w["items"]
        if all(it.get("family") == family for it in items) and \
                sorted((it["query"], it["frequency"], json.dumps(it.get("params"))) for it in items) == want:
            return w.get("id") or w.get("name")
    return None


def load(a):
    """the WorkloadFile of a command's --workload and --dataset's scale (a.sf)"""
    return WF.load(WF.resolve(a.workload, getattr(a, "dataset", None), getattr(a, "sf", None)), getattr(a, "sf", None))


def one_pair(a, cmd):
    """refuse a --dataset run of several pairs for a command that runs one dataset at one scale (cli.DatasetScale)"""
    run = getattr(a, "run", None)
    if isinstance(run, dict) and sum(len(v) for v in run.values()) > 1:
        from kvevaluator import sizes
        raise SystemExit(f"{cmd}: one --dataset pair, not the run {sizes.text(run)} (this command runs one dataset at one "
                         f"scale)")


def adapter_of(run):
    """the kvcost.cli.workloads adapter that prices a run: its dataset's, or its optional family's (LDBC's BI alone)"""
    from kvcost.cli import workloads as W
    return W.of(run.dataset, run.families())


def say_run(cmd, run):
    """the command's first line: the workload and the output tree in full (the paths printed after it are relative to it)"""
    print(f"[kvcost {cmd}] {run.name}: {len(run.queries())} queries ({', '.join(run.families())}) on {run.dataset} at "
          f"{run.scale}; output tree {Tree().root} (the paths below are relative to it)", flush=True)


def rel(path):
    """a path as a step prints it: relative to the output tree when inside it, else in full"""
    path, root = os.path.abspath(path), os.path.abspath(str(Tree().root))
    if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
        return os.path.relpath(path, root) + (os.sep if os.path.isdir(path) else "")
    return path


def recorded_argv(argv):
    """a command line as a results file records it: each absolute path in it (an argument, or a flag's =value) as
    paths.recorded_path writes it, relative to the output tree or the repo, naming no machine"""
    from kvevaluator.paths import recorded_path
    root = str(Tree().root)
    out = []
    for x in argv:
        head, eq, val = x.rpartition("=") if "=" in x and not x.startswith("/") else ("", "", x)
        out.append(head + eq + recorded_path(val, root) if os.path.isabs(val) else x)
    return out


def shown(path):
    """a file as a printed command names it: relative to the current dir when under it, else in full"""
    path = os.path.abspath(path)
    return os.path.relpath(path) if path.startswith(os.getcwd().rstrip(os.sep) + os.sep) else path


def answer(cmd, text):
    """the step's answer, its last line: '==> [kvcost <cmd>] <text>', bold on a terminal"""
    from kvevaluator import stale
    marked = stale.kinds()
    line = f"==> [kvcost {cmd}] {text}" + (f" [stale: {', '.join(marked)}]" if marked else "")
    print(f"\033[1m{line}\033[0m" if sys.stdout.isatty() else line, flush=True)


def layout_arg(dirs=False, formats=False, files=True):
    """a layout argument's type: the layout grammar (kvevaluator.layouts.arg_type: a name at any chunk size, a cell id,
    a spec file, and as asked a spec dir or a named format), or a mix named by the search row that holds it,
    mix:<question>/<model>[-<limit>] or mix:best (mix_paths resolves it)"""
    from argparse import ArgumentTypeError
    from kvevaluator import layouts as L
    grammar = L.arg_type(files=files, dirs=dirs, formats=formats)

    def check(text):
        t = text.strip()
        if t.startswith("mix:"):
            if t == "mix:" or " " in t:
                raise ArgumentTypeError(f"{t!r}: mix:<question>/<model>[-<limit>] or mix:best")
            return t
        return grammar(t)
    check.__name__ = "layout"
    return check


def mix_paths(a, layouts):
    """the layouts with each mix:<question>/<model>[-<limit>] or mix:best replaced by its spec file
    (kvcost.layout_search.results.mix_spec, read from the tree --from names at the command's dataset and scale)"""
    from kvcost.layout_search import results as R
    return [R.mix_spec(x, a) if str(x).startswith("mix:") else x for x in layouts]


def best_mix(run):
    """the spec of the last search-mix's mix of a workload (mix:best, as kvcost check-mix and the other commands take it): summary/<dataset>/<scale>/mix-search/
    <workload id>/mixes/best.json"""
    return os.path.join(searches_dir(run.dataset, run.scale, run.name), "mixes", "best.json")


def searches_dir(dataset, scale, rows, base=None):
    """the dir of one workload's (or one question's) search rows: <base>/mix-search/<rows>/, base summary/<dataset>/
    <scale> by default (kvcost.layout_search.results.home: the result file of each option set in it)"""
    from kvcost.layout_search import results as R
    return os.path.dirname(R.home(base or R.dataset_scale_dir(dataset, scale), rows, R.OPTION_SETS[0]))


def built_mix(path):
    """(name, full spec) of a mix spec file as check-mix builds it: the full form, named by its content (mix_<sha>), so
    one name never holds two specs (the store, the captures and the timings are shared by every workload)"""
    from kvevaluator import layouts as L
    with open(path) as f:
        full = L.expand(json.load(f))
    return L.mix_name(full), full


def built_mix_dir(dataset, scale, name):
    """a built mix's spec dir (one spec, as measure takes a spec set): summary/<dataset>/<scale>/built-mixes/<name>/"""
    return str(Tree().summary(dataset, scale, BUILT, name))


def layout_name(cell):
    """a layout as a step prints it: its short name (Co-Ch64k, Co-Ch4k) for a grid cell, else as given (a mix's name)"""
    from kvevaluator import layouts as L
    L.check_no_old_form(cell, "layout")            # an old spelling is refused, never shown as a mix's name
    try:
        return L.name_of(cell)
    except (KeyError, ValueError, TypeError, AttributeError):
        return cell
