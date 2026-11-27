#!/usr/bin/env python3
"""Tier 1 has no fallback for a tier-2 field: a missing one refuses, and the refusal says how to
write its flat form; a tier-2 field a file holds must agree with its tier-1 counts, or load refuses it. On the
synthetic LDBC statistics of check_predict_stats_schema.py (no store, seconds):

  load    the complete file loads; each planted disagreement is refused by name: degree_hist's vertices against
          type_counts, its summed degree against edge_totals, against the owners row, a sum of squares no degrees
          have, date_days' counts against sizes.content's u, a neighbor_degree_ratio below 1, a 'both' row against the
          label's out plus in, a bucket that is not a whole number; check_stats (its check_tier1) refuses each too
  a neighbour's degree, on a 40-person graph (check_predict_rules.py's frontier fixture; statistics built from it)
          and a two-hop knows plan (size-biased): no degree_hist row and no neighbor_degree_ratio: refused, the
          message naming both and the flat form, the ratio at 1; that form, taken from the message, gives the second
          hop the same rows as the flat degree_hist rows (every owner at the mean, from the owners row); a ratio of 2
          gives other rows (the value is read); the built histogram gives its own
  IC2's date window
          no date_days entry for Post|creationDate: refused, the message giving the flat form with the count from
          sizes.content; that form, an even spread, is read (IC2 refuses no body)

  a date filter, on the same graph
          a declared date (Person.birthday, off the date axis) with no date_days entry: refused, the flat form
          given; with its flat entry it is read; a one-bound filter on a field that is not a date (Person.firstName)
          keeps the filter rule
A write's list read (meta's degree_rows): check_predict_write_flat_statistics.py.

    python3 tests/unit/check_predict_flat_statistics.py
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its fixture graphs and statistics builder)
import check_predict_stats_schema as S  # (its synthetic file, request and tally helpers)
from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.predict import check_stats as CS

fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def copy(d):
    return json.loads(json.dumps(d))


def neighbour_cases():
    """a two-hop knows plan, size-biased: the second hop's rows under the statistics' variants"""
    lines, _, _, _, _ = R.fx_frontier_inclusion()
    steps = [R.scan("all", "Person"), R.hop("h1", "all"), R.hop("h2", "h1")]
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        order = sorted(range(len(lines)), key=lambda i: (lines[i][0] != "v", lines[i][2] if lines[i][0] == "v" else lines[i][1], i))
        with open(rec, "w") as f:
            for i in order:
                f.write(json.dumps(lines[i]) + "\n")
        R.build_stats.build(rec, R.SCHEMA, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", R.SPEC, "--records", rec], stdout=f,
                           check=True)
        req = {"params": {}, "run": {"P": 1}, "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta",
                                                         "scale": "fixture", "schema": R.SCHEMA},
               "plan": {"plan": "fixture", "params": {}, "let": {}, "estimate_hints": {"size_biased": ["knows"]},
                        "plans": {"b": {"output": "h2", "steps": steps}},
                        "bodies": {"b": {"sha": "000000000000", "sources": ["b"]}}}}
        path = S.write(os.path.join(t, "fixture__fixture__1.json"), req)
        built = json.load(open(os.path.join(t, "stats", "stats.json")))
        n = [0]

        def rows(stats):
            """(exit code, stderr, {step: rows} at level predicted)"""
            n[0] += 1
            sp = S.write(os.path.join(t, f"s{n[0]}.json"), stats)
            cap = os.path.join(t, f"cap{n[0]}")
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", cap, "--stats", sp,
                                "--anchor-stats", os.path.join(t, "stats", "anchor_stats.json"),
                                "--tally-dir", os.path.join(t, "tally"), "--schema", R.SCHEMA, path],
                               capture_output=True, text=True)
            got = {}
            if r.returncode == 0:
                with open(os.path.join(cap, "steps", "fixture.csv"), newline="") as f:
                    got = {x["step"]: float(x["rows"]) for x in csv.DictReader(f)}
            return r.returncode, r.stderr, got

        rc, err, hist = rows(built)
        case("the built degree_hist prices the second hop", rc == 0 and "h2" in hist, f"exit {rc}, {hist}")
        bare = copy(built)
        bare["degree_hist"] = {k: v for k, v in bare["degree_hist"].items() if "|knows|" not in k}
        rc, err, _ = rows(bare)
        m = re.search(r'"neighbor_degree_ratio": \{"([^"]+)": ([0-9.]+)\}', err)
        case("no degree_hist row and no neighbor_degree_ratio: refused, naming both and the flat form",
             rc != 0 and "no degree_hist row for knows|out" in err and "no neighbor_degree_ratio entry" in err and m,
             err[-300:])
        if not m:
            return
        key, val = m.group(1), float(m.group(2))
        case("the flat form is knows|out at 1", (key, val) == ("knows|out", 1.0), f"{key}: {val}")
        ratio1 = copy(bare)
        ratio1["neighbor_degree_ratio"] = {key: val}
        rc1, err1, r1 = rows(ratio1)
        # the flat degree_hist rows, every owner at the mean, from the owners row (the out lists of Person)
        o = next(x for x in built["owners"] if x["etype"] == "knows" and x["direction"] == "out")
        m_ = o["edges"] / o["owners"]
        flat = copy(built)
        flat["degree_hist"]["Person|knows|out"] = [[0, built["type_counts"]["Person"] - o["owners"], 0, 0],
                                                   [2, o["owners"], o["edges"], o["edges"] * m_]]
        rc2, err2, r2 = rows(flat)
        case("the ratio at 1 gives the second hop the flat degree_hist's rows",
             rc1 == 0 and rc2 == 0 and r1.get("h2") is not None and r1.get("h2") == r2.get("h2"),
             f"ratio 1: {r1.get('h2')}, flat rows: {r2.get('h2')} (exit {rc1} / {rc2}: {(err1 + err2)[-160:]})")
        two = copy(bare)
        two["neighbor_degree_ratio"] = {key: 2.0}
        rc3, _, r3 = rows(two)
        case("a ratio of 2 gives other rows (the value is read)", rc3 == 0 and r3.get("h2") not in (None, r1.get("h2")),
             f"{r3.get('h2')} against {r1.get('h2')}")
        case("the built histogram gives its own rows (the frontier is not flat)", hist.get("h2") != r1.get("h2"),
             f"{hist.get('h2')} against {r1.get('h2')}")

        # a date filter: a declared date (the schema's type) with no date_days entry refuses; any other field keeps the
        # filter rule. Person.birthday is a date off the date axis (Post, Comment creationDate): its
        # window is not priced on the axis's span
        def window(field, bounds=2):
            w = copy(req)
            w["plan"]["estimate_hints"] = {}
            w["plan"]["plans"] = {"b": {"output": "next", "steps": [   # the hop after the filter shows its rows
                R.scan("all", "Person"),
                {"name": "p", "op": "read_vertex_props", "vids": {"ref": "all"}, "props": [field], "label": "Person"},
                {"name": "nn", "op": "filter", "rows": {"ref": "p"}, "pred": {"notnull": {"field": field}}},   # as ic4
                {"name": "w", "op": "filter", "rows": {"ref": "nn"},
                 "pred": {"and": [{"ge": [{"field": field}, 1000]}, {"lt": [{"field": field}, 2000]}][:bounds]}},
                {"name": "wv", "op": "pluck", "rows": {"ref": "w"}, "field": "_vid"},
                R.hop("next", "wv")]}}
            S.write(path, w)
        window("birthday")
        no_days = copy(built)
        no_days.setdefault("date_days", {}).pop("Person|birthday", None)
        rc, err, _ = rows(no_days)
        case("a date filter on a declared date with no date_days entry (Person|birthday): refused with the flat form",
             "no date_days entry for 'Person|birthday'" in err and "read by a date filter" in err and '"first_day"' in err,
             err[-260:])
        u = built["sizes"]["content"]["prop|Person|birthday"]["u"]
        flat = copy(no_days)
        flat["date_days"]["Person|birthday"] = {"first_day": 0, "counts": [u / 4] * 4}
        rc, err, got = rows(flat)
        case("its flat date_days entry is read: the filter priced", rc == 0 and got.get("next") is not None, f"exit {rc}, {got}")
        window("firstName", bounds=1)   # one bound: two would take the date axis's span (a known open rule)
        rc, err, got = rows(no_days)
        case("a one-bound filter on a field that is not a date (Person.firstName): the filter rule kept, not refused",
             rc == 0 and "date_days" not in err and got.get("next") is not None, f"exit {rc}, {got}, {err[-160:]}")



def main():
    neighbour_cases()
    good = json.load(open(S.SCHEMA))["hash"]
    full = S.complete_stats(good)
    with tempfile.TemporaryDirectory() as t:
        os.makedirs(os.path.join(t, "tally"))
        open(os.path.join(t, "empty.jsonl"), "w").close()
        with open(os.path.join(t, "tally", "meta-x.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", S.SCHEMA, "--spec",
                            os.path.join(str(ROOT), "inputs", "specs", "grid", "props_split_adj_split.json"),
                            "--records", os.path.join(t, "empty.jsonl")], stdout=f, check=True)
        tally = json.load(open(os.path.join(t, "tally", "meta-x.json")))
        tally["records"] = {"path": "r.jsonl", "lines": 100}
        json.dump(tally, open(os.path.join(t, "tally", "meta-x.json"), "w"))
        anchor = S.write(os.path.join(t, "anchor.json"), {"anchor_degrees": {}, "anchor_recency": {}, "schema_hash": good,
                                                          "records": {"path": "r.jsonl", "lines": 100}})
        empty_req = S.write(os.path.join(t, "req.json"), {})
        ic2 = S.request(os.path.join(t, "reqs", "x__ic2__1.json"), "ic2", {"person": "933", "maxDate": 1354060800000})
        n = [0]

        def predict(stats, req=empty_req):
            """predict --plans: (exit code, stderr, the predicted capture's dir)"""
            n[0] += 1
            out = os.path.join(t, f"cap{n[0]}")
            path = S.write(os.path.join(t, f"stats{n[0]}.json"), stats)
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", out, "--stats", path,
                                "--anchor-stats", anchor, "--tally-dir", os.path.join(t, "tally"), "--schema", S.SCHEMA,
                                req], capture_output=True, text=True)
            return r.returncode, r.stderr, out

        # ---- load: a tier-2 field must agree with tier 1 ----
        rc, err, _ = predict(full)
        case("load: the complete file is not refused", "disagrees" not in err and "have no '" not in err, err[-160:])
        planted = []
        d = copy(full); d["degree_hist"]["Person|knows|out"] = [[0, 3, 0, 0], [3, 8, 20, 60]]
        planted.append(("degree_hist's vertices against type_counts", d, "type_counts['Person'] is 10"))
        d = copy(full); d["degree_hist"]["Person|knows|out"] = [[0, 2, 0, 0], [3, 8, 25, 90]]
        planted.append(("degree_hist's summed degree against edge_totals", d, "edge_totals['knows'] is 20"))
        d = copy(full); d["owners"] = [{"etype": "knows", "direction": "out", "owner_label": "Person", "owners": 7, "edges": 20}]
        planted.append(("degree_hist against the owners row", d, "the owners row says 7"))
        d = copy(full); d["degree_hist"]["Person|knows|out"] = [[0, 2, 0, 0], [3, 8, 20, 40]]
        planted.append(("a sum of squares below (sum d)^2 / owners", d, "which no set of degrees has"))
        d = copy(full); d["date_days"]["Post|creationDate"]["counts"] = [3, 0, 8]
        planted.append(("date_days' counts against sizes.content's u", d, "has 10 vertices with the property"))
        d = copy(full); d["neighbor_degree_ratio"] = {"knows|both": 0.8}
        planted.append(("a neighbor_degree_ratio below 1", d, "below 1"))
        d = copy(full); d["degree_hist"]["Person|knows|both"] = [[0, 2, 0, 0], [4, 8, 80, 960]]
        planted.append(("a 'both' row whose summed degree is not out's plus in's", d, "the label's out and in rows sum to 40"))
        d = copy(full); d["degree_hist"]["Person|knows|out"] = [[0, 2, 0, 0], [1.7, 8, 20, 60]]
        planted.append(("a bucket that is not a whole number", d, "not a whole number"))
        case("check_stats: the complete file agrees with tier 1", CS.check_tier1(full) == [], CS.check_tier1(full))
        for what, d, says in planted:
            rc, err, _ = predict(d)
            case(f"load refuses {what}", rc != 0 and "disagrees with tier 1" in err and says in err, err[-200:])
            py = CS.check_tier1(d)
            case(f"check_stats refuses {what} too", len(py) >= 1, py[:1])   # it lists every one; load stops at the first

        # ---- IC2's date window ----
        no_date = copy(full)
        del no_date["date_days"]["Post|creationDate"]
        rc, err, _ = predict(no_date, ic2)
        case("no date_days entry: refused, the flat form with the count from sizes.content",
             rc != 0 and "no date_days entry for 'Post|creationDate'" in err and '"first_day"' in err
             and '"counts"' in err and "each 10 / the days" in err, err[-300:])
        even = copy(no_date)
        even["date_days"]["Post|creationDate"] = {"first_day": 15000, "counts": [2.5, 2.5, 2.5, 2.5]}
        rc, err, _ = predict(even, ic2)
        # the fixture's tally is empty (no layout statistics), so no KV step is priced: the case is the refusal's
        case("that form, an even spread, is read: IC2 refuses no body", " 0 (body, level) refused" in err
             and "no date_days entry" not in err, err[-160:])
    ok = not fails
    print(f"predict flat statistics {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
