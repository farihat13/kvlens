#!/usr/bin/env python3
"""Engine-op branch check: hand-built plans that drive each C++ engine op down the branches the recorded engine
plans never reach (check_engine_plans covers the plans as written), compared STEP BY STEP with the Python engine
formats: op and miss counts per step, and the final rows.

The cells of check_engine_plans sum each plan's steps, so two compensating per-step errors could cancel there; here
every step is compared on its own. sf0.1, canonical, one anchor person (ic2's first param).

  python3 tests/gates/check_engine_ops.py            # every case, both evaluators
  python3 tests/gates/check_engine_ops.py --db nebula
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from kvevaluator.paths import ROOT

EXP = ROOT
from kvengines import live_workloads as W
from kvevaluator.measure import plan_runs, server
from kvevaluator.capture import store_identity
from kvevaluator.measure.stores import ensure_store
from kvevaluator.formats import ENGINE_FORMATS
from kvevaluator.plans.plan import Step, ref
from kvevaluator.plans.ir import authoring as E
from kvevaluator.oracle.run import build_stores
from kvevaluator.oracle.runner import run_plan
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.schema.vids import vid
from kvevaluator.datasets import demo as data

SF = "0.1"
UNORDERED = "unordered"   # a case whose final rows have no defined order: compared as a multiset


class Reach:
    """What a case must show to have REACHED its branch (checked on the reference's run; the C++ side must then agree):
    `fn(rows, last)` over the final rows and the last step's counted KVCost. A case passing without it is vacuous."""
    def __init__(self, what, fn):
        self.what, self.fn = what, fn
COUNT_LANES = ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss")
DAY = 86400000


def cases(p, friend_vids):
    """{db: [(name, steps, [UNORDERED], Reach)]} around person vid `p` (friend_vids: its knows neighbours, for first_k_by_uid's synthetic
    uid map -- reverse vid order, so the uid order is not the input order): each case ends in the op under test."""
    msgs = [Step("m", "read_neighbors", srcs=[p], etype="hasCreator", dir="in")]
    friends = [Step("f", "read_neighbors", srcs=[p], etype="knows", dir="both")]
    dg = [
        ("sort_topk asc, no second key", msgs + [
            Step("mr", "read_vertex_props", vids=ref("m"), props=["creationDate"]),
            Step("t", "sort_topk", rows=ref("mr"), prop="creationDate", k=5, desc=False)],
         Reach("5 rows, walked forward (N > 0)", lambda r, c: len(r) == 5 and c.N > 0)),
        ("sort_topk desc, second key desc, k > candidates", msgs + [
            Step("mr", "read_vertex_props", vids=ref("m"), props=["creationDate"]),
            Step("t", "sort_topk", rows=ref("mr"), prop="creationDate", k=100000, second_key="id", id_order="desc")],
         Reach("every candidate returned, the second key read (G > 0)", lambda r, c: len(r) > 5 and c.G > 0)),
        ("sort_topk over the id index (asc)", friends + [
            Step("fr", "read_vertex_props", vids=ref("f"), props=["firstName"]),
            Step("t", "sort_topk", rows=ref("fr"), prop="id", k=3, index="id", desc=False)],
         Reach("3 rows over many id buckets (S > 100)", lambda r, c: len(r) == 3 and c.S > 100)),
        ("has_scan in", [Step("h", "has_scan", etype="replyOf", dir="in")],
         Reach("subjects found, the end landing a miss", lambda r, c: len(r) > 0 and c.N_miss == 1)),
        ("ineq_root_scan lt at a midnight", [Step("w", "ineq_root_scan", prop="creationDate", fname="lt",
                                                  hi=15340 * DAY)],
         Reach("rows, a backward walk (N > 0)", lambda r, c: len(r) > 0 and c.N > 0)),
        ("ineq_root_scan between, one day", [Step("w", "ineq_root_scan", prop="creationDate", fname="between",
                                                  lo=15340 * DAY + 3600000, hi=15340 * DAY + 7200000)],
         Reach("a one-day window, re-checked (rows < the day's bucket)", lambda r, c: len(r) > 0 and c.S > 3)),
        ("sort_without_index desc", friends + [Step("s", "sort_without_index", vids=ref("f"), prop="lastName",
                                                    k=4, desc=True)],
         Reach("4 rows of more candidates", lambda r, c: len(r) == 4 and c.S > 4)),
        ("read_vertex_by_id, a miss first", [Step("a", "read_vertex_by_id", vid=[vid("Person", 999999999999), p])],
         Reach("the miss paid, then the hit", lambda r, c: r == [p] and c.S == 2 and c.S_miss == 1)),
        ("read_anchor_props, a miss first", [Step("a", "read_anchor_props", cands=[vid("Person", 999999999999), p],
                                                  props=["id", "firstName"])],
         Reach("the miss paid, then one row", lambda r, c: len(r) == 1 and c.S_miss == 1)),
        ("first_k_by_uid", friends + [Step("k", "first_k_by_uid", items=ref("f"), k=2,
                                           uid_of=[[v, i] for i, v in enumerate(sorted(friend_vids, reverse=True), 1)])],
         Reach("the 2 smallest uids = the 2 largest vids", lambda r, c: r == sorted(friend_vids, reverse=True)[:2])),
    ]
    nb = [
        ("all_tag_props, no dedup", friends + [
            Step("ff", "concat", a=ref("f"), b=ref("f")),
            Step("t", "all_tag_props", vids=ref("ff"), dedup=False, tags=["Person", "Post"])],
         Reach("one row per INPUT row, 2 GETs each", lambda r, c: len(r) == 2 * len(friend_vids) and c.G == 2 * len(r))),
        ("read_vertex_props over a candidate list", msgs + [
            Step("r", "read_vertex_props", vids=ref("m"), props=["content", "id"], label=["Post", "Comment"])],
         Reach("missed Post tags, each followed by its seek", lambda r, c: c.G_miss > 0 and c.S == c.G_miss)),
        ("tag_probe with id and an absent prop", msgs + [
            Step("r", "tag_probe", vids=ref("m"), label="Post", props=["id", "imageFile", "language"])],
         Reach("hits and misses, no seek", lambda r, c: len(r) > 0 and c.G_miss > 0 and c.S == 0)),
        ("get_edge, missing", [Step("g", "get_edge", src=p, dst=p, etype="knows")],
         Reach("one missed GET, no row", lambda r, c: r == [] and c.G == 1 and c.G_miss == 1)),
        # a whole-index / whole-space scan has no defined order: it is the store's key order, which the structural
        # (Python) and encoded (C++: length-prefixed str terms, u64 vids) keys order differently -> compared as a set
        ("tag_index_scan, full scan + base rows", [Step("s", "tag_index_scan", label="Person", prop="firstName",
                                                        base_row=True, props=["firstName", "lastName"])], UNORDERED,
         Reach("one SEEK per partition, a base row per entry", lambda r, c: len(r) > 0 and c.S == 100 and c.G == len(r))),
        ("scan_vertices", [Step("s", "scan_vertices", label="TagClass")], UNORDERED,
         Reach("one SEEK per partition, a NEXT per vertex", lambda r, c: len(r) > 0 and c.S == 100 and c.N > len(r))),
    ]
    ar = [
        ("traverse_filtered_limit bfs, edge props", [
            Step("t", "traverse_filtered_limit", srcs=[p, p], etype="knows", dir="both", limit=2, order="bfs",
                 edge_props=["creationDate"], props=["firstName"], label="Person",
                 pred=E.gt(E.field("creationDate"), 0))],
         Reach("the LIMIT cuts each driving row (limit x rows < the edges)",
               lambda r, c: len(r) == 2 * 2 and c.G < 2 * (2 + 2 * len(friend_vids) + 2 * 2))),
        ("traverse_paths in, depth 2", msgs + [
            Step("t", "traverse_paths", srcs=ref("m"), etype="replyOf", dir="in", depth=2)],
         Reach("paths found", lambda r, c: len(r) > 0 and c.N >= len(r))),
        ("docs_per_step by vid and by a wrong collection", msgs + [
            Step("d1", "docs_per_step", vids=ref("m"), props=["id", "content"], label=["Post"]),
            Step("d2", "docs_per_step", vids=ref("m"), props=["content"], label="Post")],
         Reach("the comments miss the Post pk", lambda r, c: c.G_miss > 0 and len(r) > c.G_miss)),
        ("traverse_filtered: props + edge props, allowed + excluded, path_out", friends + [
            Step("t", "traverse_filtered", srcs=[p], etype="knows", dir="both", depth=2, props=[],
                 edge_props=["creationDate"], allowed=ref("f"), excluded=[p], path_out=True,
                 pred=E.ge(E.field("_depth"), 1))],
         Reach("depth-2 steps kept by allowed", lambda r, c: any(x["_depth"] == 2 for x in r))),
        ("traverse_filtered: props + label, a reachable excluded vertex", [
            Step("t", "traverse_filtered", srcs=[p], etype="knows", dir="both", depth=1, props=["firstName"],
                 label="Person", excluded=[sorted(friend_vids)[0]], pred=E.notnull(E.field("firstName")))],
         Reach("every friend but the excluded one, both passes read", lambda r, c:
               len(r) == len(friend_vids) - 1 and sorted(friend_vids)[0] not in {x["_vid"] for x in r}
               and c.G == 2 + 2 * len(friend_vids) + 2 * len(r))),
    ]
    return {"dgraph": dg, "nebula": nb, "arangodb": ar}


def answer_key(row):
    return json.dumps(row, sort_keys=True, default=str)


def answer(rows):
    return hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="", help="only this db's cases")
    a = ap.parse_args()
    from kvevaluator.datasets.ldbc.params import param_rows
    p = vid("Person", param_rows(SF, "ic2")[0]["personId"])
    dbs = [a.db] if a.db else list(store_identity.CPP_ENGINE_FORMATS)
    stores = build_stores(SF, dbs, "abstract", engine=True)
    fmt0 = ENGINE_FORMATS[dbs[0]](data.schema(SF))
    friend_vids = run_plan([Step("f", "read_neighbors", srcs=[p], etype="knows", dir="both")], fmt0,
                           CountingKV(stores[dbs[0]][1]))[0]["f"]
    compared, bad = 0, []
    for db in dbs:
        fmt_name = store_identity.CPP_ENGINE_FORMATS[db]
        with server.CppServer(ensure_store("ldbc", SF, fmt_name, chunk=None), fmt_name) as srv:
            for name, steps, *flags in cases(p, friend_vids)[db]:
                fmt = ENGINE_FORMATS[db](data.schema(SF))
                py_out, py_kv = run_plan(steps, fmt, CountingKV(stores[db][1], sizeof=fmt.value_bytes,
                                                                 keysizeof=fmt.key_bytes))
                cpp_out, cpp_kv = plan_runs.run_steps(srv, steps, name=f"{db}-{name}")
                diffs = []
                for (st, _, pa), (_, _, ca) in zip(py_kv, cpp_kv):
                    d = {l: (getattr(pa, l), getattr(ca, l)) for l in COUNT_LANES if getattr(pa, l) != getattr(ca, l)}
                    if d:
                        diffs.append(f"step {st.out} ({st.op}): {d}")
                last = steps[-1].out
                rows_py, rows_cpp = py_out[last], cpp_out[last]
                for f in flags:
                    if isinstance(f, Reach) and not f.fn(py_out[steps[-1].out], py_kv[-1][2]):
                        diffs.append(f"did not reach its branch: {f.what}")
                if UNORDERED in flags:
                    rows_py, rows_cpp = sorted(rows_py, key=answer_key), sorted(rows_cpp, key=answer_key)
                if answer(rows_py) != answer(rows_cpp):
                    diffs.append(f"rows differ ({len(rows_py)} vs {len(rows_cpp)})")
                ops = sum(getattr(pa, l) for _, _, pa in py_kv for l in ("S", "G", "N"))
                print(f"[check_engine_ops] {db:8s} {name}: {len(steps)} steps, {len(rows_py)} rows, {ops} ops"
                      f" -- {'DIFFER' if diffs else 'identical'}", flush=True)
                compared += 1
                bad += [f"{db} {name}: {x}" for x in diffs]
    for b in bad:
        print("DIFF " + b)
    total = sum(len(cases(p, friend_vids)[db]) for db in dbs)
    print(f"[check_engine_ops] {compared} cases compared of {total}, {len({b.split(':')[0] for b in bad})} differ")
    if bad or compared == 0 or compared != total:
        print("FAIL")
        return 1
    print("PASS: every step identical (op and miss counts) and every final answer identical")
    return 0


if __name__ == "__main__":
    sys.exit(main())
