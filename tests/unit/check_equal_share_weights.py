#!/usr/bin/env python3
"""The equal-share weights file (kvcost.layout_cost.query_weighting.equal_share_workload): each item's frequency is
1 / its cost on the reference layout, so the weighted sum over the reference is the number of queries, and the "sum"
form over the file is n x the "additive" form against that layout. Synthetic workload, no data, no store.
Also: a cost <= 0 refuses, a non-uniform cell name refuses (query_costs). The tracked files (QW.EQUAL_SHARE_DIR): each
names its base workload's queries in order, and main refuses any file but the rule's (another model, chunk view, scale or
reference), a file made at another scale than its dataset's deciding scale, a new file for a workload in neither
EQUAL_SHARE_ONLY nor EQUAL_SHARE_BESIDE, and one for a workload with frequencies of its own unless it is in
EQUAL_SHARE_BESIDE; a tracked file of an unlisted workload is refused by equal_share_ids. check_reference_total refuses Sp-Co's weighted total off the query count.

    python3 tests/unit/check_equal_share_weights.py [--plant freq]   (the plant must exit 1)
"""
import argparse
import math
import sys

from kvcost.layout_cost import query_weighting as QW


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", choices=("freq",), help="a negative control: perturb one frequency (must exit 1)")
    a = ap.parse_args()
    w = {"id": "t01", "items": [("bi", f"q{i}", 1.0, None) for i in range(5)]}
    ref = [0.5, 3.0, 1e6, 7.25, 42.0]
    other = {"ref": ref, "L": [1.0, 1.0, 2e6, 7.25, 84.0]}
    out = QW.equal_share_workload(w, ref, "t01-share", "a test")
    freq = [it["frequency"] for it in out["items"]]
    if a.plant:
        freq[2] *= 1.001
    bad = []
    if [it["query"] for it in out["items"]] != [q for _, q, _, _ in w["items"]]:
        bad.append("the items' order or queries changed")
    if any(f != 1.0 / c for f, c in zip(freq, ref)):
        bad.append("a frequency is not 1 / its reference cost")
    if math.fsum(f * c for f, c in zip(freq, ref)) != 5.0:
        bad.append("the weighted sum on the reference is not the query count")
    per_query = {f"q{i}": {cell: other[cell][i] for cell in other} for i in range(5)}
    add = QW.aggregate(per_query, "additive", ref="ref")
    s = {cell: math.fsum(f * other[cell][i] for i, f in enumerate(freq)) for cell in other}
    if any(abs(s[c] - 5 * add[c]) > 1e-12 * s[c] for c in other):
        bad.append(f"sum over the file {s} is not 5 x additive {add}")
    for name, call in (("a cost <= 0", lambda: QW.equal_share_workload(w, [1, 2, 0, 4, 5], "x", "x")),
                       ("a non-uniform cell", lambda: QW.query_costs(w, None, "props_split", "wops_bytes4k"))):
        try:
            call()
            bad.append(f"{name} was not refused")
        except ValueError:
            pass
    # the tracked files: one per workload with no frequencies of its own, the base workload's queries in order
    from kvcost.layout_cost import benchmarks
    files = 0
    for wid in QW.equal_share_ids():
        es = QW.equal_share(wid)
        (base,) = benchmarks.select(es["equal_share"]["dataset"], es["equal_share"]["scale"], wid).values()
        mine = es["items"]
        if [(fam, q, p) for fam, q, _, p in mine] != [(fam, q, p) for fam, q, _, p in base["items"]]:
            bad.append(f"{wid}: the equal-share file's items are not {wid}'s, in order")
        if any(not 0 < f < float("inf") for _, _, f, _ in mine):
            bad.append(f"{wid}: a frequency <= 0 or not finite")
        files += 1
    # main writes only the rule's file: each other setting exits before reading anything
    refused = 0
    for flag in (["--model", "bill_exact"], ["--chunk-view", "measured"], ["--sf", "30"], ["--ref", "props_split_adj_split"]):
        try:
            QW.main(["--workload", "b01", "--grid", "/nonexistent", "--check", *flag])
            bad.append(f"main {' '.join(flag)} was not refused")
        except SystemExit as e:
            if "by every model and every scale (one file)" not in str(e.code):
                bad.append(f"main {' '.join(flag)}: refused for another reason: {e.code}")
            refused += 1
    # a file made off its dataset's deciding scale is refused when read
    import json
    import tempfile
    from pathlib import Path
    es = json.loads(QW.equal_share_path("b01").read_text())
    es["equal_share"]["scale"] = "0.3"
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "b01.json"
        planted.write_text(json.dumps(es))
        real = QW.equal_share_path
        QW.equal_share_path = lambda wid: planted
        try:
            QW.equal_share("b01")
            bad.append("a b01 file made at sf0.3 was not refused")
        except ValueError:
            refused += 1
        finally:
            QW.equal_share_path = real
    # a workload in neither list (i02) gets no equal-share file
    try:
        QW.main(["--workload", "i02", "--dataset", "ldbc", "--grid", "/nonexistent"])
        bad.append("main made a file for i02")
    except SystemExit as e:
        if "in neither EQUAL_SHARE_ONLY" not in str(e.code):
            bad.append(f"main i02: refused for another reason: {e.code}")
        refused += 1
    # one with frequencies of its own, listed as EQUAL_SHARE_ONLY, gets none either (only EQUAL_SHARE_BESIDE may)
    only = QW.EQUAL_SHARE_ONLY
    QW.EQUAL_SHARE_ONLY = only + ("i02",)
    try:
        QW.main(["--workload", "i02", "--dataset", "ldbc", "--grid", "/nonexistent"])
        bad.append("main made a file for i02 listed as EQUAL_SHARE_ONLY")
    except SystemExit as e:
        if "frequencies of its own" not in str(e.code):
            bad.append(f"main i02 (EQUAL_SHARE_ONLY): refused for another reason: {e.code}")
        refused += 1
    finally:
        QW.EQUAL_SHARE_ONLY = only
    # a tracked file of a workload in neither list is refused by every reader (equal_share_ids)
    beside = QW.EQUAL_SHARE_BESIDE
    QW.EQUAL_SHARE_BESIDE = ()
    try:
        QW.equal_share_ids()
        bad.append("equal_share_ids took i01.json with i01 unlisted")
    except ValueError:
        refused += 1
    finally:
        QW.EQUAL_SHARE_BESIDE = beside
    # the once weighting: each EQUAL_SHARE_ONLY workload's own file, its equal-share file's queries in order, each once;
    # refused for a workload with frequencies of its own, and for a file with a frequency other than 1
    for wid in QW.EQUAL_SHARE_ONLY:
        once, es = QW.once(wid), QW.equal_share(wid)
        if [(f, q, p) for f, q, _, p in once["items"]] != [(f, q, p) for f, q, _, p in es["items"]]:
            bad.append(f"{wid}: the once file's items are not the equal-share file's, in order")
    try:
        QW.once_path("i01")
        bad.append("once_path took i01 (it has frequencies of its own)")
    except ValueError:
        refused += 1
    twice = json.loads(QW.once_path("b01").read_text())
    twice["items"][0]["frequency"] = 2
    with tempfile.TemporaryDirectory() as tmp:
        planted = Path(tmp) / "b01.json"
        planted.write_text(json.dumps(twice))
        real = QW.once_path
        QW.once_path = lambda wid: planted
        try:
            QW.once("b01")
            bad.append("once took a file with a frequency of 2")
        except ValueError:
            refused += 1
        finally:
            QW.once_path = real
    print(f"once: {len(QW.EQUAL_SHARE_ONLY)} workloads ({', '.join(QW.EQUAL_SHARE_ONLY)}), each its equal-share file's "
          "queries once")
    # Sp-Co's weighted total must be the query count
    ok = {f"q{i}": 1.0 / c for i, c in enumerate(ref)}
    costs = {f"q{i}": c for i, c in enumerate(ref)}
    QW.check_reference_total(ok, costs, "test")
    try:
        QW.check_reference_total({**ok, "q2": ok["q2"] * 1.001}, costs, "test")
        bad.append("check_reference_total took a frequency off by 0.1%")
    except SystemExit:
        refused += 1
    for b in bad:
        print("FAIL", b)
    print(f"check_equal_share_weights: 5 queries, 2 layouts, 2 refusals; {files} tracked files; {refused} rule refusals; "
          f"{'FAIL' if bad else 'PASS'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
