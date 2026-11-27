#!/usr/bin/env python3
"""Engine-plan regression gate: the Python engine formats' counts, byte lanes and answers on every engine-fidelity
cell, recorded once and re-checked after a change that must not move them (the engine-fidelity C++ port, step A:
callable args -> expression ASTs).

The cells are the (query, db, param) keys of a fidelity snapshot's live baseline (live_kvstats.csv, status ok) --
exactly the cells validate.py runs. Each cell runs the db's ENGINE plan on the Python engine store
(build_stores(engine=True)), as validate.run_cell does, and keeps S / G / N / P / D, the misses, every byte lane and
a hash of the answer rows.

  python3 tests/gates/check_engine_plans.py --record base.json --baseline <live_kvstats.csv> [--baseline ...]
  python3 tests/gates/check_engine_plans.py --check  base.json            # sf0.1's cells, must be identical
  python3 tests/gates/check_engine_plans.py --check  base.json --sf all   # every recorded set (sf0.3 too)
  python3 tests/gates/check_engine_plans.py --check  base.json --backend cpp   # the C++ engine formats

--backend cpp runs the same engine plans, lowered to the plan IR, on the C++ engine store (store_identity.CPP_ENGINE_FORMATS)
and compares the op and miss counts and the answers with the recorded Python ones; the byte lanes are reported, not
compared (the two sides count different key / value currencies, as check_cpp_vs_python's --skip-bytes). Only the dbs
with a C++ engine format are selected.

The tracked baseline is tests/fixtures/engine_plans_python_884bbfeb61.json: the Python engine formats at
884bbfeb61 over three sets -- the ldbc cells at sf0.1 and sf0.3, the microbench cells at sf0.3. Each set holds its
cells and the Dgraph uid map of the live load they came from (dgraph_uids: vid -> uid, the plans' first_k_by_uid steps
need it; none for microbench), so the check reads nothing but the fixture. `baseline` names the live run it was recorded
from.
Re-recorded for the LDBC param switch (the param-source rule, inputs/params/ldbc/param_sources.json): only the switched queries moved (IC3 / IC11 at
sf0.1, IC3 / IC5 / IC11 at sf0.3); every other cell equals the e71a08926 recording.
NebulaGraph's IC12 cells are recorded with the twin not expanding `Thing` again at step 3 of its class
walk (queries/ic12.py): 1 SEEK (a miss), 1 GET and their bytes fewer per cell; rows and answers are the same,
and no other cell moved.
`just cpp-diff` checks its sf0.1 set on C++. A change that is MEANT to move engine-plan
counts re-records it (the same --baseline paths, named after the new SHA) and says why in the commit.
"""
import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path
from kvengines import live_workloads as W
from kvevaluator.formats import ENGINE_FORMATS
from kvevaluator.oracle.run import build_stores
from kvevaluator.oracle.runner import run_plan
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.datasets import demo as data

from kvevaluator.capture.step_record import (HIT_BYTE_LANES, MISS_BYTE_LANES, OPS, WRITE_BYTE_LANES)  # (the lanes, one definition)
COUNT_LANES = OPS
BYTE_LANES = tuple(f"{lane}_bytes" for lane in (*HIT_BYTE_LANES, *WRITE_BYTE_LANES, *MISS_BYTE_LANES))
LANES = COUNT_LANES + BYTE_LANES


def uid_map(snapdir, rows):
    """{vid: live Dgraph uid} from the snapshot's dgraph/uids_*.csv -- only when it comes from the same Dgraph load as
    the live counts (validate.py's rule), else None."""
    from kvevaluator.schema.vids import vid
    d = Path(snapdir) / "dgraph"
    meta = d / "uids.meta.json"
    if not meta.exists():
        return None
    loads = {r.get("data_created", "") for r in rows if r["db"] == "dgraph"}
    if loads != {json.loads(meta.read_text()).get("data_created")}:
        return None
    out = {}
    for label in W.DGRAPH_UID_TYPES:
        f = d / f"uids_{label.lower()}.csv"
        if not f.exists():
            return None
        out.update({vid(label, r["id"]): int(r["uid"], 16) for r in csv.DictReader(open(f))})
    return out


def baseline_cells(path):
    """(workload, sf, [(query, db, param)], uid map) from a live_kvstats.csv inside a fidelity snapshot."""
    p = Path(path).resolve()
    if not p.exists():
        raise SystemExit(f"{p} is not there: a live engine-fidelity run's live_kvstats.csv (experiments/engine_fidelity/measure.py)")
    workload = p.parent.name if p.parent.name in W.WORKLOADS else "interactive"
    sf = next(x[2:] for x in p.parts if x.startswith("sf") and x[2:3].isdigit())
    rows = [r for r in csv.DictReader(open(p)) if r["status"] == "ok"]
    suite = W.engine_registries(workload)["suite"]
    cells = sorted({(r["query"], r["db"], int(r["param_idx"])) for r in rows
                    if r["query"] in suite and r["db"] in suite[r["query"]]})
    return workload, sf, cells, uid_map(p.parent, rows)


def run(workload, sf, cells, uid_of, backend="python"):
    """{"query|db|param": {lane: n, ..., "rows": n, "answer": hash}} on the Python engine stores, or (backend cpp) on
    the C++ engine stores. The uid map rides on the plan's first_k_by_uid steps (live_workloads.with_uid_map)."""
    dbs = sorted({db for _, db, _ in cells})
    if backend == "cpp":
        from kvevaluator.measure import plan_runs, server
        from kvevaluator.capture import store_identity
        from kvevaluator.measure.stores import ensure_store
        servers = {db: server.CppServer(ensure_store("ldbc", sf, store_identity.CPP_ENGINE_FORMATS[db], chunk=None), store_identity.CPP_ENGINE_FORMATS[db])
                   .__enter__() for db in dbs}
    else:
        stores = build_stores(sf, dbs, "abstract", engine=True)
    out = {}
    try:
        for q, db, p in cells:
            try:   # a cell that fails is recorded as failing (validate.py reports it and goes on): it must stay so
                plan = W.with_uid_map(W.engine_plan(workload, q, db, W.plan_args(workload, sf, q, p)),
                                      uid_of if db == "dgraph" else None)
                if backend == "cpp":
                    outs, kv = plan_runs.run_steps(servers[db], plan, name=f"{q}-{db}-engine")
                else:
                    fmt = ENGINE_FORMATS[db](data.schema(sf))
                    outs, kv = run_plan(plan, fmt, CountingKV(stores[db][1], sizeof=fmt.value_bytes,
                                                              keysizeof=fmt.key_bytes))
            except Exception as e:
                out[f"{q}|{db}|{p}"] = {"error": f"{type(e).__name__}: {e}"[:200]}
                continue
            rows = outs[plan[-1].out]
            cell = {lane: sum(getattr(a, lane) for _, _, a in kv) for lane in LANES}
            cell["rows"] = len(rows)
            cell["answer"] = hashlib.sha1(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()[:16]
            out[f"{q}|{db}|{p}"] = cell
    finally:
        if backend == "cpp":
            for srv in servers.values():
                srv.close()
    return out


def differs(expect, got, backend):
    """{field: (expected, got)} that must agree: everything on Python (a failing cell with the same message); on C++
    the counts, rows and answer (a failing cell fails again, whatever the message -- the two sides word errors
    differently)."""
    if "error" in expect or "error" in got:
        same = "error" in expect and "error" in got and (backend == "cpp" or expect["error"] == got["error"])
        return {} if same else {"error": (expect.get("error"), got.get("error"))}
    fields = expect if backend == "python" else [f for f in expect if f not in BYTE_LANES]
    return {f: (expect.get(f), got.get(f)) for f in fields if expect.get(f) != got.get(f)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--record", metavar="FILE")
    g.add_argument("--check", metavar="FILE")
    ap.add_argument("--baseline", action="append", default=[], help="a fidelity snapshot's live_kvstats.csv (--record)")
    ap.add_argument("--query", default="", help="--check only these queries (comma list; a quick per-file check)")
    ap.add_argument("--sf", default="0.1", help="--check only this scale's sets (default 0.1, a small-scale oracle "
                                                 "check; `all` = every recorded set)")
    ap.add_argument("--backend", choices=("python", "cpp"), default="python",
                    help="--check on the Python engine formats (default) or the C++ ones")
    a = ap.parse_args()
    if a.backend == "cpp" and a.record:
        raise SystemExit("--record is the Python reference; --backend cpp only checks against it")
    if a.record:
        if not a.baseline:
            raise SystemExit("--record needs at least one --baseline")
        rec = {"sets": []}
        for b in a.baseline:
            workload, sf, cells, uids = baseline_cells(b)
            rec["sets"].append({"baseline": str(b), "workload": workload, "sf": sf, "cells": run(workload, sf, cells, uids),
                                "dgraph_uids": {k: uids[k] for k in sorted(uids)} if uids else None})
            errs = sum("error" in c for c in rec["sets"][-1]["cells"].values())
            print(f"[check_engine_plans] recorded {workload} sf{sf}: {len(cells)} cells, {errs} failing "
                  f"(Dgraph uid map: {'yes' if uids else 'none'})", flush=True)
        json.dump(rec, open(a.record, "w"), indent=1, sort_keys=True)
        return 0
    rec = json.load(open(a.check))
    total, expected, bad = 0, 0, []
    byte_diff = {lane: 0 for lane in BYTE_LANES}
    queries = set(filter(None, a.query.split(",")))
    if a.backend == "cpp":
        from kvevaluator.capture import store_identity
        dbs = set(store_identity.CPP_ENGINE_FORMATS)
    for s in rec["sets"]:
        if a.sf != "all" and s["sf"] != a.sf:
            continue
        keep = {k: v for k, v in s["cells"].items() if (not queries or k.split("|")[0] in queries)
                and (a.backend == "python" or k.split("|")[1] in dbs)}
        if not keep:
            continue
        expected += len(keep)
        cells = [tuple(k.split("|")[:2]) + (int(k.split("|")[2]),) for k in keep]
        got = run(s["workload"], s["sf"], cells, s["dgraph_uids"], a.backend)
        for k, expect in keep.items():
            total += 1
            have = got.get(k, {"not run": True})
            diff = differs(expect, have, a.backend)
            if diff:
                bad.append(f"{s['workload']} sf{s['sf']} {k}: {diff}")
            for lane in BYTE_LANES:
                byte_diff[lane] += "error" not in expect and expect.get(lane) != have.get(lane)
        print(f"[check_engine_plans] {s['workload']} sf{s['sf']}: {len(keep)} cells compared", flush=True)
    for b in bad[:30]:
        print("DIFF " + b)
    print(f"[check_engine_plans] backend {a.backend}: {total} cells compared of {expected} selected, {len(bad)} differ")
    if a.backend == "cpp":
        print("[check_engine_plans] byte lanes differing (reported, not compared): "
              + ", ".join(f"{lane} {n}" for lane, n in byte_diff.items()))
    if bad or total == 0 or total != expected:
        print("FAIL")
        return 1
    print("PASS: every cell identical (" + ("counts, byte lanes, answers" if a.backend == "python"
                                             else "op and miss counts, rows, answers") + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
