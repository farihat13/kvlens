#!/usr/bin/env python3
"""The byte-budget pick per workload: the fewest total ops among the layouts whose total bytes are within B x Sp-Co's,
the reference (settings.B_UNIT; criteria.budget_pick holds the rule). Analysis only: reads each workload's options.csv
(workload_story.py) and prices nothing.

Candidates: the 9 single layouts and, per option set, its exact solves in options.csv (per type; per type and
direction: priced, not built). The budget solve of an option set is the
fewest-ops mix of that set within the budget,
so budget_pick over these rows is the exact pick. A per type and direction pick replaces the buildable one only when it is at
least 1% cheaper in the currency that decided (ops; bytes when nothing fits) -- workload_story.py's BEAT, so a
near-tie reports a layout we can build. Currencies
as workload_story.py: total ops = read ops + the writes' own reads + PUTs + DELs (G:S:N:P:D = 1:1.1:0.01:1:1);
total bytes = bytes read + the bytes the writes read first + bytes written. No layout within the budget: fits = no,
and the pick is the lowest-byte layout.

Writes budget_picks.csv to the working dir (benchmarks.out_root; also when --root reads another analysis dir)
and prints one table per workload.

    python3 experiments/layout_choice/budget.py --sf 3 [--budgets 1,2,3,5] [--root <layout-choice dir>]
"""
import argparse
import csv
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.compose import MEASURES
from kvevaluator.plan_choice.optimizer.criteria import budget_pick
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import display_title
from kvcost.layout_search.search import BUDGETS, options_b_unit
from kvcost.layout_cost.settings import name_of
from workload_story import BEAT

KIND_ORDER = ("single", "per type", "per type and direction")   # tie order: a named layout before a mix
# an options.csv with grouped props solves: the solves without those options, the grid's three settings
KIND_ORDER_NO_GROUPING = ("single", "per type, no grouping", "per type and direction, no grouping")


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def candidates_of(d):
    """{key: row} in tie order: every single layout, then each option set's solves (options.csv), each mixed
    assignment once. Each row: kind, label, assignment, built, ops, bytes, and the measures. An options.csv with
    "no grouping" solves (a file with grouped props solves): those, the solves without the grouped props options"""
    opts = read_csv(os.path.join(d, "options.csv"))
    kinds = KIND_ORDER
    if any(o["kind"].endswith(", no grouping") for o in opts):
        kinds = KIND_ORDER_NO_GROUPING
    out, seen = {}, set()
    for kind in kinds:
        for o in opts:
            # every single layout stays (two may share an assignment on the parts a workload touches); a mix is kept
            # once, and not when it IS a single layout already listed
            if o["kind"] != kind or (kind != "single" and o["assignment"] in seen):
                continue
            seen.add(o["assignment"])
            label = o["label"] if kind == "single" else ("per type" if kind.startswith("per type,") or kind == "per type"
                                                        else ("per type and direction (built: " + o["built"] + ")" if o.get("built")
                                                              else "per type and direction (priced, not built)"))
            k = kind.replace(", no grouping", "")     # the option set, whichever candidate list it came from
            out[o["option"] if k == "single" else f"{k}: {o['assignment']}"] = {
                "kind": k, "label": label, **_values(o, o["assignment"], o.get("built", ""))}
    return out


def _values(r, assignment, built):
    return {"assignment": assignment, "built": built, "ops": float(r["total_ops"]),
            "bytes": float(r["total_bytes"]), **{m: r.get(m, "") for m in MEASURES}}


def pick(cands, B, unit):
    """(key, fits, tied): budget_pick over the buildable candidates (single + per type), replaced by the pick over
    all of them only when that one is a per type and direction mix at least 1% cheaper (BEAT) in the deciding currency.
    unit: the cell whose bytes B multiplies (options.csv's own: search.options_b_unit)"""
    buildable = {k: v for k, v in cands.items() if v["kind"] != "per type and direction"}
    b = budget_pick(buildable, unit, B)
    a = budget_pick(cands, unit, B)
    if a[0] == b[0] or (a[1] and not b[1]):          # same pick, or only a per type and direction mix fits
        return a
    cur = "ops" if b[1] else "bytes"
    return a if cands[a[0]][cur] <= BEAT * cands[b[0]][cur] else b


def parts(label, per_dir):
    """{part: layout} of an assignment label ('a=x; b=y'); per_dir spells 'E lists=v' as its two directions"""
    out = {}
    for k, v in (s.split("=", 1) for s in label.split("; ")):
        if per_dir and k.endswith(" lists") and not k.endswith("-lists"):
            e = k[:-len(" lists")]
            out[f"{e} in-lists"] = out[f"{e} out-lists"] = v
        else:
            out[k] = v
    return out


def changed(prev, cur):
    """the parts whose layout differs between two assignment labels, compared per direction when either is"""
    per_dir = "-lists=" in prev or "-lists=" in cur
    p, c = parts(prev, per_dir), parts(cur, per_dir)
    return "; ".join(f"{k}={v}" for k, v in c.items() if p.get(k) != v)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--root", default=None, help="the layout-choice output to read (default: the working dir)")
    ap.add_argument("--budgets", default=",".join(f"{b:g}" for b in BUDGETS),
                    help="B values: bytes <= B x B_UNIT's (only B values exact_search solved)")
    benchmarks.add_workload_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    a = ap.parse_args()
    budgets = [float(b) for b in a.budgets.split(",")]
    out_root = benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set)
    root = a.root or out_root
    rows = []
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        d = os.path.join(root, os.path.basename(benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=a.criterion, param_set=a.param_set)))
        cands = candidates_of(d)
        questions = {o["question"] for o in read_csv(os.path.join(d, "options.csv"))}
        unit = options_b_unit(questions)         # B's unit: Sp-Co, or Co-Sp in an options.csv in Co-Sp units
        if {f"B={b:g}" for b in budgets} - questions:   # a pick is exact only where options.csv holds that B's solve
            raise SystemExit(f"{d}: --budgets {sorted({f'B={b:g}' for b in budgets} - questions)} not solved in its options.csv")
        if unit not in cands:
            sys.exit(f"{d}: no layout of B's unit ({unit}) among the options")
        ref = cands[unit]
        print(f"\n{display_title(w)}   (B's unit, {name_of(unit)}: {ref['ops']:,.0f} ops, {ref['bytes'] / 1e6:,.1f} MB; "
              f"{len(cands):,} candidates)")
        prev = None
        for B in budgets:
            key, fits, tied = pick(cands, B, unit)
            p = cands[key]
            rows.append({"workload": name, "id": w["id"], "budget_B": B,
                         "ref_option": unit, "ref_total_ops": ref["ops"], "ref_total_bytes": ref["bytes"],
                         "fits": "yes" if fits else "no", "pick_kind": p["kind"], "pick_label": p["label"],
                         "built": p["built"], "pick_assignment": p["assignment"], "n_tied": len(tied),
                         "total_ops": p["ops"], "total_bytes": p["bytes"],
                         "ops_vs_ref": round(p["ops"] / ref["ops"], 4), "bytes_vs_ref": round(p["bytes"] / ref["bytes"], 4),
                         **{m: p[m] for m in MEASURES}})
            what = ("NO layout fits; the lowest-byte one:" if not fits else "") + \
                   (" (same)" if prev == p["assignment"] else
                    f" {p['label']}: " + (p["assignment"] if prev is None else "changes " + changed(prev, p["assignment"])))
            print(f"  B={B:<4g} {p['ops'] / ref['ops']:5.2f}x ops  {p['bytes'] / ref['bytes']:5.2f}x bytes  {what}")
            prev = p["assignment"]
    if not rows:
        sys.exit("no workload priced: wrong --root or --dataset?")
    if a.workload:   # a slice: its own file, named by its workloads; budget_picks.csv covers every workload
        os.makedirs(out_root, exist_ok=True)
        path = os.path.join(out_root, f"budget_picks-{'-'.join(sorted(w['id'] for w in benchmarks.select(a.dataset, a.sf, a.workload).values()))}.csv")
        with open(path, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        print(f"\n--workload: {len(rows)} picks -> {path} (budget_picks.csv, which covers every workload, is not written)")
        return
    os.makedirs(out_root, exist_ok=True)
    path = os.path.join(out_root, "budget_picks.csv")
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    print(f"\n{len(rows)} picks ({len(rows) // len(budgets)} workloads x {len(budgets)} budgets) -> {path}")


if __name__ == "__main__":
    main()
