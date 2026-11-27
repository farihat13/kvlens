#!/usr/bin/env python3
"""The byte-limit picks of one workload as BUILDABLE meta specs, generated from solved mixes, never typed.

For each byte limit B (bytes <= B x Sp-Co's, settings.B_UNIT; `budget.py` / `criteria.budget_pick` hold the rule) it
takes the pick over the candidates and writes the pick's
assignment as a spec `meta` can build (`vertex_props_by_label`, `adjacency_by_etype`): the components come from the
grid's own cell specs (inputs/specs/grid/*.json, the inverse of mixes.grid_settings), the base is the cell the
pick names most often, the rest are overrides; a per type and direction pick writes the direction overrides in the
format's own key ("adjacency_by_etype_dir", cpp/kvevaluator/src/formats/meta/config.h; python/kvcost/layout_cost/mixes.py spec_of) and the record
keeps the best pick WITHOUT direction beside it (what direction buys). --control flips one direction of the first per-direction spec
written and shows the round trip refusing it. A limit options.csv did not solve is refused: solve it with `kvcost search-mix --byte-limit named` and pass --results. B's unit is the
options.csv's own (search.options_b_unit), and the picks file records it as `reference`.

The candidates (nothing is solved here):
  --results DIR --grid G   the mix-search result rows (kvcost search-mix --byte-limit named, per option set, run on
                           G's tree, written into an output tree: DIR its summary/<dataset>/<scale>/): each option set's
                           named point B=<b>, the exact fewest weighted ops within
                           B; and the nine uniform layouts, priced on G (the reference, the best single within B)
  --root DIR               a layout-choice output's options.csv

Round trip = the test: mixes.assignment() on every written spec must give back the pick's assignment.
Two limits whose picks are one assignment share one spec (recorded in the picks file). Beside the spec dir, <out>-picks.json
(a sibling file, never inside the dir: every *.json in a spec dir is a cell to the grid driver) keeps per B: the spec stem, the pick (kind, ops, bytes, vs the reference), the unbuildable pick it stands in for (if any),
the best single layout within B, the nearest single that misses B (within --near of it), and the reference cell.

    python3 experiments/layout_choice/pick_specs.py --sf 3 --workload i01 --results <dir> --grid <grid> --out inputs/specs/mixes_w16_<date>
    python3 experiments/layout_choice/pick_specs.py --sf 3 --workload i01 \
        --root <layout-choice dir>/wops_bytes4k --out inputs/specs/mixes_w16
"""
import argparse
import csv
import glob
import json
import os
import sys
from kvevaluator.output_tree import same_place
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from budget import candidates_of, pick as rule_pick, read_csv
from kvcost.layout_search.search import options_b_unit
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from kvcost.layout_search.results import ResultFile  # (one reader: refuses a file mixing frequencies)
from kvcost.layout_cost.mixes import LAYOUTS, assignment, components, grid_settings, parse_assignment, spec_of
from kvevaluator.plan_choice.optimizer.criteria import budget_pick
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import display_title
from kvcost.layout_search import results as R

SINGLE = "single"
PER_DIR = "per type and direction"


def is_buildable(assign):
    """every unit a props or adjacency unit (per edge type, or per edge type and direction) on one of the grid's layouts"""
    return all(len(u) in (2, 3) for u in assign) and all(l in LAYOUTS for l in assign.values())


OPTION_SET_FILES = {"per type": "per-type", PER_DIR: "per-type-and-direction"}    # kind -> its rows' option set (results.home)


def results_candidates(results, grid, w, dataset, budgets):
    """{key: row} in budget.candidates_of's form and tie order -- every single layout (priced on `grid`), then per type,
    then per type and direction -- from the mix-search result rows: each option set's front row (--limits named, run on
    `grid`), its named points B=<b> for `budgets`, each mixed assignment once. Returns (candidates, the result files)"""
    from kvcost.layout_cost import picks as P_, compose
    from kvcost.layout_cost.cost_models import currencies
    from kvcost.layout_cost.mixes import assignment_label
    from kvcost.layout_search.search import uniform
    data = compose.load(grid, False, dataset, composer=compose.Composer(criterion=None, plan_rule=P_.PlanRule(),
                                                                    families=benchmarks.families(dataset)))
    units = compose.workload_units(w, data)
    out, seen, files = {}, set(), []
    for cell in compose.CELLS:                                      # the singles, in the grid's order
        assign = uniform(units, *cell[len("props_"):].split("_adj_"))
        c = currencies(compose.cost_mix(w, data, assign))
        out[cell] = {"kind": SINGLE, "label": cell, "assignment": assignment_label(assign), "built": cell,
                     "ops": c["ops"], "bytes": c["bytes"], "source": "priced"}
    for kind, set_file in OPTION_SET_FILES.items():
        path = R.home(results, w["id"], set_file)
        rows = [r for r in ResultFile(path).read() if r["question"] == "staircase" and r["model"] == FRONT_WOPS_BYTES and r["limit"] == "named"
                and same_place(r["capture"], grid) and r["weighting"] == "sum" and r["max_nodes"] is None]
        if len(rows) != 1 or not rows[0]["proven"]:
            raise SystemExit(f"{path}: {len(rows)} proven front rows with --limits named on {grid}; want 1")
        files.append(os.path.relpath(path, EXP) if path.startswith(EXP) else path)
        for B in budgets:
            p = rows[0]["named"].get(f"B={B:g}")
            if p is None:
                raise SystemExit(f"{path}: no mix within B={B:g}")
            label = assignment_label(R.assignment_of(p["assignment"]))
            if label in seen:
                continue
            seen.add(label)
            out[f"{kind}: {label}"] = {"kind": kind, "label": kind, "assignment": label, "built": "",
                                       "ops": p["wops"], "bytes": p["bytes"], "source": f"{set_file}.json B={B:g}"}
    return out, files


def stem_of(workload_id, B):
    """the spec's name: the workload, B and B's unit (w16_b2_spco = within 2 x Sp-Co's bytes). The unit is in the name
    because the specs picked in Co-Sp's bytes hold the names without it (w16_b2): a new pick under
    an old name would collide with that mix's cell in the pick record"""
    from kvcost.layout_cost.settings import B_UNIT
    code = {"split": "sp", "condensed": "co", "chunked": "ch"}
    props, adj = B_UNIT[len("props_"):].split("_adj_")
    return f"{workload_id}_b{B:g}".replace(".", "") + f"_{code[props]}{code[adj]}"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--workload", required=True, help="the workload id, e.g. i01")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--results", help="the mix-search results dir: the summary/<dataset>/<scale>/ of the output tree kvcost "
                     "search-mix --byte-limit named wrote into, both option sets, on --grid's tree")
    src.add_argument("--root", help="a layout-choice output (a snapshot's <criterion> dir) from before the result files")
    ap.add_argument("--grid", help="with --results: the grid capture the rows were solved on (the singles are priced on it)")
    ap.add_argument("--out", required=True, help="the spec dir to write (one spec per distinct pick); the record goes to <out>-picks.json")
    ap.add_argument("--budgets", default="1,2,3,5")
    ap.add_argument("--near", type=float, default=0.01, help="report a single layout that misses B by at most this fraction")
    ap.add_argument("--check-picks", default=None, help="a budget_picks.csv whose rows (this workload; a file "
                    "with grouped props solves: its grouped_options = no rows) the rule picks must equal, assignment for "
                    "assignment (exit 1 otherwise)")
    ap.add_argument("--control", action="store_true", help="flip one direction of the first per-direction spec written: the "
                    "round trip must refuse it (printed; exit 1 if it does not)")
    a = ap.parse_args()
    budgets = [float(b) for b in a.budgets.split(",")]
    # one select call: each call builds the workloads anew from their files, so two calls never share an object
    name, w = next((n, w) for n, w in benchmarks.select(a.dataset, a.sf, None).items() if w["id"] == a.workload)
    if a.results:
        if not a.grid:
            raise SystemExit("--results needs --grid, the capture its rows were solved on")
        d = None
        cands, source = results_candidates(a.results, a.grid, w, a.dataset, budgets)
        from kvcost.layout_cost.settings import B_UNIT as REF
    else:
        d = os.path.join(a.root, os.path.basename(benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=None)))
        cands = candidates_of(d)
        questions = {o["question"] for o in read_csv(os.path.join(d, "options.csv"))}
        REF = options_b_unit(questions)              # B's unit: Sp-Co, or Co-Sp in an options.csv in Co-Sp units
        source = None
    ref = cands[REF]
    settings, comps = grid_settings(), components()
    os.makedirs(a.out, exist_ok=True)
    # a spec dir holds SPECS only: the grid driver takes every *.json in it for a cell (a record file written
    # here was ingested as a store and refused by meta). Anything that is not a spec stops the run.
    for other in sorted(glob.glob(os.path.join(a.out, "*.json"))):
        j = json.load(open(other))
        if not ({"vertex_props", "adjacency", "index"} <= set(j)):
            raise SystemExit(f"{other}: not a meta spec; a spec dir holds specs only -- remove it first")
    # a spec or picks file is never overwritten: the built mixes (w16_b1 ...) were picked at B x Co-Sp's
    # bytes and a pick at B x Sp-Co's under the same name would replace a built store's spec. Write to a new --out.
    if os.path.exists(a.out.rstrip("/") + "-picks.json"):
        raise SystemExit(f"{a.out.rstrip('/')}-picks.json exists: pick_specs never overwrites a picks record -- give a new --out")
    picks, written, trips = {}, {}, []   # B -> record; assignment label -> stem; (stem, round trip equal?)
    print(f"{display_title(w)}  reference {REF}: {ref['ops']:,.0f} ops, {ref['bytes'] / 1e9:,.1f} GB  "
          f"({'results ' + a.results + ' on ' + a.grid if a.results else 'root ' + a.root})")
    print(f"{'B':>4} | {'pick':34s} | {'ops':>14} {'x ref':>7} | {'GB':>9} {'x ref':>6} | best single within B | spec")
    for B in budgets:
        if d is not None and f"B={B:g}" not in questions:
            raise SystemExit(f"{d}/options.csv has no solve at B={B:g} (an options.csv in Co-Sp units): solve it with "
                             f"kvcost search-mix --byte-limit named and pass --results")
        pool = dict(cands)
        key, fits, _ = rule_pick(pool, B, REF)          # budget.py's rule: per direction only when >= 1% cheaper
        full = pool[key]
        no_dir = {k: v for k, v in pool.items() if v["kind"] != PER_DIR}     # the best pick without direction
        nkey, nfits, _ = budget_pick(no_dir, REF, B)
        chosen, bfits = full, fits
        singles = {k: v for k, v in pool.items() if v["kind"] == SINGLE}
        skey, sfits, _ = budget_pick(singles, REF, B)
        limit = B * ref["bytes"]
        near = min((v for v in singles.values() if limit < v["bytes"] <= limit * (1 + a.near)),
                   key=lambda v: v["ops"], default=None)
        assign = parse_assignment(chosen["assignment"])
        if not is_buildable(assign):
            raise SystemExit(f"B={B:g}: the buildable pick is not buildable?! {chosen['assignment']}")
        if chosen["assignment"] in written:
            stem = written[chosen["assignment"]]
        else:
            stem = stem_of(a.workload, B)
            spec, base = spec_of(assign, comps)
            back = assignment(spec, sorted(assign), settings, stem)     # the round trip
            trips.append((stem, back == assign))
            if back != assign:
                raise SystemExit(f"{stem}: round trip failed:\n  wrote {assign}\n  read  {back}")
            if a.control and "adjacency_by_etype_dir" in spec and not any(t[0].endswith("(control)") for t in trips):
                e = sorted(spec["adjacency_by_etype_dir"])[0]
                dr = sorted(spec["adjacency_by_etype_dir"][e])[0]
                flipped = json.loads(json.dumps(spec))
                other = next(l for l in LAYOUTS if comps["adj"][l] != flipped["adjacency_by_etype_dir"][e][dr])
                flipped["adjacency_by_etype_dir"][e][dr] = comps["adj"][other]
                refused = assignment(flipped, sorted(assign), settings, f"{stem} (control)") != assign
                trips.append((f"{stem} (control)", refused))
                print(f"control: {stem} with {e} {dr}-lists flipped to {other}: round trip "
                      f"{'refuses it (as it must)' if refused else 'ACCEPTS IT: the check is blind'}")
                if not refused:
                    raise SystemExit(1)
            if os.path.exists(os.path.join(a.out, f"{stem}.json")):
                raise SystemExit(f"{a.out}/{stem}.json exists: pick_specs never overwrites a spec (a built store may be "
                                 f"named by it) -- give a new --out")
            with open(os.path.join(a.out, f"{stem}.json"), "w") as f:
                json.dump(spec, f, indent=2)
                f.write("\n")
            written[chosen["assignment"]] = stem
        rec = {"B": B, "fits": bfits, "spec": f"{stem}.json", "shares_spec_with": None,
               "pick": {"kind": chosen["kind"], "assignment": chosen["assignment"], "ops": chosen["ops"],
                        "bytes": chosen["bytes"], "ops_vs_ref": chosen["ops"] / ref["ops"],
                        "bytes_vs_ref": chosen["bytes"] / ref["bytes"], "source": chosen.get("source", "options.csv")},
               "best_without_direction": None if chosen["kind"] != PER_DIR else {
                   "kind": no_dir[nkey]["kind"], "assignment": no_dir[nkey]["assignment"], "ops": no_dir[nkey]["ops"],
                   "bytes": no_dir[nkey]["bytes"], "ops_vs_ref": no_dir[nkey]["ops"] / ref["ops"],
                   "bytes_vs_ref": no_dir[nkey]["bytes"] / ref["bytes"], "fits": nfits,
                   "ops_x_pick": no_dir[nkey]["ops"] / chosen["ops"],
                   "within_10pct": no_dir[nkey]["ops"] <= 1.10 * chosen["ops"]},
               "best_single_within_B": {"cell": skey, "fits": sfits, "ops": singles[skey]["ops"],
                                        "bytes": singles[skey]["bytes"], "ops_vs_ref": singles[skey]["ops"] / ref["ops"],
                                        "bytes_vs_ref": singles[skey]["bytes"] / ref["bytes"]},
               "near_miss_single": None if near is None else {
                   "cell": next(k for k, v in singles.items() if v is near), "ops": near["ops"], "bytes": near["bytes"],
                   "bytes_vs_ref": near["bytes"] / ref["bytes"]},
               "reference": {"cell": REF, "ops": ref["ops"], "bytes": ref["bytes"]}}
        for other, r2 in picks.items():
            if r2["spec"] == rec["spec"]:
                rec["shares_spec_with"] = other
        picks[f"{B:g}"] = rec
        print(f"{B:>4g} | {chosen['kind']:34s} | {chosen['ops']:>14,.0f} {chosen['ops'] / ref['ops']:>7.4f} | "
              f"{chosen['bytes'] / 1e9:>9,.1f} {chosen['bytes'] / ref['bytes']:>6.3f} | {skey} {singles[skey]['ops'] / ref['ops']:.4f}x"
              f"{'' if sfits else ' (none fits)'} | {stem}{' (shared)' if rec['shares_spec_with'] else ''}")
        if near is not None:
            print(f"       near-miss single: {rec['near_miss_single']['cell']} at {near['bytes'] / ref['bytes']:.4f}x bytes "
                  f"(misses B={B:g} by {(near['bytes'] / limit - 1) * 100:.2f}%), {near['ops'] / ref['ops']:.4f}x ops")
        if rec["best_without_direction"]:
            bw = rec["best_without_direction"]
            print(f"       best without direction: {bw['kind']} at {bw['ops_x_pick']:.4f}x the pick's ops"
                  f"{' (within 10%: say what direction buys)' if bw['within_10pct'] else ''}, {bw['bytes_vs_ref']:.3f}x ref bytes")
    n_ok = sum(1 for _, ok in trips if not _.endswith("(control)") and ok)
    print(f"round trip: {len([t for t in trips if not t[0].endswith('(control)')])} specs written, {n_ok} read back equal"
          f"{' -- 0 specs is a failure' if n_ok == 0 else ''}")
    if n_ok == 0:
        raise SystemExit(1)
    if a.check_picks:
        rows = {float(r["budget_B"]): r for r in read_csv(a.check_picks)
                if r["id"] == a.workload and r.get("grouped_options", "no") == "no"}
        bad = 0
        for B, r in sorted(rows.items()):
            if f"{B:g}" not in picks:
                continue
            rule = picks[f"{B:g}"]["pick"]
            same = rule["assignment"] == r["pick_assignment"] and abs(rule["ops"] - float(r["total_ops"])) < 1e-3
            bad += not same
            print(f"check vs {os.path.basename(a.check_picks)} B={B:g}: {'same' if same else 'DIFFERENT'} "
                  f"({r['pick_kind']}, {float(r['total_ops']):,.0f} ops)")
        print(f"picks checked: {len(rows)} rows, {bad} differ")
        if bad or not rows:
            sys.exit(1)
    out = {"workload": a.workload, "sf": a.sf, **({"results": source, "grid": a.grid} if a.results else
                                                   {"root": os.path.relpath(a.root, EXP)}),
           "reference": REF, "picks": picks, "specs": sorted(set(written.values()))}
    ppath = a.out.rstrip("/") + "-picks.json"
    with open(ppath, "w") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(f"\n{len(written)} specs for {len(budgets)} limits -> {a.out}/ (+ {ppath})")


if __name__ == "__main__":
    main()
