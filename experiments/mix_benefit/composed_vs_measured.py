#!/usr/bin/env python3
"""Composed against measured, for every BUILT mixed layout of a capture.
Composed = the mix's cost put together from the uniform captures (python/kvcost/layout_cost/compose.py's rule: each part's rows from the uniform
capture with that part's setting, the plan picked per query); measured = the built store's own capture.
Inputs (captures only; opens no store): --grid, a captures/<dataset>/<scale> dir: the uniform layouts and the built
mixes measured beside them; each --built (another captures/<dataset>/<scale> dir and the dir of its mixes' specs, e.g.
a snapshot's). A part Chunked at another chunk size than the grid's is read from that size's Chunked/Chunked capture
in --grid, its sizes taken from the mixes' specs. A mix's spec is read from its own run record and must equal its spec file (a --built's spec dir, else one of the tracked inputs/specs/mixes*). --dataset imdb and --workload pick another
store and workload (BI: --workload b01; JOB: --dataset imdb --sf full --workload j01).
Output, under output/summary/<dataset>/<scale>/mix-benefit/composed-vs-measured/ (or --out):
  composed_vs_measured_by_workload.csv   one row per (mix set, mix, workload, channel): composed, measured, the
                                         difference, the ratio, exact (sums kept as fractions: exact means equal)
  composed_vs_measured_by_row.csv        one row per (mix set, mix, family, reported or held-out params, channel): how
                                         many (query, param, plan, step, store part) rows were compared, how many equal
  mixes.csv                              the mixes found, the families each was measured on, its workloads
  picks.csv                              one row per (mix, read query): the composed pick, the built store's pick, the
                                         candidate plans usable on each side (check_plan_locality.py reads it)
  summary.md                             the doc's table, computed from the rows above
The comparing is kvcost.layout_cost.composed_vs_measured's (`compare`, the one comparer; its doc says what is compared
and the miss-key rule); this script finds the mixes and writes the files.
Exit 1 when a channel other than the miss-key bytes differs anywhere, the miss-key bytes of a cell break the miss-key
rule, a plan pick differs, or 0 rows were compared; exit 2 when the inputs cannot be compared (the message says why).
The controls, each of which must exit 1: --plant (or --plant count) adds one SEEK to one measured row; --plant pick
replaces one built store's pick by another candidate plan of other cost; --plant one-sided removes one measured row.
--sf: the capture's scale (a gate round's at sf0.1: --grid its captures/ldbc/sf0.1 dir). --held-out-rule-for-unrecorded: a built mix with no row in the pick record (never built at
the deciding scale) is compared with both sides deciding by THIS capture's held-out rule; it is said per mix, never
written to the record, and no reported number comes from it.
  python3 experiments/mix_benefit/composed_vs_measured.py --grid DIR [--sf 0.1] [--dataset imdb] [--workload W]
      [--built CAPTURES_DIR=SPEC_DIR ...] [--out DIR] [--plant [count|pick|one-sided]]
The paper's checks (the mixed layouts built for the steady latency figures) are COMPOSED_CHECK_SF3 and COMPOSED_CHECK_JOB
in paper-results (paper/derive/snapshots.py)."""
import argparse
import csv
import glob
import json
import os
import sys
from collections import defaultdict
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree, capture_in, writes_in

EXP = str(ROOT)
from kvcost.layout_cost import benchmarks, compose
from kvcost.layout_cost import composed_vs_measured as CVM
from kvcost.layout_cost.picks import PlanRule

PLANS = CVM.PLANS


def find_mixes(grid, spec_dirs, read_fam):
    """[Mix]: every layout measured in the grid's capture whose run record's spec is the spec file of a tracked
    inputs/specs/mixes* set, and every layout of a --built dir with a spec in its spec dir"""
    out = []
    tracked = sorted(glob.glob(os.path.join(EXP, "inputs", "specs", "mixes*", "")))
    for root in [grid] + sorted(spec_dirs):
        cap = capture_in(root, read_fam)
        for name in cap.stores():
            dirs = [spec_dirs[root]] if root in spec_dirs else tracked
            hits = [os.path.join(d, f"{name}.json") for d in dirs if os.path.exists(os.path.join(d, f"{name}.json"))]
            if not hits:
                continue                                  # a uniform layout or a named format
            spec = json.load(open(cap.record(name)))["spec"]
            same = [h for h in hits if json.load(open(h)) == spec]
            if not same:
                raise CVM.Refused(f"{name}: no spec file equals the run record's spec ({', '.join(hits)})")
            out.append(CVM.Mix(os.path.basename(os.path.dirname(same[0])), root, name, same[0]))
    return sorted(out, key=lambda m: (m.root != grid, m.root, m.set_name, m.name))   # the grid's mixes first, by set and name


def summary(mix_rows, by_workload, by_row):
    """the doc's table, per mix set and in total, computed from the two CSVs' rows (nothing typed)"""
    cell = defaultdict(dict)
    for r in by_workload:
        cell[(r["mix_set"], r["mix"], r["workload_id"])][r["channel"]] = r
    read_lanes = ("read_bytes_without_miss_keys", "read_miss_key_bytes", "writes_own_read_bytes_without_miss_keys",
                  "writes_own_read_miss_key_bytes")
    miss = ("read_miss_key_bytes", "writes_own_read_miss_key_bytes")
    out = ["| built mixes | mixes | (mix, workload) cells | rows compared (reported + held-out params) | rows equal in every "
           "operation count | rows equal in every byte lane outside miss keys | rows that differ in miss-key bytes | cells "
           "that differ in miss-key bytes | largest miss-key difference, bytes (share of the workload's bytes read) | plan "
           "picks: the same plan / an exact tie under another name / differ | candidate plans: sets equal, of (mix, query) | rows of "
           "the other candidate plans equal in every operation count and byte lane outside miss keys |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    sets = list(dict.fromkeys(m["mix_set"] for m in mix_rows)) + ["all"]
    for st in sets:
        ms = [m for m in mix_rows if st in ("all", m["mix_set"])]
        cs = {k: d for k, d in cell.items() if st in ("all", k[0])}
        rows = lambda ch, col, pl=PLANS[0]: sum(r[col] for r in by_row
                                                if r["channel"] == ch and r["plans"] == pl and st in ("all", r["mix_set"]))
        held = sum(r["rows_compared"] for r in by_row if r["channel"] == "S" and r["params"] == "held-out"
                   and r["plans"] == PLANS[0] and st in ("all", r["mix_set"]))
        o_n = rows("all_op_counts", "rows_compared", PLANS[1])
        o_e = min(rows("all_op_counts", "rows_equal", PLANS[1]), rows("all_bytes_without_miss_keys", "rows_equal", PLANS[1]))
        n = rows("all_op_counts", "rows_compared")
        diffs = [(sum(float(d[c]["difference"]) for c in miss), sum(float(d[c]["measured"]) for c in read_lanes)) for d in cs.values()]
        worst = max(diffs, key=lambda x: abs(x[0]) / x[1] if x[1] else 0.0)
        out.append(f"| {'**all**' if st == 'all' else '`' + st + '`'} | {len(ms)} | {len(cs)} | {n:,} ({n - held:,} + {held:,}) | "
                   f"{rows('all_op_counts', 'rows_equal'):,} of {n:,} | {rows('all_bytes_without_miss_keys', 'rows_equal'):,} of {n:,} | "
                   f"{rows('all_miss_key_bytes', 'rows_differ'):,} | {sum(1 for x in diffs if x[0])} | "
                   + (f"{worst[0]:+,.0f} of {worst[1]:,.0f} ({100 * worst[0] / worst[1]:+.7f}%)" if worst[0] else "0") + " | "
                   f"{sum(int(m['picks_same_plan']) for m in ms)} / {sum(int(m['picks_exact_tie_other_name']) for m in ms)} / "
                   f"{sum(int(m['picks_differ']) for m in ms)} | "
                   f"{sum(int(m['candidate_sets_compared']) - int(m['candidate_sets_differ']) for m in ms)} of "
                   f"{sum(int(m['candidate_sets_compared']) for m in ms)} ({sum(int(m['candidate_plans']) for m in ms):,} plans) | "
                   f"{o_e:,} of {o_n:,} |")
    tot = sum(float(d["bytes"]["measured"]) for d in cell.values())
    off = sum(abs(float(d["bytes"]["difference"])) for d in cell.values())
    ops_cells = sum(1 for d in cell.values() if d["ops"]["exact"] == 1)
    byte_cells = sum(1 for d in cell.values() if d["bytes_without_miss_keys"]["exact"] == 1)
    out += ["", f"Per (mix, workload) cell: ops equal on {ops_cells} of {len(cell)}, bytes outside miss keys equal on {byte_cells} of "
            f"{len(cell)}. Over all cells together {off:,.0f} of {tot:,.0f} bytes differ: {100 * (1 - off / tot):.5f}% of the "
            "bytes are equal, and every byte that differs is a miss key's.",
            "", "The mixes: " + "; ".join(f"`{m['mix']}` ({m['n_workloads']} workloads)" for m in mix_rows) + "."]
    return "\n".join(out) + "\n"


def main(families=None, settings=None, rule=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="3", help="the capture's scale (default 3: the committed snapshots)")
    ap.add_argument("--grid", required=True, help="a captures/<dataset>/<scale> dir: the uniform layouts and the built mixes")
    ap.add_argument("--out", default=None, help="default output/summary/<dataset>/<scale>/mix-benefit/composed-vs-measured")
    ap.add_argument("--plant", nargs="?", const="count", default=None, choices=CVM.PLANTS,
                    help="a control, must exit 1: one SEEK added to one measured row (count, the default), one built pick "
                    "replaced by another candidate (pick), one measured row removed (one-sided)")
    ap.add_argument("--held-out-rule-for-unrecorded", action="store_true", help="a built mix with no pick-record row: both "
                    "sides decide by this capture's held-out rule (said per mix; never for a reported number)")
    ap.add_argument("--dataset", default="ldbc", choices=sorted(benchmarks.FAMILIES), help="the store's dataset (imdb: JOB)")
    ap.add_argument("--workload", default=None, help="one workload id or name (b01: BI, j01: JOB); default: the "
                    "dataset's own workloads (LDBC: the W-workloads, read and write families)")
    ap.add_argument("--built", action="append", default=[], metavar="CAPTURES_DIR=SPEC_DIR",
                    help="another captures/<dataset>/<scale> dir holding built mixes and the dir of their specs; "
                    "repeatable. Compared beside the mixes found in the grid")
    compose.add_pick_by_arg(ap)
    a = ap.parse_args()
    try:
        return run(a, families, settings, rule)
    except CVM.Refused as e:
        print(f"composed against measured (sf{a.sf}): REFUSED: {e}")
        return 2


def run(a, families, settings, rule):
    spec_dirs = {}       # --built: {a captures dir holding built mixes: the dir holding their specs}
    for b in a.built:
        root, _, spec_dir = b.partition("=")
        if not (root and spec_dir and os.path.isdir(root) and os.path.isdir(spec_dir)):
            raise SystemExit(f"--built {b}: give CAPTURES_DIR=SPEC_DIR, both existing dirs")
        spec_dirs[os.path.abspath(root)] = os.path.abspath(spec_dir)
    dataset, sf = a.dataset, a.sf
    fams = dict(benchmarks.FAMILIES[dataset])
    if families is not None:
        fams = dict(families)
    elif a.workload or dataset != "ldbc":   # one workload's families (BI: bi; JOB: job), not the LDBC store's
        fams = dict(benchmarks.families_read(dataset, list(benchmarks.select(dataset, sf, a.workload).values())))
    read_fam = next(f for f, k in fams.items() if k == "read")
    grid = os.path.abspath(a.grid)
    # a wrong --grid (an old layout's path, a dir with no captures) stops here, with the path it should be
    if not glob.glob(os.path.join(capture_in(grid, read_fam).dir("counts"), "*.csv")):
        raise SystemExit(f"--grid {a.grid}: no grid captures below it (no counts files); give "
                         f"a captures dir, e.g. {Tree().place_dir('captures', dataset, sf)} (kvcost measure --dataset ldbc={sf} writes it)")
    # a grid with fewer families (the latency snapshot's: no microbench): compare those it holds, said
    held = {f: k for f, k in fams.items() if glob.glob(os.path.join(
        (capture_in(grid, f) if k == "read" else writes_in(grid, dataset)).dir("counts"), "*.csv"))}
    if held != fams:
        print(f"[families] the grid holds {', '.join(sorted(held))}; not compared: {', '.join(sorted(set(fams) - set(held)))}")
        fams = held
    out = a.out or str(Tree().summary(dataset, sf, "mix-benefit", "composed-vs-measured"))
    found = find_mixes(grid, spec_dirs, read_fam)
    if not found:
        raise CVM.Refused(f"no built mixes in {grid} (a layout of an inputs/specs/mixes* set; kvcost measure --dataset ldbc={sf} "
                          f"inputs/specs/mixes builds and measures them, its held-out set too)")
    res = CVM.compare(grid, found, sf=sf, dataset=dataset, families=tuple(fams.items()),
                      workloads=benchmarks.select(dataset, sf, a.workload), criterion=a.criterion,
                      rule=rule or PlanRule(),   # a caller's rule (the scale check: a stand-in record), else the pick record's
                      settings=settings, plant=a.plant, held_out_rule_for_unrecorded=a.held_out_rule_for_unrecorded)
    for line in res.notes:
        print(line)
    if not a.plant:                             # the control writes nothing
        os.makedirs(out, exist_ok=True)
        for fname, rows in (("mixes.csv", res.mixes), ("composed_vs_measured_by_workload.csv", res.by_workload),
                            ("composed_vs_measured_by_row.csv", res.by_row), ("picks.csv", res.picks)):
            with open(os.path.join(out, fname), "w", newline="") as f:
                wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
                wr.writeheader()
                wr.writerows(rows)
        with open(os.path.join(out, "summary.md"), "w") as f:
            f.write(summary(res.mixes, res.by_workload, res.by_row))
    print("\n" + res.counts_line(sf))
    print(res.miss_line())
    if res.grouped_skipped:
        print(f"{len(res.grouped_skipped)} built mixes with grouped props not compared (the search does not offer that option): "
              + " ".join(res.grouped_skipped))
    for b in res.findings[:20]:
        print("  DIFFERS:", b)
    print(f"composed against measured (sf{sf}): {'PASS' if res.ok else f'{len(res.findings)} findings'}"
          + ("" if not a.plant else f"  (control run --plant {a.plant}: nothing written)"))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
