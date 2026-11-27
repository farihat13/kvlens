#!/usr/bin/env python3
"""A write's list read has no fallback either: with no degree_hist row for any owner label of the list it rewrites
(edge type and direction), a write step in predict --plans refuses, the message giving the flat form; that form, put
back, is read. On a 40-person knows graph (check_predict_rules.py's frontier fixture, statistics built from it) and a
Condensed-adjacency layout (an edge delete rewrites both ends' lists), the plan w_del_e, level predicted. No store. (An edge
insert reads degree_by_far_label instead: check_predict_insert_far_label.py.)

    python3 tests/unit/check_predict_write_flat_statistics.py
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its fixture graphs and statistics builder)
import check_predict_stats_schema as S  # (its file writer)
from kvevaluator.paths import ROOT, cpp_bin

SPEC = os.path.join(str(ROOT), "inputs", "specs", "grid", "props_split_adj_condensed.json")   # lists are read and rewritten
PLAN = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "writes", "microbench", "plans", "w_del_e.json")

fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def main():
    lines, _, _, _, _ = R.fx_frontier_inclusion()
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        order = sorted(range(len(lines)), key=lambda i: (lines[i][0] != "v", lines[i][2] if lines[i][0] == "v" else lines[i][1], i))
        with open(rec, "w") as f:
            for i in order:
                f.write(json.dumps(lines[i]) + "\n")
        R.build_stats.build(rec, R.SCHEMA, "ldbc", "fixture", os.path.join(t, "stats", "stats.json"))
        os.makedirs(os.path.join(t, "tally"))
        with open(os.path.join(t, "tally", "meta-fixture.json"), "w") as f:
            subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", SPEC, "--records", rec], stdout=f,
                           check=True)
        req = {"params": {"src": 0, "dst": 30}, "run": {"P": 1}, "plan": json.load(open(PLAN)),
               "store": {"dir": os.path.join(t, "meta-fixture"), "format": "meta", "scale": "fixture", "schema": R.SCHEMA}}
        path = S.write(os.path.join(t, "fixture__w_ins_e_plain__1.json"), req)
        built = json.load(open(os.path.join(t, "stats", "stats.json")))
        n = [0]

        def run(stats):
            """(exit code, stderr, the run row, the write step's row) at level predicted"""
            n[0] += 1
            sp = S.write(os.path.join(t, f"s{n[0]}.json"), stats)
            cap = os.path.join(t, f"cap{n[0]}")
            r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--out-predicted", cap, "--stats", sp,
                                "--anchor-stats", os.path.join(t, "stats", "anchor_stats.json"),
                                "--tally-dir", os.path.join(t, "tally"), "--schema", R.SCHEMA, path],
                               capture_output=True, text=True)
            row, step = {}, {}
            if os.path.exists(os.path.join(cap, "counts", "fixture.csv")):
                row = next(csv.DictReader(open(os.path.join(cap, "counts", "fixture.csv"), newline="")), {})
                step = next(csv.DictReader(open(os.path.join(cap, "steps", "fixture.csv"), newline="")), {})
            return r.returncode, r.stderr, row, step

        rc, err, row, step = run(built)
        case("the built degree_hist prices the write: ok, PUTs and the lists' bytes", rc == 0 and row.get("ok") == "1"
             and float(step.get("P") or 0) > 0 and float(step.get("write_value") or 0) > 0,
             f"exit {rc}, ok {row.get('ok')}, P {step.get('P')}, write_value {step.get('write_value')} {err[-160:]}")
        bare = json.loads(json.dumps(built))
        bare["degree_hist"] = {k: v for k, v in bare["degree_hist"].items() if "|knows|" not in k}
        rc, err, row, _ = run(bare)
        m = re.search(r'"degree_hist": (\{.*\}) \(inputs/contract/stats\.schema\.json\)', err)
        case("no degree_hist row for knows: the write step refuses, naming the list and the flat form",
             row.get("ok") == "0" and re.search(r"no degree_hist row for knows\|(out|in)", err) is not None
             and "read by a write's list sizes" in err and m is not None, f"exit {rc}, ok {row.get('ok')}: {err[-300:]}")
        if m:
            flat = json.loads(m.group(1))
            case("the flat form names Person's knows list", bool(flat) and all(k.startswith("Person|knows|") for k in flat),
                 str(list(flat)))
            # put each flat form back as the messages give it (the step reads both ends' lists: up to two refusals)
            back = json.loads(json.dumps(bare))
            for _ in range(2):
                back["degree_hist"].update(flat)
                rc, err, row, step = run(back)
                m = re.search(r'"degree_hist": (\{.*\}) \(inputs/contract/stats\.schema\.json\)', err)
                if row.get("ok") == "1" or not m:
                    break
                flat = json.loads(m.group(1))
            case("the flat forms, put back from the messages, are read: the write is priced", rc == 0 and row.get("ok") == "1"
                 and float(step.get("P") or 0) > 0, f"exit {rc}, ok {row.get('ok')}, {list(back['degree_hist'])}: {err[-200:]}")
    ok = not fails
    print(f"predict write flat statistics {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
