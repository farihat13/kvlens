#!/usr/bin/env python3
"""The chunk-size front's 64 KB line equals the complete 3-setting front's fewest-wops mix within each byte limit (ops, bytes, setting per part).

The control of chunk_size_front.py at the grid's own chunk size: the same grid, priced two ways (a branch-and-bound
solve per limit, and every mix of the complete front from experiments/mix_benefit/run-mix-benefit-w16-front.sh).
Opens no store; prints how many limits it compared; --plant adds 1 to one limit's ops (the check must fail).

    python3 experiments/layout_choice/check_front_against_complete.py --front <root>/i01-rw-ldbc-mix/chunk_size_front.csv \
        --complete <i01-front out>/complete_front.json [--plant]
"""
import argparse
import csv
import json
import sys


def norm(assignment):
    """an assignment with the grid's own chunk size named as the plain setting, parts sorted"""
    return "; ".join(sorted(x.strip().replace("chunked_c64k", "chunked") for x in assignment.split(";")))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--front", required=True)
    ap.add_argument("--complete", required=True)
    ap.add_argument("--plant", action="store_true", help="the control: one limit's ops + 1")
    a = ap.parse_args()
    front = json.load(open(a.complete))
    rows = [r for r in csv.DictReader(open(a.front)) if r["line"] == "chunked_c64k" and r["unit_set"] == "per type"]
    if a.plant and rows:
        rows[0] = {**rows[0], "total_ops": str(float(rows[0]["total_ops"]) + 1)}
    n = bad = 0
    for r in rows:
        cap = float(r["cap_bytes"])
        fit = [p for p in front if p["bytes"] <= cap * (1 + 1e-12)]
        if not fit:                          # below the front's fewest bytes: nothing to compare
            continue
        best = min(fit, key=lambda p: (p["ops"], p["bytes"]))
        n += 1
        same = (abs(best["ops"] - float(r["total_ops"])) <= 1e-9 * best["ops"]
                and abs(best["bytes"] - float(r["total_bytes"])) <= 1e-9 * best["bytes"]
                and norm(best["assignment"]) == norm(r["assignment"]))
        if not same:
            bad += 1
            print(f"  DIFF limit {cap:,.0f}: complete front {best['ops']} ops / {best['bytes']} bytes, 64 KB line "
                  f"{r['total_ops']} / {r['total_bytes']}")
    print(f"64 KB line vs the complete front: {len(rows)} limits, {n} compared, {bad} differ")
    ok = n and not bad
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
