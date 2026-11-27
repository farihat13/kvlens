#!/usr/bin/env python3
"""stored_size and the Dollars section of cost_models on a synthetic tally library (no store, no data, under a second):

  diagonal   a uniform spec's size is its own setting tally's totals, per part too
  off        Condensed props with Split adjacency: Condensed's props rows + Split's adjacency rows + the index rows
  mix        a per-type mix (mixes.spec_of: one edge type Condensed on a Split base) takes that edge type's rows from
             Condensed, every other part from Split
  print      fingerprint is the sha256 of the sorted "<file name> <sha256>" lines of the library's setting files
  refused    a block no entry serves (Chunked) and entries of two tally versions are refused, never built
  chunks     chunk_value_log2_max is the largest bucket of the chunk families the spec's parts take, None with none
  dollars    stored_bytes adds 100 B per item; monthly_dollars and crossover_rate agree at the crossover rate
  slo        slo_choice keeps only the layouts within k on every query; balance is each layout's worst ratio to the best
  control    (--control) the mix with its edge type swapped back to Split sizes as Split: the mix case must then fail

    python3 tests/unit/check_stored_size.py [--control]
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile

from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import mixes
from kvcost.layout_cost import stored_size as Z
from kvevaluator.paths import ROOT

GRID = ROOT / "inputs" / "specs" / "grid"
RECORDS = {"lines": 10, "content_sha256": "c" * 64}
fails, cases = [], 0


def check(name, ok, detail=""):
    global cases
    cases += 1
    if not ok:
        fails.append(f"{name}: {detail}")


def spec(cell):
    with open(GRID / f"{cell}.json") as f:
        return json.load(f)


def group(part, typ, direction, keys, kb, vb):
    return {"part": part, "type": typ, "direction": direction, "owner": typ if part != "adjacency" else "Person",
            "detail": "", "keys": keys, "key_bytes": kb, "value_bytes": vb}


def tally(sp, scale):
    """a setting tally over Person props, knows and likes out- and in-lists, one index; sizes scaled per setting"""
    groups = [group("vertex_props", "Person", "", 10 * scale, 100 * scale, 1000 * scale),
              group("index", "Person", "", 10, 50, 0)]
    for e, base in (("knows", 20), ("likes", 30)):
        for d in ("in", "out"):
            groups.append(group("adjacency", e, d, base * scale, base * 10 * scale, base * 100 * scale))
    groups.sort(key=lambda g: (g["part"], g["type"], g["direction"], g["owner"], g["detail"]))
    return {"format": "meta", "tally_version": 2, "spec": sp, "records": RECORDS, "groups": groups, "chunks": {},
            "locality": [], "totals": {k: sum(g[k] for g in groups) for k in ("keys", "key_bytes", "value_bytes")},
            "wall_s": 0.0, "provenance": {}}


def library(root, entries, version=2, chunks=None):
    """a library of one synthetic setting tally per (cell, scale); `chunks` {cell: the tally's chunk families}"""
    os.makedirs(root, exist_ok=True)
    for i, (cell, scale) in enumerate(entries):
        sp = spec(cell)
        stem = os.path.join(root, f"setting-{i:012d}")
        with open(stem + ".json", "w") as f:
            json.dump({**tally(sp, scale), "chunks": (chunks or {}).get(cell, {})}, f)
        with open(stem + ".key.json", "w") as f:
            json.dump({"spec": sp, "tally_version": version if i == 0 else 2}, f)
    return Z.Frozen(root)


def part_sum(t, part, typ=None):
    return sum(g["keys"] for g in t["groups"] if g["part"] == part and (typ is None or g["type"] == typ))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--control", action="store_true", help="swap the mix back to Split: the mix case must fail")
    a = ap.parse_args()
    sp_t = tally(spec("props_split_adj_split"), 1)
    co_t = tally(spec("props_condensed_adj_condensed"), 2)
    with tempfile.TemporaryDirectory() as tmp:
        lib = library(os.path.join(tmp, "lib"), [("props_split_adj_split", 1), ("props_condensed_adj_condensed", 2)])

        z = Z.size_of(spec("props_split_adj_split"), lib)
        check("diagonal", all(z[k] == sp_t["totals"][k] for k in sp_t["totals"]) and
              z["adjacency_keys"] == part_sum(sp_t, "adjacency"), json.dumps(z))

        z = Z.size_of(spec("props_condensed_adj_split"), lib)
        want = part_sum(co_t, "vertex_props") + part_sum(sp_t, "adjacency") + part_sum(sp_t, "index")
        check("off", z["keys"] == want and z["vertex_props_keys"] == part_sum(co_t, "vertex_props"), f"{z['keys']} != {want}")

        check("no chunk families: no largest item", z["chunk_value_log2_max"] is None, str(z["chunk_value_log2_max"]))
        fam = {"knows:out": {"chunks": 3, "id_span": 9, "value_bytes_log2": {"10": 2, "13": 1}},
               "knows:in": {"chunks": 1, "id_span": 3, "value_bytes_log2": {"11": 1}}}
        lib2 = library(os.path.join(tmp, "lib2"), [("props_split_adj_split", 1), ("props_condensed_adj_condensed", 2)],
                       chunks={"props_split_adj_split": fam})
        z = Z.size_of(spec("props_split_adj_split"), lib2)
        check("largest Chunked item", z["chunk_value_log2_max"] == 13, str(z["chunk_value_log2_max"]))
        z = Z.size_of(spec("props_condensed_adj_condensed"), lib2)
        check("a Condensed part takes no family of the Split entry", z["chunk_value_log2_max"] is None, str(z["chunk_value_log2_max"]))

        lines = "".join(f"{n} {hashlib.sha256(open(os.path.join(tmp, 'lib', n), 'rb').read()).hexdigest()}\n"
                        for n in sorted(os.listdir(os.path.join(tmp, "lib"))))
        check("print", Z.fingerprint(os.path.join(tmp, "lib")) == hashlib.sha256(lines.encode()).hexdigest()
              and Z.fingerprint(os.path.join(tmp, "lib")) != Z.fingerprint(os.path.join(tmp, "lib2")))

        mix = {("props", "Person"): "split", ("adj", "knows"): "split" if a.control else "condensed",
               ("adj", "likes"): "split"}
        mspec, _ = mixes.spec_of(mix)
        z = Z.size_of(mspec, lib)
        want = (part_sum(sp_t, "vertex_props") + part_sum(co_t, "adjacency", "knows") + part_sum(sp_t, "adjacency", "likes")
                + part_sum(sp_t, "index"))
        check("mix", z["keys"] == want, f"{z['keys']} != {want}")

        for name, run in (("refused: no entry", lambda: Z.size_of(spec("props_chunked_adj_split"), lib)),
                          ("refused: two versions", lambda: library(os.path.join(tmp, "v"), [
                              ("props_split_adj_split", 1), ("props_condensed_adj_condensed", 2)], version=1))):
            try:
                run()
                check(name, False, "not refused")
            except SystemExit:
                check(name, True)

    check("dollars: item overhead", CM.stored_bytes(3, 10, 20) == 330)
    m = CM.monthly_dollars(100.0, 2.0, 5 * CM.GB, CM.crossover_rate(100.0, 2.0, 5 * CM.GB))
    check("dollars: crossover", abs(m["requests"] - m["storage"]) < 1e-9 * m["total"], json.dumps(m))
    check("dollars: storage", CM.storage_dollars(4 * CM.GB) == 4 * CM.DOLLARS_PER["gb_month"])

    costs = {"A": {"q1": 1.0, "q2": 4.0}, "B": {"q1": 2.0, "q2": 1.0}, "C": {"q1": 0.5, "q2": 2.0}}
    ratios = CM.slo_ratios(costs, costs["A"])
    totals = {"A": 10.0, "B": 3.0, "C": 5.0}
    check("slo: k=1", CM.slo_choice(ratios, totals, 1.0) == ("C", 5.0), str(CM.slo_choice(ratios, totals, 1.0)))
    check("slo: k=2", CM.slo_choice(ratios, totals, 2.0) == ("B", 3.0), str(CM.slo_choice(ratios, totals, 2.0)))
    check("slo: none", CM.slo_choice({"B": ratios["B"]}, totals, 1.5) is None)
    check("balance", CM.balance(costs) == {"A": (4.0, "q2"), "B": (4.0, "q1"), "C": (2.0, "q2")}, str(CM.balance(costs)))

    for f in fails:
        print(f"FAIL {f}")
    if a.control:
        caught = any(f.startswith("mix:") for f in fails) and len(fails) == 1
        print(f"{cases} cases; control {'caught: PASS' if caught else 'NOT caught: FAIL'}")
        return 0 if caught else 1
    print(f"{cases} cases, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
