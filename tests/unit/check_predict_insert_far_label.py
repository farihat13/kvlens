#!/usr/bin/env python3
"""An edge insert prices the Condensed list it grows as the list a new edge from the far end's label meets
(degree_by_far_label: sum / n of the owner label|etype|dir|far label entry), and past the split size reads one part:
a full part for an existing far end, the last part (split / 2 expected) for a far vertex the request created. On a
40-person knows graph (check_predict_rules.py's frontier fixture, statistics built from it), level predicted, no store:

  - the list bytes read follow the entry: twice the entry's sum / n, twice the bytes (the old rules read degree_hist,
    so they do not move: the test fails there);
  - a file without the entry is refused, the message naming the flat form;
  - tier 1's flat entry (kvevaluator.predict.tier1) passes check_stats and costs the insert at the mean degree; on a
    hand-made file, an owner label's edges split over two declared far labels by their vertex counts;
  - with the adjacency split at 64 bytes, an insert towards an existing person reads a full part per side, one towards
    a person created earlier in the request reads half a part on the existing owner's side.

    python3 tests/unit/check_predict_insert_far_label.py
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its fixture graphs and statistics builder)
import check_predict_stats_schema as S  # (its file writer)
from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.predict import check_stats as CS
from kvevaluator.predict import tier1

GRID = os.path.join(str(ROOT), "inputs", "specs", "grid", "props_split_adj_condensed.json")
PLAN = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "writes", "microbench", "plans", "w_ins_e_plain.json")
SPLIT = 64

fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def person(n):
    return {"vid": [{"lit": "Person"}, {"lit": n}]}


def created_plan():
    """insert a new person, then a knows edge from person 0 to it"""
    return {"plan": "t_ins_e_to_new", "question": "an edge to a person created in the request", "for": "all formats",
            "params": {}, "let": {"old": person(0), "new": person(990)}, "steps": [
                {"name": "v", "op": "insert_vertex", "vid": {"let": "new"}, "label": "Person", "props": {}},
                {"name": "out", "op": "insert_edge", "etype": "knows", "src": {"let": "old"}, "dst": {"let": "new"},
                 "props": {},
                 "src_label": "Person", "dst_label": "Person"}], "output": "out"}


def main():
    lines, _, _, _, _ = R.fx_frontier_inclusion()
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        order = sorted(range(len(lines)), key=lambda i: (lines[i][0] != "v", lines[i][2] if lines[i][0] == "v" else lines[i][1], i))
        with open(rec, "w") as f:
            for i in order:
                f.write(json.dumps(lines[i]) + "\n")
        R.build_stats.build(rec, R.SCHEMA, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        built = json.load(open(os.path.join(t, "stats", "stats.json")))
        small = json.load(open(GRID))
        small["adjacency"]["split"] = SPLIT
        spec_small = os.path.join(t, "small.json")
        json.dump(small, open(spec_small, "w"))
        n = [0]

        def run(stats, spec, plan, params, store):
            """the step rows (by name) of one insert at level predicted, or the refusal's message"""
            os.makedirs(os.path.join(t, "tally"), exist_ok=True)
            tally = os.path.join(t, "tally", f"meta-{store}.json")
            if not os.path.exists(tally):
                with open(tally, "w") as f:
                    subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", spec, "--records", rec],
                                   stdout=f, check=True)
            n[0] += 1
            req = {"params": params, "run": {"P": 1}, "plan": plan,
                   "store": {"dir": os.path.join(t, f"meta-{store}"), "format": "meta", "scale": "fixture",
                             "schema": R.SCHEMA}}
            path = S.write(os.path.join(t, f"{store}__{plan['plan']}__{n[0]}.json"), req)
            sp = S.write(os.path.join(t, f"s{n[0]}.json"), stats)
            cap = os.path.join(t, f"cap{n[0]}")
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", cap, "--stats", sp,
                                "--anchor-stats", os.path.join(t, "stats", "anchor_stats.json"),
                                "--tally-dir", os.path.join(t, "tally"), "--schema", R.SCHEMA, path],
                               capture_output=True, text=True)
            steps = os.path.join(cap, "steps", f"{store}.csv")
            if r.returncode != 0 or not os.path.exists(steps):
                return r.stderr
            return {row["step"]: row for row in csv.DictReader(open(steps, newline=""))}

        def with_entry(ratio):
            """the built statistics, each knows entry set to `ratio` entries per list"""
            st = json.loads(json.dumps(built))
            for k, v in st["degree_by_far_label"].items():
                if "|knows|" in k:
                    st["degree_by_far_label"][k] = [v[0], v[0] * ratio]
            return st

        plan = json.load(open(PLAN))
        keys = sorted(k for k in built.get("degree_by_far_label", {}) if "|knows|" in k)
        case("the built statistics hold the knows entries", len(keys) == 2, str(keys))
        a, b = run(with_entry(4.0), GRID, plan, {"src": 0, "dst": 30}, "grid"), \
            run(with_entry(8.0), GRID, plan, {"src": 0, "dst": 30}, "grid")
        ok = isinstance(a, dict) and isinstance(b, dict)
        sa, sb = (float(a["out"]["scan_value"]), float(b["out"]["scan_value"])) if ok else (0, 0)
        case("the list bytes read follow the entry: twice sum / n, twice the bytes (the old rules ignore it)",
             ok and sa > 0 and abs(sb - 2 * sa) < 1e-9 * sb, f"4 per list {sa:.6g}, 8 per list {sb:.6g}" if ok else str(a)[-300:])
        st = json.loads(json.dumps(built))
        st.pop("degree_by_far_label", None)
        msg = run(st, GRID, plan, {"src": 0, "dst": 30}, "grid")
        case("a file without the entry is refused, naming the flat form",
             isinstance(msg, str) and "degree_by_far_label" in msg and "flat form" in msg, str(msg)[-200:])
        flat = tier1.derive(built)
        fk = sorted(k for k in flat.get("degree_by_far_label", {}) if "|knows|" in k)
        mean = {f"Person|knows|{o['direction']}": o["edges"] / o["owners"] for o in built["owners"]
                if o["etype"] == "knows" and o["owner_label"] == "Person"}
        case("tier 1 holds a flat knows entry per built key, each [n, n x the mean degree], check_stats agrees",
             fk == keys and all(abs(flat["degree_by_far_label"][k][1] - flat["degree_by_far_label"][k][0]
                                    * mean[k.rsplit("|", 1)[0]]) < 1e-9 * flat["degree_by_far_label"][k][1] for k in fk)
             and CS.check_tier1(flat) == [], f"{fk}, {CS.check_tier1(flat)[:2]}")
        m = run(flat, GRID, plan, {"src": 0, "dst": 30}, "grid")
        m1 = run(with_entry(1.0), GRID, plan, {"src": 0, "dst": 30}, "grid")
        ok = isinstance(m, dict) and isinstance(m1, dict)
        case("tier 1's flat entry costs the insert (not refused), more bytes than one entry per list",
             ok and float(m["out"]["scan_value"]) > float(m1["out"]["scan_value"]) > 0,
             f"flat {m['out']['scan_value']}, 1 per list {m1['out']['scan_value']}" if ok else str(m)[-300:])
        hand = {"owners": [{"etype": "e", "direction": "out", "owner_label": "A", "owners": 10, "edges": 40}],
                "type_counts": {"A": 10, "B": 30, "C": 10}}
        got = tier1._degree_by_far_label(hand, {"degree_by_far_label": {"A|e|out|B": [1, 1], "A|e|out|C": [1, 1],
                                                                        "C|e|in|A": [1, 1]}})
        case("two declared far labels: the edges split 3:1 by vertex count, at the mean degree 4; no owners row [0, 0]",
             got == {"A|e|out|B": [30.0, 120.0], "A|e|out|C": [10.0, 40.0], "C|e|in|A": [0, 0]}, str(got))
        big = with_entry(1000.0)   # every list past the 64-byte split
        e = run(big, spec_small, plan, {"src": 0, "dst": 30}, "small")
        c = run(big, spec_small, created_plan(), {}, "small")
        ok = isinstance(e, dict) and isinstance(c, dict)
        se, sc = (float(e["out"]["scan_value"]), float(c["out"]["scan_value"])) if ok else (0, 0)
        case("past the split: an existing far end reads a full part per side, a created one half a part",
             ok and abs(se - 2 * SPLIT) < 1e-9 and abs(sc - SPLIT / 2) < 1e-9,
             f"existing {se:g} (want {2 * SPLIT}), created {sc:g} (want {SPLIT / 2:g})" if ok else str(e if not isinstance(e, dict) else c)[-300:])
    ok = not fails
    print(f"predict insert far label {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
