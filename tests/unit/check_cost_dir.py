#!/usr/bin/env python3
"""cost-layouts' one place (kvcost.layout_cost.layout_costs.place_of_run): the output tree's summary/<dataset>/<scale>/
layout-costs/<workload id>/<count source>/, then one level for each non-default identity part: pick-by-<rule>,
pick-on-in-sample, op-weights-<G>-<S>-<N>, params-<set> (the default params and pick-on, named or not, keep the bare path; a set the
workload does not list is refused). No data, no store, no binary.

Run:  python3 tests/unit/check_cost_dir.py
"""
import argparse
import json
import os
import sys
import tempfile

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def refused(fn):
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


def main():
    tmp = tempfile.mkdtemp(prefix="cost-dir-")
    os.environ["KVEVAL_OUTPUT_DIR"] = os.path.join(tmp, "output")
    from kvcost.cli import runs
    from kvcost.layout_cost import layout_costs as LC
    from kvevaluator.output_tree import Tree
    from kvcost.cli.predict import default_set
    SET = default_set()   # a predicted set: the default tier's
    wl = os.path.join(tmp, "w.json")
    json.dump({"id": "w", "name": "w", "items": [{"family": "interactive", "query": "ic7", "frequency": 1, "params": None}]},
              open(wl, "w"))
    run = runs.load(argparse.Namespace(workload=wl, sf="0.1"))
    cost = os.path.join(tmp, "output", "summary", "ldbc", "sf0.1", "layout-costs", "w")
    at = lambda *a, **k: str(LC.place_of_run(Tree(), run, *a, **k))
    case("measured, the defaults: .../layout-costs/<id>/measured", at() == os.path.join(cost, "measured"), at())
    case("a predicted set: .../<id>/<set>", at(SET) == os.path.join(cost, SET))
    case("the default params named are the bare path", at(SET, param_set="ldbc") == os.path.join(cost, SET))
    case("a non-default param set: params-<set>", at(param_set="uniform") == os.path.join(cost, "measured", "params-uniform"))
    case("the default op weights are the bare path", at(op_weights="1:1.1:0.01") == os.path.join(cost, "measured"))
    case("other op weights: op-weights-<G>-<S>-<N>", at(op_weights="1:30:0.01") == os.path.join(cost, "measured",
                                                                                             "op-weights-1-30-0.01"),
         at(op_weights="1:30:0.01"))
    case("the levels in one order", at(pick_by="wops", pick_on="in-sample", param_set="uniform") ==
         os.path.join(cost, "measured", "pick-by-wops", "pick-on-in-sample", "params-uniform"))
    lb = argparse.Namespace(dataset="linkbench", scale="n1m", name="l01",
                            families=lambda: ["linkbench", "linkbench_writes"])
    lb_cost = os.path.join(tmp, "output", "summary", "linkbench", "n1m", "layout-costs", "l01", "measured")
    lb_at = lambda ps: str(LC.place_of_run(Tree(), lb, "measured", param_set=ps))
    case("LinkBench: the whole log (its default) adds no level, named or not; anchors is its own level, though its "
         "capture dir is reported/", lb_at(None) == lb_at("log") == lb_cost
         and lb_at("anchors") == os.path.join(lb_cost, "params-anchors"), (lb_at("log"), lb_at("anchors")))
    from kvcost.cli import workloads as W
    case("--pick-on as the place names it: the dataset's default (held-out; LinkBench's in-sample) adds no level, given "
         "or not; the other value is its level",
         W.place_pick_on("ldbc", ["interactive"], "held-out") is None and W.place_pick_on("ldbc", ["interactive"], None)
         is None and W.place_pick_on("ldbc", ["interactive"], "in-sample") == "in-sample"
         and W.place_pick_on("linkbench", ["linkbench"], "in-sample") is None
         and W.place_pick_on("linkbench", ["linkbench"], "held-out") == "held-out")
    why = refused(lambda: at(SET, param_set="nonsense"))
    case("a set the workload does not list is refused", why is not None and "nonsense" in why, why)
    n_ok = sum(results)
    print(f"check_cost_dir {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
