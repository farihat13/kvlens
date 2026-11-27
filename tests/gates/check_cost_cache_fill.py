#!/usr/bin/env python3
"""The vectorised cost cache fill (kvcost.layout_search.fill.vector_cost_cache) equals the scalar fill (search.Problem.scalar_fill:
one compose.query_cost per entry) entry for entry, on a grid's real workloads.

Per (option set, workload, query): both fills over every assignment of the query's parts, ops, bytes and one extra lane
(the sum, wops + bytes / 4,096, as the solves add it); equal means the same float, INF where no plan is usable. A query
the vectorised path hands back (vector_cost_cache returns None: a grouped option, a measure-only-where body, ...) is counted
as "scalar path" and not compared. A query shared by two workloads is checked once. Prints the cost cache and entries
compared and the two fills' seconds; exit 1 on any difference or when no entry was compared.

    python3 tests/gates/check_cost_cache_fill.py --dataset ldbc --sf 3 --grid <capture> [--workload i01,i02] [--size 4k]
    python3 tests/gates/check_cost_cache_fill.py --dataset linkbench --sf n1m --grid <capture> --historical --no-record --all-queries
    python3 tests/gates/check_cost_cache_fill.py --dataset ldbc --sf 3 --grid <BI capture> --families bi:read --all-queries

--all-queries: one workload of every query the grid holds (weight 1; a query's costs do not depend on the weights), for a
grid whose workloads are built elsewhere (BI, LinkBench, JOB).
"""
import argparse
import sys
import time

from kvcost.layout_cost import benchmarks, picks
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost.compose import Composer, add_pick_by_arg, load, workload_units
from kvcost.layout_cost.settings import size_options
from kvcost.layout_search import fill
from kvcost.layout_search.search import Problem, query_parts
from kvevaluator.measure import grid_capture


def the_sum(c):
    """the extra lane: wops + bytes / 4,096 of a query's cost vector"""
    t = CM.currencies(c)
    return CM.wops_bytes4k(t["ops"], t["bytes"])


def order_of(w, data, parts):
    """the search order Problem gives these parts for this workload (most-touched first)"""
    qparts = [query_parts(data[(fam, q)]) for fam, q, _, _ in w["items"]]
    degree = {u: sum(u in ps for ps in qparts) for u in parts}
    return sorted(parts, key=lambda u: (-degree[u], parts.index(u)))


def same(a, b):
    return len(a) == len(b) and all(x == y for x, y in zip(a, b))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--option-sets", default="per type,per type and direction")
    ap.add_argument("--historical", action="store_true", help="a committed capture read as captured (PlanRule historical)")
    ap.add_argument("--no-record", action="store_true", help="price without the pick record (LinkBench)")
    ap.add_argument("--max-entries", type=int, default=2_000_000, help="skip a query whose costs are larger (said)")
    ap.add_argument("--all-queries", action="store_true", help="one workload of every query the grid holds")
    ap.add_argument("--size", action="append", default=[], metavar="NAME", help="Chunked at another chunk size as "
                    "a setting (settings.size_options), e.g. 4k: its Chunked/Chunked in --grid's place")
    ap.add_argument("--vector-only", action="store_true", help="time the vectorised fill alone, compare nothing (the "
                    "seven-setting I01 cost cache: the scalar fill takes about an hour on 24 cores)")
    ap.add_argument("--families", default=None, help="the families the load reads, e.g. bi:read (default the suite's)")
    benchmarks.add_workload_arg(ap)
    add_pick_by_arg(ap)
    a = ap.parse_args()
    rule = picks.PlanRule(record=None if a.no_record else grid_capture.PICKS_FILE, historical=a.historical)
    fams = tuple(tuple(x.split(":")) for x in a.families.split(",")) if a.families else None
    composer = Composer.from_args(a, families=fams, sizes=size_options(a.size, a.grid), plan_rule=rule)
    ws = None if a.all_queries else benchmarks.select(a.dataset, a.sf, a.workload)
    sets = [s.strip() for s in a.option_sets.split(",")]
    extra = {"sum": the_sum}
    tables = entries = differ = scalar_path = skipped = 0
    t_scalar = t_vector = 0.0
    seen = set()
    for by_dir in (False, True):
        name_set = "per type and direction" if by_dir else "per type"
        if name_set not in sets:
            continue
        data = load(a.grid, by_dir, a.dataset, composer=composer)
        if a.all_queries:
            ws = {"all": {"id": "all", "items": [(f, q, 1.0, None) for (f, q) in sorted(k for k in data if isinstance(k, tuple))]}}
        for name, w in ws.items():
            units = workload_units(w, data)
            pb = Problem({"items": []}, data, units, extra=extra, order=order_of(w, data, list(units)))
            for item in w["items"]:
                d, pp, ps, where = pb.query_axes(data, item)
                opts = [pb.options[p] for p in where]
                key = (name_set, item[0], item[1], None if item[3] is None else tuple(item[3]), tuple(ps), tuple(map(tuple, opts)))
                if key in seen:
                    continue
                seen.add(key)
                n = 1
                for o in opts:
                    n *= len(o)
                if n > a.max_entries:
                    skipped += 1
                    print(f"  skip {name_set} {name} {item[1]}: {n:,} entries (over --max-entries)")
                    continue
                t0 = time.time()
                vec = fill.vector_cost_cache(d, pp, ps, opts, extra)
                t1 = time.time()
                if vec is None:
                    scalar_path += 1
                    continue
                if a.vector_only:
                    t_vector += t1 - t0
                    tables += 1
                    entries += n
                    continue
                sc = pb.scalar_fill(data, item, extra)
                t2 = time.time()
                t_vector += t1 - t0
                t_scalar += t2 - t1
                tables += 1
                entries += n
                ok = same(sc[0], vec[0]) and same(sc[1], vec[1]) and all(same(sc[2][k], vec[2][k]) for k in extra)
                if not ok:
                    differ += 1
                    bad = [i for i in range(n) if sc[0][i] != vec[0][i] or sc[1][i] != vec[1][i]][:3]
                    print(f"  DIFFER {name_set} {name} {item[1]}: {n} entries, first at {bad}: "
                          f"scalar {[(sc[0][i], sc[1][i]) for i in bad]} vector {[(vec[0][i], vec[1][i]) for i in bad]}")
    if a.vector_only:
        print(f"check_cost_cache_fill (vector only, nothing compared): {a.dataset} {a.sf}: {tables} queries, {entries:,} entries "
              f"filled in {t_vector:.1f} s ({1e6 * t_vector / max(entries, 1):.2f} us per entry); {scalar_path} on the "
              f"scalar path, {skipped} skipped")
        return 0 if entries > 0 else 1
    ok = differ == 0 and entries > 0
    print(f"check_cost_cache_fill {'OK' if ok else 'FAIL'}: {a.dataset} {a.sf}: {tables} queries, {entries:,} entries compared, "
          f"{differ} differ; {scalar_path} on the scalar path, {skipped} skipped; scalar {t_scalar:.1f} s, vector {t_vector:.1f} s")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
