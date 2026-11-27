#!/usr/bin/env python3
"""Way 6's figure: which queries of a workload disagree with the workload's best uniform layout, and how much they weigh.
One file per family (LDBC I01, BI, IMDb / JOB, LinkBench), queries sorted by their share of the workload's cost:
  top     the query's share of the workload's cost on the workload's best uniform layout (how often it runs x its cost)
  bottom  that layout's cost on the query / the query's own cheapest of the nine uniform layouts (1x = the query agrees),
          coloured by the adjacency of the query's own cheapest layout
The workload's best uniform layout = its headline rule (per_query_disagreement.BEST_BY): the lowest total over its queries;
for BI and JOB, which define no frequencies, the lowest geomean (equal contribution). Reads per_query_disagreement.py's
costs.csv.
Counts are deterministic: no error bars.

    python3 experiments/mix_benefit/plot_per_query_disagreement.py [--costs <costs.csv>] [--model wops_bytes4k] [--out DIR]
Output: <out>/per_query_disagreement_<family>[_<model>].{png,pdf}
"""
import argparse
import collections
import csv
import os
import re

import matplotlib
from kvevaluator.paths import ROOT
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

EXP = str(ROOT)
from kvcost.plotting.style import apply_house_style, format_color
from figure_common import SUM_LABEL, cell_label
from kvcost.layout_cost import query_weighting as QW  # (sum / geomean: the one definition)
from per_query_disagreement import BEST_BY  # (each family's headline rule)
from kvevaluator.output_tree import Tree

COSTS = str(Tree().summary("ldbc", "3", "mix-benefit", "per-query-disagreement") / "costs.csv")
OUT = str(Tree().summary("ldbc", "3", "mix-benefit", "per-query-disagreement", "plots"))
FAMILIES = ("LDBC, all of I01", "BI", "IMDb / JOB", "LinkBench")
MODELS = {"wops": "weighted ops", "bytes": "bytes", "wops_bytes4k": SUM_LABEL}
# adjacency -> (label, the palette's format with that adjacency)
ADJ = {"split": ("Split", "nebula"), "condensed": ("Condensed", "all_condensed"), "chunked": ("Chunked", "pertype_csr")}
TIE, OVER = 1.01, 1.5      # within 1% of its own cheapest = agrees (per_query_disagreement.py's tie rule); the tables' 1.5x line


def name(cell):
    return cell_label(cell)                           # Co-Sp: properties-adjacency, as every figure of the set


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--costs", default=COSTS)
    ap.add_argument("--model", default="wops_bytes4k", choices=sorted(MODELS))
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--pending", action="append", default=[], metavar="FAMILY",
                    help="a family not captured yet (e.g. 'IMDb / JOB'): no figure; it must then have no rows")
    a = ap.parse_args()
    cost, weight = collections.defaultdict(lambda: collections.defaultdict(dict)), {}
    for r in csv.DictReader(open(a.costs)):
        cost[r["family"]][r["query"]][r["cell"]] = float(r[a.model])
        weight[(r["family"], r["query"])] = float(r["weight"])
    apply_house_style(extralarge=1)
    os.makedirs(a.out, exist_ok=True)
    for fam in FAMILIES:
        if fam in a.pending:
            if cost.get(fam):
                raise SystemExit(f"{fam}: --pending names it, but {a.costs} has {len(cost[fam])} of its queries")
            print(f"{fam}: pending (no capture yet), no figure")
            continue
        q = cost[fam]
        assert q and all(len(v) == 9 for v in q.values()), f"{fam}: {len(q)} queries, not all on nine layouts"
        cells = sorted(next(iter(q.values())))
        total = {c: sum(weight[(fam, k)] * v[c] for k, v in q.items()) for c in cells}
        rule = BEST_BY.get(fam, "sum")
        best = min(cells, key=lambda c: (total[c], c)) if rule == "sum" else QW.best(QW.aggregate({k: dict(v) for k, v in q.items()}, rule))
        share = {k: weight[(fam, k)] * v[best] / total[best] for k, v in q.items()}
        ratio = {k: v[best] / min(v.values()) for k, v in q.items()}
        own = {k: min(v, key=lambda c: (v[c], c)) for k, v in q.items()}
        order = sorted(q, key=lambda k: (-share[k], k))
        n = len(order)
        agree = [k for k in order if ratio[k] <= TIE]
        over = [k for k in order if ratio[k] > OVER]
        gain = (total[best] / sum(weight[(fam, k)] * min(v.values()) for k, v in q.items()) if rule == "sum"
                else QW.aggregate({k: {"r": ratio[k]} for k in q}, "geomean")["r"])
        fig, (top, bot) = plt.subplots(2, 1, figsize=(max(11.0, 0.17 * n + 2.2), 7.2), sharex=True, height_ratios=(1, 1.6))
        xs = range(n)
        top.bar(xs, [100 * share[k] for k in order], 0.8, color="0.55")
        top.set_ylabel(f"share of the workload's\ncost on {name(best)} (%)" + ("\n(each query once)" if rule != "sum" else ""), fontsize=9)
        colors = ["0.8" if k in agree else format_color(ADJ[own[k].split("_adj_")[1]][1]) for k in order]
        bot.bar(xs, [ratio[k] - 1 for k in order], 0.8, bottom=1, color=colors, edgecolor="white", linewidth=0.3)
        bot.axhline(OVER, color="black", lw=0.8, ls="--")
        bot.set_yscale("log")
        ymax = max(ratio.values())
        bot.set_ylim(1, max(2.0, ymax * 2.3))
        ticks = [t for t in (1, 1.5, 2, 3, 5, 10, 20, 50, 100) if t <= max(2.0, ymax * 2.3)]
        bot.set_yticks(ticks, [f"{t:g}x" for t in ticks]); bot.minorticks_off()
        small = n > 40
        for x, k in enumerate(order):
            if ratio[k] > OVER:
                bot.text(x, ratio[k] * 1.04, f"{ratio[k]:.1f}x" if not small else f"{ratio[k]:.0f}" if ratio[k] >= 9.5 else f"{ratio[k]:.1f}",
                         ha="center", va="bottom", rotation=90, fontsize=6.5 if small else 8.5)
        bot.set_ylabel(f"{name(best)}'s cost on the query /\nthe query's own cheapest layout", fontsize=9)
        bot.set_xticks(list(xs), [re.sub(r"^job", "", k) if fam == "IMDb / JOB" else k for k in order], rotation=90, fontsize=6.5 if small else 9)
        bot.set_xlim(-0.7, n - 0.3)
        bot.set_xlabel("query" + (" (JOB's number and letter)" if fam == "IMDb / JOB" else "") + ", by its share of the workload's cost", fontsize=10)
        for ax in (top, bot):
            ax.grid(axis="y", alpha=0.3); ax.set_axisbelow(True)
        handles = [Patch(color="0.8", label=f"agrees (within 1%): {len(agree)} of {n}")]
        handles += [Patch(color=format_color(k), label=f"own cheapest has {lab} adjacency") for lab, k in ADJ.values()]
        fig.legend(handles=handles, loc="lower center", ncol=2, frameon=False, fontsize=9.5)
        fig.suptitle(f"{fam}: which queries disagree with the workload's best uniform layout, {name(best)} ({MODELS[a.model]})\n"
                     f"{len(agree)} of {n} queries agree; the {len(over)} above {OVER}x carry {100 * sum(share[k] for k in over):.1f}% of the workload's cost"
                     f";\na layout per query would cost {gain:.2f}x less" + (" (geomean over queries, equal contribution)" if rule != "sum" else ""), fontsize=10.5)
        longest = max(len(t.get_text()) for t in bot.get_xticklabels())
        fig.subplots_adjust(top=0.86, bottom=(1.25 + 0.075 * longest) / fig.get_figheight(), left=1.05 / fig.get_figwidth(), right=0.99, hspace=0.08)
        slug = re.sub(r"[^a-z0-9]+", "-", fam.lower()).strip("-")
        tag = "" if a.model == "wops_bytes4k" else f"_{a.model}"
        for ext in ("png", "pdf"):
            fig.savefig(os.path.join(a.out, f"per_query_disagreement_{slug}{tag}.{ext}"), dpi=130)
        plt.close(fig)
        print(f"{fam}: best uniform {name(best)}; {n} queries, {len(agree)} agree, {len(over)} over {OVER}x carrying {100 * sum(share[k] for k in over):.1f}%; "
              f"largest ratio {ymax:.2f}x ({max(ratio, key=ratio.get)})")
    print(f"wrote {len(FAMILIES) - len(a.pending)} figures to {a.out}")


if __name__ == "__main__":
    main()
