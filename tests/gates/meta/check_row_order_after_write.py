#!/usr/bin/env python3
"""Row order after a write inside one plan: does every layout hand on the same rows in the same order when a step reads
a list an earlier step of the SAME plan wrote? (The composition proof's A2(ii) and A3.)
No workload plan reads after it writes, so this check runs three fixture plans
(fixtures/write_then_read/: insert an edge, delete an edge, insert two edges; each then reads the source's out list
and the destination's in list) on every cell store and compares, per (plan, param, step), the digest of the step's
output rows (run.emit.row_digest: order included) across the stores. The fixtures are no workload's plans: nothing is
captured and no number comes from them.

Cases: per source Person (the writes workload's own sources, plus the first and the last Person by vid: the owners at
a chunk's edges) an insert of a new neighbour whose id is below, inside and above the ids the list holds, two inserts
into one list, and a delete of the first and the last stored neighbour. Also required, so the check cannot pass on a
write nobody sees: after an insert the out list has one row more than before the write, after a delete one less.

--control flips one digest of one store in the collected data: the check must fail and name that store.
--expect-fail: the binary is a control built to return another order; exit 0 only if it is caught by its signature
(steps with the same rows in "another ORDER", nothing else). A throw, other rows or a wrong row count is CONTROL VOID.

  python3 tests/gates/meta/check_row_order_after_write.py [--sf 0.1] [--nparam 5] [--spec-dir DIR ...] [--control]
Exit 0 = every step's rows equal, in order, on every store (and the control caught). Needs the stores
(kvcost measure, or the gate round's); the binary is KVEVAL_CPP_BIN or the build's.
"""
import argparse
import copy
import glob
import json
import os
import subprocess
import sys
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.capture import binary
from kvevaluator.measure import records
from kvevaluator.measure.server import CppServer
from kvevaluator.workloads.writes import params as write_params
from kvevaluator.measure import grid_capture

SCHEMA = schema_path("ldbc")
FIXTURES = os.path.join(_HERE, "fixtures", "write_then_read")
SPEC_DIRS = (os.path.join(EXP, "inputs", "specs", "grid"), os.path.join(EXP, "inputs", "specs", "grid_chunked_own"),
             os.path.join(EXP, "inputs", "specs", "mixes_per_direction"))
EXPECT = {"insert_edge_then_read": 1, "delete_edge_then_read": -1, "insert_two_edges_then_read": 2}   # rows of nb_out against before


def knows_graph(sf):
    """(the Person ids in vid order, {person: its neighbours either way}, {person: the dsts of its stored out edges})
    from the record stream; a Person's vid is its id"""
    persons, either, out = [], {}, {}
    with open(records.records_file("ldbc", sf)) as f:
        for line in f:
            if line.startswith('["v"'):
                r = json.loads(line)
                if r[2] == "Person":
                    persons.append(int(r[1], 16))
            elif line.startswith('["e","knows"'):
                r = json.loads(line)
                u, v = int(r[2], 16), int(r[3], 16)
                either.setdefault(u, set()).add(v); either.setdefault(v, set()).add(u)
                out.setdefault(u, set()).add(v)
    return sorted(persons), either, out


def cases(sf, nparam):
    """[(plan, tag, params)]: per source Person (the writes workload's own sources, plus the first and the last Person by
    vid: the owners at the two ends of the label's chunks), an insert of a new neighbour below, inside and above the ids
    the list holds, two inserts into one list, and a delete of its first and its last stored out neighbour"""
    persons, either, outs = knows_graph(sf)
    srcs = [(f"p{i + 1}", int(p["src"])) for i, p in enumerate(write_params.query_params(sf, "w_ins_e_plain", nparam))]
    srcs += [("first-person", persons[0]), ("last-person", persons[-1])]
    todo = []
    for tag, src in srcs:
        free = [x for x in persons if x != src and x not in either.get(src, ())]
        for where, dst in (("low", free[0]), ("mid", free[len(free) // 2]), ("high", free[-1])):
            todo.append(("insert_edge_then_read", f"{tag}-{where}", {"src": str(src), "dst": str(dst)}))
        todo.append(("insert_two_edges_then_read", tag, {"src": str(src), "dst": str(free[-1]), "dst2": str(free[0])}))
        stored = sorted(outs.get(src, ()))
        for where, dst in (("first", stored[:1]), ("last", stored[-1:] if len(stored) > 1 else [])):
            if dst:
                todo.append(("delete_edge_then_read", f"{tag}-{where}", {"src": str(src), "dst": str(dst[0])}))
    return todo


def run(server, store, ir, params):
    resp = server.request({"store": {"dir": store, "schema": SCHEMA, "format": "meta"}, "plan": ir, "params": params,
                           "run": {"P": 1, "parts": True, "warmup": 0, "measured": 1,
                                   "emit": {"histograms": False, "row_digest": True}}})
    if resp.get("error"):
        raise SystemExit(f"{os.path.basename(store)}: {ir['plan']} {params}: {resp['error']}")
    if any("rows_digest" not in st for st in resp["steps"]):
        raise SystemExit("the binary returns no rows_digest: it predates run.emit.row_digest")
    return {st["name"]: (st["rows"], st["rows_digest"], st["rows_set_digest"]) for st in resp["steps"]}


def compare(got, order):
    """got: {(plan, tag): {store: {step: (rows, digest, set digest)}}} -> (compared, [failure lines])"""
    compared, fails = 0, []
    for key in order:
        per = got[key]
        ref = sorted(per)[0]
        for step in per[ref]:
            compared += 1
            groups = {}
            for store in sorted(per):
                groups.setdefault(per[store][step], []).append(store)
            if len(groups) > 1:
                kind = "another ORDER" if len({g[2] for g in groups}) == 1 else "other ROWS"
                small = min(groups.values(), key=len)
                fails.append(f"{key[0]} {key[1]} step {step}: {kind} on {len(small)} of {len(per)} stores "
                             f"(e.g. {small[0]} against {max(groups.values(), key=len)[0]}; rows "
                             f"{sorted({g[0] for g in groups})})")
    return compared, fails


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--spec-dir", action="append", default=None, help="a spec set; repeat (default: the grid, the "
                    "own-placement cells, the per-direction mixes)")
    ap.add_argument("--control", action="store_true", help="flip one digest of one store: the check must fail and name it")
    ap.add_argument("--expect-fail", action="store_true", help="a control binary: exit 0 only if the digests differ")
    a = ap.parse_args()
    ver = json.loads(subprocess.run([binary.binary_path(), "--version"], capture_output=True, text=True).stdout)
    print(f"[row-order-after-write] binary {binary.binary_path()} sha {ver['binary_git_sha'][:9]} dirty {ver['binary_dirty']}")
    dirs = a.spec_dir or SPEC_DIRS
    grid_capture.store_names_unique(dirs)
    stores = {os.path.basename(p)[:-5]: grid_capture.meta_store(a.sf, "interactive", p) for d in dirs for p in sorted(glob.glob(os.path.join(d, "*.json")))}
    plans = {os.path.basename(p)[:-5]: json.load(open(p)) for p in sorted(glob.glob(os.path.join(FIXTURES, "*.json")))}
    todo = cases(a.sf, a.nparam)
    got, counted, count_fails = {key[:2]: {} for key in todo}, 0, []
    for name, store in sorted(stores.items()):             # one server per store: one open, every case
        with CppServer(store, fmt="meta", schema=SCHEMA) as server:
            for plan, tag, params in todo:
                ir = plans[plan]
                before = dict(ir, steps=[st for st in ir["steps"] if not st["op"].startswith(("insert_", "delete_"))])   # the reads alone
                got[(plan, tag)][name] = res = run(server, store, ir, params)
                counted += 1                                # the later step saw the write: the list grew or shrank by it
                was = run(server, store, before, params)["nb_out"][0]
                if res["nb_out"][0] - was != EXPECT[plan]:
                    count_fails.append(f"{plan} {tag} on {name}: nb_out has {res['nb_out'][0]} rows after the write, "
                                       f"{was} before (expected {EXPECT[plan]:+d})")
    order = [(p, t) for p, t, _ in todo]
    compared, fails = compare(got, order)
    for line in (fails + count_fails)[:20]:
        print("   " + line)
    ok = not fails and not count_fails and compared > 0 and counted > 0
    print(f"[row-order-after-write] sf{a.sf}: {len(stores)} stores x {len(todo)} (plan, param) cases, {compared} steps "
          f"compared by digest, {len(fails)} differ; {counted} (case, store) row counts against before the write, "
          f"{len(count_fails)} wrong -> {'OK' if ok else 'FAIL'}")
    if a.expect_fail:   # caught only as the planted defect: the same rows in another order, nothing else
        other = [f for f in fails if "another ORDER" not in f] + count_fails
        verdict = ("NOT caught" if not fails else f"CONTROL VOID ({len(other)} failures are not another order: {other[0][:200]})"
                   if other else f"caught by its signature (another ORDER on {len(fails)} steps)")
        print(f"[row-order-after-write] CONTROL BINARY: {verdict}")
        return 0 if fails and not other else 1
    if a.control:
        ctl = copy.deepcopy(got)
        key, victim = order[0], sorted(stores)[-1]
        rows, dig, sdig = ctl[key][victim]["nb_out"]
        ctl[key][victim]["nb_out"] = (rows, "f" * 16 if dig != "f" * 16 else "0" * 16, sdig)
        _, cf = compare(ctl, order)
        caught = any(victim in f and "another ORDER" in f for f in cf)
        print(f"[row-order-after-write] CONTROL (one digest of {victim} flipped at {key}): "
              f"{'caught, as another order' if caught else 'NOT caught'}")
        ok = ok and caught
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    except (SystemExit, Exception) as e:   # a control binary that throws (or a refused store) is not the planted defect
        if "--expect-fail" not in sys.argv:
            raise
        print(f"[row-order-after-write] CONTROL BINARY: CONTROL VOID (it threw: {str(e)[:300]})")
        rc = 1
    sys.exit(rc)
