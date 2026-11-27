#!/usr/bin/env python3
"""The saved mix cost cache (search.set_cache): a query's costs loaded from the cache equal the ones built fresh, a shared
query is priced once, and a changed input or source misses. Analysis only: opens no store.

    python3 tests/gates/layout_choice/check_cost_cache.py --sf 0.1 --grid <grid> [--size 4k
        --size 16k] --cache <new dir> [--queries is1,is3,iu4 | all] [--per-type-only] [--jobs 8]
        [--no-microbench] [--control-no-data-key | --control-no-composer-key]
With no --size, the nine cells only (run/meta-gates.sh stream k, on the round's own capture: 8 cases, about 15 s).

Cases (I01 then I02, the chunk-size front's option set; per type, and per type and direction unless --per-type-only):
  1. I01 built with no cache = built into an empty cache (all miss) = loaded from it (all hit), field by field;
     no repo module outside cost_cache.SOURCES was imported by the fills;
  1c. I01 again in a new process, the same inputs: every query loaded, none built (a key that names something of one
     process, such as an object's address, would miss every run);
  2. I02 after I01: every I02 query is a hit (its reads are I01's);
  3. one query's costs x 1.5 in memory (its costs move): exactly that query misses, and its costs equal a fresh build;
  4. a source file's content changed (its hash in cost_cache.SOURCES): every query misses;
  5. --fresh: every query's costs built again, equal;
  6. the same grid loaded under another pricing setting (compose.Composer, bind_per_type_record off: the parts' costs are
     the same numbers): every query misses.
--control-no-data-key: the key without the query's data (a control: case 3 must then FAIL, stale costs served).
--control-no-composer-key: the key without the query's pricing settings (a control: case 6 must then FAIL).
Exit 1 when a case goes the wrong way.
"""
import argparse
import copy
import os
import re
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import compose, settings
from kvcost.layout_cost import benchmarks
from kvcost.layout_search import search as cost_cache
from kvevaluator.paths import ROOT
from kvcost.layout_cost.compose import add_pick_by_arg

FIELDS = ("parts", "order", "options", "size", "queries", "touch")


def same(a, b):
    """the fields the search reads, equal (the cost cache, their order, the weights)"""
    return all(getattr(a, f) == getattr(b, f) for f in FIELDS)


def build(specs, jobs, cache, fresh=False):
    cost_cache.set_cache(cache, fresh)
    t0 = time.time()
    out = cost_cache.problems(specs, jobs)
    print(f"   build: {time.time() - t0:.1f} s, cache {cache or 'off'}{' fresh' if fresh else ''}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--grid", required=True)
    ap.add_argument("--size", action="append", default=[], metavar="NAME", help="another chunk size (1k): its Chunked/Chunked in --grid's place; repeatable")
    ap.add_argument("--cache", required=True, help="a new dir (removed at the end)")
    ap.add_argument("--per-type-only", action="store_true")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--control-no-data-key", action="store_true")
    ap.add_argument("--control-no-composer-key", action="store_true")
    ap.add_argument("--queries", default="is1,is3,iu4", help="the workloads' items kept (a comma list, or 'all'): a fill takes "
                    "seconds, not minutes (the default: two reads I02 shares and one write I01 alone has, 2-5 parts each, "
                    "at most 3,125 entries a query)")
    ap.add_argument("--no-microbench", action="store_true", help="a grid with the LDBC reads and writes only (I01 and I02 read no other)")
    ap.add_argument("--child-load", action="store_true", help=argparse.SUPPRESS)   # case 1c's second process: I01 into --cache
    add_pick_by_arg(ap)
    a = ap.parse_args()
    if os.path.exists(a.cache) and not a.child_load:
        sys.exit(f"{a.cache} exists: give a new dir")
    composer = compose.Composer(criterion=a.criterion, families=benchmarks.families("ldbc", drop=("microbench",)) if a.no_microbench else None,
                              sizes=settings.parse_sizes(a.size, a.grid),
                              plan_rule=compose.PlanRule())
    if a.control_no_data_key:            # the control: a key blind to the query's data serves stale costs in case 3
        cost_cache._canon = lambda d: "same"
    if a.control_no_composer_key:         # the control: a key blind to the pricing settings serves another setting's costs
        canon = cost_cache._canon
        cost_cache._canon = lambda d: canon({k: v for k, v in d.items() if k != "composer"} if isinstance(d, dict) else d)
    unit_sets = (False,) if a.per_type_only else (False, True)
    ws = benchmarks.select("ldbc", a.sf, "i01,i02")
    (n16, i01), (n17, i02) = list(ws.items())
    if a.queries != "all":               # a slice of the workloads: the same cases on fewer queries
        keep = set(a.queries.split(","))
        i01, i02 = ({**w, "items": [it for it in w["items"] if it[1] in keep]} for w in (i01, i02))
        if not i01["items"] or not i02["items"]:
            sys.exit(f"--queries {a.queries}: I01 keeps {len(i01['items'])}, I02 {len(i02['items'])}; both need one or more")
    datas = {bd: compose.load(a.grid, bd, composer=composer) for bd in unit_sets}
    spec = lambda w: [(w, datas[bd], compose.workload_units(w, datas[bd])) for bd in unit_sets]
    stats = []
    real_print = cost_cache.print if hasattr(cost_cache, "print") else print

    def tap(*args, **kw):                # keep each problems() summary line (hits, misses) for the cases
        if args and str(args[0]).startswith("[mix cost cache] ") and "loaded" in str(args[0]):
            stats.append(str(args[0]))
        real_print(*args, **kw)
    cost_cache.print = tap
    bad, n = 0, 0

    def case(ok, what):
        nonlocal bad, n
        n += 1
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}", flush=True)

    def hm(line):
        """(loaded, built) from a summary line"""
        m = re.search(r"(\d+) queries' costs loaded, (\d+) built", line)
        return int(m.group(1)), int(m.group(2))

    if a.child_load:                     # case 1c: the parent reads this process's summary line
        build(spec(i01), a.jobs, a.cache)
        return 0
    ref = build(spec(i01), a.jobs, None)
    cold = build(spec(i01), a.jobs, a.cache)
    h_cold = hm(stats[-1])
    warm = build(spec(i01), a.jobs, a.cache)
    h_warm = hm(stats[-1])
    nq = sum(len(pb.queries) for pb in ref)
    case(all(same(x, y) and same(x, z) for x, y, z in zip(ref, cold, warm)) and h_cold == (0, nq) and h_warm == (nq, 0),
         f"1 {n16}: {nq} queries' costs over {len(unit_sets)} unit sets; no cache = cold {h_cold} = warm {h_warm} (loaded, built)")
    exp = str(ROOT)
    srcs = set(cost_cache.SOURCES)
    extra = sorted(os.path.relpath(m.__file__, exp) for k, m in list(sys.modules.items()) if getattr(m, "__file__", None)
                   and os.path.abspath(m.__file__).startswith(exp + os.sep) and os.path.abspath(m.__file__) not in srcs
                   and not os.path.abspath(m.__file__).startswith(os.path.abspath(sys.prefix) + os.sep)   # the venv may sit in the repo
                   and os.path.abspath(m.__file__) != os.path.abspath(__file__))
    case(not extra, f"1b repo modules loaded and not in cost_cache.SOURCES: {len(extra)} {extra}")
    child = subprocess.run([sys.executable, os.path.abspath(__file__), *sys.argv[1:], "--child-load"], capture_output=True,
                           text=True)
    lines = [x for x in (child.stdout + child.stderr).splitlines() if "queries' costs loaded" in x]
    h_new = hm(lines[-1]) if child.returncode == 0 and lines else None
    case(h_new == (nq, 0), f"1c {n16} again in a new process: {h_new} (loaded, built), want ({nq}, 0)"
         + ("" if child.returncode == 0 else f"; rc {child.returncode}: {child.stderr[-300:]}"))

    r17 = build(spec(i02), a.jobs, a.cache)
    h17 = hm(stats[-1])
    f17 = build(spec(i02), a.jobs, None)
    n17q = sum(len(pb.queries) for pb in f17)
    case(h17 == (n17q, 0) and all(same(x, y) for x, y in zip(r17, f17)), f"2 {n17} after {n16}: {h17} of {n17q} (loaded, built), equal to a fresh build")

    # 3: one cost of one query changed (a copy of the data): that query, and only it, misses
    bd = unit_sets[0]
    data = datas[bd]
    fam, q = i01["items"][0][0], i01["items"][0][1]
    orig = data[(fam, q)]
    dq = copy.deepcopy(orig)
    for key in ("cost", "cost_h"):        # every cost of the query x 1.5: its costs must change, whichever plan is picked
        for per in dq.get(key, {}).values():
            for vec in per.values():
                for c in vec:
                    vec[c] *= 1.5
    data[(fam, q)] = dq                  # the plant, in place (the load's attributes stay); restored below
    sp = [(i01, data, compose.workload_units(i01, data))]
    try:
        got = build(sp, a.jobs, a.cache)
        h3 = hm(stats[-1])
        fresh3 = build(sp, a.jobs, None)
    finally:
        data[(fam, q)] = orig
    moved = not same(fresh3[0], ref[0])
    case(h3 == (len(i01["items"]) - 1, 1) and same(got[0], fresh3[0]) and moved,
         f"3 the costs of {fam} {q} x 1.5: {h3} (loaded, built), want ({len(i01['items']) - 1}, 1); equal to a fresh build: "
         f"{same(got[0], fresh3[0])}; the plant moved the costs: {moved}")

    # 4: a source file's content changed: its hash differs, every query misses
    src = os.path.join(cost_cache.HERE, "search.py")
    keep = cost_cache._FILE_SHA.get(src)
    cost_cache._FILE_SHA[src] = "an edited search.py"
    build(spec(i01), a.jobs, a.cache)
    h4 = hm(stats[-1])
    cost_cache._FILE_SHA[src] = keep
    case(h4 == (0, nq), f"4 search.py changed: {h4} (loaded, built), want (0, {nq})")

    # 5: --fresh builds every query's costs again, equal
    f5 = build(spec(i01), a.jobs, a.cache, fresh=True)
    h5 = hm(stats[-1])
    case(h5 == (0, nq) and all(same(x, y) for x, y in zip(ref, f5)), f"5 --fresh: {h5} (loaded, built), equal to no cache")

    # 6: another pricing setting, the same numbers: the key must still differ (the settings are an input of the costs)
    other = {bd: compose.load(a.grid, bd, composer=composer.but(bind_per_type_record=False)) for bd in unit_sets}
    build([(i01, other[bd], compose.workload_units(i01, other[bd])) for bd in unit_sets], a.jobs, a.cache)
    h6 = hm(stats[-1])
    case(h6 == (0, nq), f"6 another pricing setting (bind_per_type_record off): {h6} (loaded, built), want (0, {nq})")

    nf, nb = cost_cache.cache_size()
    print(f"the cache: {nf} files, {nb / 2**20:.1f} MiB; removed")
    shutil.rmtree(a.cache)
    print(f"{n} cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
