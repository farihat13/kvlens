#!/usr/bin/env python3
"""read_filter_by_label_probe costs one probe per DISTINCT vid on every format (the audit's minor 4): a vid repeated in
the step's input is decided once and kept at every occurrence. Runs a one-step fixture plan on every sf<SF> store, the
input [A, B, X] (two Persons and a Post, untagged) against the same three vids repeated, [A, A, B, X, B, A, X, X]: every
count and byte lane must be equal, and the repeated input must keep 5 rows (A three times, B twice) where the first
keeps 2. No workload plan and no number comes from it.

--control adds one GET to one store's repeated-input result after the runs: the check must fail and name that store.

  python3 tests/gates/check_label_probe_distinct.py [--sf 0.1] [--stores a,b] [--control]
  python3 tests/gates/check_label_probe_distinct.py --names     # the stores it opens (for store_copies.py make)
Exit 0 = equal on every store run, and every selected store ran (the count run of selected is printed; 0 run is a
failure). A store that cannot be opened or run (a held LOCK among them) is a failure, not a skip: a skip would let a
race shrink the check silently. A store of a parked schema (datasets/ldbc/schema.py PARKED_SCHEMAS) or of a retired
format is not selected, and counted. Stores are read through stores.store_dir: a round's copy (KVEVAL_STORE_COPIES)
where it has one. The binary is KVEVAL_CPP_BIN or the build's.
"""
import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.capture import binary, store_identity
from kvevaluator.measure import stores
from kvevaluator.measure.server import CppServer
from kvevaluator.datasets.ldbc.schema import PARKED_SCHEMAS
PARKED_SCHEMA_FILES = {f"schema_{s}.json" for s in PARKED_SCHEMAS}
RETIRED_FORMATS = ("hybrid",)   # the per-type router (retired): the binary refuses an old store of it

A, B, X = "00000000000003a5", "0000000000000469", "1000000000000003"   # Person 933, Person 1129, Post 3 (sf0.1 records)
BASE = [A, B, X]
REPEATED = [A, A, B, X, B, A, X, X]
PLAN = {"plan": "label_probe_distinct", "for": "check_label_probe_distinct.py; not a workload plan",
        "params": {"vids": {}},
        "steps": [{"name": "p", "op": "read_filter_by_label_probe", "vids": {"param": "vids"}, "label": "Person"}]}
LANES = ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss")


def run(server, store, fmt, schema, vids):
    resp = server.request({"store": {"dir": store, "schema": schema, "format": fmt}, "plan": PLAN,
                           "params": {"vids": vids}, "run": {"P": 1, "warmup": 0, "measured": 1}})
    if resp.get("error"):
        return None, resp["error"]
    st = resp["steps"][0]
    return ({k: st[k] for k in LANES} | {f"bytes.{k}": v for k, v in sorted(st["bytes"].items())}, st["rows"]), None


def select(sf, only):
    """(the stores to run: [(name, format, schema path)], not selected: [reason], parked: [name]) from the manifests"""
    root = stores.scale_root("ldbc", sf)   # the originals' dir: a round's copy set may hold only some of them
    names = only.split(",") if only else sorted(
        d for d in os.listdir(root) if os.path.exists(os.path.join(root, d, "manifest.json")))
    run_, skipped, parked = [], [], []
    for name in names:
        m = json.load(open(os.path.join(root, name, "manifest.json")))
        fmt, schema = m.get("format"), store_identity.manifest_schema_path(m)   # in this tree, a moved file resolved
        if not fmt or not schema:
            skipped.append(f"{name} (no format, or no schema file, in its manifest)")
        elif fmt in RETIRED_FORMATS:
            skipped.append(f"{name} (format {fmt} is not built)")
        elif os.path.basename(schema) in PARKED_SCHEMA_FILES:   # parked: not maintained
            parked.append(name)
        else:
            run_.append((name, fmt, str(schema)))
    return run_, skipped, parked


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--stores", help="comma-separated store names (default: every store with a manifest)")
    ap.add_argument("--control", action="store_true", help="add one GET to one store's result: the check must fail")
    ap.add_argument("--names", action="store_true", help="print the store names this check opens (for store copies) and exit")
    a = ap.parse_args()
    selected, skipped, parked = select(a.sf, a.stores)
    if a.names:
        print(" ".join(name for name, _, _ in selected))
        return
    ver = json.loads(subprocess.run([binary.binary_path(), "--version"], capture_output=True, text=True).stdout)
    print(f"[label-probe-distinct] binary {binary.binary_path()} sha {ver['binary_git_sha'][:10]} "
          f"dirty {ver['binary_dirty']}")
    copies = os.environ.get("KVEVAL_STORE_COPIES")
    print(f"[label-probe-distinct] stores read from {'the copies in ' + copies if copies else 'the originals'} (where a copy exists)")
    got, fails = {}, []
    for name, fmt, schema in selected:
        store = os.path.abspath(stores.store_dir("ldbc", a.sf, name))
        with CppServer(store, fmt=fmt, schema=schema) as server:
            base, err = run(server, store, fmt, schema, BASE)
            rep, err2 = run(server, store, fmt, schema, REPEATED)
        if err or err2:   # a store that does not run is a failure (a held LOCK: another opener)
            fails.append(f"{name}: did not run: {(err or err2).get('message', err or err2)}"[:200])
            continue
        got[name] = (base, rep)
    if a.control and got:
        name = sorted(got)[0]
        counts, rows = got[name][1]
        got[name] = (got[name][0], (counts | {"G": counts["G"] + 1}, rows))
        print(f"[label-probe-distinct] CONTROL: one GET added to {name}'s repeated-input result")
    for name, ((c1, r1), (c2, r2)) in sorted(got.items()):
        moved = {k: (c1[k], c2[k]) for k in c1 if c1[k] != c2.get(k)}
        if moved:
            fails.append(f"{name}: the repeated input costs more: {moved}")
        if (r1, r2) != (2, 5):
            fails.append(f"{name}: rows {r1} and {r2}, not 2 and 5")
    for s in skipped:
        print(f"  not selected: {s}")
    for f in fails:
        print(f"  FAIL {f}")
    n = len(got)
    caught = a.control and n > 0 and any(f.startswith(sorted(got)[0] + ":") for f in fails)
    verdict = (caught and len(fails) == 1) if a.control else (n > 0 and n == len(selected) and not fails)
    print(f"[label-probe-distinct] sf{a.sf}: {n} of {len(selected)} stores run, {len(skipped)} not selected, "
          f"{len(parked)} parked (not run), {len(fails)} failed -> "
          + ("OK" if verdict else "FAILED") + (" (control caught)" if a.control and verdict else ""))
    sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
