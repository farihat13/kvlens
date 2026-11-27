"""The output tree: where every result is written and read, one function per kind, the same shape for every dataset.

    output/
      stores/<dataset>/<scale>/<store>/                       a built store (measure/stores.py names <store>)
      stats/<dataset>/<scale>/                                the record stream, statistics, tallies, store-size.csv
      captures/<dataset>/<scale>/<family>/<params>/<kind>/<layout>.csv    measured: counts (+ <layout>.json, its
                                                              run record), steps, parts, row-hashes
      composed/<dataset>/<scale>/<family>/<params>/<kind>/<layout>.csv    the same files, composed from measured ones
                                                              (kvcost compose: the layouts not measured themselves)
      predicted/<dataset>/<scale>/<set>/<family>/<params>/<kind>/<layout>.csv   the same files, predicted from
                                                              statistics (kvcost predict; <set>: predicted-tier1,
                                                              predicted-tier2, each -anchor twin); never measured
      timings/<dataset>/<scale>/<family>/<layout>/<regime>/ timed runs (+ <family>/runs/: one record per run)
      summary/<dataset>/<scale>/<analysis>/...                a study's or an analysis's tables and plots
      engines/<dataset>/<scale>/<run>/...                     a live engine's run (fidelity/<family>/: its answers,
                                                              counters and live_kvstats.csv; a timing or sweep's own dir)
      figures/<figure>/<run>/                                 a paper figure drawn from one run (run: paper-results, i01-sf0.1, ...)
      specs/<layout>.json                                     a named layout's spec that no spec set in inputs/specs/ holds,
                                                              made from its name (kvevaluator.layouts.spec_of; the shared
                                                              tree's, as stores: kvevaluator.paths.specs_dir)
      paper/build/, paper/draft/                              the paper's tables and number keys; what the draft takes
      paper/run-times/                                        the run times recorded in paper-results/ (measure/run_costs.py)
      studies/<name>/                                         a study that writes a whole tree of its own (the same shape)
      logs/                                                   run logs; logs/journals/: the captures' cells in flight

<dataset> and <scale> are the dataset manifest's names (ldbc/sf0.1, linkbench/n1m, imdb-rows/full). <params> is
`reported` (the family's default param set), `held-out`, or another param set's own name (uniform, quick, ...).
<family> is a query set (interactive, bi, microbench, writes, job, linkbench, linkbench_writes), not a workload file.
<layout> is a layout id (kvevaluator.layouts.cell_id) or a named format (dgraph, nebula, ...).

Where the counts of a layout are (one rule, layout_capture, which every reader of a layout calls): a layout measured
itself (its counts file and run record in captures/) is read from captures/, any other from composed/. A reader names
the dataset's place by its captures/ dir; compose writes the layouts not measured, never a copy of a measured one.

A study that needs a tree of its own writes below Tree().study(<name>), in the same shape. Stores and stats are always
the shared tree's: they are named by what they hold, so every tree reads them in place and none builds its own.

    python -m kvevaluator.output_tree capture --family interactive --scale 0.1 [--params held-out] [--root DIR]
    python -m kvevaluator.output_tree composed|predicted|layout|stats|summary ...    (each kind's flags: --help)
"""
import argparse
import glob
import os
from pathlib import Path

from kvevaluator import datasets, workloads
from kvevaluator.paths import OUTPUT_DIR, resolve_recorded, specs_dir as specs_root, stats_dir as stats_root, store_dir as store_root

KINDS = ("counts", "steps", "parts", "row-hashes", "exact")   # a capture's file kinds (exact: a composed capture's marks)
REPORTED, HELD_OUT = "reported", "held-out"


def place(dataset, scale):
    """<dataset>/<scale>: the two names every kind's path starts with, from the dataset's manifest"""
    ds = datasets.get(dataset)
    return Path(ds.output_root_name()) / ds.scale_name(scale)


def default_param_set(workload):
    """the set a family reads when none is named (None): its params module's DEFAULT_PARAM_SET (LinkBench: log), else
    its first set (LDBC: ldbc), else None (a family with one set)"""
    mod = workloads.params_module(workload)
    sets = getattr(mod, "PARAM_SETS", ())
    return getattr(mod, "DEFAULT_PARAM_SET", sets[0] if sets else None)


def params_dir(workload, param_set=None):
    """the <params> name of a family's param set: `reported` for its first set (or a family with one set), else the
    set's own name; None is the family's default set (default_param_set); a set the family's params module does not
    list is refused"""
    sets = getattr(workloads.params_module(workload), "PARAM_SETS", ())
    if param_set is None:
        param_set = default_param_set(workload)
    if param_set is None or param_set == (sets[0] if sets else None):
        return REPORTED
    if param_set == HELD_OUT:
        return HELD_OUT
    if param_set not in sets:
        raise SystemExit(f"{workload}: no param set {param_set!r} (its sets: {', '.join(sets) or 'one, the default'})")
    return param_set


def checked_param_set(workload, param_set):
    """a driver's --param-set: None for None, or for the family's default set where that is its first set (its dir
    is reported/); else the set's name (LinkBench's anchors: reported/, not its default); a set the family does not list
    is refused (params_dir)"""
    first = params_dir(workload, param_set) == REPORTED and param_set == default_param_set(workload)
    return None if param_set is None or first else param_set


def capture_in(place_dir, workload, param_set=None):
    """a family's capture below a <kind>/<dataset>/<scale> dir: <place_dir>/<family>/<params>"""
    return Capture(Path(place_dir) / workload / params_dir(workload, param_set))


def place_pair(place_dir):
    """(captures/<dataset>/<scale>, composed/<dataset>/<scale>) of the tree place `place_dir` names (either kind), or
    None for any other dir"""
    p = Path(place_dir)
    if len(p.parts) < 4 or p.parent.parent.name not in ("captures", "composed"):
        return None
    root, rest = p.parent.parent.parent, p.relative_to(p.parent.parent)
    return root / "captures" / rest, root / "composed" / rest


def tree_root(place_dir):
    """the root of the tree whose captures/<dataset>/<scale> (or composed/) place `place_dir` is, else None"""
    pair = place_pair(os.path.abspath(place_dir))
    return None if pair is None else pair[0].parent.parent.parent


def tree_of(place_dir):
    """tree_root, any other dir refused"""
    root = tree_root(place_dir)
    if root is None:
        raise SystemExit(f"{place_dir}: not a tree's captures/<dataset>/<scale> or composed/<dataset>/<scale> place")
    return root


def same_place(a, b):
    """whether two dirs name one place: a tree's captures/<dataset>/<scale> and its composed/ twin are one place (a
    result row records the captures place of the tree it read, --from, as paths.recorded_path writes it: read under the
    output tree, else the repo); any other dir is itself"""
    full = lambda d: os.path.abspath(resolve_recorded(str(d), str(Tree().root)))
    key = lambda d: place_pair(full(d)) or full(d)
    return key(a) == key(b)


def own_tree(root):
    """the environment of a command run with the output tree `root` (KVEVAL_OUTPUT_DIR: it writes there), its stores
    stats and generated specs still the shared ones: KVEVAL_STORE_DIR, KVEVAL_STATS_DIR and KVEVAL_SPECS_DIR. Stores,
    stats and specs are named by what they
    hold, so every tree reads them in place (the record stream, the statistics, the tallies, the mix cost cache)"""
    root = Path(root).absolute()
    root.mkdir(parents=True, exist_ok=True)
    return dict(os.environ, KVEVAL_OUTPUT_DIR=str(root), KVEVAL_STORE_DIR=str(store_root()),
                KVEVAL_STATS_DIR=str(stats_root()), KVEVAL_SPECS_DIR=str(specs_root()))


def layout_capture(place_dir, workload, cell, param_set=None):
    """the capture that holds layout `cell` of a family: THE rule, every reader of a layout calls it. Below a tree's
    captures/<dataset>/<scale> or composed/<dataset>/<scale> dir (either names the place): the measured capture when
    the layout was measured itself (its counts file and its run record), else the composed one (kvcost compose). Any
    other dir (a test's fixture, a whole grid composed elsewhere) is read as it is"""
    pair = place_pair(place_dir)
    if pair is None:
        return capture_in(place_dir, workload, param_set)
    measured = capture_in(pair[0], workload, param_set)
    if os.path.exists(measured.path_of("counts", cell)) and os.path.exists(measured.record(cell)):
        return measured
    composed = capture_in(pair[1], workload, param_set)
    return composed if os.path.exists(composed.path_of("counts", cell)) else measured


def writes_layout_capture(place_dir, dataset, cell, param_set=None):
    """layout_capture of the dataset's write family (its param set as writes_in takes it)"""
    write = writes_in(place_dir, dataset, param_set)
    return layout_capture(place_dir, write.workload(), cell, write.param_set())


def timing_in(timings_dir, layout, regime):
    """one layout's timings of one regime below a family's timings dir (Tree.timings): <timings_dir>/<layout>/<regime>"""
    return Path(timings_dir) / layout / regime


def writes_in(place_dir, dataset, param_set=None):
    """the dataset's write family's capture below a <kind>/<dataset>/<scale> dir. `param_set` is the reads' set: the
    writes take it only when their family lists it, else their default set (they have no uniform or held-out rows, so a
    cost on the reads' uniform params adds the default writes, as kvcost measure writes them)"""
    write = workloads.names(kind="write", dataset=dataset)[0]
    sets = getattr(workloads.params_module(write), "PARAM_SETS", ())
    return capture_in(place_dir, write, param_set if param_set in sets else None)


class Capture:
    """one capture: <...>/<family>/<params>/, holding one file per (kind, layout)"""

    def __init__(self, path):
        self.path = str(path)

    def __repr__(self):
        return f"Capture({self.path!r})"

    def workload(self):
        return os.path.basename(os.path.dirname(os.path.normpath(self.path)))

    def params(self):
        return os.path.basename(os.path.normpath(self.path))

    def param_set(self):
        """the param set it holds, as checked_param_set gives it: None for `reported` where the family's default set is
        its first, else the set's name (LinkBench's reported/ holds anchors)"""
        if self.params() != REPORTED or self.workload() not in workloads.names():
            return None if self.params() == REPORTED else self.params()
        sets = getattr(workloads.params_module(self.workload()), "PARAM_SETS", ())
        return checked_param_set(self.workload(), sets[0]) if sets else None

    def dir(self, kind):
        if kind not in KINDS:
            raise ValueError(f"kind {kind!r}: one of {KINDS}")
        return os.path.join(self.path, kind)

    def path_of(self, kind, layout, ext="csv"):
        return os.path.join(self.dir(kind), f"{layout}.{ext}")

    def of_layout(self, layout):
        """the capture that holds `layout` for a reader given this one, its family's capture in a place: layout_capture
        with this capture's place, family and param set (measured itself, else composed). A capture outside a tree's
        captures/ or composed/ place (a fixture, a dir given as --counts) is read as it is"""
        place = os.path.dirname(os.path.dirname(os.path.normpath(self.path)))
        if place_pair(place) is None:
            return self
        return layout_capture(place, self.workload(), layout, self.param_set())

    def record(self, layout):
        """a layout's run record: counts/<layout>.json"""
        return self.path_of("counts", layout, "json")

    def stores(self, kind="counts"):
        """the layouts this capture holds files of (the stems of its kind dir's CSVs)"""
        return sorted(os.path.basename(p)[:-4] for p in glob.glob(os.path.join(self.dir(kind), "*.csv")))

    def has_held_out(self):
        """whether its family picks plans on held-out params (its manifest's pick_params); a path that is not a
        capture (<...>/<family>/<params>: a place dir given for a capture) is refused by name"""
        if self.workload() not in workloads.names():
            raise ValueError(f"{self.path} is not a capture (<...>/<family>/<params>): its family dir "
                             f"{self.workload()!r} is not a family; a <kind>/<dataset>/<scale> dir takes capture_in")
        return workloads.get(self.workload()).pick_params == HELD_OUT

    def held_out(self):
        """the same family's held-out capture beside this one; refused for a family that has none"""
        if not self.has_held_out():
            raise ValueError(f"{self}: {self.workload()} has no held-out params")
        return Capture(os.path.join(os.path.dirname(os.path.normpath(self.path)), HELD_OUT))


class Tree:
    """an output tree below `root` (default: the output dir); stores and stats are always the shared output dir's"""

    def __init__(self, root=None):
        self.root = Path(root) if root is not None else Path(OUTPUT_DIR)

    def capture(self, workload, scale, param_set=None):
        """the measured capture of a family at a scale (its dataset: the family's manifest)"""
        return self._capture("captures", workload, scale, param_set)

    def composed(self, workload, scale, param_set=None):
        """the composed capture: the same files, composed from measured layouts (kvcost.layout_cost.compose_cells)"""
        return self._capture("composed", workload, scale, param_set)

    def predicted(self, workload, scale, set_name, param_set=None):
        """a predicted capture (kvcost predict): the same files, predicted from statistics; `set_name` names the set
        (predicted-tier1, predicted-tier2, each -anchor twin; kvcost.cli.predict.TIER_SETS)"""
        return capture_in(self.predicted_dir(workloads.get(workload).dataset, scale, set_name), workload, param_set)

    def predicted_dir(self, dataset, scale, set_name):
        """<root>/predicted/<dataset>/<scale>/<set>: one predicted set of a dataset at a scale, shaped like composed/"""
        if not set_name or os.sep in set_name or set_name in (".", ".."):
            raise ValueError(f"predicted set {set_name!r}: a plain name (predicted-tier1, predicted-tier2, ...)")
        return self.place_dir("predicted", dataset, scale) / set_name

    def place_dir(self, kind, dataset, scale):
        """<root>/<kind>/<dataset>/<scale>: one dataset's captures (or composed captures) at one scale"""
        return self.root / kind / place(dataset, scale)

    def _capture(self, kind, workload, scale, param_set):
        return capture_in(self.place_dir(kind, workloads.get(workload).dataset, scale), workload, param_set)

    def summary(self, dataset, scale, *parts):
        """<root>/summary/<dataset>/<scale>/<analysis>/...: a study's or an analysis's tables and plots"""
        return self.root.joinpath("summary", place(dataset, scale), *parts)

    def engines(self, dataset, scale, *parts):
        """<root>/engines/<dataset>/<scale>/<run>/...: a live engine's run (Dgraph, NebulaGraph, ArangoDB)"""
        return self.root.joinpath("engines", place(dataset, scale), *parts)

    def timings(self, workload, scale, *parts):
        """<root>/timings/<dataset>/<scale>/<family>[/runs]; a layout's regime below it: timing_in"""
        return self.root.joinpath("timings", place(workloads.get(workload).dataset, scale), workload, *parts)

    def figure(self, figure, run):
        return self.root / "figures" / figure / run

    def logs(self, *parts):
        return self.root.joinpath("logs", *parts)

    def journals(self):
        """<root>/logs/journals: every capture's cells in flight (measure/grid_bodies.journal_dir), outside the captures"""
        return self.logs("journals")

    def paper(self, *parts):
        """<root>/paper/...: build/data (the cost cache), build/numbers (the number keys), draft (what the LaTeX draft
        takes), run-times (paper-results/'s recorded run times)"""
        return self.root.joinpath("paper", *parts)

    def study(self, name):
        """a study's own output tree, <root>/studies/<name>, in the same shape (a slice of a family, another param set)"""
        return Tree(self.root / "studies" / name)

    def stats(self, dataset, scale):
        """this tree's statistics of a dataset at a scale: <root>/stats/<dataset>/<scale>/ (records.jsonl, stats.json,
        store sizes); a command reads and writes the shared ones, stats_dir"""
        return self.root / "stats" / place(dataset, scale)

    def check(self, check, dataset, scale, *parts):
        """a check's own working files (the stores it builds for itself, its scratch): <root>/checks/<check>/<dataset>/<scale>,
        apart from the shared stores, so nothing that lists stores sees them"""
        return self.root.joinpath("checks", check, place(dataset, scale), *parts)


def _json(path):
    import json
    with open(path) as f:
        return json.load(f)


def stores_dir(dataset, scale):
    """the stores of a dataset at a scale: <stores root>/<dataset>/<scale>/ (the root: output/stores, or KVEVAL_STORE_DIR;
    kvevaluator.paths.store_dir, the one rule)"""
    return Path(store_root()) / place(dataset, scale)


def stats_dir(dataset, scale):
    """the shared statistics of a dataset at a scale: <stats root>/<dataset>/<scale>/ (the root: output/stats, or
    KVEVAL_STATS_DIR; kvevaluator.paths.stats_dir, the one rule)"""
    return Path(stats_root()) / place(dataset, scale)


def main():
    ap = argparse.ArgumentParser(description="Print a path of the output tree (the shell drivers' one way to get one).")
    ap.add_argument("kind", choices=tuple(PRINTS),
                    help="tree-of: the root of the tree whose captures place --root is; own-tree: make --root a tree "
                         "that reads the shared stores and stats (own_tree), print its root")
    ap.add_argument("--family", dest="workload", help="capture, composed, predicted, layout: a query set")
    ap.add_argument("--dataset", help="captures-dir, composed-dir, predicted-dir, layout, stores, stats, summary")
    ap.add_argument("--layout", help="layout: a cell id; prints the capture its counts are read from (layout_capture)")
    ap.add_argument("--set", dest="set_name", help="predicted, predicted-dir: the predicted set (predicted-tier1, predicted-tier2, ...)")
    ap.add_argument("--scale")
    ap.add_argument("--params", default=None, help="capture, composed, predicted: held-out or a param set (default: reported)")
    ap.add_argument("--analysis"); ap.add_argument("--subject")
    ap.add_argument("--figure"); ap.add_argument("--run"); ap.add_argument("--check")
    ap.add_argument("--name", action="append", default=[], help="logs: the dirs below logs/, in order (repeatable); study: its name")
    ap.add_argument("--root", default=None, help="the tree's root (default: the output dir)")
    a = ap.parse_args()
    print(PRINTS[a.kind](Tree(a.root), a))


# what main prints of each kind: (the tree --root names, the flags) -> a path
PRINTS = {
    "capture": lambda t, a: t.capture(a.workload, a.scale, a.params).path,
    "composed": lambda t, a: t.composed(a.workload, a.scale, a.params).path,
    "predicted": lambda t, a: t.predicted(a.workload, a.scale, a.set_name, a.params).path,
    "captures-dir": lambda t, a: t.place_dir("captures", a.dataset, a.scale),
    "composed-dir": lambda t, a: t.place_dir("composed", a.dataset, a.scale),
    "predicted-dir": lambda t, a: t.predicted_dir(a.dataset, a.scale, a.set_name),
    "layout": lambda t, a: layout_capture(t.place_dir("captures", a.dataset, a.scale), a.workload, a.layout,
                                          a.params).path,
    "stores": lambda t, a: stores_dir(a.dataset, a.scale),
    "stats": lambda t, a: stats_dir(a.dataset, a.scale),
    "logs": lambda t, a: t.logs(*a.name),
    "study": lambda t, a: t.study(a.name[0]).root,
    "root": lambda t, a: t.root,
    "check": lambda t, a: t.check(a.check, a.dataset, a.scale),
    "summary": lambda t, a: t.summary(a.dataset, a.scale, *[p for p in (a.analysis, a.subject) if p]),
    "figure": lambda t, a: t.figure(a.figure, a.run),
    "tree-of": lambda t, a: tree_of(a.root),
    "own-tree": lambda t, a: own_tree(a.root)["KVEVAL_OUTPUT_DIR"],
}


if __name__ == "__main__":
    main()
