#!/usr/bin/env python3
"""The gate round's store preflight (run/meta-gates.sh, before any stream): every configurable-format store the round
will read, checked against what THIS binary writes for its spec (grid_capture.store_is_current: the manifest's recorded
config against the spec's canonical form) and against the key layout this binary reads (store_identity.stale_key_layout: a
format's key-layout stamp moved, e.g. split-part-start-keys) and the index + filter layout it ingests
(python/kvevaluator/measure/store_layout.py), with the block codec and filter inputs/contract/store_config.json
gives the store's name; with --corners also the engine-format reference stores kept beside the corners
(stamp and index + filter layout). A stale store is refused when a gate opens it, one gate at a time; here ALL
of them are named at once, each with what differs, before anything runs. Reads manifests only: opens no store.

    python3 tests/gates/meta/check_round_stores.py --sf 0.1 --spec-dir DIR [--spec-dir DIR ...] [--corners]
        [--store-root DIR] [--meta-vs-formats DIR]
--spec-dir (repeatable): spec sets whose stores live under the store root (output/stores/ldbc/sf<SF>/meta-<hash>).
--corners: also the engine-shaped corner specs (inputs/specs/corners/), under the store root (the writes checks)
  and under output/checks/meta-vs-formats/ldbc/sf<SF>/ (the LDBC checks), with the grid cells' stores kept there.
--store-root / --meta-vs-formats: other dirs to look in (a test on a scratch copy).
A store that is not there is listed as missing and is no failure (the round builds it at its start). Exit 1 when a
store is stale; the line says what to do (move it aside: nothing here removes a store).
"""
import argparse
import glob
import json
import os
import sys
import time
from kvevaluator.paths import ROOT

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import grid_capture
from kvevaluator.capture import store_identity
from kvevaluator.measure import stores
from kvevaluator.measure import store_layout
from kvevaluator.output_tree import Tree

CORNERS = os.path.join(EXP, "inputs", "specs", "corners")


def differs(have, want, at=""):
    """the paths at which two configs differ: 'adjacency.own_chunk: the store has nothing, the spec list_ordinals'"""
    if isinstance(have, dict) and isinstance(want, dict):
        out = []
        for k in sorted(set(have) | set(want)):
            out += differs(have.get(k), want.get(k), f"{at}.{k}" if at else k)
        return out
    if have == want:
        return []
    say = lambda v: "nothing" if v is None else json.dumps(v, sort_keys=True)
    return [f"{at}: the store has {say(have)}, the spec {say(want)}"]


def why_stale(store, spec_path):
    schema = stores._schema_artifact("canonical")
    with open(os.path.join(store, "manifest.json")) as f:
        m = json.load(f)
    if not store_layout.is_current(m):
        return f"index_filter_blocks {store_layout.of_manifest(m)!r}, this binary ingests {store_layout.ingest_default()!r}"
    have = m.get("value_codec")
    with open(spec_path) as f:
        want = stores.canonical_meta_spec(json.load(f), schema)
    return "; ".join(differs(have, want)[:4]) or "the recorded config differs"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--spec-dir", action="append", default=[])
    ap.add_argument("--corners", action="store_true")
    ap.add_argument("--store-root", default=None, help="default: the store root the round uses (stores.cpp_stores_root)")
    ap.add_argument("--meta-vs-formats", default=None, help="default: output/checks/meta-vs-formats/ldbc/sf<SF>")
    a = ap.parse_args()
    t0 = time.time()
    todo = []                                   # (store dir, spec path)
    specs = [p for d in a.spec_dir for p in sorted(glob.glob(os.path.join(os.path.abspath(d), "*.json")))]
    corner_specs = sorted(glob.glob(os.path.join(CORNERS, "corner_*.json"))) if a.corners else []
    for p in specs + corner_specs:
        store = grid_capture.meta_store(a.sf, "interactive", p, build=False)
        if a.store_root:
            store = os.path.join(a.store_root, os.path.basename(store))
        todo.append((store, p))
    if a.corners:                               # the LDBC corner checks' own stores: the corners and the grid cells kept there
        mvf = a.meta_vs_formats or str(Tree().check("meta-vs-formats", "ldbc", a.sf))   # check_meta_vs_formats' stores
        for store in sorted(glob.glob(os.path.join(mvf, "meta-*"))):
            stem = os.path.basename(store)[len("meta-"):]
            if os.path.exists(os.path.join(store, stores.META_LAYOUT_FILE)):     # a store named by its hash: its layout file
                stem = json.load(open(os.path.join(store, stores.META_LAYOUT_FILE)))["layout"]
            for d in (CORNERS, grid_capture.GRID):
                if os.path.exists(os.path.join(d, f"{stem}.json")):
                    todo.append((store, os.path.join(d, f"{stem}.json")))
                    break
        for store in sorted(glob.glob(os.path.join(mvf, "*", "manifest.json"))):   # the engine formats' stores: no spec
            store = os.path.dirname(store)
            if not os.path.basename(store).startswith("meta-"):
                todo.append((store, None))
    seen, stale, missing, checked = set(), [], [], 0
    for store, spec in todo:
        if store in seen:
            continue
        seen.add(store)
        # the one check (stores.store_staleness): stamp, settled, index + filter layout, and the spec's config (an
        # engine format's store has no spec)
        state, why = stores.store_staleness(store, spec, "interactive")
        if state == store_identity.MISSING:
            missing.append(store)
            continue
        checked += 1
        if state != store_identity.USABLE:
            if spec and why.startswith("its recorded config"):
                why = why_stale(store, spec)    # a spec config that differs: name the fields
            stale.append((store, spec, why))
    for store, spec, why in stale:
        print(f"  STALE {store}\n        {('spec ' + os.path.relpath(spec, EXP)) if spec else 'engine format'}: {why}")
    if stale:
        print(f"  {len(stale)} stale stores: move each aside (e.g. into <its dir>-stale-<date>/); the round then builds it again. "
              f"A store goes stale when the binary writes another config for the same spec (a format change), its key-layout stamp moved, "
              f"its spec file was edited, its index + filter layout is not the binary's ingest default, or its codec or filter is "
              f"not what inputs/contract/store_config.json gives its name.")
    print(f"store preflight sf{a.sf}: {checked} stores checked against their specs, key layouts, index + filter layouts, codecs and filters, "
          f"{len(stale)} stale, {len(missing)} not there yet "
          f"(built at the round's start), {time.time() - t0:.1f} s")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
