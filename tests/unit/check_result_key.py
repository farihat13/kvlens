#!/usr/bin/env python3
"""The answer's identity every legal body must agree on (kvevaluator.plan_choice.candidates.result_key, query_order): no store.

  1. every plan file has one order (query_order raises otherwise), counted by kind;
  2. an unordered query's answer is a set: a reorder agrees, a changed row does not;
  3. an ordered query: a reorder of rows tied on its keys agrees (microbench hop1p3_knows sorts by firstName alone), a
     reorder across keys does not; rows that lack the key fields (renamed after the sort) compare strictly.

    python3 tests/unit/check_result_key.py
"""
import collections
import glob
import json
import os
import sys

from kvevaluator.paths import ROOT
from kvevaluator.plan_choice.candidates import query_order, result_key


def main():
    kinds = collections.Counter()
    for f in sorted(glob.glob(os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "*", "plans", "*.json"))):
        o = query_order(json.load(open(f)))
        kinds["a set" if o is None else "strict" if o == [] else "by keys"] += 1
    a, b, c = {"n": "Ann", "x": 1}, {"n": "Ann", "x": 2}, {"n": "Bob", "x": 3}
    cases = [
        ("set: a reorder agrees", result_key([a, b, c], None) == result_key([c, b, a], None), True),
        ("set: a changed row differs", result_key([a, b], None) == result_key([a, c], None), False),
        ("keys: a reorder within a tie agrees", result_key([a, b, c], ["n"]) == result_key([b, a, c], ["n"]), True),
        ("keys: a reorder across keys differs", result_key([a, b, c], ["n"]) == result_key([c, a, b], ["n"]), False),
        ("keys absent from the rows: strict", result_key([a, b], ["y"]) == result_key([b, a], ["y"]), False),
        ("strict: a reorder differs", result_key([a, b], []) == result_key([b, a], []), False),
    ]
    bad = 0
    for what, got, want in cases:
        bad += got != want
        print(f"{'PASS' if got == want else 'FAIL'} {what}")
    n = sum(kinds.values())
    print(f"check_result_key: {n} plan files ({dict(kinds)}), {len(cases)} cases, {bad} failed")
    return 1 if bad or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
