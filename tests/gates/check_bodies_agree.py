"""Gate (sf0.1): every candidate body of a read query returns the same rows on every store it is legal on, per
(query, param row): one answer per row across the named formats, the meta cells and every body. A body that
disagrees is a plan bug (or a store bug) to fix or drop before any measurement at scale; a body that fails to run on
a store it is legal on fails too. The answer is candidates.result_key: the rows in the order the query sets (a top-k feeds
its output; rows tied on its keys as a set), else as a set. Runs every body over `kvevaluator serve` per store, the stores in parallel.

    just bodies-agree            # = python3 tests/gates/check_bodies_agree.py [--families interactive,bi,microbench] [--queries ...] [--sf 0.1]"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from kvevaluator.capture.binary import binary_provenance
from kvevaluator.datasets import schema_path
from kvevaluator.measure.grid_capture import meta_store, query_params
from kvevaluator.measure.grid_bodies import ServeRunner, request
from kvevaluator.plan_choice import generate as G
from kvevaluator.plan_choice.candidates import query_order, result_key, workload_queries

SCHEMA = schema_path("ldbc")
# the param rows: LDBC reports 1-5 and chooses on 11-15; BI and the microbench have no spare rows (1-5)
ROWS = {"interactive": [1, 2, 3, 4, 5, 11, 12, 13, 14, 15], "bi": [1, 2, 3, 4, 5], "microbench": [1, 2, 3, 4, 5]}


def store_path(st, sf):
    if st.fmt == "meta":
        return meta_store(sf, "interactive", st.spec, build=True)   # the LDBC store: BI and the microbench share it
    from kvevaluator.measure.stores import ensure_store
    return ensure_store("ldbc", sf, st.fmt, chunk=st.chunk, quiet=True)   # seconds at sf0.1 if missing


def run_store(st, path, work, binary):
    """[(workload, query, row, body name, sha, answer (result_key), error)] of every legal body on one store"""
    if path is None:
        return st.name, None
    runner = ServeRunner(binary, path, SCHEMA, 600, st.fmt)
    out = []
    try:
        for wl, q, gen, params in work:
            schema = G._schema(wl)
            sc = G.store_caps(st, schema, sorted(set(schema.vertex_props) | set(schema.etypes)))
            order = query_order({"plans": {b.name: {"steps": b.steps, "output": b.output} for b in gen.bodies.values()}})
            for sha, b in sorted(gen.bodies.items()):
                if G.legal(b, sc):
                    continue
                ir = {"plan": q, "params": gen.params, "steps": b.steps, "output": b.output}
                if gen.let:
                    ir["let"] = gen.let
                for idx, p in params:
                    resp, err = runner.call(request(path, SCHEMA, ir, p, 1, st.fmt))
                    answer = result_key(resp.get("rows"), order) if resp else ""
                    out.append((wl, q, idx, b.name, sha, answer, err))
    finally:
        runner.close()
    return st.name, out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--families", dest="workloads", default="interactive,bi,microbench")
    ap.add_argument("--queries", default=None)
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()
    prov = binary_provenance()
    print(f"binary {prov['path']}: {str(prov.get('binary_git_sha'))[:12]} dirty {prov.get('binary_dirty')}", flush=True)
    t0 = time.time()
    by_store = {}
    for wl in a.workloads.split(","):
        qs = a.queries.split(",") if a.queries else workload_queries(wl)[0]
        work = []
        for q in qs:
            if q not in workload_queries(wl)[0]:
                continue
            ps = query_params(wl, a.sf, q, max(ROWS[wl]))
            work.append((wl, q, G.generate(wl, q), [(i, ps[i - 1]) for i in ROWS[wl] if i <= len(ps)]))
        for st in G.stores(wl):
            by_store.setdefault(st, []).extend(work)
    from kvevaluator.capture.binary import binary_path
    binary = binary_path()
    results, skipped = {}, []
    # the paths first, one at a time (a store may be built on first use)
    paths = {st: store_path(st, a.sf) for st in by_store}
    with ThreadPoolExecutor(a.jobs) as ex:
        for name, rows in ex.map(lambda kv: run_store(kv[0], paths[kv[0]], kv[1], binary), by_store.items()):
            if rows is None:
                skipped.append(name)
                continue
            print(f"  {name}: {len(rows)} runs ({time.time() - t0:.0f}s)", flush=True)
            for wl, q, idx, body, sha, ans, err in rows:
                results.setdefault((wl, q, idx), []).append((name, body, sha, ans, err))
    split, failed, runs = [], [], 0
    for key, rs in sorted(results.items()):
        runs += len(rs)
        for r in rs:
            if r[4]:
                failed.append(f"{key} {r[0]} {r[1]}: {r[4][:160]}")
        ok = [r for r in rs if not r[4]]
        answers = {r[3] for r in ok}
        if len(answers) > 1:
            groups = {}
            for r in ok:
                groups.setdefault(r[3], []).append(f"{r[0]}:{r[1]}")
            split.append(f"{key}: {len(answers)} answers: " + " | ".join(f"{len(v)} x [{', '.join(sorted(v)[:4])}...]" for v in groups.values()))
    print(f"stores without an sf{a.sf} store here (not run): {skipped}")
    for title, rows in (("runs that failed on a store the body is legal on", failed),
                        ("(query, row) with more than one answer", split)):
        print(f"{title}: {len(rows)}")
        for r in rows[:40]:
            print("  ", r)
    print(f"covered: {len(results)} (query, row) groups, {runs} runs, {len(by_store) - len(skipped)} stores, "
          f"{time.time() - t0:.0f}s")
    ok = runs > 0 and not failed and not split
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
