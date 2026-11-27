"""Predict mode's accuracy on two param sets side by side: LDBC's curated params and the uniform set (the bound person or
message drawn uniformly at random). One table, each number named by its currency and level. Analysis only: it reads
the two runs' outputs and the captures, opens no store.

Reads the study's output tree --root (experiments/predict_statistics/run-predict-statistics-uniform-params.sh writes
it); --compare-to ROOT adds the LDBC rows' drift against another output tree's counts. Writes compare.csv (section,
metric, level or currency, ldbc, uniform) and compare.md in the summary dir.

    python -m kvcost.predict_accuracy.compare_params --root <study tree> --sf 3 [--compare-to paper-results]
"""
import argparse
import csv
import json
import os
import sys

from kvevaluator.output_tree import Tree
from kvevaluator.plan_choice import candidates
from kvevaluator.weights import wops

WORKLOAD = "interactive"
STUDY = "uniform-params"                            # the driver's summary dir
SETS = (("ldbc-params", None), ("uniform", "uniform"))   # (name, param set)
COUNTS = ("S", "G", "N", "read_bytes", "rows")     # compared in the drift check
WORK_CELL = "props_condensed_adj_condensed"         # the cell whose work per query is shown (Co-Co)


def _json(path):
    if not os.path.exists(path):
        sys.exit(f"compare_params: {path} is missing (run the driver's accuracy stage)")
    with open(path) as f:
        return json.load(f)


def _csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def accuracy_rows(root):
    """(section, metric, level, ldbc, uniform) from the two summary.json"""
    s = {name: _json(os.path.join(root, f"accuracy-{name}", "summary.json")) for name, _ in SETS}
    if s["ldbc-params"]["binary"] != s["uniform"]["binary"]:   # compared side by side, both named (main's text), marked
        from kvevaluator import stale
        stale.note("binaries_differ", f"the two accuracy runs name different binaries ({s['ldbc-params']['binary']}; "
                   f"{s['uniform']['binary']}); their summaries' counting and tally versions are not recorded to compare")
    for part in ("bar1", "bar2", "ratios"):
        if sorted(s["ldbc-params"][part]) != sorted(s["uniform"][part]):
            sys.exit(f"compare_params: {part}: levels {sorted(s['ldbc-params'][part])} vs {sorted(s['uniform'][part])} -- FAIL")
    rows = []
    for lv in sorted(s["ldbc-params"]["bar1"]):
        for k, what in (("judged", "steps judged"), ("pass", "steps that pass"),
                        ("wops_share_pass", "share of the judged steps' measured wops that pass")):
            rows.append(("bar 1 (per step, within 10% on S, G, N, get_value, scan_value)", what, lv,
                         s["ldbc-params"]["bar1"][lv][k], s["uniform"]["bar1"].get(lv, {}).get(k)))
    for lv in sorted(s["ldbc-params"]["bar2"]):
        for k, what in (("clear", "clear margin: right / n"), ("near_tie", "near-tie: right / n"),
                        ("per_query", "per query, params summed: right / n")):
            rows.append(("bar 2 (cheapest diagonal cell named, summed wops)", what, lv,
                         "/".join(map(str, s["ldbc-params"]["bar2"][lv][k])),
                         "/".join(map(str, s["uniform"]["bar2"].get(lv, {}).get(k, [])))))
    for lv in sorted(s["ldbc-params"]["ratios"]):
        for m in ("wops", "bytes"):
            for k in ("median", "p10", "p90", "within_20"):
                rows.append((f"the spread, predicted / measured per step ({m}: "
                             + ("G + 1.1 S + 0.01 N" if m == "wops" else "bytes read, 7 lanes") + ")",
                             {"within_20": "share of steps within 20%"}.get(k, k), lv,
                             s["ldbc-params"]["ratios"][lv][m][k], s["uniform"]["ratios"].get(lv, {}).get(m, {}).get(k)))
    return rows


def layout_rows(root):
    """(section, metric, currency, ldbc, uniform) from the two picks.csv (the workload's and the check workload's rows,
    by their kind), or [] when either is missing"""
    p = {name: os.path.join(root, f"layouts-{name}", "layout_choice", "picks.csv") for name, _ in SETS}
    if not any(os.path.exists(x) for x in p.values()):
        return []          # the diagonals only (--cells diagonals): no layouts rows
    if not all(os.path.exists(x) for x in p.values()):
        sys.exit(f"compare_params: one set's layout picks is missing: {[x for x in p.values() if not os.path.exists(x)]} -- FAIL")
    table = {name: _csv(path) for name, path in p.items()}
    picks = {name: {(r["what"], r["currency"]): r for r in t} for name, t in table.items()}
    whats = []      # the workload rows, in the file's order: the workload, then the check workload
    for kind in ("workload", "check"):
        whats += [w for w in dict.fromkeys(r["what"] for r in table["ldbc-params"] if r["kind"] == kind)]
    rows = []
    for what in whats:
        for cur in sorted({c for (w, c) in picks["ldbc-params"] if w == what}):
            a, b = picks["ldbc-params"][(what, cur)], picks["uniform"][(what, cur)]
            n = a["layouts"]
            for k, label in (("named_short", "layout named from statistics"), ("measured_best_short", "measured best"),
                             ("named_measured_over_best", "named layout's measured cost over the best"),
                             ("measured_rank_of_named", "named layout's measured rank"),
                             ("spearman", f"Spearman, predicted vs measured over the {n}"),
                             ("top5_overlap", "top 5 overlap"),
                             ("sp_co_over_best", "Sp-Co over the best"), ("co_co_over_best", "Co-Co over the best"),
                             ("median_over_best", "median layout over the best"),
                             ("worst_over_best", "worst layout over the best"), ("gap_share", "gap share")):
                rows.append((f"the {n} uniform layouts, {what}", label, cur, a.get(k), b.get(k)))
    for cur in sorted({c for (w, c) in picks["ldbc-params"] if w == whats[0]}):
        hits = {name: [r for r in table[name] if r["currency"] == cur and r["kind"] == "query"] for name, _ in SETS}
        n = picks["ldbc-params"][(whats[0], cur)]["layouts"]
        rows.append((f"the {n} uniform layouts, per query", "queries whose named layout is the measured best", cur,
                     f"{sum(int(r['hit']) for r in hits['ldbc-params'])}/{len(hits['ldbc-params'])}",
                     f"{sum(int(r['hit']) for r in hits['uniform'])}/{len(hits['uniform'])}"))
    return rows


def capture_runs(tree, sf, param_set, cell):
    path = tree.capture(WORKLOAD, sf, param_set).path_of("counts", cell)
    return _csv(path) if os.path.exists(path) else []


def answer_rows(tree, sf):
    """[(query, param, ldbc answer rows, uniform answer rows, ldbc wops, uniform wops)] on WORK_CELL: the answer's row
    count (the same on every body that ran ok) and the cheapest ok body's wops"""
    per = {}
    for name, param_set in SETS:
        for r in capture_runs(tree, sf, param_set, WORK_CELL):
            if r["ok"] != "1" or r["measure_only"] == "1":
                continue
            k = (r["query"], int(r["param_idx"]))
            ans, w = per.setdefault(name, {}).get(k, (None, float("inf")))
            per[name][k] = (int(r["rows"]), min(w, wops(float(r["S"]), float(r["G"]), float(r["N"]))))
    keys = sorted(set(per.get("ldbc-params", {})) | set(per.get("uniform", {})), key=lambda k: (k[0][:2], int(k[0][2:].split("_")[0]), k[1]))
    out = []
    for k in keys:
        a, b = per.get("ldbc-params", {}).get(k, (None, None)), per.get("uniform", {}).get(k, (None, None))
        out.append((k[0], k[1], a[0], b[0], a[1], b[1]))
    return out


def drift(tree, sf, other):
    """the LDBC reported counts against another output tree's (e.g. paper-results/), per (cell, query, param, body sha),
    over the cells both hold: (compared, differ [(key, column, ours, theirs)], runs only ours, runs only theirs, cells
    only ours)"""
    ours = {}
    cap = tree.capture(WORKLOAD, sf)
    for c in cap.stores():
        for r in _csv(cap.path_of("counts", c)):
            if r["ok"] == "1":
                ours[(r["cell"], r["query"], r["param_idx"], candidates.successor_sha(WORKLOAD, r["query"], r["body_sha"]))] = r
    theirs_rows = {}
    theirs = Tree(other).capture(WORKLOAD, sf)
    for c in theirs.stores():
        for r in _csv(theirs.path_of("counts", c)):
            if r.get("ok") == "1":
                theirs_rows[(r["cell"], r["query"], r["param_idx"], candidates.successor_sha(WORKLOAD, r["query"], r["body_sha"]))] = r
    cells = {k[0] for k in ours} & {k[0] for k in theirs_rows}
    common = sorted(set(ours) & set(theirs_rows))
    differ = []
    for k in common:
        for c in COUNTS:
            if float(ours[k][c]) != float(theirs_rows[k][c]):
                differ.append((k, c, float(ours[k][c]), float(theirs_rows[k][c])))
    only_ours = sorted(k for k in set(ours) - set(theirs_rows) if k[0] in cells)
    only_theirs = sorted(k for k in set(theirs_rows) - set(ours) if k[0] in cells)
    return len(common), differ, only_ours, only_theirs, sorted({k[0] for k in ours} - cells)


def fmt(v):
    if v is None or v == "":
        return "-"
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return v
    if isinstance(v, float) and v != v:
        return "-"
    return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", required=True, help="the driver's output tree")
    ap.add_argument("--sf", required=True, help="the LDBC scale factor")
    ap.add_argument("--compare-to", default=None, help="another output tree (e.g. paper-results): the drift section")
    a = ap.parse_args(argv)
    tree = Tree(a.root)
    out = str(tree.summary("ldbc", a.sf, STUDY))
    rows = accuracy_rows(out) + layout_rows(out)
    bins = [_json(os.path.join(out, f"accuracy-{name}", "summary.json"))["binary"] for name, _ in SETS]
    binary = bins[0] if bins[0] == bins[1] else f"{bins[0]} (ldbc-params) and {bins[1]} (uniform: another binary)"
    meta = _json(os.path.join(out, "uniform_params.meta.json")) if os.path.exists(
        os.path.join(out, "uniform_params.meta.json")) else {}
    with open(os.path.join(out, "compare.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["section", "metric", "level_or_currency", "ldbc-params", "uniform"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], fmt(r[3]), fmt(r[4])])
    ans = answer_rows(tree, a.sf)
    with open(os.path.join(out, "compare_answers.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["query", "param", "ldbc_params_answer_rows", "uniform_answer_rows", "ldbc_params_wops", "uniform_wops"])
        for r in ans:
            w.writerow([r[0], r[1]] + [fmt(x) for x in r[2:]])
    L = ["# Predict accuracy on LDBC's params and on uniform-random params", "",
         "> Generated by "
         "`python -m kvcost.predict_accuracy.compare_params`; data in compare.csv and compare_answers.csv.", "",
         f"Two param sets on the same stores, the accuracy runs on binary {binary}: **ldbc-params**, LDBC's hosted rows 1-5; **uniform**, the same rows "
         "with the bound person (IC1-12, IS1-3) or message (IS4-7) drawn uniformly at random "
         f"({meta.get('generated_by', 'gen_uniform_params')}). Levels: predicted = population statistics only; predicted_anchor = predicted "
         "plus the bound vertex's own statistics. Currencies: wops = G + 1.1 S + 0.01 N; wops_bytes4k = wops + bytes "
         "read / 4096.", "",
         "| section | metric | level or currency | ldbc-params | uniform |", "|---|---|---|---|---|"]
    L += [f"| {r[0]} | {r[1]} | {r[2]} | {fmt(r[3])} | {fmt(r[4])} |" for r in rows]
    L += ["", f"Each (query, param) on {WORK_CELL}: the answer's rows (0 = an empty answer) and the cheapest body's "
          "measured wops (G + 1.1 S + 0.01 N):", "",
          "| query | param | ldbc-params answer rows | uniform answer rows | ldbc-params wops | uniform wops |", "|---|---|---|---|---|---|"]
    L += [f"| {r[0]} | {r[1]} | " + " | ".join(fmt(x) for x in r[2:]) + " |" for r in ans]
    empty = {name: sum(1 for r in ans if r[i] == 0) for name, i in (("ldbc-params", 2), ("uniform", 3))}
    L += ["", f"Empty answers: ldbc-params {empty['ldbc-params']} of {len(ans)}, uniform {empty['uniform']} of {len(ans)}."]
    for q in ("ic1", "ic3"):
        qa = [r for r in ans if r[0] == q]
        L.append(f"{q} (its other columns are LDBC's row, chosen for LDBC's person): answer rows ldbc-params "
                 f"{[r[2] for r in qa]}, uniform {[r[3] for r in qa]}.")
    if a.compare_to:
        n, differ, only_ours, only_theirs, no_cell = drift(tree, a.sf, a.compare_to)
        if n == 0:
            sys.exit(f"compare_params: 0 runs compared with {a.compare_to} (no cell in common, or no ok run) -- FAIL")
        L += ["", f"**Drift of the LDBC rows against `{a.compare_to}`** (per (cell, query, param, body sha), on {', '.join(COUNTS)}): "
              f"{n} runs compared, {len({d[0] for d in differ})} differ; {len(only_ours)} runs only here (a body the "
              f"other tree did not run), {len(only_theirs)} only in the other tree"
              + (f"; cells the other tree does not hold: {', '.join(no_cell)}" if no_cell else "") + "."]
        if differ:
            big = sorted(differ, key=lambda d: -abs(d[2] - d[3]) / max(1.0, abs(d[3])))[:10]
            L += ["", "| cell | query | param | body sha | column | here | other | here / other |", "|---|---|---|---|---|---|---|---|"]
            L += [f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | {c} | {x:g} | {y:g} | {fmt(x / y if y else float('nan'))} |"
                  for k, c, x, y in big]
        with open(os.path.join(out, "compare_drift.csv"), "w", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(["cell", "query", "param", "body_sha", "column", "here", "other"])
            for k, c, x, y in differ:
                w.writerow([*k, c, x, y])
    with open(os.path.join(out, "compare.md"), "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    missing = [r[0] for r in ans if r[2] is None or r[3] is None]
    if not rows or not ans or missing:
        sys.exit(f"compare_params: {len(rows)} metric rows, {len(ans)} (query, param) rows, "
                 f"{len(missing)} measured on one set only ({sorted(set(missing))[:5]}) -- FAIL")


if __name__ == "__main__":
    main()
