#!/usr/bin/env python3
"""Tier 1's most common values: tier1.derive keeps each indexed property's first k values of the
full file's top_values, k the contract's x-tier1-k (10), with the holders unchanged; the order (by count, then value)
is kept, and a file with no top_values gets none. On the synthetic LDBC statistics of check_predict_stats_schema.py (no
store, no binary, a second):

    python3 tests/unit/check_predict_tier1_top_values.py
"""
import json
import sys

import check_predict_stats_schema as S  # (its synthetic statistics)
from kvevaluator.predict import tier1

fails, cases = [], 0


def case(what, ok):
    global cases
    cases += 1
    print(f"{'PASS' if ok else 'FAIL'} {what}")
    if not ok:
        fails.append(what)


def main():
    k = json.load(open(tier1.CONTRACT))["properties"]["top_values"]["x-tier1-k"]
    case(f"the contract's x-tier1-k is 10 (got {k})", k == 10)
    full = S.complete_stats("h")
    full.setdefault("owners", [])   # tier1.derive reads it (the synthetic file has no lists: its degree rows are not the case)
    listed = [[f"name{i:02d}", 100 - i] for i in range(15)]   # 15 values, by count
    full["top_values"]["Person|firstName"] = {"k": 1000, "holders": 2000, "values": listed}
    t1 = tier1.derive(json.loads(json.dumps(full)))["top_values"]
    got = t1["Person|firstName"]
    case("tier 1 keeps the first 10 values, in order", got["values"] == listed[:10])
    case("tier 1 keeps the holders", got["holders"] == 2000)
    case("tier 1 writes k = 10", got["k"] == 10)
    case("every indexed property of the full file is in tier 1", sorted(t1) == sorted(full["top_values"]))
    small = t1["Place|name"]
    case("a list shorter than 10 is kept whole", small["values"] == full["top_values"]["Place|name"]["values"])
    del full["top_values"]
    case("a full file with no top_values gives tier 1 none", "top_values" not in tier1.derive(full))
    ok = not fails
    print(f"predict tier-1 top values {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
