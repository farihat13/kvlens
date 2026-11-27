#!/usr/bin/env python3
"""kvcost search-mix and front reproduce the old scripts' answers on the same captures (the mix pipeline's refactor gate).

Reads the result files `kvcost search-mix` / `kvcost search-mix --front` wrote under --results (<results>/mix-search/<workload>/<option
set>.json) and the old scripts' outputs under --baseline (their own layout, as run at the head on the same grid):
  workload_story   layout-choice/<criterion>/<i01 dir>/options.csv: fewest ops, fewest bytes, B = 1, 2, 3, 5, per option
                   set (an options.csv with grouped props solves: its "no grouping" rows)  <->  search-mix --cost-model wops | bytes | wops --byte-limit Bx
  sum_search       sum-optimum/sum_optimum_all.csv (W01-W15, I01, I02, both sets)  <->  search-mix --cost-model wops_bytes4k
  equal_bytes_front equal-bytes-front/<w dir>/equal_bytes_front.csv (I01, I02; every capped row)  <->  the front row's
                   walk, read at each cap (the fewest-wops point within it), or a staircase row's entry at that cap
                   (front --no-walk --limits bytes:<the old caps>)
  i01 complete front i01-front/complete_front.json (I01 per type)  <->  the front row's points
  chunk_size_front chunk-size-front/<w dir>/chunk_size_front.csv (per type): its "any size" line  <->  the
                   per-type-any-size front (front --option-set per-type-any-size --size ...), read at each cap; its
                   64 KB line (chunked_c64k)  <->  the per-type front; checked when the baseline holds the file
Totals compare to 1e-9 relative, sum_search's to its 3 printed decimals; assignments by their text; every row must be proven.
Prints each comparison's count; exit 1 on any difference, an unproven row, or a comparison of 0 cells.

    python3 tests/gates/check_mix_search_reproduces.py --baseline <old at head> --results <kvcost --out dir> [--parts sum_search,...]
"""
import argparse
import csv
import glob
import json
import os
import sys

from kvcost.layout_cost.mixes import assignment_label
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from kvcost.layout_search import results as R

SETS = {"per type": "per-type", "per type and direction": "per-type-and-direction"}
REL = 1e-9


def close(a, b, printed=None):
    """equal to 1e-9 relative; printed=d: the old file printed b to d decimals, so equal once rounded the same way"""
    if printed is not None:
        return round(float(a), printed) == round(float(b), printed)
    return abs(float(a) - float(b)) <= REL * max(1.0, abs(float(b)))


def label(assign):
    """an assignment as the old files print it, from a row's {"kind|name": setting}"""
    return assignment_label(R.assignment_of(assign))


def rows_of(results, workload, option_set):
    path = R.home(results, workload, option_set)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return json.load(f)["rows"]


def find(rows, **want):
    hits = [r for r in rows if all(r.get(k) == v for k, v in want.items())]
    return hits[0] if len(hits) == 1 else None


class Tally:
    def __init__(self):
        self.n, self.bad, self.unproven = {}, [], 0

    def check(self, what, ok, detail):
        self.n[what] = self.n.get(what, 0) + 1
        if not ok:
            self.bad.append(f"{what}: {detail}")


def story(a, t):
    for f in sorted(glob.glob(os.path.join(a.baseline, "layout-choice", "*", "*", "options.csv"))):
        wid = os.path.basename(os.path.dirname(f)).split("-")[0]
        rows = list(csv.DictReader(open(f)))
        nog = any("no grouping" in r["option_set"] for r in rows)    # a file with grouped props solves: those options too
        for r in rows:
            if r["kind"] == "single" or (nog and "no grouping" not in r["option_set"]):
                continue
            oset = SETS[r["option_set"].replace(", no grouping", "")]
            q = r["question"]
            want = (dict(question="fewest", model="wops", limit=None) if q == "fewest ops" else
                    dict(question="fewest", model="bytes", limit=None) if q == "fewest bytes" else
                    dict(question="within", model="wops", limit=float(q[2:])))
            row = find(rows_of(a.results, wid, oset), **want)
            what = f"workload_story {wid} {oset} {q}"
            if row is None:
                t.check("workload_story", False, f"{what}: no row")
                continue
            t.unproven += not row["proven"]
            ok = close(row["totals"]["wops"], r["total_ops"]) and close(row["totals"]["bytes"], r["total_bytes"])
            t.check("workload_story", ok, f"{what}: {row['totals']} vs {r['total_ops']}, {r['total_bytes']}")
            t.check("workload_story assignments", label(row["assignment"]) == r["assignment"],
                    f"{what}: {label(row['assignment'])} vs {r['assignment']}")


def sums(a, t):
    for r in csv.DictReader(open(os.path.join(a.baseline, "sum-optimum", "sum_optimum_all.csv"))):
        oset = SETS[r["option_set"]]
        row = find(rows_of(a.results, r["id"], oset), question="fewest", model="wops_bytes4k", limit=None)
        what = f"sum_search {r['id']} {oset}"
        if row is None:
            t.check("sum_search", False, f"{what}: no row")
            continue
        t.unproven += not row["proven"]
        tot = row["totals"]
        ok = (close(tot["wops_bytes4k"], r["sum"], printed=3) and close(tot["wops"], r["total_ops"], printed=3)
              and close(tot["bytes"], r["total_bytes"], printed=3))       # sum_search prints 3 decimals
        t.check("sum_search", ok, f"{what}: {tot} vs sum {r['sum']}, {r['total_ops']}, {r['total_bytes']}")
        t.check("sum_search assignments", label(row["assignment"]) == r["assignment"],
                f"{what}: {label(row['assignment'])} vs {r['assignment']}")


def walk_at(points, cap):
    inside = [p for p in points if p["bytes"] <= cap * (1 + 1e-9)]
    return min(inside, key=lambda p: (p["wops"], p["bytes"])) if inside else None


def fronts(a, t):
    for f in sorted(glob.glob(os.path.join(a.baseline, "equal-bytes-front", "*", "equal_bytes_front.csv"))):
        wid = os.path.basename(os.path.dirname(f)).split("-")[0]
        for r in csv.DictReader(open(f)):
            if r["option"] != "fewest_ops_within_cap":
                continue
            oset = SETS[r["option_set"]]
            what = f"equal_bytes_front {wid} {oset} cap {float(r['cap_bytes']):.6g}"
            found, p = at_cap(rows_of(a.results, wid, oset), float(r["cap_bytes"]))
            if not found:
                t.check("equal_bytes_front", False, f"{what}: no front or staircase entry at this cap")
                continue
            ok = p is not None and close(p["wops"], r["total_ops"]) and close(p["bytes"], r["total_bytes"])
            t.check("equal_bytes_front", ok, f"{what}: walk {p and (p['wops'], p['bytes'])} vs {r['total_ops']}, {r['total_bytes']}")


def at_cap(rows, cap):
    """(found, point) at a byte cap: a complete front row's walk (the fewest-wops point within cap), else the staircase
    entry at that cap, of that front row (a walk stopped by --walk-seconds) or of a staircase row (front --no-walk)"""
    row = find(rows, question="front", model=FRONT_WOPS_BYTES)
    if row is not None and row.get("walk_complete") is not False:
        return True, walk_at(row["points"], cap)
    stair = row if row is not None else find(rows, question="staircase", model=FRONT_WOPS_BYTES)
    hits = [x for x in (stair or {}).get("staircase", []) if abs(x["cap_bytes"] - cap) <= 1e-12 * cap]
    if not hits:
        return False, None
    return True, (hits[0] if hits[0]["wops"] is not None else None)


def unproven(r):
    """a front or staircase row the gate cannot use: not proven, except a walk stopped by time (each of its points
    exact) whose staircase is proven, since the gate then reads the staircase"""
    if r["question"] == "front" and r.get("walk_complete") is False and not r.get("unproven_steps"):
        return not all(x["proven"] for x in r["staircase"])
    return not r["proven"]


def chunk_sizes(a, t):
    lines = {"any size": "per-type-any-size", "chunked_c64k": "per-type"}
    for f in sorted(glob.glob(os.path.join(a.baseline, "chunk-size-front", "*", "chunk_size_front.csv"))):
        wid = os.path.basename(os.path.dirname(f)).split("-")[0]
        for r in csv.DictReader(open(f)):
            if r["unit_set"] != "per type" or r["line"] not in lines:
                continue
            what = f"chunk_size_front {wid} {r['line']} cap {float(r['cap_bytes']):.6g}"
            found, p = at_cap(rows_of(a.results, wid, lines[r["line"]]), float(r["cap_bytes"]))
            if not found:
                t.check("chunk_size_front", False, f"{what}: no front or staircase entry at this cap")
                continue
            ok = p is not None and close(p["wops"], r["total_ops"]) and close(p["bytes"], r["total_bytes"])
            t.check("chunk_size_front", ok, f"{what}: {p and (p['wops'], p['bytes'])} vs {r['total_ops']}, {r['total_bytes']}")


def iter_front_rows(a):
    for path in glob.glob(os.path.join(a.results, "mix-search", "*", "*.json")):
        for r in json.load(open(path))["rows"]:
            if r["question"] in ("front", "staircase") and r["model"] == FRONT_WOPS_BYTES:
                yield r


def complete(a, t):
    f = os.path.join(a.baseline, "i01-front", "complete_front.json")
    old = json.load(open(f))
    row = find(rows_of(a.results, "i01", "per-type"), question="front", model=FRONT_WOPS_BYTES)
    if row is None:
        t.check("complete front", False, "i01 per-type: no front row")
        return
    t.check("complete front points", len(row["points"]) == len(old), f"{len(row['points'])} points vs {len(old)}")
    for p, o in zip(row["points"], old):
        t.check("complete front", close(p["wops"], o["ops"]) and close(p["bytes"], o["bytes"]),
                f"({p['wops']}, {p['bytes']}) vs ({o['ops']}, {o['bytes']})")
        t.check("complete front assignments", p["assignment"] == o["assign"], f"{p['assignment']} vs {o['assign']}")


PARTS = {"workload_story": story, "sum_search": sums, "equal_bytes_front": fronts, "complete_front": complete,
         "chunk_size_front": chunk_sizes}
NEEDS = {"workload_story": "workload_story", "sum_search": "sum_search", "equal_bytes_front": "equal_bytes_front",
         "complete_front": "complete front", "chunk_size_front": "chunk_size_front"}   # the tally names each must fill


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", required=True, help="the old scripts' outputs (old at head)")
    ap.add_argument("--results", required=True, help="the summary/<dataset>/<scale>/ dir kvcost search-mix wrote under (its output dir's)")
    ap.add_argument("--parts", default=",".join(PARTS), help=f"the comparisons to run and require (default all: {','.join(PARTS)})")
    a = ap.parse_args()
    parts = [x for x in a.parts.split(",") if x]
    if not parts or set(parts) - set(PARTS):
        raise SystemExit(f"--parts {a.parts}: some of {','.join(PARTS)}")
    t = Tally()
    for name in parts:
        PARTS[name](a, t)
    for r in iter_front_rows(a):
        t.unproven += unproven(r)
    for line in t.bad[:30]:
        print("  DIFFER", line)
    for what, n in sorted(t.n.items()):
        print(f"  {what}: {n} compared, {sum(1 for b in t.bad if b.startswith(what + ':'))} differ")
    need = [NEEDS[x] for x in parts if x != "chunk_size_front"]
    if "chunk_size_front" in parts and glob.glob(os.path.join(a.baseline, "chunk-size-front", "*", "chunk_size_front.csv")):
        need.append("chunk_size_front")
    ok = not t.bad and t.unproven == 0 and all(t.n.get(k, 0) > 0 for k in need)
    print(f"check_mix_search_reproduces {'OK' if ok else 'FAIL'}: {sum(t.n.values())} comparisons, {len(t.bad)} differ, "
          f"{t.unproven} rows not proven")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
