#!/usr/bin/env python3
"""The test of the workloads' why texts (why_numbers.py): no plain number unless declared scale-free with its reason,
and every {name} defined and used, for every LDBC workload with a why text (and with --sf, every workload of the
fresh run). With --sf, every text also renders from this scale's fresh run (the working output workload_story.py
wrote: every workload of the suite must have its folder there), so a placeholder that names a missing option, query or
part fails here, not in a README; the layout-choice driver runs it so after workload_story.

    python3 experiments/layout_choice/check_why_numbers.py [--sf 0.1 [--dataset ldbc] [--pick-by C] [--workload i01]]
    (exit 1 on any problem)
"""
import argparse
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
import why_numbers
from kvcost.layout_cost import benchmarks


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default=None, help="also render from this scale's fresh run (the working output)")
    ap.add_argument("--dataset", default="ldbc", choices=("ldbc", "linkbench"))
    ap.add_argument("--workload", default=None, help="only these workloads of the fresh run (ids or names)")
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    a = ap.parse_args()
    bad = []
    texts = {w["id"]: w for w in benchmarks.workloads("ldbc", "3").values() if w.get("why")}
    fresh = []                      # this scale's run: (workload, its folder), each must exist
    if a.sf is not None:
        for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
            if w.get("why"):
                fresh.append((w, benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=a.criterion, param_set=a.param_set)))
    n_fresh = 0
    for w, d in fresh:
        if not os.path.exists(os.path.join(d, "options.csv")):
            bad.append(f"{w['id']}: the fresh run wrote no {d}/options.csv (run workload_story.py first)")
            continue
        try:
            why_numbers.render(w, why_numbers.Data.from_dir(d))
            n_fresh += 1
        except KeyError as e:
            bad.append(f"{w['id']}: {d} (the fresh run): {e}")
    for w, _ in fresh:
        texts.setdefault(w["id"], w)
    for w in texts.values():
        bad += [f"{w['id']}: {p}" for p in why_numbers.problems(w)]
    print(f"why texts: {len(texts)} checked" + (f", {n_fresh} of {len(fresh)} rendered from the sf{a.sf} run"
                                                  if a.sf is not None else "") + f", {len(bad)} problems")
    for b in bad:
        print("  FAIL", b)
    sys.exit(1 if bad or (a.sf is not None and n_fresh == 0) else 0)


if __name__ == "__main__":
    main()
