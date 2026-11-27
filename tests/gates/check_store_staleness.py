#!/usr/bin/env python3
"""Every tool that asks "is this store usable" gives the same answer (owner: evaluator infra; the planted test of
stores.store_staleness and its callers).

ensure_stores.py, the round's preflight and reingest_stores.py --stale must agree: a store is present only with a
current key-layout stamp, and a store another process holds open (its LOCK) is not stale. A scratch store root (KVEVAL_STORE_DIR) gets, at --sf:
  - two real configurable-format stores, copied: one with an older key-layout stamp planted in its manifest, one
    current (the control);
  - two engine-format manifests (no data): nebula with the old stamp, dgraph current.
Then each caller must name the stamped stores stale and leave the controls alone:
  1. stores.store_staleness itself (and probe=True on the control: usable);
  2. python/kvevaluator/measure/ensure_stores.py: rc 1, the stamped store STALE and not rebuilt, the control present;
  3. tests/gates/meta/check_round_stores.py --store-root: rc 1, only the stamped store named;
  4. tests/gates/gate_preflight.py --gate cpp-diff: the stamped nebula STALE, dgraph not;
  5. python/kvevaluator/measure/reingest_stores.py --stale --dry-run: 1 of the 2 data stores stale (the engine manifests carry no
     data and are left out of this one);
  6. a held LOCK is not stale: with the control open in a server, store_staleness(probe=True) is BUSY and reingest
     --stale names it busy and does not select it.

  python3 tests/gates/check_store_staleness.py [--sf 0.1] [--ensure-stores PATH]
--ensure-stores: the ensure_stores.py to test (the copy from before the change must fail). Copies ~220 MB at sf0.1
(two stores) into a scratch dir under output/ (removed at the end). Exit 0 = every assertion holds."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from kvevaluator.paths import OUTPUT_DIR, ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import server, stores
from kvevaluator.capture import store_identity

GRID = os.path.join(EXP, "inputs", "specs", "grid")
STAMPED, CONTROL = "props_chunked_adj_chunked", "props_split_adj_split"     # two grid cells
OLD = "-planted-old"                                                         # appended to a stamp: a layout no binary reads


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--ensure-stores", default=os.path.join(EXP, "python", "kvevaluator", "measure", "ensure_stores.py"))
    a = ap.parse_args()
    py = sys.executable
    real = stores.scale_root("ldbc", a.sf)
    tmp = tempfile.mkdtemp(prefix="store-staleness-", dir=os.path.join(str(OUTPUT_DIR)))
    root = stores.scale_root("ldbc", a.sf, tmp)
    fails, checked = [], 0

    def expect(what, got, want):
        nonlocal checked
        checked += 1
        print(f"  {'ok  ' if got == want else 'FAIL'} {what}: {got!r}" + ("" if got == want else f" (want {want!r})"))
        if got != want:
            fails.append(what)

    def stamp(path):
        mp = os.path.join(path, "manifest.json")
        m = json.load(open(mp))
        m["key_codec"] = m["key_codec"] + OLD
        json.dump(m, open(mp, "w"), indent=1)

    def run(*cmd):
        env = dict(os.environ, KVEVAL_STORE_DIR=tmp)
        r = subprocess.run([py, *cmd], capture_output=True, text=True, env=env, cwd=EXP)
        return r.returncode, r.stdout + r.stderr

    try:
        os.makedirs(root)
        meta = {}
        for cell in (STAMPED, CONTROL):
            src = stores.store_path(real, f"meta-{cell}")      # its store, meta-<hash> or an older meta-<cell>
            if not os.path.exists(os.path.join(src, "manifest.json")):
                raise SystemExit(f"no sf{a.sf} store meta-{cell} under {real}: run kvcost measure --dataset ldbc={a.sf} --workload i01 Sp-Sp Co-Co Ch1k-Ch1k Ch4k-Ch4k Ch16k-Ch16k Ch64k-Ch64k Ch256k-Ch256k first")
            shutil.copytree(src, os.path.join(root, os.path.basename(src)), symlinks=True)
            meta[cell] = os.path.join(root, os.path.basename(src))
        for fmt in ("nebula", "dgraph"):                      # engine formats: the manifest alone
            os.makedirs(os.path.join(root, fmt))
            shutil.copy(os.path.join(real, fmt, "manifest.json"), os.path.join(root, fmt, "manifest.json"))
        stamp(meta[STAMPED])
        stamp(os.path.join(root, "nebula"))
        name = {cell: os.path.basename(meta[cell]) for cell in meta}   # the stores' dir names, as every tool prints them
        print(f"[staleness] sf{a.sf} scratch root {tmp}: {name[STAMPED]} ({STAMPED}) and nebula stamped {OLD!r}; "
              f"{name[CONTROL]} ({CONTROL}) and dgraph the controls")

        print("1. stores.store_staleness")
        s = stores.store_staleness(meta[STAMPED], os.path.join(GRID, f"{STAMPED}.json"))
        expect("the stamped store", (s[0], "key layout" in s[1]), (store_identity.STALE, True))
        expect("the control", stores.store_staleness(meta[CONTROL], os.path.join(GRID, f"{CONTROL}.json"))[0], store_identity.USABLE)
        expect("the control, probed (opened)", stores.store_staleness(meta[CONTROL], probe=True)[0], store_identity.USABLE)
        expect("the stamped engine manifest", stores.store_staleness(os.path.join(root, "nebula"))[0], store_identity.STALE)
        expect("the control engine manifest", stores.store_staleness(os.path.join(root, "dgraph"))[0], store_identity.USABLE)
        expect("a store not there", stores.store_staleness(os.path.join(root, "nope"))[0], store_identity.MISSING)

        print(f"2. ensure_stores ({os.path.relpath(a.ensure_stores, EXP)})")
        rc, out = run(a.ensure_stores, "--sf", a.sf, "--cells", f"{STAMPED},{CONTROL}", "--jobs", "1")
        expect("rc", rc, 1)
        expect("the stamped store is named STALE", f"{name[STAMPED]}: STALE" in out, True)
        expect("the control is present", f"{name[CONTROL]}: present" in out, True)
        expect("nothing was built", "built in" in out, False)

        print("3. check_round_stores (the round's preflight)")
        rc, out = run("tests/gates/meta/check_round_stores.py", "--sf", a.sf, "--spec-dir", GRID, "--store-root", root)
        expect("rc", rc, 1)
        expect("the stamped store is named", f"STALE {meta[STAMPED]}" in out, True)
        expect("the control is not", f"STALE {meta[CONTROL]}" in out, False)

        print("4. gate_preflight --gate cpp-diff")
        rc, out = run("tests/gates/gate_preflight.py", "--gate", "cpp-diff", "--sf", a.sf)
        expect("the stamped nebula is STALE", f"store nebula (sf{a.sf}; cpp-diff) is STALE: key layout" in out, True)
        expect("dgraph is not STALE", f"store dgraph (sf{a.sf}; cpp-diff) is STALE" in out, False)

        print("5. reingest_stores --stale --dry-run")
        for fmt in ("nebula", "dgraph"):                      # no data to open: out of the probe's way
            shutil.move(os.path.join(root, fmt), os.path.join(tmp, fmt))
        rc, out = run("python/kvevaluator/measure/reingest_stores.py", "--sf", a.sf, "--stale", "--dry-run", "--ignore-disk")
        expect("1 of the 2 stores is stale", "[reingest] 1 of 2 stores are stale" in out, True)
        expect("the stamped one is the one re-ingested", name[STAMPED] in out and f"{name[CONTROL]}:" not in out, True)

        print("6. a held LOCK is not stale")
        m = json.load(open(os.path.join(meta[CONTROL], "manifest.json")))
        with server.CppServer(meta[CONTROL], m["format"], schema=store_identity.manifest_schema_path(m) or "") as srv:
            srv.request({"store": {"dir": os.path.abspath(meta[CONTROL]), "format": m["format"],
                                   "schema": os.path.abspath(srv.schema)}})
            expect("the held control, probed", stores.store_staleness(meta[CONTROL], probe=True)[0], store_identity.BUSY)
            rc, out = run("python/kvevaluator/measure/reingest_stores.py", "--sf", a.sf, "--stale", "--dry-run", "--ignore-disk")
            expect("reingest: still 1 stale, the held one busy",
                   "[reingest] 1 of 2 stores are stale, 1 busy" in out and f"{name[CONTROL]}: BUSY" in out, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[staleness] {checked} assertions over 6 callers' views (2 data stores + 2 engine manifests)")
    if fails:
        print(f"STORE STALENESS FAIL: {len(fails)} of {checked}: {fails}")
        return 1
    print("STORE STALENESS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
