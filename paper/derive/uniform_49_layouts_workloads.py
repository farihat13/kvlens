#!/usr/bin/env python3
"""Derive uniform-49-layouts-workloads' tables: the 49 uniform layouts (properties and adjacency each Split, Condensed,
or Chunked at 1, 4, 16, 64 or 256 KB) on four workloads (I01 and B01 at the LDBC run's scale (the paper: SF3), J01, L01;
I01 and B01 also at SF30 with --dataset ldbc=3,ldbc=30,...), under the combined cost (wops_bytes4k, the writes' last
chunk charged half a chunk) and DynamoDB's bill, from one output tree (--from; the paper's runs by default). A workload
whose captures the tree does not hold (SF30, JOB or LinkBench beside an sf0.1 run) is skipped and named; the rows keep
the paper's workload names (the draw scripts' keys: ldbc-sf3-* is the run's LDBC scale).

Each workload sums its queries by frequency: I01 and L01 by the benchmark's own (`sum`); B01 and J01, which define none,
by their equal-share file (`spco-share`: each query costs 1 on Sp-Co; at the file's own scale a combined Sp-Co score
must be the query count, 17 or 113, else exit). Nothing is costed here: each query's cost
on each of the 49 is read from kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k's query_costs.csv (the combined model: its
half view's rows; the bill: its measured view's rows, their exact unit lanes) in the tree's
summary/<dataset>/<scale>/layout-costs/<workload>/measured/ (kvcost.layout_cost.layout_costs), and summed here by the
workload's frequencies; I01's sums are checked equal to that place's layouts.csv, whose totals are the published text.
No mix is read: the figure shows the uniform layouts only. A (workload, model) pair in PENDING is not derived, and its
reason is printed.

Writes build/data/uniform-49-layouts-workloads.csv (588 rows: 49 x 6 x 2), its
-per-query.csv (each query's cost on each of the 49; checked to sum to the scores), -query-best.csv (per query: its cheapest cost and which settings are within 1%, BEST_WITHIN) and -best-total.csv.
Each pending pair removes 49 rows from the main table.

    uv run python paper/derive/uniform_49_layouts_workloads.py [--from output --dataset ldbc=0.1] [--compare <a CSV of the same columns>]
        [--plant per-query|best-total]
"""
import argparse
import csv
import math
import sys

from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import layout_costs as LC
from kvevaluator.layouts import cell_id
from kvcost.layout_cost import query_weighting as QW
from kvevaluator import sizes
from snapshots import PAPER, add_tree_args, tree_of, write_table


NAME = "uniform-49-layouts-workloads"
COLUMNS = ["model", "workload", "cell", "props", "adjacency", "form", "score", "ratio"]
PER_QUERY = NAME + "-per-query"
# one row per (model, workload, item, uniform layout): wops and bytes of one run of the query (cost-layouts),
# cost in the model (wops_bytes4k or bill_exact); the score sums frequency x cost
PER_QUERY_COLUMNS = ["model", "workload", "family", "query", "frequency", "cell", "props", "adjacency", "wops", "bytes", "cost"]
QUERY_BEST, BEST_TOTAL = NAME + "-query-best", NAME + "-best-total"
BEST_WITHIN = 1.01             # a setting is among a query's best when its cheapest cost is within 1% of the query's (inclusive)
SETTING_SHORT = {"split": "sp", "condensed": "co", "chunked_c1k": "ch1k", "chunked_c4k": "ch4k", "chunked_c16k": "ch16k",
                 "chunked": "ch64k", "chunked_c256k": "ch256k"}     # the 7 settings, in the label's order
GROUPS = {"ic": "complex", "is": "short", "iu": "update"}     # LDBC Interactive's three kinds, by the query's id
QUERY_BEST_COLUMNS = (["model", "workload", "family", "query", "frequency", "lo", "weight", "share"]
                      + [f"props_{x}" for x in SETTING_SHORT.values()] + [f"adj_{x}" for x in SETTING_SHORT.values()])
BEST_TOTAL_COLUMNS = ["model", "workload", "items", "best_total", "best_cell", "best_score", "ratio"]
SETTINGS = tuple(SETTING_SHORT)           # the 7 settings in uniform_49.settings_of's order (64 KB is `chunked`)
REF = QW.REF                              # Sp-Co
TOL = 1e-12                               # a sum of the per-query costs against its published total, relative
# model -> (the cost model, the chunk view its rows are read in: query_costs.csv's chunk_view)
MODELS = {"combined": ("wops_bytes4k", "half"), "bill": ("bill_exact", "measured")}

def workloads(tree, run):
    """[(workload, dataset, sf, id, form, the costed 49 (cost-layouts' place), the place whose layouts.csv holds the
    published combined 49 or None)] in `tree`: each dataset at its scale in the run
    (kvevaluator.sizes: a dataset the run leaves out has no row), and LDBC's two workloads also at each larger LDBC scale
    of the run (--dataset ldbc=3,ldbc=30,...: SF30; the paper reports none). The main LDBC rows keep the names the paper's tables use (ldbc-sf3-*) at any scale"""
    costed = lambda ds, sf, wid: LC.place(tree, ds, sf, wid)
    ldbc, linkbench, job = (sizes.scale(run, ds) for ds in ("ldbc", "linkbench", "imdb"))
    larger = sizes.larger(run, "ldbc")
    interactive = lambda name, sf: (name, "ldbc", sf, "i01", "sum", costed("ldbc", sf, "i01"), costed("ldbc", sf, "i01"))
    bi = lambda name, sf: (name, "ldbc", sf, "b01", "spco-share", costed("ldbc", sf, "b01"), None)
    rows = []
    if ldbc:
        rows.append(interactive("ldbc-sf3-interactive", ldbc))
        rows += [interactive(f"ldbc-sf{sf}-interactive", sf) for sf in larger]
    if linkbench:
        rows.append(("linkbench", "linkbench", linkbench, "l01", "sum", costed("linkbench", linkbench, "l01"), None))
    if ldbc:
        rows.append(bi("ldbc-sf3-bi", ldbc))
        rows += [bi(f"ldbc-sf{sf}-bi", sf) for sf in larger]
    if job:
        rows.append(("imdb-job", "imdb", job, "j01", "spco-share", costed("imdb", job, "j01"), None))
    for ds in ("ldbc", "linkbench", "imdb"):
        if not sizes.scale(run, ds):
            print(f"{ds}: not in this run (--scale {sizes.text(run)}): its rows are left out")
    return tuple(rows)


WORKLOADS = workloads(PAPER, sizes.resolve(sizes.PAPER))   # the paper's runs (spco_per_query_i01.py and census_workloads.py read it)


def costs_file(spec):
    """the per-query costs cost-layouts wrote for `spec` (both chunk views' rows)"""
    return spec[5] / "query_costs.csv"


def missing(spec):
    """the per-query costs file of `spec` when the tree does not hold it, or None"""
    return None if costs_file(spec).is_file() else costs_file(spec)
# (workload, model) pairs not derived until their captures are measured again, each with its reason
PENDING = {("ldbc-sf30-interactive", "bill"):
           "SF30's write captures (captures/ldbc/sf30) predate the exact write units (put_units_1k, del_units_1k): "
           "measured again on the next machine, then this entry goes"}


def close(a, b):
    return abs(a - b) <= TOL * max(1.0, abs(b))


def pinned_49(place):
    """{cell: total_ops + total_bytes / 4096} of the 49 in a cost-layouts place's layouts.csv (the half view, the
    combined model's)"""
    return {r["layout"]: float(r["total_ops"]) + float(r["total_bytes"]) / 4096
            for r in LC.read_layouts(place, LC.VIEWS[0], kind="uniform")}


def query_rows(spec, mname):
    """(w, cells, per): one workload in one model (MODELS): its workload file's items (BI, JOB: the equal-share file),
    the 49 {cell: (props, adjacency)} and, per cell, each item's (wops, bytes, cost) for one run, read from cost-layouts
    per-query costs (costs_file, the model's chunk view): cost = wops_bytes4k, or the bill's exact units (cost_models.bill_exact of
    the row's unit lanes). paper/derive/spco_per_query_i01.py calls it too."""
    name, dataset, sf, wid, form, *_ = spec
    model = MODELS[mname][0]
    if form == "spco-share":
        QW.equal_share(wid)                            # the tracked file's checks (model, chunk view, deciding scale)
        _, w = benchmarks.read_workload(str(QW.equal_share_path(wid)))      # BI, JOB: the equal-share file
    else:
        (_, w), = benchmarks.select(dataset, sf, wid).items()
    _, costs = LC.read_query_costs(spec[5], MODELS[mname][1])
    if set(costs) != {(f, q) for f, q, *_ in w["items"]}:
        raise SystemExit(f"{name}: {costs_file(spec)} holds other queries than {wid}'s file")
    cells = {cell_id(p, a): (p, a) for a in SETTINGS for p in SETTINGS}
    per = {}
    for c in cells:
        per[c] = []
        for fam, q, _, _ in w["items"]:
            r = costs[(fam, q)][c]
            cost = (float(r["wops_bytes4k"]) if model == "wops_bytes4k"
                    else CM.bill_exact({k: float(v) for k, v in r.items() if k.startswith("bill_")}))
            per[c].append((float(r["wops"]), float(r["bytes"]), cost))
    return w, cells, per


def workload_rows(spec, counts):
    """(the 49 of one workload, both models; each query's own cost on each of the 49)"""
    name, _dataset, sf, wid, form, _, pinned = spec
    if pinned is not None and not (pinned / "layouts.csv").is_file():
        raise SystemExit(f"{name}: no {pinned}/layouts.csv beside the per-query costs (kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k writes both)")
    rows, per_query = [], []
    for mname in MODELS:
        if (name, mname) in PENDING:
            print(f"{name} {mname}: PENDING, not derived: {PENDING[(name, mname)]}")
            continue
        w, cells, per = query_rows(spec, mname)
        score = {c: math.fsum(often * cost for (_, _, often, _), (_, _, cost) in zip(w["items"], per[c])) for c in per}
        for c, (p, a) in cells.items():
            for (fam, q, often, _), (ops, nbytes, cost) in zip(w["items"], per[c]):
                per_query.append({"model": mname, "workload": name, "family": fam, "query": q, "frequency": float(often),
                                  "cell": c, "props": p, "adjacency": a, "wops": float(ops), "bytes": float(nbytes),
                                  "cost": float(cost)})
        if form == "spco-share" and mname == "combined" and sf == QW.equal_share(wid)["equal_share"]["scale"]:
            freq = {q: f for _, q, f, _ in w["items"]}
            ref = {q: c for (_, q, _, _), (_, _, c) in zip(w["items"], per[REF])}
            QW.check_reference_total(freq, ref, f"{name} combined")
            counts["checked"] += 1
            print(f"{name}: Sp-Co's weighted combined total = {score[REF]!r} ({len(freq)} queries, each 1 on Sp-Co)")
        if pinned is not None and mname == "combined":
            table = pinned_49(pinned)
            if set(table) != set(score):
                raise SystemExit(f"{name}: {pinned}/layouts.csv holds other cells than the 49 here")
            bad = [c for c in score if not close(score[c], table[c])]
            if bad:
                raise SystemExit(f"{name}: the per-query costs' sums differ from {pinned} at {bad[:5]}")
            score = {c: table[c] for c in score}      # the published totals' text
            counts["read"] += 49
            print(f"{name} {mname}: the 49 read from layouts.csv (49 of 49 equal to the per-query costs' sums)")
        else:
            counts["made"] += 49
        for c, (p, a) in cells.items():
            rows.append({"model": mname, "workload": name, "cell": c, "props": p, "adjacency": a, "form": form,
                         "score": score[c], "ratio": score[c] / score[REF]})
        best = min(sorted(score), key=lambda c: score[c])
        print(f"{name} {mname}: Sp-Co {score[REF]:.6g}; the best uniform {best} {score[best] / score[REF]:.4f} of "
              f"Sp-Co's ({len(w['items'])} items)")
    return rows, per_query


def check_per_query(rows, per_query):
    """per (model, workload, cell) of the 49: fsum(frequency x cost) over its per-query rows equals its score row (the
    same relative tolerance as a pinned total); exit 1 otherwise. Returns (layouts checked, the worst relative difference)"""
    terms = {}
    for r in per_query:
        terms.setdefault((r["model"], r["workload"], r["cell"]), []).append(r["frequency"] * r["cost"])
    scores = {(r["model"], r["workload"], r["cell"]): r["score"] for r in rows}
    if set(terms) != set(scores):
        raise SystemExit(f"{PER_QUERY}: its (model, workload, cell) set is not the score rows' "
                         f"({len(terms)} here, {len(scores)} there)")
    worst, bad = 0.0, []
    for k, t in sorted(terms.items()):
        got, want = math.fsum(t), scores[k]
        worst = max(worst, abs(got - want) / max(1.0, abs(want)))
        if not close(got, want):
            bad.append((k, got, want))
    for k, got, want in bad[:5]:
        print(f"  {'/'.join(k)}: per-query rows sum to {got!r}, the score row says {want!r}")
    print(f"{PER_QUERY}: {len(terms) - len(bad)} of {len(terms)} layouts sum to their score row (worst relative "
          f"difference {worst:.1e}, tolerance {TOL:g})")
    if bad:
        sys.exit(1)
    return len(terms), worst


def query_best(rows, per_query):
    """(the query-best rows, the best-total rows) from the per-query rows; exit 1 when a per-query-best total exceeds a
    layout's score (it cannot: each query's cheapest cost is at most its cost on that layout)"""
    by_item = {}
    for r in per_query:
        by_item.setdefault((r["model"], r["workload"], r["family"], r["query"]), []).append(r)
    qrows, weights = [], {}
    for (m, wl, fam, q), rs in sorted(by_item.items()):
        if len(rs) != 49:
            raise SystemExit(f"{QUERY_BEST}: {m}/{wl}/{q} has {len(rs)} layouts, not 49")
        lo = min(r["cost"] for r in rs)
        best_p = {r["props"] for r in rs if min(x["cost"] for x in rs if x["props"] == r["props"]) <= lo * BEST_WITHIN}
        best_a = {r["adjacency"] for r in rs
                  if min(x["cost"] for x in rs if x["adjacency"] == r["adjacency"]) <= lo * BEST_WITHIN}
        row = {"model": m, "workload": wl, "family": fam, "query": q, "frequency": rs[0]["frequency"], "lo": lo,
               "weight": rs[0]["frequency"] * lo}
        row.update({f"props_{x}": str(k in best_p).lower() for k, x in SETTING_SHORT.items()})
        row.update({f"adj_{x}": str(k in best_a).lower() for k, x in SETTING_SHORT.items()})
        qrows.append(row)
        weights.setdefault((m, wl), []).append(row["weight"])
    totals = {k: math.fsum(v) for k, v in weights.items()}
    for row in qrows:
        row["share"] = row["weight"] / totals[(row["model"], row["workload"])]
    trows, over = [], []
    for (m, wl), total in sorted(totals.items()):
        scores = {r["cell"]: r["score"] for r in rows if (r["model"], r["workload"]) == (m, wl)}
        over += [(m, wl, c, total, sc) for c, sc in scores.items() if total > sc * (1 + TOL)]
        best = min(sorted(scores), key=lambda c: scores[c])
        trows.append({"model": m, "workload": wl, "items": len(weights[(m, wl)]), "best_total": total, "best_cell": best,
                      "best_score": scores[best], "ratio": total / scores[best]})
    for m, wl, c, total, sc in over[:5]:
        print(f"  {m}/{wl}: the per-query-best total {total!r} exceeds {c}'s score {sc!r}")
    print(f"{BEST_TOTAL}: {len(trows)} (model, workload); the per-query-best total is at most each of the 49 scores in "
          f"{len(trows) * 49 - len(over)} of {len(trows) * 49}")
    if over:
        sys.exit(1)
    return qrows, trows


def compare(path, mine):
    """every row of `path` against ours by (model, workload, cell): each field's text; prints the differences (a report,
    not a gate: it does not change the exit code)"""
    with open(path) as fh:
        theirs = {(r["model"], r["workload"], r["cell"]): r for r in csv.DictReader(fh)}
    text = lambda v: repr(v) if isinstance(v, float) else str(v)  # (write_table's)
    ours = {(r["model"], r["workload"], r["cell"]): {c: text(r[c]) for c in COLUMNS} for r in mine}
    differ = [(k, c, theirs[k][c], ours[k][c]) for k in sorted(set(ours) & set(theirs)) for c in COLUMNS
              if theirs[k][c] != ours[k][c]]
    for k, c, t, o in differ:
        rel = abs(float(t) - float(o)) / max(abs(float(t)), 1e-300) if c in ("score", "ratio") else None
        print(f"  differs: {'/'.join(k)} {c}: {t} there, {o} here" + (f" (relative {rel:.1e})" if rel is not None else ""))
    rows_differ = len({k for k, _, _, _ in differ})
    print(f"compare: {len(set(ours) & set(theirs))} rows in both ({len(theirs)} there, {len(ours)} here), "
          f"{rows_differ} differ in some field")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    ap.add_argument("--compare", help="a CSV of the same columns (gs-paper's table): print each field that differs")
    ap.add_argument("--plant", choices=("per-query", "best-total"),
                    help="a check's control, must exit 1: per-query = one row's cost off by 1e-6 relative; best-total = one "
                         "layout's score lowered below its workload's per-query-best total (after the per-query check)")
    a = ap.parse_args()
    tree = tree_of(a.tree)
    counts = {"made": 0, "read": 0, "checked": 0}
    rows, per_query = [], []
    kept = []
    for spec in workloads(tree, a.run):
        gone = missing(spec)
        if gone is not None:
            print(f"{spec[0]}: not in this tree ({gone})")
            continue
        kept.append(spec)
        r, pq = workload_rows(spec, counts)
        rows += r
        per_query += pq
    names = {s[0] for s in kept}
    pending = [k for k in PENDING if k[0] in names]
    checks = sum(s[4] == "spco-share" and s[2] == QW.equal_share(s[3])["equal_share"]["scale"] for s in kept)
    want = 98 * len(kept) - 49 * len(pending)
    if len(rows) != want or counts["checked"] != checks:
        raise SystemExit(f"{len(rows)} rows, not {want} (49 x {len(kept)} workloads x 2, less 49 per "
                         f"pending pair: {sorted(pending)}); {counts['checked']} Sp-Co checks, not {checks}")
    if a.plant == "per-query":
        planted = sorted(per_query, key=lambda r: (r["model"], r["workload"], r["family"], r["query"], r["cell"]))[0]
        planted["cost"] *= 1 + 1e-6
        print(f"PLANT: {planted['model']}/{planted['workload']}/{planted['query']}/{planted['cell']}: cost x (1 + 1e-6)")
    check_per_query(rows, per_query)
    if a.plant == "best-total":
        planted = sorted(rows, key=lambda r: (r["model"], r["workload"], r["cell"]))[0]
        planted["score"] *= 1e-6
        print(f"PLANT: {planted['model']}/{planted['workload']}/{planted['cell']}: score x 1e-6")
    qrows, trows = query_best(rows, per_query)
    out = {"out_dir": a.tables}
    write_table(NAME, COLUMNS, rows, key=("model", "workload", "cell"), **out)
    write_table(PER_QUERY, PER_QUERY_COLUMNS, per_query, key=("model", "workload", "family", "query", "cell"), **out)
    write_table(QUERY_BEST, QUERY_BEST_COLUMNS, qrows, key=("model", "workload", "family", "query"), **out)
    write_table(BEST_TOTAL, BEST_TOTAL_COLUMNS, trows, key=("model", "workload"), **out)
    print(f"{NAME}: {counts['made']} uniform rows summed from the per-query costs, {counts['read']} read from the published "
          f"49-layout totals (checked against those sums), {counts['checked']} equal-share files whose Sp-Co total is the query count; {len(rows)} rows"
          f"; pending (not derived): {', '.join('/'.join(k) for k in sorted(pending)) or 'none'}; not in this tree: "
          f"{', '.join(s[0] for s in workloads(tree, a.run) if s[0] not in names) or 'none'}")
    if a.compare:
        compare(a.compare, rows)


if __name__ == "__main__":
    main()
