#!/usr/bin/env python3
"""A grid capture is reused when what its cell runs is unchanged, and re-measured when it moved (owner: experiments/meta;
the planted-control test of grid_bodies.fresh's plan half: inputs_fingerprint + cell_fingerprint).

A key that hashed whole plan files would stale every grid capture of a bundle on a stamp such as `requires:
property_index` although no body changed. The key is what runs: each candidate's body sha, each plan file's
plan-level fields (bind / let / params), the measure-only bodies, and per cell the invalid candidates and the cell's
measure-only bodies. Which body a store runs is the pick record's, decided on the capture: not part of its key.

Two real grid cells at --sf (stores built if missing), a capture record each written by grid_bodies.write_cell with
no rows (no measurement), the LDBC and microbench plans copied to a temp dir (kvevaluator.plan_choice.candidates.PLANS_ROOT), then:
  1. HEAD: both captures are fresh;
  2. a file-only edit: every `property_index` stamp taken out (a whole-file
     key would move) and every LDBC/microbench file re-indented: fresh;
  3. one step of one is7 body changed: stale (both cells: every cell runs every candidate);
  4. is7's `let` / `params` edited (body sha unchanged): stale;
  5. the pick record's is7 entries of both cells moved to another legal body: both captures stay fresh (a capture runs
     every body; the pick is decided on it, never part of its key);
  6. control: a microbench capture stays fresh under every LDBC edit.

  python3 tests/gates/meta/check_grid_body_key.py [--sf 0.1] [--module-dir DIR]
Exit 0 = all of it holds; the cases and assertions are printed. --module-dir: the grid_bodies.py to test (a copy of
another commit's experiments/meta: the check must fail on the code from before the body key)."""
import argparse
import hashlib
import inspect
import json
import os
import shutil
import sys
import tempfile
import time
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import capture_in
from kvevaluator.measure import stores

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
NPARAM = 2


def old_file_key(C, workload):
    """the pre-2026-10-18 plan half of the key: every plan file's bytes (to show an edit is a real file change)"""
    h = hashlib.sha256()
    for q in C.workload_queries(workload)[0]:
        for path in C.plan_files(workload, q):
            h.update(open(path, "rb").read())
    return h.hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--module-dir", default=os.path.join(EXP, "python", "kvevaluator", "measure"), help="where grid_bodies.py is (the code under test)")
    a = ap.parse_args()
    from kvevaluator.measure import (grid_capture)  # this tree's: its cell_selection picks the two cells, whatever code is tested
    from kvevaluator.plan_choice import cells as plan_cells
    from kvevaluator.measure import grid_bodies as G
    from kvevaluator.plan_choice import candidates as C
    from kvevaluator.capture.binary import binary_path
    print(f"[body-key] grid_bodies: {inspect.getsourcefile(G)}")
    binary = binary_path()
    real_root = C.PLANS_ROOT
    tmp = tempfile.mkdtemp(prefix="grid-body-key-")
    fails, checked = [], 0

    def expect(what, got, want):
        nonlocal checked
        checked += 1
        print(f"  {'ok  ' if got == want else 'FAIL'} {what}: {got}" + ("" if got == want else f" (want {want})"))
        if got != want:
            fails.append(what)

    def plans(wl):
        return os.path.join(C.PLANS_ROOT, wl, "plans")

    def edit(wl, name, fn):
        path = os.path.join(plans(wl), f"{name}.json")
        ir = json.load(open(path))
        fn(ir)
        json.dump(ir, open(path, "w"), indent=1)

    def reset():
        for wl in ("interactive", "microbench"):
            shutil.rmtree(os.path.join(tmp, wl), ignore_errors=True)
            shutil.copytree(os.path.join(real_root, wl, "plans"), os.path.join(tmp, wl, "plans"))
        C.PLANS_ROOT = tmp

    def capture(wl, cell, spec):
        """a capture record of `cell` as write_cell writes it, keyed on the plans under C.PLANS_ROOT now"""
        store = grid_capture.meta_store(a.sf, wl, spec)                     # the real store, built if missing
        out = capture_in(os.path.join(tmp, f"capture-{cell}"), wl).path
        inputs = G.inputs_fingerprint(wl, a.sf, NPARAM)
        extra = {}
        if hasattr(G, "cell_fingerprint"):
            extra["cell_inputs"] = G.cell_fingerprint(wl, a.sf, spec, store)
        G.write_cell(cell, wl, a.sf, NPARAM, 1, binary, out, inputs, spec, store, "serve", [], [], [], time.time(), **extra)
        return lambda: G.fresh(out, cell, wl, a.sf, NPARAM, G.inputs_fingerprint(wl, a.sf, NPARAM), grid_capture.GRID)

    try:
        reset()
        # two cells, and is7's pick on each from the record
        from kvevaluator.plan_choice import pick_record
        cells = sorted(f[:-5] for f in os.listdir(grid_capture.GRID) if f.endswith(".json"))
        flip, same = cells[0], cells[-1]
        before = {c: pick_record.entry("interactive", c, "is7")["body"] for c in (flip, same)}
        print(f"[body-key] sf{a.sf}, nparam {NPARAM}; cells: {flip} (is7 {before[flip]}), {same} (is7 {before[same]}); "
              f"control: microbench on {same}")
        spec = {c: os.path.join(grid_capture.GRID, f"{c}.json") for c in (flip, same)}

        def captures():
            return {"flip": capture("interactive", flip, spec[flip]), "same": capture("interactive", same, spec[same]),
                    "micro": capture("microbench", same, spec[same])}

        print("1. HEAD")
        fr = captures()
        for k, f in fr.items():
            expect(f"HEAD: the {k} capture is fresh", f(), True)

        print("2. a file-only edit (the property_index stamps out, every file re-indented)")
        old = {wl: old_file_key(C, wl) for wl in ("interactive", "microbench")}
        removed = {"interactive": 0, "microbench": 0}
        for wl in ("interactive", "microbench"):
            for name in sorted(n for n in os.listdir(plans(wl)) if n.endswith(".json")):
                def unstamp(ir):
                    if "property_index" in ir.get("requires", []):
                        ir["requires"] = [c for c in ir["requires"] if c != "property_index"]
                        removed[wl] += 1
                        if not ir["requires"]:
                            del ir["requires"]
                    for v in ir.get("variants", []):
                        removed[wl] += "property_index" in v.get("requires", [])
                        v["requires"] = [c for c in v.get("requires", []) if c != "property_index"]
                    for m in ir.get("bodies", {}).values():          # a generated file: per body, {cap: labels} or [cap]
                        req = m.get("requires", {})
                        if "property_index" in req:
                            removed[wl] += 1
                            if isinstance(req, dict):
                                del req["property_index"]
                            else:
                                m["requires"] = [c for c in req if c != "property_index"]
                edit(wl, name[:-5], unstamp)
        for wl in ("interactive", "microbench"):
            expect(f"{wl}: property_index stamps taken out (> 0)", removed[wl] > 0, True)
            expect(f"{wl}: the whole-file key moved (the edit is real)", old_file_key(C, wl) != old[wl], True)
        for k, f in fr.items():
            expect(f"file-only edit: the {k} capture is fresh", f(), True)

        print("3. one step of one is7 body changed")
        reset(); fr = captures()
        edit("interactive", "is7", lambda ir: ir["plans"][next(iter(ir["plans"]))]["steps"][0].__setitem__("planted", 1))
        expect("body edit: the flip-cell capture is stale", fr["flip"](), False)
        expect("body edit: the same-cell capture is stale (it runs every candidate)", fr["same"](), False)
        expect("body edit: the microbench capture is fresh", fr["micro"](), True)

        print("4. is7's plan-level fields (body sha unchanged)")
        for field in ("let", "params"):
            reset(); fr = captures()
            edit("interactive", "is7", lambda ir: ir.setdefault(field, {}).__setitem__("planted", {"lit": 1} if field == "let" else {}))
            expect(f"`{field}` edit: the same-cell capture is stale", fr["same"](), False)

        print("5. the pick record's is7 entries moved to another legal body")
        reset(); fr = captures()
        rec = pick_record.load_full(pick_record.PICKS_FILE)
        for c in (flip, same):
            ir7 = json.load(open(os.path.join(plans("interactive"), "is7.json")))
            caps7, of7 = plan_cells.store_caps("interactive", spec[c])
            other = next(b for b in pick_record.legal_bodies(ir7, caps7, of7) if b != before[c])
            rec["cells"][c]["is7"] = dict(rec["cells"][c]["is7"], body=other)
        planted_rec = os.path.join(tmp, "planted-record", "picks.csv")
        pick_record.dump(rec, planted_rec)
        real_rec = (pick_record.PICKS_FILE, grid_capture.PICKS_FILE)
        pick_record.PICKS_FILE = grid_capture.PICKS_FILE = planted_rec
        try:
            expect("record edit: is7's recorded pick moved", pick_record.entry("interactive", flip, "is7")["body"] != before[flip], True)
            for k in ("flip", "same", "micro"):
                expect(f"record edit: the {k} capture is fresh", fr[k](), True)
        finally:
            pick_record.PICKS_FILE, grid_capture.PICKS_FILE = real_rec

        print("6. one step of a twin's body changed (ic1_expand_name_matches(...), a body in ic1's file)")
        reset(); fr = captures()
        def twin_step(ir):
            body = next(b for b in ir["plans"] if b.startswith("ic1_expand_name_matches"))
            ir["plans"][body]["steps"][0]["planted"] = 1
        edit("interactive", "ic1", twin_step)
        expect("twin body edit: the same-cell capture is stale", fr["same"](), False)

        print("7. an invalid candidate on the cell, then the caps it lacks changed (its row's error text), the pick unchanged")
        from kvevaluator.plans.ir.capabilities import VALIDITY_CAPS
        from kvevaluator.formats.caps import caps_for
        from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
        lacked = sorted(VALIDITY_CAPS - caps_for("meta", LDBC_SCHEMA, spec=spec[same]))   # the caps the control cell lacks
        expect(f"{same} lacks at least 2 validity caps (the precondition)", len(lacked) >= 2, True)
        body = next(b for b in json.load(open(os.path.join(plans("interactive"), "is7.json")))["plans"]
                    if b != before[same])                                  # a body the control cell does not run
        def lack(n):                     # validity reads the generated file's `bodies` block (candidates.body_lacks)
            def f(ir):
                meta = ir["bodies"][body]
                meta["requires"] = sorted(set(meta.get("requires", [])) | set(lacked[:n]))
            return f
        reset(); edit("interactive", "is7", lack(1)); fr = captures()             # state A: `body` invalid, lacking 1 cap
        expect(f"state A: {body} is invalid on {same}", body in grid_capture.invalid_on_cell("interactive", spec[same]).get("is7", {}), True)
        expect("state A: the same-cell capture is fresh", fr["same"](), True)
        reset(); edit("interactive", "is7", lack(2))                               # state B: lacking 2 caps
        expect(f"state B: {same}'s is7 pick is unchanged", pick_record.entry("interactive", same, "is7")["body"], before[same])
        expect("state B (the lacking caps moved): the same-cell capture is stale", fr["same"](), False)

        print("8. the order of a `let` whose entries depend on each other (microbench entry_range)")
        reset(); fr = captures()
        edit("microbench", "entry_range", lambda ir: ir.__setitem__("let", dict(reversed(list(ir["let"].items())))))
        expect("let reorder: the microbench capture is stale", fr["micro"](), False)

        print("9. the store rebuilt from another schema or record stream behind the same codecs")
        reset(); fr = captures()
        store = grid_capture.meta_store(a.sf, "interactive", spec[same], build=False)
        m = json.load(open(os.path.join(store, "manifest.json")))
        m["records"]["sha256"] = "planted-" + str(m["records"].get("sha256"))
        copy = stores.copy_dir(os.path.join(tmp, "copies"), "ldbc", a.sf, store)
        os.makedirs(copy)
        json.dump(m, open(os.path.join(copy, "manifest.json"), "w"))
        os.environ["KVEVAL_STORE_COPIES"] = os.path.join(tmp, "copies")
        try:
            expect("the copy is a usable store", grid_capture.store_is_current(copy, spec[same], "interactive", layout=False), True)
            expect("another record stream: the same-cell capture is stale", fr["same"](), False)
        finally:
            os.environ.pop("KVEVAL_STORE_COPIES", None)
        expect("the real store again: fresh", fr["same"](), True)

        # (a case 10 checked the C++ planner's pick against the key's, grid_bodies.selection_mismatches: since
        # no run lets the C++ pick, every run names its body, so there is no pick to check)
    finally:
        C.PLANS_ROOT = real_root
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[body-key] {checked} assertions over 3 captures (2 LDBC cells + 1 microbench control), 9 cases")
    if fails:
        print(f"GRID BODY KEY FAIL: {len(fails)} of {checked}: {fails}")
        return 1
    print("GRID BODY KEY OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
