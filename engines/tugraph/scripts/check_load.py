#!/usr/bin/env python3
"""Check the running TuGraph against what was loaded: every label holds the rows of its CSV files (the load's
expected_rows.tsv), and every index of the design (schema/ldbc_import.json) exists. Exit 1 on a difference.

    check_load.py --expected backup/runs-sf0.3/expected_rows.tsv --schema schema/ldbc_import.json [--no-edges | --no-rows]
"""
import argparse
import collections
import json
import os
import sys
import urllib.request

BASE = f"http://127.0.0.1:{os.environ.get('TUGRAPH_PORT', '7070')}"


def post(path, body, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--expected", required=True, help="label, rows, file per line (prepare_import.py wrote it)")
    ap.add_argument("--schema", required=True)
    ap.add_argument("--no-edges", action="store_true", help="skip the edge counts (each scans its label)")
    ap.add_argument("--no-rows", action="store_true", help="count no label at all, check the indexes alone: the check "
                                                           "of a cold start, which must leave the store unread")
    a = ap.parse_args()
    token = post("/login", {"user": os.environ.get("TUGRAPH_USER", "admin"),
                            "password": os.environ.get("TUGRAPH_PASSWORD", "73@TuGraph")})["jwt"]

    def cypher(text):
        out = post("/cypher", {"graph": "default", "script": text}, token)
        if "error_message" in out:
            sys.exit(f"check_load: {text!r}: {out['error_message']}")
        return out["result"]

    expected = collections.Counter()
    with open(a.expected, encoding="utf-8") as f:
        for line in f:
            label, rows, _ = line.rstrip("\n").split("\t")
            expected[label] += int(rows)
    with open(a.schema, encoding="utf-8") as f:
        design = json.load(f)["schema"]
    bad = checked = 0
    for label in design:
        name, is_vertex = label["label"], label["type"] == "VERTEX"
        if a.no_rows or (not is_vertex and a.no_edges):
            continue
        got = cypher(f"MATCH (n:{name}) RETURN count(n)" if is_vertex else f"MATCH ()-[r:{name}]->() RETURN count(r)")[0][0]
        checked += 1
        if got != expected[name]:
            bad += 1
            print(f"check_load: {name}: {got} loaded, {expected[name]} in the CSV files", file=sys.stderr)
    want_index = {(lb["label"], p["name"]) for lb in design if lb["type"] == "VERTEX"
                  for p in lb["properties"] if p.get("index") or p["name"] == lb["primary"]}
    have_index = {(row[0], row[1]) for row in cypher("CALL db.indexes()")}
    for miss in sorted(want_index - have_index):
        bad += 1
        print(f"check_load: no index on {miss[0]}.{miss[1]}", file=sys.stderr)
    for extra in sorted(have_index - want_index):
        bad += 1
        print(f"check_load: an index the design does not name: {extra[0]}.{extra[1]}", file=sys.stderr)
    print(f"check_load: {checked} labels and {len(want_index)} indexes checked, {bad} differ")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
