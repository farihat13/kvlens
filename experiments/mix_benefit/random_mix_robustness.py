#!/usr/bin/env python3
"""Random query mixes on BI and JOB: does the geomean's pick (equal contribution) also win when the mix is drawn at random?

The mix rule: a cost share s_q on Sp-Co per query, flat Dir(alpha) over the benchmark's queries, f_q = s_q / c(q, Sp-Co) with c in wops_bytes4k (the share model).
A layout's cost on a mix under model m: sum_q f_q c_m(q, L). Per (benchmark, alpha, model, layout): how many mixes it is
the best uniform layout on, and its cost over each mix's best uniform layout (median, p95, worst). Also 100 Dir(1) BI mixes
at a second fixed seed, as a control; and the Dir(1) draws with the shares taken on the geomean's
pick instead of Sp-Co (a random mix centres on the additive form with its share layout as the reference). Reads costs.csv only; no store, seconds.

    python3 experiments/mix_benefit/random_mix_robustness.py --costs <costs.csv> --out <dir>
        -> <dir>/random_mix_robustness.csv (the summary), random_mix_ratios.csv (each main-set mix's ratio per layout)
"""
import argparse
import csv
import os
from collections import defaultdict

import numpy as np

import figure_common as F
from kvcost.layout_cost import query_weighting as QW  # (sum / geomean: the one definition)

MODELS = ("wops", "bytes", "wops_bytes4k")
SHARE_MODEL = "wops_bytes4k"
ALPHAS, MAIN_ALPHA, N = (1.0, 0.3, 3.0), 1.0, 1000
# fixed 19:10 UTC (date -u), before any result of this script was read
SEED_BI, SEED_JOB = 20261024, 20261025
BI_ROUND_SEED, BI_ROUND_N = 20261019, 100        # the control's seed and number of mixes
FAMILIES = (("BI", "BI"), ("IMDb / JOB", "IMDb / JOB"))   # (benchmark, family of costs.csv)


def draws(n_queries, alpha, seed, n, salt):
    rng = np.random.default_rng(np.random.SeedSequence([seed, salt]))
    return rng.dirichlet(np.full(n_queries, alpha), size=n) if n else None


def bi_round_draws(queries):
    """bi_round.bi_workloads' 100 shares: one dirichlet per mix, queries in bi1, bi2, ... order"""
    order = sorted(queries, key=lambda q: int(q[2:]))
    rng = np.random.default_rng(np.random.SeedSequence([BI_ROUND_SEED, 17]))
    rows = np.array([rng.dirichlet(np.ones(len(order))) for _ in range(BI_ROUND_N)])
    idx = [order.index(q) for q in queries]          # back to the sorted-name order of `queries`
    return rows[:, idx]


PER_MIX = []        # (benchmark, model, draw, cell, ratio) of the main set: Dir(MAIN_ALPHA), shares on Sp-Co


def summarize(bench, label, alpha, shares, queries, cost, share_on=None, keep=False):
    """rows per (model, layout); shares: draws x queries, a cost share on share_on (default Sp-Co, the rule's)"""
    share_on = share_on or F.cells.REF
    ref = np.array([cost[q][share_on][SHARE_MODEL] for q in queries])
    freq = shares / ref                                                  # draws x queries
    out = []
    for m in MODELS:
        c = np.array([[cost[q][L][m] for L in F.CELLS] for q in queries])   # queries x layouts
        tot = freq @ c                                                   # draws x layouts
        best = tot.min(axis=1, keepdims=True)
        ratio = tot / best
        win = np.argmin(tot, axis=1)                                     # first minimum = grid order on a tie
        geo = QW.best(QW.aggregate({q: {L: cost[q][L][m] for L in F.CELLS} for q in queries}, "geomean"))
        if keep:
            PER_MIX.extend({"benchmark": bench, "model": m, "draw": d, "cell": L, "ratio": round(float(ratio[d, j]), 5)}
                           for d in range(len(ratio)) for j, L in enumerate(F.CELLS))
        for j, L in enumerate(F.CELLS):
            r = ratio[:, j]
            out.append({"benchmark": bench, "mixes": label, "shares_on": share_on, "alpha": alpha, "draws": len(r), "model": m, "cell": L,
                        "wins": int((win == j).sum()), "ratio_median": round(float(np.median(r)), 4),
                        "ratio_p95": round(float(np.quantile(r, 0.95)), 4), "ratio_max": round(float(r.max()), 4),
                        "geomean_pick": geo})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--costs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pending", action="append", default=[], metavar="FAMILY",
                    help="a costs.csv family not captured yet (e.g. 'IMDb / JOB'): left out, not a failure; it must then have no "
                         "rows. Each family draws from its own seed, so the others' draws do not change")
    a = ap.parse_args()
    cost = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))   # family -> query -> cell -> model -> v
    with open(a.costs) as f:
        for r in csv.DictReader(f):
            for m in MODELS:
                cost[r["family"]][r["query"]][r["cell"]][m] = float(r[m])
    rows = []
    for (bench, fam), seed in zip(FAMILIES, (SEED_BI, SEED_JOB)):
        queries = sorted(cost[fam])
        if fam in a.pending:
            if queries:
                raise SystemExit(f"{fam}: --pending names it, but costs.csv has {len(queries)} of its queries")
            print(f"   PENDING {bench}: no capture yet (--pending)")
            continue
        if not queries or any(set(cost[fam][q]) != set(F.CELLS) for q in queries):
            raise SystemExit(f"{fam}: {len(queries)} queries, not all on the nine layouts")
        for k, alpha in enumerate(ALPHAS):
            rows += summarize(bench, f"Dir({alpha:g}) x {N}", alpha, draws(len(queries), alpha, seed, N, k), queries, cost[fam],
                              keep=alpha == MAIN_ALPHA)
        # the reference check: the same Dir(1) draws, the shares taken on the geomean's pick under the share model
        geo = QW.best(QW.aggregate({q: {L: cost[fam][q][L][SHARE_MODEL] for L in F.CELLS} for q in queries}, "geomean"))
        rows += summarize(bench, f"Dir(1) x {N}, shares on {F.cell_label(geo)}", 1.0, draws(len(queries), 1.0, seed, N, 0),
                          queries, cost[fam], share_on=geo)
        if bench == "BI":
            rows += summarize(bench, f"bi_round's {BI_ROUND_N}", 1.0, bi_round_draws(queries), queries, cost[fam])
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, "random_mix_robustness.csv")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(a.out, "random_mix_ratios.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(PER_MIX[0]))
        w.writeheader()
        w.writerows(PER_MIX)
    groups = sorted({(r["benchmark"], r["mixes"], r["model"]) for r in rows})
    print(f"[random_mix_robustness] {len(groups)} (benchmark, mix set, model) groups x {len(F.CELLS)} layouts = {len(rows)} rows -> {path}")
    for g in groups:
        rs = [r for r in rows if (r["benchmark"], r["mixes"], r["model"]) == g]
        top = max(rs, key=lambda r: (r["wins"], -r["ratio_median"]))
        geo = next(r for r in rs if r["cell"] == rs[0]["geomean_pick"])
        print(f"   {g[0]}, {g[1]}, {g[2]}: most wins {F.cell_label(top['cell'])} {top['wins']}/{top['draws']}; "
              f"geomean pick {F.cell_label(geo['cell'])}: wins {geo['wins']}, median {geo['ratio_median']:.3f}, "
              f"p95 {geo['ratio_p95']:.3f}, worst {geo['ratio_max']:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
