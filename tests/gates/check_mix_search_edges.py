#!/usr/bin/env python3
"""kvcost search-mix and front on the paths the reproduction gate never reaches (check_mix_search_reproduces.py covers
the answers the old scripts gave): each case runs the CLI on a grid capture and asserts on the row it wrote.

  1. a byte limit no mix fits (B = 0.1): search-mix answers with the fewest-bytes mix, fits false, proven, under wops
     and under wops_bytes4k (whose re-price check reads the model's table, not the fallback's)
  2. front with a walk and limits below every mix: a "front" row (not "staircase"), its named points read off the walk,
     every staircase entry empty
  3. front --no-walk --limits caps: a "staircase" row whose named points are the named budgets (B=1, 2, 3, 5)
  4. the result key: the same question run again from another directory replaces its row; with --max-nodes it adds
     one (a node-limited answer never replaces a proven one)
  5. lanes: every search-mix row's and every staircase step's whole cost vector adds up to its wops and bytes
     (cost_models.currencies, relative 1e-9)
  6. --limits file:<json>: the staircase at case 3's limits, written to a file with their kinds, gives case 3's
     answers limit by limit (wops, bytes, kind)

    python3 tests/gates/check_mix_search_edges.py --sf 0.1 --from <grid> [--dataset ldbc] [--workload i01]
Prints each case and the cells it covered; exit 1 on a failed case or one that covered nothing.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.output_tree import Tree, own_tree, tree_of
from kvevaluator.paths import ROOT
from kvcost.layout_cost.cost_models import currencies
from kvcost.layout_search import results as R

FAILS, RAN = [], []


def check(ok, what):
    RAN.append(what)
    print(f"  {'ok  ' if ok else 'FAIL'} {what}")
    if not ok:
        FAILS.append(what)


def kvcost(a, out, *args, cwd=ROOT):
    """kvcost on the tree whose captures place --from names, written into the output tree `out` (its stores shared)"""
    cmd = [sys.executable, "-m", "kvcost.cli.cli", *args, "--dataset", f"{a.dataset}={a.sf}", "--from", a.tree,
           "--workload", a.workload, "--settings", "Sp,Co,Ch64k"]
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=own_tree(out))
    if r.returncode != 0:
        print(r.stdout[-2000:], r.stderr[-2000:])
    return r.returncode


def rows(out, a):
    with open(Tree(out).summary(a.dataset, a.sf, "mix-search", a.workload, R.stem("per-type") + ".json")) as f:
        return json.load(f)["rows"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--from", dest="from_dir", required=True, help="a grid capture root")
    ap.add_argument("--dataset", default="ldbc")
    ap.add_argument("--workload", default="i01")
    a = ap.parse_args()
    a.from_dir = os.path.abspath(a.from_dir)
    a.tree = str(tree_of(a.from_dir))
    out = tempfile.mkdtemp(prefix="mix-search-edges-", dir=os.environ.get("TMPDIR"))
    print(f"[check_mix_search_edges] {a.dataset} sf{a.sf} {a.workload} on {a.from_dir}; rows under {out}")

    # 1. no mix fits B = 0.1
    rc = [kvcost(a, out, "search-mix", "--cost-model", "bytes"),
          kvcost(a, out, "search-mix", "--cost-model", "wops", "--byte-limit", "0.1x"),
          kvcost(a, out, "search-mix", "--cost-model", "wops_bytes4k", "--byte-limit", "0.1x")]
    check(rc == [0, 0, 0], f"1 search-mix runs: rc {rc}")
    if rc == [0, 0, 0]:
        rs = rows(out, a)
        fewest = [r for r in rs if r["question"] == "fewest" and r["model"] == "bytes"][0]
        check(fewest["totals"]["bytes"] > 0.1 * fewest["reference_bytes"],
              "1 the limit is below the fewest-bytes mix (else the case asks nothing)")
        for m in ("wops", "wops_bytes4k"):
            r = [x for x in rs if x["question"] == "within" and x["model"] == m][0]
            check(r["fits"] is False and r["proven"] is True and r["assignment"] == fewest["assignment"],
                  f"1 {m} within 0.1: fits false, proven, the fewest-bytes mix")

    # 2. a walk and limits below every mix
    rc = kvcost(a, out, "search-mix", "--front", "--byte-limit", "0.1")
    check(rc == 0, f"2 front --byte-limit 0.1: rc {rc}")
    if rc == 0:
        r = [x for x in rows(out, a) if x["limit"] == "0.1"][0]
        check(r["question"] == "front" and r["points"] and "fewest wops" in r["named"]
              and all(x["wops"] is None for x in r["staircase"]),
              f"2 a front row, {len(r['points'])} points, named {sorted(r['named'])}, {len(r['staircase'])} empty limits")

    # 3. the staircase alone names the named budgets
    rc = kvcost(a, out, "search-mix", "--byte-limit", "caps")
    check(rc == 0, f"3 --byte-limit caps (the staircase alone): rc {rc}")
    if rc == 0:
        r = [x for x in rows(out, a) if x["question"] == "staircase" and x["limit"] == "caps"][0]
        check({"B=1", "B=2", "B=3", "B=5"} <= set(r["named"]), f"3 named {sorted(r['named'])}")

    # 4. the key: another directory replaces; a node limit adds
    before = len(rows(out, a)) if os.path.exists(Tree(out).summary(a.dataset, a.sf, "mix-search")) else 0
    rc = [kvcost(a, out, "search-mix", "--cost-model", "bytes", cwd=out),
          kvcost(a, out, "search-mix", "--cost-model", "bytes", "--max-nodes", "1")]
    check(rc == [0, 0], f"4 runs: rc {rc}")
    if rc == [0, 0]:
        rs = [x for x in rows(out, a) if x["question"] == "fewest" and x["model"] == "bytes"]
        check(len(rows(out, a)) == before + 1 and sorted(str(x["max_nodes"]) for x in rs) == ["1", "None"],
              f"4 rows {before} -> {len(rows(out, a))}; fewest-bytes rows by node limit {sorted(str(x['max_nodes']) for x in rs)}")

    # 5. lanes add up to the totals
    rs = rows(out, a)
    pairs = [(f"{r['model']} {r['question']}", r["lanes"], r["totals"]["wops"], r["totals"]["bytes"])
             for r in rs if r["question"] in ("fewest", "within")]
    pairs += [(f"staircase {x['cap_kind']}", x["lanes"], x["wops"], x["bytes"])
              for r in rs if r["question"] in ("front", "staircase") for x in r["staircase"] if x["wops"] is not None]
    off = [w for w, v, o, b in pairs if v is None or any(abs(c - t) > 1e-9 * max(1.0, abs(t))
                                                         for c, t in zip(currencies(v).values(), (o, b)))]
    check(pairs and not off, f"5 lanes add up to wops and bytes on {len(pairs)} answers; off: {off[:3]}")

    # 6. the same limits from a file
    stair = [x for x in rs if x["question"] == "staircase" and x["limit"] == "caps"][0]["staircase"]
    path = os.path.join(out, "limits.json")
    with open(path, "w") as f:
        json.dump({"limits": [{"cap_bytes": x["cap_bytes"], "cap_kind": x["cap_kind"]} for x in stair]}, f)
    rc = kvcost(a, out, "search-mix", "--byte-limit", f"file:{path}")
    check(rc == 0, f"6 --byte-limit file: rc {rc}")
    if rc == 0:
        got = [x for x in rows(out, a) if x["question"] == "staircase" and x["limit"] == f"file:{path}"][0]["staircase"]
        same = [(x["cap_bytes"], x["cap_kind"], x["wops"], x["bytes"]) for x in got] == \
               [(x["cap_bytes"], x["cap_kind"], x["wops"], x["bytes"]) for x in stair]
        check(same and len(got) > 0, f"6 {len(got)} limits from the file, each equal to case 3's")

    ok = not FAILS and len(RAN) >= 13
    print(f"check_mix_search_edges {'OK' if ok else 'FAIL'}: {len(RAN)} checks, {len(FAILS)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
