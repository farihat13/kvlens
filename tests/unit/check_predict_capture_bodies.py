#!/usr/bin/env python3
"""A predicted capture names each row's plan and body as a measured capture does, also when every body is sent: two
requests for one body each (`<cell>__ic2@<body>__1.json`, measure/predict_requests.py) give run rows with query `ic2` and
body `<body>`, never `ic2@<body>` in the query column. Synthetic statistics (check_predict_stats_schema.complete_stats) and
a tally holding only the spec: the steps are not estimated (ok 0), and the run rows are still written. Under a second.

Run:  python3 tests/unit/check_predict_capture_bodies.py
"""
import csv
import json
import os
import subprocess
import sys
import tempfile

from check_predict_stats_schema import SCHEMA, complete_stats

from kvevaluator.measure import predict_requests
from kvevaluator.paths import INPUTS_DIR, cpp_bin

CELL = "props_split_adj_split"
BODIES = ["index_walk", "two_phase"]


def main():
    h = json.load(open(SCHEMA))["hash"]
    with tempfile.TemporaryDirectory() as t:
        json.dump(complete_stats(h), open(f"{t}/stats.json", "w"))
        json.dump({"anchor_degrees": {}, "anchor_recency": {}, "records": {"path": "r.jsonl", "lines": 100},
                   "schema_hash": h}, open(f"{t}/anchor.json", "w"))
        for d in (f"stores/ldbc/sf0.1/meta-{CELL}", "tally", "req"):
            os.makedirs(f"{t}/{d}")
        spec = json.load(open(os.path.join(str(INPUTS_DIR), "specs", "grid", f"{CELL}.json")))
        json.dump({"spec": spec}, open(f"{t}/tally/meta-{CELL}.json", "w"))
        predict_requests.main(["--family", "interactive", "--sf", "0.1", "--cells", CELL, "--queries",
                               ",".join(f"ic2@{b}" for b in BODIES), "--nparam", "1", "--store-root", f"{t}/stores",
                               "--out", f"{t}/req"])
        reqs = sorted(f"{t}/req/{f}" for f in os.listdir(f"{t}/req"))
        r = subprocess.run([str(cpp_bin()), "predict", "--plans", "--stats", f"{t}/stats.json", "--anchor-stats",
                            f"{t}/anchor.json", "--tally-dir", f"{t}/tally", "--schema", SCHEMA, "--out-predicted",
                            f"{t}/cap/p", "--out-predicted-anchor", f"{t}/cap/a", *reqs], capture_output=True, text=True)
        path = f"{t}/cap/p/counts/{CELL}.csv"
        if not os.path.exists(path):
            print(f"FAIL: no capture written (rc {r.returncode}): {r.stderr.strip()[-400:]}")
            return 1
        rows = sorted((x["query"], x["body"]) for x in csv.DictReader(open(path)))
    want = sorted(("ic2", b) for b in BODIES)
    ok = rows == want
    print(f"  {'ok  ' if ok else 'FAIL'} run rows (query, body): {rows}; want {want}")
    print(f"predict capture bodies {'PASS' if ok else 'FAIL'}: {len(reqs)} requests, {len(rows)} run rows compared")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
