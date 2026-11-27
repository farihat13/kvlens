#!/usr/bin/env python3
"""The output tree (kvevaluator.output_tree) gives one path shape for every dataset, workload and scale. No data, under a
second.

    python3 tests/unit/check_output_tree.py
"""
import os
import sys
import tempfile

from kvevaluator import workloads
from kvevaluator.output_tree import (HELD_OUT, Capture, Tree, capture_in, own_tree, params_dir, stats_dir, stores_dir,
                                     writes_in)
from kvevaluator.paths import OUTPUT_DIR, specs_dir, store_dir

BAD = []
N = [0]


def check(name, ok, got=None):
    N[0] += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}"))
    if not ok:
        BAD.append(name)


def refused(fn):
    try:
        fn()
    except (SystemExit, ValueError):
        return True
    return False


def main():
    root = tempfile.mkdtemp()
    t = Tree(root)
    want = {("interactive", "0.1"): "captures/ldbc/sf0.1/interactive/reported",
            ("writes", "3"): "captures/ldbc/sf3/writes/reported",
            ("bi", "0.1"): "captures/ldbc/sf0.1/bi/reported",
            ("microbench", "0.1"): "captures/ldbc/sf0.1/microbench/reported",
            ("job", "full"): "captures/imdb-rows/full/job/reported",
            # LinkBench's default set is its whole request log (log/; its first set, anchors, is reported/)
            ("linkbench", "n1m"): "captures/linkbench/n1m/linkbench/log",
            ("linkbench_writes", "n1m"): "captures/linkbench/n1m/linkbench_writes/log"}
    for (w, s), rel in want.items():
        got = t.capture(w, s).path
        check(f"{w} at {s}: {rel}", got == f"{root}/{rel}", got)
    check("every workload has a case", set(workloads.names()) <= {w for w, _ in want},
          sorted(set(workloads.names()) - {w for w, _ in want}))

    held = t.capture("interactive", "0.1", HELD_OUT)
    check("held-out sits beside reported", held.path == f"{root}/captures/ldbc/sf0.1/interactive/held-out", held.path)
    check("reported.held_out() is that dir", t.capture("interactive", "0.1").held_out().path == held.path)
    check("a workload with no held-out params refuses held_out()", refused(lambda: t.capture("job", "full").held_out()))
    check("an unknown param set is refused", refused(lambda: params_dir("interactive", "no-such-set")))
    check("the default set's name is `reported`", params_dir("interactive", None) == "reported")

    comp = t.composed("interactive", "0.1")
    check("composed has the captures' shape", comp.path == f"{root}/composed/ldbc/sf0.1/interactive/reported", comp.path)
    check("capture_in a place dir = Tree.capture",
          capture_in(t.place_dir("captures", "ldbc", "0.1"), "interactive").path == t.capture("interactive", "0.1").path)
    check("writes_in a place dir finds the dataset's write workload",
          writes_in(t.place_dir("captures", "linkbench", "n1m"), "linkbench").workload() == "linkbench_writes")
    cap = Capture(f"{root}/captures/ldbc/sf0.1/interactive/reported")
    check("a capture reads its workload and params from its path", (cap.workload(), cap.params()) == ("interactive", "reported"))
    check("a file is <kind>/<layout>.csv, its run record counts/<layout>.json",
          (cap.path_of("steps", "x"), cap.record("x")) == (f"{cap.path}/steps/x.csv", f"{cap.path}/counts/x.json"))

    check("stores and stats are always the shared tree's",
          (str(stores_dir("ldbc", "0.1")), str(stats_dir("linkbench", "n1m")))
          == (f"{store_dir()}/ldbc/sf0.1", f"{OUTPUT_DIR}/stats/linkbench/n1m"))
    with tempfile.TemporaryDirectory() as tmp:
        env = own_tree(os.path.join(tmp, "elsewhere"))
        check("a tree written elsewhere reads the shared stats by KVEVAL_STATS_DIR, with no stats link in it",
              (env["KVEVAL_STATS_DIR"], os.listdir(os.path.join(tmp, "elsewhere")))
              == (str(stats_dir("ldbc", "0.1").parent.parent), []), env.get("KVEVAL_STATS_DIR"))
        os.environ["KVEVAL_STATS_DIR"] = os.path.join(tmp, "shared-stats")
        try:
            check("KVEVAL_STATS_DIR names where every command reads the stats",
                  str(stats_dir("ldbc", "3")) == os.path.join(tmp, "shared-stats", "ldbc", "sf3"), str(stats_dir("ldbc", "3")))
        finally:
            del os.environ["KVEVAL_STATS_DIR"]
        check("a tree written elsewhere reads and writes the shared specs by KVEVAL_SPECS_DIR",
              env.get("KVEVAL_SPECS_DIR") == str(specs_dir()), env.get("KVEVAL_SPECS_DIR"))
        os.environ["KVEVAL_SPECS_DIR"] = os.path.join(tmp, "shared-specs")
        try:
            check("KVEVAL_SPECS_DIR names where a spec made from a layout's name goes",
                  str(specs_dir()) == os.path.join(tmp, "shared-specs"), str(specs_dir()))
        finally:
            del os.environ["KVEVAL_SPECS_DIR"]
    check("a study is a tree of its own; the paper's build and the journals have one place each",
          (str(t.study("uniform-params").capture("interactive", "0.1").path), str(t.paper("build", "data")), str(t.journals()))
          == (f"{root}/studies/uniform-params/captures/ldbc/sf0.1/interactive/reported", f"{root}/paper/build/data",
              f"{root}/logs/journals"))
    check("summary and figure paths",
          (str(t.summary("ldbc", "3", "uniform-49", "i01")), str(t.figure("uniform-49", "paper-results")))
          == (f"{root}/summary/ldbc/sf3/uniform-49/i01", f"{root}/figures/uniform-49/paper-results"))

    print(f"check_output_tree {'FAIL' if BAD else 'OK'}: {N[0] - len(BAD)}/{N[0]} cases")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
