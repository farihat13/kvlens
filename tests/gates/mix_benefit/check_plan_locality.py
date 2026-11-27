#!/usr/bin/env python3
"""The plan's locality under a per-direction mix (the composition proof's Lemma 3, h2): a capability of an edge type can
depend on BOTH its directions (meta advertises `adjacency_lists` only when every stored direction is Condensed or
Chunked), while a query may read one. So with an edge type's two directions in different layouts, the composed side
and the built store must still see the same candidate plans and pick the same one.

Reads picks.csv of composed_vs_measured.py (one row per (mix, read query): both picks and both candidate sets), the
per-direction spec files and the plan files. The cases: per spec, each edge type whose out and in layouts differ, each
query with a step on that edge type. Required on every case: the candidate sets are equal and the picks are the same
plan (or an exact tie, which composed_vs_measured.py has already told from a difference). At least one case must be a
query whose plans name `adjacency_lists` on an edge type with exactly one Split direction: the case h2 is about.

The control: for such a query, the built store's pick (or its candidate set) on the half-Split mix must differ from the
one on a mix that keeps both directions of that edge type in lists, on at least one (query, mix pair); if nothing
differs, the capability decides nothing here and the test does not reach it. (Both bodies of is7 and edge_probe answer
on every store, so the capability moves the pick, not the set.)

  python3 tests/gates/mix_benefit/check_plan_locality.py --picks <composed-vs-measured dir>/picks.csv
Exit 0 = every case equal, the lists case present, the control reached. Opens no store.
"""
import argparse
import csv
import glob
import json
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
SPECS = os.path.join(str(ROOT), "inputs", "specs", "mixes_per_direction")
PLANS = os.path.join(EXP, "python", "kvevaluator", "workloads", "{package}", "plans", "{query}.json")


def kind(component):
    """a component's layout by its storage kind: lists (Condensed or Chunked) or Split"""
    if isinstance(component.get("owners"), dict):
        return "chunked"
    return "condensed" if component.get("group") == "owner" else "split"


def etypes_of(node, found):
    """every edge type a plan file's steps name, over all its bodies"""
    if isinstance(node, dict):
        if isinstance(node.get("etype"), str):
            found.add(node["etype"])
        for v in node.values():
            etypes_of(v, found)
    elif isinstance(node, list):
        for v in node:
            etypes_of(v, found)
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--picks", required=True, help="picks.csv of composed_vs_measured.py")
    ap.add_argument("--mix-set", default="mixes_per_direction")
    a = ap.parse_args()
    picks = {}
    with open(a.picks) as f:
        for r in csv.DictReader(f):
            if r["mix_set"] == a.mix_set:
                picks[(r["mix"], r["family"], r["query"])] = r
    plan = {}                                                   # (family, query) -> (its edge types, does it name adjacency_lists)
    for mix, fam, q in picks:
        if (fam, q) not in plan:
            path = PLANS.format(package=fam, query=q)                  # a workload's plans: kvevaluator.workloads.<workload>
            text = open(path).read()
            plan[(fam, q)] = (etypes_of(json.loads(text), set()), "adjacency_lists" in text)
    cases, bad, lists_cases, tie_mixes = 0, [], [], ties(a.picks)
    layouts = {}                                                # mix -> {etype: (out kind, in kind)}
    for path in sorted(glob.glob(os.path.join(SPECS, "*.json"))):
        mix = os.path.basename(path)[:-5]
        spec = json.load(open(path))
        layouts[mix] = {e: (kind(d["out"]), kind(d["in"])) for e, d in spec.get("adjacency_by_etype_dir", {}).items()
                        if "out" in d and "in" in d}
        for etype, (out, in_) in sorted(layouts[mix].items()):
            if out == in_:
                continue
            for (m, fam, q), r in sorted(picks.items()):
                if m != mix or etype not in plan[(fam, q)][0]:
                    continue
                cases += 1
                half_split = (out == "split") != (in_ == "split")
                if half_split and plan[(fam, q)][1]:
                    lists_cases.append((mix, fam, q, etype))
                if r["candidate_sets_equal"] != "1":
                    bad.append(f"{mix} {fam} {q} ({etype}: out {out}, in {in_}): candidate plans differ: composed "
                               f"[{r['candidates_composed']}], built [{r['candidates_built']}]")
                elif r["composed_pick"] != r["built_pick"] and m not in tie_mixes:
                    bad.append(f"{mix} {fam} {q} ({etype}: out {out}, in {in_}): composed pick {r['composed_pick']!r}, built "
                               f"{r['built_pick']!r}")
    # the control: a lists-naming query's pick and candidate set on a half-Split mix against a mix with both directions in lists
    ctl = []
    for mix, fam, q, etype in lists_cases:
        for other, per in sorted(layouts.items()):
            if etype in per and "split" not in per[etype] and (other, fam, q) in picks:
                ctl.append((mix, other, fam, q, any(picks[(mix, fam, q)][c] != picks[(other, fam, q)][c] for c in ("candidates_built", "built_pick"))))
    for line in bad[:12]:
        print("  DIFFER", line)
    for mix, fam, q, etype in lists_cases:
        print(f"  the lists case: {mix} {fam} {q} on {etype}: {len(picks[(mix, fam, q)]['candidates_built'].split())} candidate "
              f"plans on both sides, pick {picks[(mix, fam, q)]['built_pick']!r}")
    reached = [c for c in ctl if c[4]]
    ok = not bad and cases > 0 and bool(lists_cases) and bool(reached)
    print(f"[plan locality] {len(layouts)} per-direction specs, {cases} (spec, edge type with two layouts, query) cases: "
          f"{cases - len(bad)} with equal candidate plans and the same pick; {len(lists_cases)} name adjacency_lists on an edge "
          f"type with one Split direction -> {'OK' if not bad and cases and lists_cases else 'FAIL'}; control: the pick or "
          f"the candidate set differs from a both-lists mix's on {len(reached)} of {len(ctl)} (query, mix pair)"
          + (": " + ", ".join(sorted({f"{c[3]} ({picks[(c[0], c[2], c[3])]['built_pick']} against {picks[(c[1], c[2], c[3])]['built_pick']})" for c in reached}))
             if reached else " -> NOT REACHED: the capability decides no plan here"))
    return 0 if ok else 1


def ties(picks_path):
    """the mixes whose every differently named pick composed_vs_measured.py showed to be an exact tie (mixes.csv, written
    beside picks.csv: picks_differ is 0); on any other mix a differently named pick counts as a difference here"""
    with open(os.path.join(os.path.dirname(picks_path), "mixes.csv")) as f:
        return {r["mix"] for r in csv.DictReader(f) if int(r["picks_differ"]) == 0}


if __name__ == "__main__":
    sys.exit(main())
