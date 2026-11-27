#!/usr/bin/env python3
"""A mix costed from a predicted set's part rows equals the same mix predicted whole, at sf0.1, end to end through the
front door (kvcost predict, then kvcost cost-layouts --count-source <the default tier's set> [--mix | a mix's spec
file]).

The check predicts, in a scratch tree of its own (as tests/gates/check_predict_sizes.py does: kvcost predict with
KVEVAL_OUTPUT_DIR, the stores shared: output_tree.own_tree; its statistics and tallies in the tree, which a library is
written in only), the nine grid layouts (the composer reads each part from the grid's cell that holds its setting), the
seven diagonals (Sp-Sp, Co-Co, Ch1k-Ch1k ... Ch256k-Ch256k) and inputs/specs/mixes/hubs_split_knows_chunked.json, for a
workload of five reads and five writes. Then it costs the set twice, each into an output dir of its own:
  direct    cost-layouts --count-source <the set>: every layout of the set costed whole, Sp-Co and the mix among them;
  composed  the same call with the mix's spec file as a layout, and Sp-Co as --mix "" --mix-base Sp-Co: both composed from
            the diagonals' part rows (the composer a predicted search-mix takes).
Checks (each prints what it covered; 0 covered fails):
  1. the composed call exits 0 and its rows say measured_as composed (direct: predicted);
  2. for Sp-Co and the mix, in both chunk views, every query's plan on the reads equals the direct plan, and every
     query's wops, bytes and wops_bytes4k and every raw lane (compose.CHANNELS) equal it (relative 1e-9);
  3. the workload's totals (the layouts' total_ops and total_bytes, and each model's total over the queries) equal it;
  4. a mix with a part the set does not hold (Chunked at 512k), and a set without the grid's nine, are refused, naming the
     kvcost predict call.
About a minute. Exit 1 when one fails.

    python tests/gates/check_predicted_mix.py"""
import json
import os
import subprocess
import sys
import tempfile

from kvcost.cli.predict import default_set
from kvevaluator import layouts as L
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree, own_tree
from kvcost.layout_cost import benchmarks, compose
from kvcost.layout_cost import layout_costs as LC

NAME = "check_predicted_mix"
KV = [sys.executable, "-m", "kvcost.cli.cli"]
MIX_SPEC = os.path.join(ROOT, "inputs", "specs", "mixes", "hubs_split_knows_chunked.json")
REL = 1e-9
MODELS = ("wops", "bytes", "wops_bytes4k")
ITEMS = [("interactive", q) for q in ("ic2", "ic7", "ic5", "is1", "is3")] + [("writes", q) for q in ("iu1", "iu2", "iu5", "iu6", "iu8")]
BAD = []


def check(name, ok, got=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {got}"), flush=True)
    if not ok:
        BAD.append(name)


def kv(*args, out=None, own_stats=False):
    """(rc, output) of a kvcost call into the output tree `out` (own_tree: the stores shared). own_stats: the statistics
    and tallies in the tree too (a tally library is written only inside the run's output root: tally_library.check_writable)"""
    env = own_tree(out) if out else None
    if env and own_stats:
        env["KVEVAL_STATS_DIR"] = os.path.join(env["KVEVAL_OUTPUT_DIR"], "stats")
    r = subprocess.run([*KV, *args], cwd=ROOT, capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


def close(x, y):
    return abs(x - y) <= REL * max(1.0, abs(x), abs(y))


def place(tree, wid, source=None):
    return LC.place(Tree(tree), "ldbc", "0.1", wid, source or default_set())


def rows_of(out, layout, view):
    """({(family, query): the layout's query_costs row}, its layouts.csv row) in one chunk view"""
    order, by_query = LC.read_query_costs(out, view)
    lay = [r for r in LC.read_layouts(out, view) if r["layout"] == layout]
    return {fq: per[layout] for fq, per in by_query.items() if layout in per}, (lay[0] if lay else None)


def compare(label, direct, composed, dkey, ckey, covered):
    """one layout's direct rows against its composed rows, both views: plans on the reads, every model per query, totals"""
    for view in LC.VIEWS:
        dq, dl = rows_of(direct, dkey, view)
        cq, cl = rows_of(composed, ckey, view)
        if not dq or not cq or dl is None or cl is None:
            check(f"2 {label} ({view} view) has rows on both sides", False, f"direct {len(dq)} queries, composed {len(cq)}")
            continue
        plans = [fq for fq in dq if fq[0] == "interactive" and dq[fq]["body"] != cq[fq]["body"]]
        costs = [(fq, m) for fq in dq for m in MODELS if not close(float(dq[fq][m]), float(cq[fq][m]))]
        lanes = [(fq, k) for fq in dq for k in compose.CHANNELS if not close(float(dq[fq][k] or 0), float(cq[fq][k] or 0))]
        n_plans = sum(fq[0] == "interactive" for fq in dq)
        covered["plans"] += n_plans
        covered["costs"] += len(dq) * len(MODELS)
        covered["lanes"] += len(dq) * len(compose.CHANNELS)
        check(f"2 {label} {view} view: {n_plans} read plans, {len(dq) * len(MODELS)} query costs and "
              f"{len(dq) * len(compose.CHANNELS)} raw lanes equal",
              not plans and not costs and not lanes and set(dq) == set(cq), (plans[:3], costs[:3], lanes[:3]))
        tot = [k for k in ("total_ops", "total_bytes") if not close(float(dl[k]), float(cl[k]))]
        covered["totals"] += 2
        check(f"3 {label} {view} view: total_ops {float(dl['total_ops']):.6g} and total_bytes {float(dl['total_bytes']):.6g} equal",
              not tot and cl["measured_as"] == "composed" and dl["measured_as"] == "predicted", (tot, cl["measured_as"], dl["measured_as"]))


def main():
    with tempfile.TemporaryDirectory() as tree, tempfile.TemporaryDirectory() as tmp:
        wl = os.path.join(tmp, f"{NAME}.json")
        json.dump({"id": NAME, "name": NAME, "dataset": "ldbc", "scale": "0.1", "title": f"tests/gates/{NAME}.py",
                   "items": [{"family": f, "query": q, "frequency": 1 + i, "params": None} for i, (f, q) in enumerate(ITEMS)]},
                  open(wl, "w"), indent=1)
        nine = [L.name_of(c) for c in L.CELLS]      # the composer reads each part from the grid's cell that holds its setting
        sizes = [L.name_of(L.cell_id(s, s)) for s in L.DIAGONALS]
        diag = nine + [x for x in sizes if x not in nine]
        for step in (("generate-plans", "--workload", wl, "--dataset", "ldbc=0.1"),
                     ("predict", "--workload", wl, "--dataset", "ldbc=0.1", *diag, "Sp-Co", MIX_SPEC)):
            rc, out = kv(*step, out=tree, own_stats=True)
            if rc:
                print(out[-2000:])
                print(f"{NAME} FAIL: kvcost {step[0]} rc={rc}")
                return 1
        base = ("--workload", wl, "--dataset", "ldbc=0.1", "--count-source", default_set(), "--from", tree)
        dout, cout = os.path.join(tmp, "direct"), os.path.join(tmp, "composed")
        rc, out = kv("cost-layouts", *base, out=dout)
        check(f"1 cost-layouts on the predicted set (whole layouts) exits 0: rc={rc}", rc == 0, out[-500:])
        rc, out = kv("cost-layouts", *base, MIX_SPEC, "--mix", "", "--mix-base", "Sp-Co", "--mix-name", "sp-co-composed", out=cout)
        check(f"1 cost-layouts with the mix's spec file and --mix composes from the part rows: rc={rc}", rc == 0, out[-800:])
        if BAD:
            print(f"{NAME} FAIL: {len(BAD)} failed: {BAD}")
            return 1
        d, c = place(dout, NAME), place(cout, NAME)
        mix = L.mix_name(L.expand(json.load(open(MIX_SPEC))))
        covered = {"plans": 0, "costs": 0, "lanes": 0, "totals": 0}
        compare("Sp-Co", d, c, "props_split_adj_condensed", sp_co_key(c), covered)
        compare("hubs_split_knows_chunked", d, c, mix, mix, covered)
        for m in MODELS:       # the model's total over the queries, as every reader makes it
            wk = benchmarks.read_workload(wl)[1]
            for dk, ck, label in (("props_split_adj_condensed", sp_co_key(c), "Sp-Co"), (mix, mix, "mix")):
                x, y = LC.combined(d, wk, m).get(dk), LC.combined(c, wk, m).get(ck)
                covered["totals"] += 1
                check(f"3 {label} total {m} over the queries: {x:.9g} and {y:.9g} equal", x is not None and y is not None and close(x, y), (x, y))
        # the mix's part not held: refused, naming the predict call
        rc, out = kv("cost-layouts", *base, "--mix", "knows lists=Ch512k", "--mix-base", "Sp-Co", "--mix-name", "no-512k", out=os.path.join(tmp, "refused"))
        check("4 a mix with Chunked at a size the set does not hold is refused, naming kvcost predict",
              rc != 0 and "kvcost predict" in out and "Ch<K>k-Ch<K>k" in out, out[-400:])
        # a set without the grid's nine: refused, naming the predict call
        rc, out = kv("predict", "--workload", wl, "--dataset", "ldbc=0.1", "--save-as", "diagonals-only", "Sp-Sp", "Co-Co", "Ch64k-Ch64k",
                     out=tree, own_stats=True)
        check(f"4 kvcost predict --save-as diagonals-only (the three diagonals) exits 0: rc={rc}", rc == 0, out[-400:])
        rc, out = kv("cost-layouts", *base[:5], "diagonals-only", *base[6:], "--mix", "", "--mix-base", "Sp-Co", "--mix-name", "sp-co",
                     out=os.path.join(tmp, "partial"))
        check("4 a set without the nine grid layouts is refused, naming the kvcost predict call",
              rc != 0 and "kvcost predict --workload" in out and "Sp-Ch64k" in out, out[-400:])
    n = sum(covered.values())
    print(f"{NAME} {'FAIL' if BAD else 'OK'}: {n} cells compared ({covered['plans']} read plans, {covered['costs']} query costs, "
          f"{covered['lanes']} raw lanes, {covered['totals']} totals), {len(BAD)} failed" + (f": {BAD}" if BAD else ""))
    return 1 if BAD or n == 0 else 0


def sp_co_key(out):
    """the layout id the composed Sp-Co rows carry (a composed mix is named by its content)"""
    return next(r["layout"] for r in LC.read_layouts(out) if r["name"] == "sp-co-composed")


if __name__ == "__main__":
    sys.exit(main())
