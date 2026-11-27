#!/usr/bin/env python3
"""A grid writes capture is reused when what its cell runs is unchanged, and re-measured when it moved (owner:
experiments/meta; the planted-control test of grid_writes.fresh's plan and params half, inputs_fingerprint).

A key that hashed whole write-plan files and whole param files would re-measure every writes capture on a doc edit,
or on a regenerated param file whose used rows did not change. The key is what runs: per write query its IR's
run identity (kvevaluator.plans.ir.bundle.run_identity: body shas, bind / let / params in file order, selectors) and the param rows
the cell runs. fresh() still compares the store, CAPTURE_SHAPE, the binary's write counting versions, nparam and the slice.

One real grid cell at --sf (its store built if missing), a capture record written as grid_writes.main writes it (no
measurement), the write plans copied to a temp dir (plan_runs.ir_for reads them there), then:
  1. HEAD: fresh;
  2. a doc-only edit (`for` changed, every file re-indented; the whole-file key moves): fresh;
  3. one step of one write body changed: stale;
  4. a `let` added; the `let` order reversed (the first write plan with 2+ lets): stale;
  5. a top-level `requires` added to one plan: stale;
  6. a param row the cell does not run changed (row nparam+1): fresh; a row it runs changed: stale;
  7. the record's write counting version (its `counting` write_seam) off by one: stale;
  8. the store rebuilt from another schema behind the same codecs (a copy of its manifest with another schema content
     sha, reached through KVEVAL_STORE_COPIES): stale;
  9. a slice capture (params 1 and 99, past every query's end): fresh; a row inside the slice changed: stale.

  python3 tests/gates/meta/check_writes_body_key.py [--sf 0.1] [--module-dir DIR]
Exit 0 = all of it holds. --module-dir: the grid_writes.py to test (the copy from before must fail the fresh cases)."""
import argparse
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
from kvevaluator.paths import OUTPUT_DIR, ROOT
from kvevaluator.measure import stores

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
NPARAM = 2
CELL = "props_split_adj_split"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--module-dir", default=os.path.join(EXP, "python", "kvevaluator", "measure"), help="where grid_writes.py is (the code under test)")
    a = ap.parse_args()
    from kvevaluator.measure import grid_capture
    from kvevaluator.measure import grid_writes as W
    from kvevaluator.measure import workload_plans
    from kvevaluator.measure import plan_runs
    print(f"[writes-key] grid_writes: {inspect.getsourcefile(W)}")
    real_ir_for, real_qp = plan_runs.ir_for, workload_plans.query_params
    base = os.path.join(EXP, "python", "kvevaluator", "workloads")
    tmp = tempfile.mkdtemp(prefix="writes-body-key-", dir=str(OUTPUT_DIR))
    plans = os.path.join(tmp, "plans")
    planted_rows = {}                                         # (query, 0-based row) -> a changed row
    fails, checked = [], 0

    def expect(what, got, want):
        nonlocal checked
        checked += 1
        print(f"  {'ok  ' if got == want else 'FAIL'} {what}: {got}" + ("" if got == want else f" (want {want})"))
        if got != want:
            fails.append(what)

    def ir_for(workload, q, *args, **kw):                     # the write plans from the temp copy
        if workload != "writes":
            return real_ir_for(workload, q, *args, **kw)
        for d in plan_runs.PLAN_DIRS["writes"]:
            p = os.path.join(plans, d, f"{q}.json")
            if os.path.exists(p):
                return json.load(open(p))
        raise FileNotFoundError(q)

    def query_params(workload, sf, q, n=0, param_set=None):   # the real rows, with the planted ones swapped in
        rows = [dict(r) for r in real_qp(workload, sf, q, n, param_set)]
        for (pq, i), r in planted_rows.items():
            if pq == q and i < len(rows):
                rows[i] = r
        return rows

    def files():
        return [os.path.join(plans, d, f) for d in plan_runs.PLAN_DIRS["writes"]
                for f in sorted(os.listdir(os.path.join(plans, d))) if f.endswith(".json")]

    def old_file_key():
        h = hashlib.sha256()
        for p in files():
            h.update(open(p, "rb").read())
        return h.hexdigest()[:16]

    def edit(q, fn):
        p = next(p for p in files() if os.path.basename(p) == f"{q}.json")
        ir = json.load(open(p))
        fn(ir)
        json.dump(ir, open(p, "w"), indent=1)

    def reset():
        shutil.rmtree(plans, ignore_errors=True)
        for d in plan_runs.PLAN_DIRS["writes"]:
            shutil.copytree(os.path.join(base, d), os.path.join(plans, d))
        planted_rows.clear()

    def capture(params=None):
        """a writes capture record of CELL as grid_writes.main writes it, keyed on the plans and rows as they are now"""
        out = os.path.join(tmp, "root", "writes", f"{CELL}.csv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        open(out, "w").close()
        for p in W.kvstats_files(a.sf, CELL, "writes", os.path.join(tmp, "root")):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        if "nparam" in inspect.signature(W.inputs_fingerprint).parameters:
            inputs = W.inputs_fingerprint(a.sf, "writes", NPARAM, params)
        else:                                                 # the code before: (sf, workload)
            inputs = W.inputs_fingerprint(a.sf, "writes")
        rec = {"cell": CELL, "sf": a.sf, "nparam": NPARAM, "measurement_version": W.CAPTURE_SHAPE,
               "counting": W.counting.stamp(W.FORMAT), "slice": {"queries": None, "params": params},
               "inputs": inputs, "store": W.store_identity(a.sf, CELL, grid_capture.GRID, "writes")}
        json.dump(rec, open(out[:-4] + ".json", "w"))
        return lambda: W.fresh(a.sf, CELL, out, NPARAM, grid_capture.GRID, "writes", os.path.join(tmp, "root"), (None, params))

    try:
        grid_capture.meta_store(a.sf, "interactive", os.path.join(grid_capture.GRID, f"{CELL}.json"))    # the real store, built if missing
        plan_runs.ir_for, workload_plans.query_params = ir_for, query_params
        reset()
        queries = W.write_queries("writes") if hasattr(W, "write_queries") else None
        q0 = sorted(os.path.basename(p)[:-5] for p in files())[0]
        lets = next(os.path.basename(p)[:-5] for p in files() if len(json.load(open(p)).get("let", {})) >= 2)
        rowq = next(q for q in sorted(os.path.basename(p)[:-5] for p in files())
                    if len(real_qp("writes", a.sf, q, NPARAM + 1)) > NPARAM)
        print(f"[writes-key] sf{a.sf}, cell {CELL}, nparam {NPARAM}; edits on {q0} (a step, `requires`), {lets} (`let`), "
              f"{rowq} (param rows); {len(files())} write plans" + (f", {len(queries)} write queries" if queries else ""))

        print("1. HEAD")
        f = capture()
        expect("HEAD: fresh", f(), True)

        print("2. a doc-only edit (`for` changed, every plan re-indented)")
        old = old_file_key()
        for p in files():
            edit(os.path.basename(p)[:-5], lambda ir: ir.__setitem__("for", "a planted doc edit"))
        expect("the whole-file key moved (the edit is real)", old_file_key() != old, True)
        expect("doc-only edit: fresh", f(), True)

        print("3. one step of one write body changed")
        reset(); f = capture()
        edit(q0, lambda ir: ir["steps"][0].__setitem__("planted", 1))
        expect("step edit: stale", f(), False)

        print("4. a `let` added; the `let` order reversed")
        reset(); f = capture()
        edit(lets, lambda ir: ir["let"].__setitem__("planted", {"lit": 1}))
        expect("`let` added: stale", f(), False)
        reset(); f = capture()
        edit(lets, lambda ir: ir.__setitem__("let", dict(reversed(list(ir["let"].items())))))
        expect("`let` reversed: stale", f(), False)

        print("5. a top-level `requires` added")
        reset(); f = capture()
        edit(q0, lambda ir: ir.__setitem__("requires", ["property_index"]))
        expect("`requires` added: stale", f(), False)

        print(f"6. param rows ({rowq})")
        reset(); f = capture()
        planted_rows[(rowq, NPARAM)] = {"planted": "a row the cell does not run"}
        expect(f"row {NPARAM + 1} (not run) changed: fresh", f(), True)
        planted_rows[(rowq, 0)] = {"planted": "a row the cell runs"}
        expect("row 1 (run) changed: stale", f(), False)

        print("7. the write counting version")
        reset(); f = capture()
        side = os.path.join(tmp, "root", "writes", f"{CELL}.json")
        rec = json.load(open(side))
        rec["counting"]["write_seam"] -= 1
        json.dump(rec, open(side, "w"))
        expect("an older write counting version: stale", f(), False)

        print("8. the store rebuilt from another schema behind the same codecs")
        reset(); f = capture()
        store = grid_capture.meta_store(a.sf, "interactive", os.path.join(grid_capture.GRID, f"{CELL}.json"), build=False)
        m = json.load(open(os.path.join(store, "manifest.json")))
        m["schema"]["content_sha"] = "planted-" + str(m["schema"].get("content_sha"))
        copy = stores.copy_dir(os.path.join(tmp, "copies"), "ldbc", a.sf, store)
        os.makedirs(copy)
        json.dump(m, open(os.path.join(copy, "manifest.json"), "w"))
        os.environ["KVEVAL_STORE_COPIES"] = os.path.join(tmp, "copies")
        try:
            expect("the tools read the copied manifest",
                   os.path.samefile(grid_capture.meta_store(a.sf, "interactive", os.path.join(grid_capture.GRID, f"{CELL}.json"), build=False), copy), True)
            expect("the copy is a usable store (stale by its content, not refused)",
                   W.store_identity(a.sf, CELL, grid_capture.GRID, "writes") is not None, True)
            expect("another schema content: stale", f(), False)
        finally:
            os.environ.pop("KVEVAL_STORE_COPIES", None)
        expect("the real store again: fresh", f(), True)

        print("9. a slice capture (params 1 and 99: past every query's end)")
        reset(); f = capture([1, 99])
        expect("slice: fresh", f(), True)
        planted_rows[(rowq, 0)] = {"planted": "a row inside the slice"}
        expect("slice: row 1 changed: stale", f(), False)
    finally:
        plan_runs.ir_for, workload_plans.query_params = real_ir_for, real_qp
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[writes-key] {checked} assertions, 9 cases, 1 cell")
    if fails:
        print(f"WRITES BODY KEY FAIL: {len(fails)} of {checked}: {fails}")
        return 1
    print("WRITES BODY KEY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
