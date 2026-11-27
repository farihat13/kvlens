#!/usr/bin/env python3
"""Hard-link copies of C++ stores for ONE gate run, so parts that read the same store can run side by side.

A session opens its store read-write (the read-only path records no tickers), so a store dir has one opener at a time.
`make` copies each named store with the binary's `checkpoint` (RocksDB hard links: the same SST files, no bytes
copied, about a second) into a fresh dir beside the stores, and prints that dir; a gate part started with
KVEVAL_STORE_COPIES=<dir> reads the copy (stores.store_dir). The copies are made at the start of the run and
removed at its end, so a copy can never outlive a re-ingest of its store. A copy is a plain store dir: the arena
marker `checkpoint` writes is replaced by copy.json (its source), so nothing can apply writes to it, and a write
check can take its own arena from it.

    python3 tests/gates/store_copies.py make --sf 0.1 [--sets 2] pertype_csr-c64k nebula   # prints one dir per set
    python3 tests/gates/store_copies.py remove <dir>...
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from kvevaluator.capture import binary
from kvevaluator.measure import stores

MARK = "gate-store-copies.json"   # in a dir this tool made: `remove` deletes nothing else


def make(sf, names, sets):
    src_root = os.path.dirname(os.path.realpath(stores.store_dir("ldbc", sf, "x")))   # the copies' filesystem = the stores'
    home = os.path.join(os.path.dirname(src_root), ".gate-copies")
    os.makedirs(home, exist_ok=True)
    out = []
    for _ in range(sets):
        root = tempfile.mkdtemp(prefix="run-", dir=home)
        with open(os.path.join(root, MARK), "w") as f:
            json.dump({"sf": sf, "stores": names, "pid": os.getpid()}, f)
        for name in names:
            src = stores.store_path(src_root, name)      # a meta-<layout> name: its store, meta-<hash>
            dst = stores.copy_dir(root, "ldbc", sf, src)
            if not os.path.exists(os.path.join(src, "manifest.json")):
                raise SystemExit(f"store_copies: no store {src} (just gate-prep {sf})")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            r = subprocess.run([binary.binary_path(), "checkpoint", src, dst], capture_output=True, text=True)
            if r.returncode != 0:
                raise SystemExit(f"store_copies: checkpoint {name} failed: {(r.stderr or r.stdout)[-800:]}")
            os.replace(os.path.join(dst, "arena.json"), os.path.join(dst, "copy.json"))
        out.append(root)
    return out


def remove(dirs):
    for d in dirs:
        if not os.path.exists(os.path.join(d, MARK)):
            raise SystemExit(f"store_copies: {d} was not made by this tool (no {MARK}): nothing removed")
        shutil.rmtree(d)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--sf", default="0.1")
    m.add_argument("--sets", type=int, default=1, help="how many copies of each store (one dir per set)")
    m.add_argument("names", nargs="+", help="store names under output/stores/ldbc/sf<SF>/")
    r = sub.add_parser("remove")
    r.add_argument("dirs", nargs="+")
    a = ap.parse_args()
    if a.cmd == "make":
        print("\n".join(make(a.sf, a.names, a.sets)))
    else:
        remove(a.dirs)


if __name__ == "__main__":
    main()
