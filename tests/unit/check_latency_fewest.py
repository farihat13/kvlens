#!/usr/bin/env python3
"""kvcost measure-latency's timed mix (latency._fewest), on planted rows files (no store, no search): one fewest row is
taken; a file holding a proven row and a two-step row (search-mix --search proven, then --search two-step) gives the
proven row, a two-step row alone gives itself; two rows of one search are refused; a file with no row of the model gives
None.

Run:  python3 tests/unit/check_latency_fewest.py
"""
import json
import os
import sys
import tempfile

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def row(search, nodes, proven=True, model="wops_bytes4k"):
    return {"question": "fewest", "model": model, "limit": None, "search": search, "proven": proven, "nodes": nodes}


def main():
    from kvcost.cli import latency as LAT
    path = os.path.join(tempfile.mkdtemp(prefix="latency-fewest-"), "rows.json")

    def fewest(*rows, model="wops_bytes4k"):
        json.dump({"rows": list(rows)}, open(path, "w"))
        try:
            got = LAT._fewest(path, model)
        except SystemExit as e:
            return "refused", str(e)
        return got

    def taken(got, nodes, said):
        return isinstance(got, tuple) and isinstance(got[0], dict) and got[0]["nodes"] == nodes and said in got[1]

    got = fewest(row("proven", 7))
    case("one proven row: taken", taken(got, 7, "proven"), got)
    got = fewest(row("proven", 7), row("two-step", 1, proven=False))
    case("a proven and a two-step row: the proven row", taken(got, 7, "proven search"), got)
    got = fewest(row("two-step", 1, proven=False))
    case("a two-step row alone: taken, said NOT proven", taken(got, 1, "NOT proven"), got)
    got = fewest(row("proven", 7), row("proven", 9))
    case("two proven rows: refused", got[0] == "refused", got)
    got = fewest(row("proven", 7, model="bill_exact"))
    case("no row of the model: None", got is None, got)
    print(f"check_latency_fewest {'OK' if all(results) else 'FAIL'}: {sum(results)}/{len(results)} cases")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
