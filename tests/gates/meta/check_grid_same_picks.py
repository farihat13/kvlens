#!/usr/bin/env python3
"""Two grid captures of the same cells give the same runs and the same picks, matched by body (owner: experiments/meta).

For a change that must move no number but renames, drops or merges candidates (one candidate per body):
capture the grid before and after it (kvcost measure --fresh, the same scale and params), then
  1. runs: every (read family, cell, query, param, body_sha) is in both, with every count, byte lane, rows and result
     hash equal (a body run twice in one capture must agree with itself too);
  2. valid bodies: per (family, cell, query) the bodies the rule may pick (picks.choose_cell's costs + measure-only),
     as body shas, are the same;
  3. picks: per (family, cell, query) the picked body's sha is the same (choose_cell on each capture: the record's
     pick at a smaller scale, the held-out rule or the in-sample rule as the family is priced).
Names never decide: a name is turned into the body it ran by its rows' body_sha. Picks are computed with the code of
this tree; --dump-picks writes them so a run in another tree (the code before the change) can be compared with
--picks-before. --plant makes one run's G one higher in the after capture (read in memory): the gate must fail.

  python3 tests/gates/meta/check_grid_same_picks.py --before DIR --after DIR [--picks-before TSV] [--plant]
  python3 tests/gates/meta/check_grid_same_picks.py --dump-picks OUT --after DIR
DIR is LDBC's captures dir at a scale (<output>/captures/ldbc/sf<SF>: interactive/, microbench/ and bi/ below, each
with reported/; interactive also held-out/). Exit 0 = all hold; each part prints how many it compared, and 0 compared fails.
"""
import argparse
import sys
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import HELD_OUT, capture_in
from kvevaluator.plan_choice import candidates as C

# (label, workload, params): the read captures below a captures dir
# (family label, workload, param set; None = the workload's default)
FAMILIES = (("interactive", "interactive", None), ("interactive-heldout", "interactive", HELD_OUT), ("microbench", "microbench", None),
            ("bi", "bi", None))


def cell_files(cap):
    """(cell, its counts file) for every grid cell the capture holds"""
    return [(c, cap.path_of("counts", c)) for c in cap.stores() if c.startswith("props_")]
from kvevaluator.capture.step_record import READ_BYTE_LANES, READ_OPS  # (the lanes, one definition)
COLS = ("ok", *READ_OPS, *READ_BYTE_LANES, "read_bytes", "rows", "result_hash")


def runs(root, plant=False):
    """{(family, cell, query, param, body_sha or the name when a row has none): the row's COLS}; a body run twice in one
    capture with different counts is a problem"""
    out, probs = {}, []
    for fam, wl, ps in FAMILIES:
        for cell, p in cell_files(capture_in(root, wl, ps)):
            for r in capture_layout.reader(open(p)):
                if plant and not out and r["ok"] == "1":
                    r = {**r, "G": str(int(r["G"]) + 1)}
                # a body rewritten count-equal since a capture: keyed as its successor (candidates.successor_sha)
                k = (fam, cell, r["query"], r["param_idx"],
                     C.successor_sha(wl, r["query"], r.get("body_sha")) or "name:" + r["body"])
                v = tuple(r.get(c, "") for c in COLS)
                if out.setdefault(k, v) != v:
                    probs.append(f"{root}: {k} ran twice with different counts")
    return out, probs


def picks(root):
    """{(family, cell, query): (picked body sha, the valid bodies' shas)} by choose_cell, names turned into the bodies
    their rows ran"""
    from kvcost.layout_cost import picks as PK
    out = {}
    for fam, wl, ps in FAMILIES:
        if ps == HELD_OUT:                    # the held-out rows are read by choose_cell with the reported ones
            continue
        cap = capture_in(root, wl, ps)
        d = cap.path
        for cell, p in cell_files(cap):
            sha = {}
            for r in capture_layout.reader(open(p)):
                if r.get("body_sha"):
                    sha[(r["query"], r["body"])] = C.successor_sha(wl, r["query"], r["body_sha"])
            for q, c in PK.choose_cell(d, cell, rule=PK.PlanRule(), drift="warn").items():
                of = lambda v: sha.get((q, v), "name:" + str(v))
                valid = ",".join(sorted({of(v) for v in set(c["costs"]) | set(c.get("measure_only", {}))}))
                out[(fam, cell, q)] = (of(c["body"]) if c["body"] else "NONE", valid)
    return out


def compare(name, a, b):
    """the problems between two {key: value} maps, and how many keys were compared"""
    probs = [f"{name}: only before: {k}" for k in sorted(set(a) - set(b))]
    probs += [f"{name}: only after: {k}" for k in sorted(set(b) - set(a))]
    probs += [f"{name}: {k}: before {a[k]} after {b[k]}" for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    return probs, len(set(a) & set(b))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--before")
    ap.add_argument("--after", required=True)
    ap.add_argument("--picks-before", help="picks of --before computed in another tree (--dump-picks there)")
    ap.add_argument("--dump-picks", help="write the picks of --after to this TSV and stop")
    ap.add_argument("--plant", action="store_true", help="the control: one run's G + 1 in the after capture")
    a = ap.parse_args()
    if a.dump_picks:
        pk = picks(a.after)
        with open(a.dump_picks, "w") as f:
            f.write("family\tcell\tquery\tpicked_body_sha\tvalid_body_shas\n")
            f.writelines("\t".join([*k, *v]) + "\n" for k, v in sorted(pk.items()))
        print(f"{len(pk)} picks written to {a.dump_picks}")
        return 0 if pk else 1
    bad = []
    ra, pa = runs(a.before)
    rb, pb = runs(a.after, plant=a.plant)
    p, n_runs = compare("runs", ra, rb)
    bad += pa + pb + p
    print(f"1 runs: {n_runs} (family, cell, query, param, body) compared, {len(pa + pb + p)} problems")
    ka, kb = picks(a.before), picks(a.after)
    p_valid, n = compare("valid bodies", {k: v[1] for k, v in ka.items()}, {k: v[1] for k, v in kb.items()})
    p_pick, _ = compare("picks", {k: v[0] for k, v in ka.items()}, {k: v[0] for k, v in kb.items()})
    bad += p_valid + p_pick
    print(f"2 valid bodies: {n} (family, cell, query) compared, {len(p_valid)} problems")
    print(f"3 picks (this tree's code on both captures): {n} compared, {len(p_pick)} problems")
    n_x = 1
    if a.picks_before:
        rows = list(capture_layout.reader(open(a.picks_before), delimiter="\t"))
        ko = {(r["family"], r["cell"], r["query"]): (r["picked_body_sha"], r["valid_body_shas"]) for r in rows}
        p_x, n_x = compare("picks across trees", ko, kb)
        bad += p_x
        print(f"3b picks, the before tree's code on --before vs this tree's on --after: {n_x} compared, {len(p_x)} problems")
    for line in bad[:40]:
        print("  " + line)
    if not n_runs or not n or not n_x:
        print("FAIL: a part compared 0 entries")
        return 1
    print("SAME PICKS: OK" if not bad else f"FAIL: {len(bad)} problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
