#!/usr/bin/env python3
"""An old store beside its old capture is NOT reused (owner: experiments/meta; the injection test of
grid_capture.store_is_current, which grid_bodies.fresh and grid_writes.fresh ask).

A capture records its store's config hash, and the reuse test compared that with the manifest on disk. When the format
changes what it stores for the SAME spec, an old store and its old capture still agree with each other, so the capture
looked fresh although the binary refuses to open the store (own edge-prop placement's adjacency chunk).
The test now also asks whether the store's recorded config is what the current binary writes for the spec.

For one own-placement cell and one inline cell (the control) at --sf, on their real stores (built if missing):
  1. the real store is current, and a capture record made for it here (in a temp dir; no measurement) is fresh;
  2. the injection: a copy of the store's manifest in the form of before the change (the adjacency component's
     `own_chunk` taken out, the hash kept, as the old capture has it), reached through KVEVAL_STORE_COPIES: the store
     is not current, grid_bodies.fresh is False and grid_writes.store_identity is None;
  3. the control cell has no such field: its copied manifest stays current and fresh (the injection is not a blanket no).

  python3 tests/gates/meta/check_stale_store_not_reused.py [--sf 0.1]
Exit 0 = all of it holds; the cells and the assertions checked are printed."""
import argparse
import json
import os
import shutil
import sys
import tempfile
from kvevaluator.paths import ROOT

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import grid_capture, stores
from kvevaluator.measure import grid_bodies
from kvevaluator.measure import grid_writes
from kvevaluator.output_tree import capture_in

OWN = (os.path.join(EXP, "inputs", "specs", "grid_chunked_own"), "props_chunked_adj_chunked_own")
INLINE = (grid_capture.GRID, "props_chunked_adj_chunked")
NPARAM = 5


def capture_for(tmp, cell, manifest, sf, inputs, cell_inputs):
    """a capture record of `cell` on the store with this manifest, as grid_bodies writes it (no measurement)"""
    cap = capture_in(os.path.join(tmp, "capture-" + cell), "interactive")
    out = cap.path
    for kind in ("counts", "parts"):
        os.makedirs(cap.dir(kind), exist_ok=True)
        open(cap.path_of(kind, cell), "w").close()
    with open(cap.record(cell), "w") as f:
        json.dump({"complete": True, "workload": "interactive", "nparam": NPARAM, "inputs": inputs, "cell_inputs": cell_inputs,
                   "measurement_version": grid_bodies.CAPTURE_SHAPE, "counting": grid_bodies.counting.stamp(manifest["format"]),
                   "store_manifest": {k: manifest.get(k) for k in grid_bodies.STORE_MANIFEST_KEYS},
                   "store_content": grid_bodies.store_identity.store_content_identity(manifest)}, f)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    a = ap.parse_args()
    os.environ.pop("KVEVAL_STORE_COPIES", None)
    inputs = grid_bodies.inputs_fingerprint("interactive", a.sf, NPARAM, None)
    fails, checked = [], 0

    def expect(what, got, want):
        nonlocal checked
        checked += 1
        if got != want:
            fails.append(f"{what}: {got!r}, want {want!r}")

    tmp = tempfile.mkdtemp(prefix="stale-store-")
    try:
        for (spec_dir, cell), injected in ((OWN, True), (INLINE, False)):
            spec = os.path.join(spec_dir, cell + ".json")
            store = grid_capture.meta_store(a.sf, "interactive", spec)           # the real store, built if missing
            manifest = json.load(open(os.path.join(store, "manifest.json")))
            out = capture_for(tmp, cell, manifest, a.sf, inputs, grid_bodies.cell_fingerprint("interactive", a.sf, spec, store))
            fresh = lambda: grid_bodies.fresh(out, cell, "interactive", a.sf, NPARAM, inputs, spec_dir)
            expect(f"{cell}: the real store is current", grid_capture.store_is_current(store, spec, "interactive"), True)
            expect(f"{cell}: a capture on the real store is fresh", fresh(), True)
            expect(f"{cell}: the writes tool sees the real store", grid_writes.store_identity(a.sf, cell, spec_dir) is not None, True)
            # the copy: the manifest alone, in the form of before the change (the hash kept: the old capture agrees with it)
            old = json.loads(json.dumps(manifest))
            had = old["value_codec"]["adjacency"].pop("own_chunk", None)
            expect(f"{cell}: its recorded config {'has' if injected else 'has no'} own_chunk", had is not None, injected)
            copy = stores.copy_dir(os.path.join(tmp, "copies-" + cell), "ldbc", a.sf, store)
            os.makedirs(copy)
            with open(os.path.join(copy, "manifest.json"), "w") as f:
                json.dump(old, f)
            os.environ["KVEVAL_STORE_COPIES"] = os.path.join(tmp, "copies-" + cell)
            try:
                seen = grid_capture.meta_store(a.sf, "interactive", spec, build=False)
                expect(f"{cell}: the tools now read the copied manifest", os.path.samefile(seen, copy), True)
                expect(f"{cell}: the old-form store is current", grid_capture.store_is_current(seen, spec, "interactive"), not injected)
                expect(f"{cell}: its capture is fresh", fresh(), not injected)
                expect(f"{cell}: the writes tool sees it", grid_writes.store_identity(a.sf, cell, spec_dir) is not None, not injected)
            finally:
                os.environ.pop("KVEVAL_STORE_COPIES", None)
    finally:
        shutil.rmtree(tmp)
    for f in fails:
        print("  FAIL " + f)
    print(f"[stale-store] sf{a.sf}: 2 cells ({OWN[1]}: an old-form store injected; {INLINE[1]}: the control), {checked} assertions "
          f"-> {'FAIL (' + str(len(fails)) + ')' if fails else 'OK: an old store beside its old capture is not reused; the control is'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
