#!/usr/bin/env python3
"""The files that show the mix search's inputs and outputs (python/kvcost/layout_search/export.py) say what the search
uses, and the trace does not change the search:

  1. cost cache: on the toy example (Person props, knows
     lists, likes lists; q1 reads knows and Person with two plans, probe and scan; q2 reads likes and Person, ten times
     as often), q1's costs have 9 entries, each checked equal to the search's cost cache, and the plan inside an entry
     follows Person's setting (probe on Condensed, scan on Split and Chunked)
  2. uniform: 2 queries x 9 uniform layouts, each equal to the cost cache entry with every part at that setting
  3. trace: search.solve with and without a trace gives the same answer, totals, node count and proof, on the toy
     (the fewest wops, the fewest bytes, the fewest wops under a byte limit) and on random planted problems (with a
     limit and a node limit); every row is checked against the search's rules (child numbers 2, 3, ... in order; a
     row's mix is its node's; each verdict's condition holds on its floor, best and the limit; a leaf's floor is its
     mix's total; a new best becomes the best; the answer is the last new best; trace_rows' columns); best_mix under a
     limit no mix fits ends the trace with the fallback's row; each verdict kind is seen at least once
  4. rows and spec: a result row's mix (its parts as --mix spells them, its settings in short: Ch4k) as CSV columns,
     and as a meta spec that reads back as the same mix (a chunk size option and per-direction parts included)

    python3 tests/unit/check_mix_search_export.py [--cases 300] [--seed 1]
Prints each kind's count of cases; exit 1 on a difference, or a kind that ran 0 cases.
"""
import argparse
import math
import random
import sys
from collections import Counter

from kvcost.layout_cost import compose
from kvcost.layout_cost.picks import PlanRule
from kvcost.layout_search import export as X
from kvcost.layout_search import questions as Q
from kvcost.layout_search import search as S

FAM = "toy"
COMPOSER = compose.Composer(criterion=None, plan_rule=PlanRule(in_sample_default=(FAM,)))
P, K, L = ("props", "Person"), ("adj", "knows"), ("adj", "likes")


class Data(dict):
    """the slice of compose.load's data the cost cache and the pricing read: queries by (family, query), the pricing"""
    def __init__(self, queries):
        super().__init__(queries)
        self.composer, self.options, self.sized = COMPOSER, {}, ()


def row(G=0, S=0, N=0, get_value=0):
    """a read part's cost vector, as compose.cost_vec makes it from a step row"""
    return compose.cost_vec({"G": G, "S": S, "N": N, "get_value": get_value}, False, COMPOSER)


def side(cell, u):
    p, a = cell[len("props_"):].split("_adj_")
    return p if u[0] == "props" else a


def query(name, cost, bodies, valid=None):
    """a toy query: cost(body, setting of the part, part) per part, every body valid on every cell unless `valid`"""
    d = {"fam": FAM, "q": name, "composer": COMPOSER, "sized_missing": {}, "valid": {c: set() for c in compose.CELLS}, "cost": {}}
    for b in bodies:
        for c in compose.CELLS:
            if valid is None or c in valid(b):
                d["valid"][c].add(b)
            parts = {u: cost(b, side(c, u), u) for u in cost.parts}
            d["cost"][(c, b, "1")] = {**parts, None: row()}
    for (c, v, i), rows in list(d["cost"].items()):   # the 64 KB size's rows: its Chunked/Chunked's, as compose.load reads them
        if c == compose.CH_CH:
            d["cost"][(compose.GRID_CHUNKED, v, i)] = {u: r for u, r in rows.items() if u is not None}
    return d


def toy():
    """the mix-choice example's shape: q1 probes or scans Person (probe wins only on Condensed), q2 probes"""
    probe_g = {"split": 50, "condensed": 5, "chunked": 50}
    scan_n = {"split": 1000, "condensed": 2000, "chunked": 500}
    knows_g = {"split": 3, "condensed": 1, "chunked": 2}

    def q1(b, s, u):
        if u == K:
            return row(G=knows_g[s], get_value=4096)
        return row(G=probe_g[s], get_value=100 * probe_g[s]) if b == "probe" else row(S=1, N=scan_n[s], get_value=8 * scan_n[s])
    q1.parts = [K, P]
    name_bytes = {"split": 4096, "condensed": 8192, "chunked": 65536}
    likes = {"split": row(G=2, get_value=64), "condensed": row(G=1, get_value=256), "chunked": row(S=1, get_value=256)}

    def q2(b, s, u):
        return likes[s] if u == L else row(G=1, get_value=name_bytes[s])
    q2.parts = [L, P]
    data = Data({(FAM, "q1"): query("q1", q1, ["probe", "scan"]), (FAM, "q2"): query("q2", q2, ["probe"])})
    w = {"id": "toy", "items": [(FAM, "q1", 1, None), (FAM, "q2", 10, None)]}
    return data, w, [K, L, P]


def check_tables(out):
    data, w, parts = toy()
    pb = S.Problem(w, data, parts)
    cols, rows = X.cost_cache_rows(pb, data, w, 0)            # checks each entry against the search's cost cache itself
    plans = {r["Person props"]: r["plan"] for r in rows}
    ok = len(rows) == 9 and cols == ["knows lists", "Person props"] and \
        all(r["plan"] == ("probe" if r["Person props"] == "condensed" else "scan") for r in rows)
    out.append(("cost cache", ok, f"q1's costs: {len(rows)} entries over {cols}, plans by Person's setting {plans}"))
    cols2, rows2 = X.cost_cache_rows(pb, data, w, 1)
    out.append(("cost cache", len(rows2) == 9 and {r["plan"] for r in rows2} == {"probe"}, f"q2's costs: {len(rows2)} entries"))
    return pb, data, w


def check_uniform(out, pb, data, w):
    rows = X.uniform_rows(w, data)
    tabs = {q: X.cost_cache_rows(pb, data, w, i)[1] for i, (_, q, _, _) in enumerate(w["items"])}
    bad = []
    for r in rows:
        p, a = r["layout"][len("props_"):].split("_adj_")
        match = [t for t in tabs[r["query"]]
                 if all(v == (p if name.endswith(" props") else a) for name, v in t.items() if name.endswith((" props", " lists")))]
        if len(match) != 1 or (match[0]["plan"], match[0]["wops"], match[0]["bytes"]) != (r["plan"], r["wops"], r["bytes"]):
            bad.append((r["query"], r["layout"]))
    out.append(("uniform", len(rows) == 18 and not bad, f"{len(rows)} rows; not equal to the cost cache: {bad}"))


def traced(pb, primary, cap=None, max_nodes=None):
    """solve without and with a trace: (both results equal, the trace, the result)"""
    i0, i1 = {}, {}
    plain = S.solve(pb, primary, cap, max_nodes=max_nodes, info=i0)
    trace = []
    with_t = S.solve(pb, primary, cap, max_nodes=max_nodes, info=i1, trace=trace)
    return plain[:4] == with_t[:4] and i0 == i1, trace, with_t, i1


def trace_consistent(pb, primary, cap, trace, result, info):
    """every row against the search's rules: child numbers 2, 3, ... in order; a row at node c extends the mix of the
    explored row that entered c; each verdict's condition holds on its floor, best and the limit; a leaf's floor is its
    mix's total; a new best becomes the best; the answer is the last new best (or the start); trace_rows' columns"""
    tol = lambda x: 0.0 if x == math.inf else 1e-9 * max(1.0, abs(x))
    assign, _, _, nodes, _ = result
    mix_of = {1: {}}                     # node -> the mix decided when it was entered
    nxt, prev_best = 2, None
    for i, t in enumerate(trace):
        v, fl, be, node = t["verdict"], t["floor"], t["best"], t["node"]
        where = f"row {i} ({v}, node {node})"
        if v in ("start", "dominated") or v.startswith("stop"):
            prev_best = be
            continue
        if v.startswith("node limit"):
            if node != nxt - 1 or info.get("proven", True):
                return False, f"{where}: the limit names node {node}, the last explored child was {nxt - 1}"
            continue
        if node not in mix_of:
            return False, f"{where}: a node no explored row entered"
        if v.startswith("leaf"):
            if t["mix"] != mix_of[node] or len(t["mix"]) != len(pb.order):
                return False, f"{where}: a leaf's mix {t['mix']} is not its node's {mix_of[node]}"
            combo = [pb.options[k].index(t["mix"][u]) for k, u in enumerate(pb.order)]
            o, b = pb.total(combo)
            want = (o, b) if primary == "ops" else (b, o)
            if any(abs(x - y) > tol(y) for x, y in zip(fl, want)):
                return False, f"{where}: floor {fl} is not the mix's total {want}"
            better = fl[0] < prev_best[0] - tol(prev_best[0]) or (abs(fl[0] - prev_best[0]) <= tol(prev_best[0])
                                                                   and fl[1] <= prev_best[1] + tol(prev_best[1]))
            if v == "leaf: new best" and (not better or be != fl):
                return False, f"{where}: a new best {fl} that is not below the best {prev_best}, or not made the best {be}"
            if v == "leaf: not better" and (be != prev_best or (fl[0] < prev_best[0] - tol(prev_best[0]))):
                return False, f"{where}: not better, but {fl} is below {prev_best} or the best moved to {be}"
            prev_best = be
            continue
        if t["mix"] != mix_of[node]:
            return False, f"{where}: mix {t['mix']} is not its node's {mix_of[node]}"
        if be != prev_best:
            return False, f"{where}: the best moved without a new-best leaf"
        floor_cut = fl[0] > be[0] + tol(be[0]) or (abs(fl[0] - be[0]) <= tol(be[0]) and fl[1] > be[1] + tol(be[1]))
        nb = fl[1 if primary == "ops" else 0]          # the floor's bytes: the primary lane when the search is on bytes
        over = cap is not None and nb > cap + tol(cap)
        if v == "explored":
            if floor_cut or over or t["child"] != nxt:
                return False, f"{where}: explored with floor {fl}, best {be}, limit {cap}, child {t['child']} (next {nxt})"
            mix_of[nxt] = {**t["mix"], t["part"]: t["option"]}
            nxt += 1
        elif v == "cut: floor not below the best" and not floor_cut:
            return False, f"{where}: floor {fl} is below the best {be}"
        elif v == "cut: over the byte limit" and (not over or floor_cut):
            return False, f"{where}: floor {fl}, limit {cap}, best {be}"
        elif v == "cut: no plan" and math.inf not in fl:
            return False, f"{where}: a finite floor {fl}"
    stopped = any(t["verdict"].startswith("stop") for t in trace)
    if (nodes, nxt - 1) != ((0, 1) if stopped else (nxt - 1, nxt - 1)):
        return False, f"{nodes} nodes, children numbered up to {nxt - 1}"
    news = [t for t in trace if t["verdict"] == "leaf: new best"]
    if assign is not None and news and news[-1]["mix"] != assign:
        return False, f"the last new best {news[-1]['mix']} is not the answer {assign}"
    model = "wops" if primary == "ops" else "bytes"
    cols, rows = X.trace_rows(trace, model)
    other = "bytes" if model == "wops" else "wops"
    if len(rows) != len(trace) or cols[-4:] != [f"floor_{model}", f"floor_{other}", f"best_{model}", f"best_{other}"] \
            or any(r[f"floor_{model}"] != (t["floor"] or (None,))[0] for r, t in zip(rows, trace)):
        return False, f"trace_rows: columns {cols}, {len(rows)} rows for {len(trace)} decisions"
    return True, ""


def check_trace(out, pb, data, w, kinds, cases, seed):
    for primary, cap in (("ops", None), ("bytes", None), ("ops", 0.0)):
        if cap is not None:      # a limit between the fewest bytes and the fewest-wops mix's bytes: the limit binds
            fewest_b = S.solve(pb, "bytes")[2]
            cap = (fewest_b + S.solve(pb, "ops")[2]) / 2
        same, trace, res, info = traced(pb, primary, cap)
        ok, why = trace_consistent(pb, primary, cap, trace, res, info)
        kinds.update(t["verdict"] for t in trace)
        out.append(("trace toy", same and ok, f"{primary} cap {cap}: same {same}; {why}; {len(trace)} rows"))
    for within in (1e-6, None):              # best_mix: a limit no mix fits ends the trace with the fallback's row
        rows = []
        for tr in (None, []):
            r = Q.best_mix(pb, data, w, "wops", within=within, ref_bytes=1.0, trace=tr)
            rows.append(({k: v for k, v in r.items() if k != "times"}, tr))
        (a, _), (b, tr) = rows
        last = tr[-1]["verdict"]
        ok = a == b and (last.startswith("no mix fits") if within else not any(t["verdict"].startswith("no mix") for t in tr))
        out.append(("trace best_mix", ok, f"within {within}: rows equal {a == b}, fits {a['fits']}, last row {last!r}"))
    rng = random.Random(seed)
    for _ in range(cases):
        data, w, parts = random_problem(rng)
        pb = S.Problem(w, data, parts)
        cap = None
        if rng.random() < 0.5:
            lo, hi = S.solve(pb, "bytes")[2], S.solve(pb, "ops")[2]
            if lo < float("inf") and hi < float("inf"):
                cap = lo + rng.random() * (hi - lo)
        max_nodes = rng.choice([None, None, 3])
        primary = rng.choice(["ops", "bytes"])
        same, trace, res, info = traced(pb, primary, cap, max_nodes)
        ok, why = trace_consistent(pb, primary, cap, trace, res, info)
        kinds.update(t["verdict"] for t in trace)
        out.append(("trace random", same and ok, f"cap {cap}, max_nodes {max_nodes}: same {same}; {why}"))


def random_problem(rng):
    """3 to 5 parts of the toy's labels and others, 2 to 4 queries, 1 to 3 bodies valid on random cells, small integer
    costs (ties)"""
    pool = [P, K, L, ("props", "Forum"), ("adj", "hasTag")]
    parts = rng.sample(pool, rng.randint(3, 5))
    qs = {}
    for i in range(rng.randint(2, 4)):
        reads = sorted(rng.sample(parts, rng.randint(1, len(parts))))
        table = {(b, s, u): row(G=rng.randint(0, 5), S=rng.randint(0, 2), get_value=4096 * rng.randint(0, 3))
                 for b in range(3) for s in compose.LAYOUTS for u in reads}

        def cost(b, s, u, table=table):
            return table[(int(b[1:]), s, u)]
        cost.parts = reads
        valid = {f"b{b}": [c for c in compose.CELLS if rng.random() < 0.8] for b in range(3)}
        qs[(FAM, f"q{i}")] = query(f"q{i}", cost, [f"b{b}" for b in range(rng.randint(1, 3))], valid=valid.get)
    data = Data(qs)
    used = sorted({u for d in qs.values() for per in d["cost"].values() for u in per if u is not None})
    w = {"id": "random", "items": [(FAM, q, 1 + i % 3, None) for i, (_, q) in enumerate(sorted(qs))]}
    return data, w, used


def check_rows_and_spec(out):
    rows = [{"workload": "toy", "option_set": "per-type-any-size", "question": "fewest", "model": "wops", "limit": None,
             "weighting": "sum", "sizes": [["chunked_c4k", "/x", "props_chunked_c4k_adj_chunked_c4k"]], "proven": True,
             "nodes": 6, "fits": True, "totals": {"wops": 75.0, "bytes": 2e6},
             "assignment": {"Person props": "Co", "knows lists": "Ch4k", "likes lists": "Sp"}},
            {"workload": "toy", "option_set": "per-type-and-direction", "question": "within", "model": "wops", "limit": 2.0,
             "weighting": "geomean", "score": {"form": "geomean", "value": 1.5}, "sizes": [], "proven": True, "nodes": 9, "fits": True, "totals": {"wops": 80.0, "bytes": 1e6},
             "assignment": {"Person props": "Ch64k", "knows out-lists": "Sp", "knows in-lists": "Ch64k",
                            "likes out-lists": "Co", "likes in-lists": "Co"}}]
    cols, csv_rows = X.result_rows(rows)
    want = {"Person props", "knows lists", "likes lists", "knows in-lists", "knows out-lists", "likes in-lists", "likes out-lists"}
    out.append(("rows", want <= set(cols) and csv_rows[0]["knows lists"] == "chunked_c4k" and csv_rows[1]["knows lists"] is None
                and csv_rows[0]["score"] is None and csv_rows[1]["score"] == 1.5
                and csv_rows[0]["sizes"] == "chunked_c4k", f"columns {cols}"))
    for r in rows:
        try:
            spec, base = X.row_spec(r)                  # reads back as the same mix, or SystemExit
            out.append(("spec", True, f"{r['option_set']}: built on {base}"))
        except SystemExit as e:
            out.append(("spec", False, f"{r['option_set']}: {e}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    out, kinds = [], Counter()
    pb, data, w = check_tables(out)
    check_uniform(out, pb, data, w)
    check_trace(out, pb, data, w, kinds, a.cases, a.seed)
    check_rows_and_spec(out)
    want_kinds = {"start", "dominated", "explored", "cut: floor not below the best", "cut: over the byte limit",
                  "cut: no plan", "stop: a query has no plan under any mix", "leaf: new best", "leaf: not better", "node limit: not entered"}
    out.append(("trace kinds", want_kinds <= set(kinds), f"seen {dict(sorted(kinds.items()))}; never {sorted(want_kinds - set(kinds))}"))
    by = Counter(k for k, _, _ in out)
    bad = [(k, why) for k, ok, why in out if not ok]
    for k, why in bad[:10]:
        print(f"FAIL {k}: {why}")
    print("cases: " + ", ".join(f"{k} {n}" for k, n in sorted(by.items())))
    expect = {"cost cache", "uniform", "trace toy", "trace best_mix", "trace random", "rows", "spec", "trace kinds"}
    if bad or not expect <= set(by):
        print(f"check_mix_search_export FAIL: {len(bad)} of {len(out)} cases; kinds with 0 cases: {sorted(expect - set(by))}")
        return 1
    print(f"check_mix_search_export OK: {len(out)} cases, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
