#!/usr/bin/env python3
"""A store is reused only when its ingest finished (owner: evaluator infra; the planted test of store_identity.store_built
and ingest_needed, and of the store builds that ask them).

A store is built when its manifest says so, not when its DIR exists: an ingest stopped midway leaves a dir of SSTs
and no manifest, which the next run must not take as built. In a scratch
store root (KVEVAL_STORE_DIR), at --sf:
  0. the control: a complete store (dgraph) is reused by ensure_store: its manifest's bytes, mtime and inode and
     the dir's file list unchanged, no aside dir made;
  1. a real ingest (nebula) killed once it has written a file: ensure_store moves the dir aside
     (<root>/ldbc/aside-<date>/sf<SF>/nebula, store_aside.aside_dir_for) and rebuilds it;
  2. a store whose manifest says settled: false (arangodb): rebuilt, the old one aside;
  3. check_meta_vs_formats.ensure_store on four copies of the control: a complete one (reused, untouched), and no
     manifest, settled false, a truncated manifest (each rebuilt, the old one aside);
  4. python/kvevaluator/measure/ensure_stores.py on a grid cell whose ingest was killed midway: rebuilt, the old one aside;
  5. a store an ingest is still writing (the ingest stopped with SIGSTOP): ensure_store refuses it (rc != 0,
     naming the running ingest) and does not move it; resumed, that ingest finishes the store. The second caller
     waits on the store's build lock, held by the first; it refuses once it sees the ingest stopped (store_aside);
  6. the same for a two-codec ingest (--compression <the decided codec>,zstd) with a relative --out, through ensure_store on its
     zstd dir.
A rebuilt store must be built (a settled manifest) and its aside entry must hold the half-built dir.

  python3 tests/gates/check_half_built_store.py [--sf 0.1]
This tree's Python is tested (the old --tree option, which ran another tree's Python through PYTHONPATH, is gone: the
packages are installed, so run the other tree's own copy of this check). The binary is KVEVAL_CPP_BIN or the build's,
the record stream this tree's cache. Nine sf0.1 ingests, about
two minutes; the scratch root (under output/) is removed at the end. Exit 0 = every assertion holds."""
import argparse
import glob
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from kvevaluator.paths import OUTPUT_DIR, ROOT
from kvevaluator.output_tree import place
from kvevaluator.datasets import schema_path

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.capture import binary, store_identity
from kvevaluator.contract import store_build
from kvevaluator.measure import records

META = os.path.join(EXP, "experiments", "meta")    # check_meta_vs_formats.py's dir: a -c child imports it from its cwd
CELL = "props_split_adj_split"                       # a grid cell (case 4)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    a = ap.parse_args()
    tree = EXP
    tmp = tempfile.mkdtemp(prefix="half-built-", dir=os.path.join(str(OUTPUT_DIR)))
    root = os.path.join(tmp, place("ldbc", a.sf))   # where ensure_store builds below the store root: ldbc/sf<SF>
    def aside(store):
        """the aside entry of the store dir `store` (store_aside.aside_dir_for: <its parent's parent>/aside-<date>/<its
        parent's name>/<its name>), found by its name: store_aside dates the dir when it moves the store, so a check that
        crosses midnight UTC sees a later date than its start"""
        parent = os.path.dirname(store)
        rel = os.path.join(os.path.basename(parent), os.path.basename(store))
        found = sorted(glob.glob(os.path.join(os.path.dirname(parent), "aside-*", rel)))
        return found[-1] if found else os.path.join(os.path.dirname(parent), "aside-<none>", rel)

    def asides():
        """every aside dir made below the scratch root"""
        return sorted(glob.glob(os.path.join(tmp, "**", "aside-*"), recursive=True))
    env = dict(os.environ, KVEVAL_STORE_DIR=tmp, KVEVAL_CPP_BIN=binary.binary_path())
    env.pop("KVEVAL_STORE_COPIES", None)            # a round's store copies would shadow the scratch root
    fails, checked = [], 0
    started = []                                      # every ingest started here: none outlives the check
    print(f"[half-built] sf{a.sf}, the Python of {tree}, binary {env['KVEVAL_CPP_BIN']}, scratch root {tmp}")

    def expect(what, got, want):
        nonlocal checked
        checked += 1
        print(f"  {'ok  ' if got == want else 'FAIL'} {what}: {got!r}" + ("" if got == want else f" (want {want!r})"))
        if got != want:
            fails.append(what)

    def py(code, background=False, cwd=tree):
        cmd = [sys.executable, "-c", "from kvevaluator.capture import binary, store_identity\nfrom kvevaluator.measure import records, stores\n" + code]
        if background:
            started.append(subprocess.Popen(cmd, env=env, cwd=cwd, start_new_session=True,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
            return started[-1]
        r = subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def ensure(fmt, chunk=None, background=False):
        return py(f"stores.ensure_store('ldbc', {a.sf!r}, {fmt!r}, chunk={chunk!r})", background)

    def built(path):                                  # judged here, not by the tested tree's code
        try:
            return bool(json.load(open(os.path.join(path, "manifest.json"))).get("settled"))
        except (OSError, ValueError):
            return False

    def wait_for_files(path, proc, what):
        """until the ingest has written a file into path (then it is half-built), or SystemExit"""
        t0 = time.time()
        while not (os.path.isdir(path) and os.listdir(path)):
            if proc.poll() is not None or time.time() - t0 > 120:
                raise SystemExit(f"{what}: no file in {path} before the ingest ended: {proc.stdout.read()[-600:]}")
            time.sleep(0.01)

    def kill_midway(proc, path, what):
        wait_for_files(path, proc, what)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        expect(f"{what}: half-built after the kill (files, no settled manifest)",
               (bool(os.listdir(path)), built(path)), (True, False))

    def rebuilt(what, path, aside_entry):
        expect(f"{what}: built after the call", built(path), True)
        expect(f"{what}: the half-built dir is aside, not removed", os.path.isdir(aside_entry), True)
        expect(f"{what}: the aside entry is the half-built dir (files, no settled manifest)",
               os.path.isdir(aside_entry) and bool(os.listdir(aside_entry)) and not built(aside_entry), True)

    def stopped_ingest_refused(what, ingest_proc, store, call):
        """case 5 / 6: the ingest writing store stopped; call() must refuse it and not move it; resumed, it finishes"""
        wait_for_files(store, ingest_proc, what)
        pids = store_identity.live_ingests(store)
        expect(f"{what}: the running ingest is found from /proc (this tree's live_ingests)", len(pids), 1)
        for p in pids:
            os.kill(p, signal.SIGSTOP)
        try:
            rc, out = call()
        finally:
            for p in pids:
                os.kill(p, signal.SIGCONT)
        expect(f"{what}: refused (rc != 0)", rc != 0, True)
        expect(f"{what}: names the running ingest", "still writing" in out, True)
        aside_entry = aside(store)
        expect(f"{what}: not moved aside", os.path.exists(aside_entry), False)
        ingest_proc.wait()
        expect(f"{what}: the resumed ingest finishes it", (ingest_proc.returncode, built(store)), (0, True))

    try:
        os.makedirs(root)
        print("0. the control: a complete store is reused, untouched")
        rc, out = ensure("dgraph")
        dg = os.path.join(root, "dgraph")
        if rc != 0 or not built(dg):
            raise SystemExit(f"the control store did not build: {out[-800:]}")
        mp = os.path.join(dg, "manifest.json")
        before = (open(mp, "rb").read(), os.stat(mp).st_mtime_ns, os.stat(mp).st_ino, sorted(os.listdir(dg)))
        rc, out = ensure("dgraph")
        after = (open(mp, "rb").read(), os.stat(mp).st_mtime_ns, os.stat(mp).st_ino, sorted(os.listdir(dg)))
        expect("control: rc", rc, 0)
        expect("control: manifest bytes, mtime, inode and file list unchanged", after == before, True)
        expect("control: no aside dir", asides(), [])

        print("1. a real ingest killed midway (nebula), then ensure_store")
        neb = os.path.join(root, "nebula")
        kill_midway(ensure("nebula", background=True), neb, "nebula")
        rc, out = ensure("nebula")
        expect("nebula: rc", rc, 0)
        rebuilt("nebula", neb, aside(neb))

        print("2. settled: false (arangodb), then ensure_store")
        ar = os.path.join(root, "arangodb")
        rc, out = ensure("arangodb")
        m = json.load(open(os.path.join(ar, "manifest.json")))
        m["settled"] = False
        json.dump(m, open(os.path.join(ar, "manifest.json"), "w"))
        rc, out = ensure("arangodb")
        expect("arangodb: rc", rc, 0)
        rebuilt("arangodb", ar, aside(ar))

        print("3. check_meta_vs_formats.ensure_store on copies of the control: complete, no manifest, settled false, truncated")
        corners = os.path.join(tmp, "corners")
        def unsettle(p):
            m = json.load(open(p))
            m["settled"] = False
            json.dump(m, open(p, "w"))

        def truncate(p):
            with open(p, "r+") as f:
                f.truncate(os.path.getsize(p) // 2)

        store = os.path.join(corners, "complete")
        shutil.copytree(dg, store)
        mp = os.path.join(store, "manifest.json")
        before = (open(mp, "rb").read(), os.stat(mp).st_mtime_ns, sorted(os.listdir(store)))
        rc, out = py(f"import check_meta_vs_formats as m\nm.ensure_store({a.sf!r}, {corners!r}, 'dgraph', None, 'complete')",
                     cwd=META)
        expect("ensure_store complete: rc", rc, 0)
        expect("ensure_store complete: manifest bytes, mtime and file list unchanged",
               (open(mp, "rb").read(), os.stat(mp).st_mtime_ns, sorted(os.listdir(store))) == before, True)
        expect("ensure_store complete: no aside entry", os.path.exists(aside(store)), False)
        for tag, plant in (("no-manifest", os.remove), ("unsettled", unsettle), ("truncated", truncate)):
            store = os.path.join(corners, tag)
            shutil.copytree(dg, store)
            plant(os.path.join(store, "manifest.json"))
            rc, out = py(f"import check_meta_vs_formats as m\nm.ensure_store({a.sf!r}, {corners!r}, 'dgraph', None, {tag!r})",
                         cwd=META)
            expect(f"ensure_store {tag}: rc", rc, 0)
            rebuilt(f"ensure_store {tag}", store, aside(store))

        print(f"4. ensure_stores.py on the grid cell {CELL}, its ingest killed midway")
        es = [sys.executable, "-m", "kvevaluator.measure.ensure_stores", "--sf", a.sf, "--cells", CELL]
        name = subprocess.run(es + ["--names"], env=env, cwd=tree, capture_output=True, text=True).stdout.split()[0]
        cell = os.path.join(root, name)
        proc = subprocess.Popen(es + ["--jobs", "1"], env=env, cwd=tree, start_new_session=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        started.append(proc)
        kill_midway(proc, cell, name)
        r = subprocess.run(es + ["--jobs", "1"], env=env, cwd=tree, capture_output=True, text=True)
        expect(f"{name}: ensure_stores rc", r.returncode, 0)
        rebuilt(name, cell, aside(cell))

        print("5. a store an ingest is still writing (pertype_csr-c64k, the ingest stopped), then ensure_store")
        pt = os.path.join(root, "pertype_csr-c64k")
        stopped_ingest_refused("pertype_csr-c64k", ensure("pertype_csr", 65536, background=True), pt,
                               lambda: ensure("pertype_csr", 65536))

        print("6. a two-codec ingest (the decided codec and zstd) with a relative --out, then ensure_store on its zstd dir")
        two = os.path.join(corners, "two-zstd")
        with open(records.records_file("ldbc", a.sf)) as rin:
            started.append(subprocess.Popen(
                [env["KVEVAL_CPP_BIN"], "ingest", "--schema", schema_path("ldbc"),
                 "--format", "dgraph", "--out", "two", "--compression", f"{store_build('compression')},zstd", "--scale", a.sf],
                stdin=rin, cwd=corners, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
        stopped_ingest_refused("two-zstd", started[-1], two, lambda: py(
            f"import check_meta_vs_formats as m\nm.ensure_store({a.sf!r}, {corners!r}, 'dgraph', None, 'two-zstd')",
            cwd=META))
    finally:
        for proc in started:                          # resumed, then ended: a stopped ingest must not outlive the check
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGCONT)
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[half-built] {checked} checks, {len(fails)} failed" + (f": {fails}" if fails else ""))
    if checked < 42:
        print(f"HALF-BUILT FAIL: only {checked} checks ran (want 42)")
        return 1
    print("HALF-BUILT OK" if not fails else "HALF-BUILT FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
