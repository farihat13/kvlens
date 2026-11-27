#!/usr/bin/env python3
"""Row order across layouts: does every store hand on the same rows, in the same order, between the steps of a plan?
(The composition proof's A3 and Lemma 2. A step's count can depend on the
order of its input, so equal row COUNTS, which check_grid.py compares, are not enough.)

Reads the row hashes of a capture (grid_bodies.py writes row-hashes/<cell>.csv: per KV step its output rows as a digest, in
order and with the order ignored) for every store of the captures (the cells, the mixes, the named stores), and compares,
per (workload, query, param, plan, step), the digest across every store that ran that plan. Every plan that was
measured is compared, not only the chosen one. Flow steps are not in the capture: their output is a function of the KV
steps' outputs. A difference is reported as another ORDER (the same rows) or other ROWS.

--control flips one digest of one store in the loaded data: the check must fail and name that store and step.

  python3 tests/gates/meta/check_row_order.py --sf 0.1 [--root <captures dir>] [--control]
Exit 0 = every compared step equal on every store (and the control caught); a capture without row hashes fails.
Opens no store. Needs the captures (kvcost measure).
"""
import argparse
import copy
import os
import sys
from collections import Counter, defaultdict

from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Tree, capture_in

# the read captures below a captures dir: (label, workload, param set: None the workload's default, whose dir is reported/)
SUBDIRS = (("interactive", "interactive", None), ("interactive held-out", "interactive", "held-out"), ("microbench", "microbench", None))


def load(root):
    """{(capture, query, param, plan, step idx, step, op): {store: (rows, digest, set digest)}}, the stores per capture,
    [stores whose file lacks a digest]"""
    got, stores, empty = defaultdict(dict), defaultdict(set), []
    for capture, wl, params in SUBDIRS:
        cap = capture_in(root, wl, params)
        for store in cap.stores("row-hashes"):
            path = cap.path_of("row-hashes", store)
            stores[capture].add(store)
            with open(path) as f:
                for r in capture_layout.reader(f):
                    if not r["rows_digest"] or not r["rows_set_digest"]:
                        empty.append(f"{capture}: {store}")
                        break
                    k = (capture, r["query"], r["param_idx"], r["body"], r["step_idx"], r["step"], r["op"])
                    got[k][store] = (r["rows"], r["rows_digest"], r["rows_set_digest"])
    return got, stores, empty


def compare(got):
    """(steps compared on 2+ stores, (store, step) digests compared, [(key, kind, minority stores, majority store)])"""
    compared = pairs = 0
    fails = []
    for k in sorted(got):
        per = got[k]
        if len(per) < 2:
            continue
        compared += 1
        pairs += len(per)
        groups = defaultdict(list)
        for store in sorted(per):
            groups[per[store]].append(store)
        if len(groups) > 1:
            kind = "another ORDER" if len({g[2] for g in groups}) == 1 else "other ROWS"
            big = max(groups.values(), key=len)
            small = sorted(s for g in groups.values() if g is not big for s in g)
            fails.append((k, kind, small, big[0], sorted({g[0] for g in groups})))
    return compared, pairs, fails


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--root", default=None, help="LDBC's captures dir at the scale (default: output/captures/ldbc/sf<SF>)")
    ap.add_argument("--control", action="store_true", help="flip one digest of one store: the check must fail and name it")
    a = ap.parse_args()
    root = os.path.abspath(a.root) if a.root else str(Tree().place_dir("captures", "ldbc", a.sf))
    got, stores, empty = load(root)
    compared, pairs, fails = compare(got)
    for capture, _wl, _rows in SUBDIRS:
        n = sum(1 for k, per in got.items() if k[0] == capture and len(per) > 1)
        print(f"  {capture}: {len(stores[capture])} stores, {n} (query, param, plan, step) compared")
    for line in sorted(set(empty))[:10]:
        print(f"  NO DIGEST {line}: the capture predates run.emit.row_digest (re-measure it)")
    by = Counter((k[0], k[6], kind) for k, kind, *_ in fails)
    for (capture, op, kind), n in sorted(by.items()):
        print(f"  DIFFER {capture}, op {op}: {kind} on {n} steps")
    for k, kind, small, big, rows in fails[:12]:
        print(f"    {k[0]} {k[1]} param {k[2]} plan {k[3]} step {k[4]} {k[5]} ({k[6]}): {kind}; {len(small)} stores "
              f"(e.g. {small[0]}) against {big}; rows {rows}")
    nstores = len(set().union(*stores.values())) if stores else 0
    ok = not fails and not empty and compared > 0
    print(f"[row-order] sf{a.sf}: {nstores} stores ({root}), {compared} steps compared on 2+ stores "
          f"({pairs} digests), {len(fails)} differ -> {'OK: the same rows in the same order on every store' if ok else 'FAIL'}")
    if a.control and compared:
        ctl = copy.deepcopy(got)
        k = next(k for k in sorted(ctl) if len(ctl[k]) > 2)
        victim = sorted(ctl[k])[-1]
        rows, dig, sdig = ctl[k][victim]
        ctl[k][victim] = (rows, "f" * 16 if dig != "f" * 16 else "0" * 16, sdig)
        _, _, cf = compare(ctl)
        caught = any(f[0] == k and f[1] == "another ORDER" and victim in f[2] for f in cf)
        print(f"[row-order] CONTROL (one digest of {victim} flipped at {k[1]} param {k[2]} step {k[5]}): "
              f"{'caught, as another order' if caught else 'NOT caught'}")
        ok = ok and caught
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
