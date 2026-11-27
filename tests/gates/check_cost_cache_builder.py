#!/usr/bin/env python3
"""The one mix cost cache builder (kvcost.layout_search.search.problems) equals Problem() field by field: without a cache, with a
fresh cache, loaded from it, by problem(), and Joint's problems (one shared order).

Per workload: order, options, sizes, touch, the extra lanes' names, and per query its parts, frequency, one-run costs
and prefix mins. Prints each comparison; exit 1 on any difference or when nothing was compared.

    python3 tests/gates/check_cost_cache_builder.py --grid <SF3 meta grid> --sf 3 --workload w01,i01
"""
import argparse
import sys
import tempfile

from kvcost.layout_cost import benchmarks, picks
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_cost.compose import Composer, load, workload_units
from kvcost.layout_search import search as S


def fields(pb):
    return (pb.order, pb.options, pb.size, pb.touch, pb.extra,
            [(q["parts"], q["often"], q["one_run"], q["mins"]) for q in pb.queries])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--grid", required=True)
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--workload", default="w01,i01")
    ap.add_argument("--jobs", type=int, default=4, help="the fresh-cache build's cost cache slices at a time")
    a = ap.parse_args()
    composer = Composer(criterion=None, plan_rule=picks.PlanRule())
    data = load(a.grid, False, a.dataset, composer=composer)
    ws = benchmarks.select(a.dataset, a.sf, a.workload)
    extra = {"sum": lambda t: cost_of(t, "sum")}
    n = bad = 0

    def report(what, ok):
        nonlocal n, bad
        n += 1
        bad += not ok
        print(f"  {what}: {'equal' if ok else 'DIFFER'}")
    for name, w in ws.items():
        units = workload_units(w, data)
        ref = fields(S.Problem(w, data, units, extra=extra))
        S.set_cache(None)
        report(f"{name} ({len(ref[5])} queries), no cache", fields(S.problems([(w, data, units, extra)], 1)[0]) == ref)
        with tempfile.TemporaryDirectory() as d:
            S.set_cache(d)
            report(f"{name}, cache built", fields(S.problems([(w, data, units, extra)], a.jobs)[0]) == ref)
            report(f"{name}, cache loaded", fields(S.problems([(w, data, units, extra)], 1)[0]) == ref)
            report(f"{name}, problem()", fields(S.problem(w, data, units, extra)) == ref)
            S.set_cache(None)
    J = S.Joint(ws, data)
    for name, w in ws.items():
        order = [u for u in J.order if u in J.own[name]]
        report(f"Joint {name}", fields(J.pbs[name]) == fields(S.Problem(w, data, J.own[name], extra=extra, order=order)))
    ok = n > 0 and bad == 0
    print(f"check_cost_cache_builder {'OK' if ok else 'FAIL'}: {n} comparisons, {bad} differ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
