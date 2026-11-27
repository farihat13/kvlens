#!/usr/bin/env python3
"""Draw random_mix_robustness.py's main set: per uniform layout, its cost over each random mix's best uniform layout.
One panel per benchmark (BI, IMDb / JOB) x model; a box per layout (25-75%, median line, whiskers 5-95%, a dot at the
worst mix); the geomean's pick (equal contribution, the headline) has a star. Counts are deterministic: no error bars.

    python3 experiments/mix_benefit/plot_random_mix_robustness.py --data <dir>/random_mix_ratios.csv --out <dir>/plots
        -> <out>/random_mix_robustness.{png,pdf}
"""
import argparse
import csv
import os
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

import figure_common as F
import random_mix_robustness as R  # (its constants: the mix rule, N, the main alpha)
from kvcost.plotting.style import apply_house_style, format_color

BENCHMARKS = ("BI", "IMDb / JOB")
DATA = {"BI": "BI at SF3 (17 queries)", "IMDb / JOB": "IMDb / JOB on the full data (113 queries)"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--summary", default="", help="random_mix_robustness.csv (default: beside --data)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--source", default=F.SOURCE, help="the caption's last sentences: which data")
    ap.add_argument("--pending", action="append", default=[], metavar="BENCHMARK",
                    help="a benchmark not captured yet (e.g. 'IMDb / JOB'): its row is left out and named in the caption")
    a = ap.parse_args()
    benchmarks = [b for b in BENCHMARKS if b not in a.pending]
    ratio = defaultdict(lambda: defaultdict(list))            # (benchmark, model) -> cell -> [ratio per mix]
    with open(a.data) as f:
        for r in csv.DictReader(f):
            ratio[(r["benchmark"], r["model"])][r["cell"]].append(float(r["ratio"]))
    summary = a.summary or os.path.join(os.path.dirname(a.data), "random_mix_robustness.csv")
    main_set = f"Dir({R.MAIN_ALPHA:g}) x {R.N}"
    with open(summary) as f:
        rows = [r for r in csv.DictReader(f) if r["mixes"] == main_set]
    geo = {(r["benchmark"], r["model"]): r["geomean_pick"] for r in rows}
    wins = {(r["benchmark"], r["model"], r["cell"]): int(r["wins"]) for r in rows}
    apply_house_style(extralarge=1)
    plt.rcParams.update({"font.size": 9, "axes.titlesize": 9.5, "hatch.linewidth": 0.6})
    fig, axes = plt.subplots(len(benchmarks), len(R.MODELS), figsize=(13.5, 3.7 * len(benchmarks)), constrained_layout=True, squeeze=False)
    covered = 0
    for i, b in enumerate(benchmarks):
        for j, m in enumerate(R.MODELS):
            ax = axes[i][j]
            data = [ratio[(b, m)][c] for c in F.CELLS]
            n = {len(d) for d in data}
            if n != {R.N}:
                raise SystemExit(f"{b}, {m}: {n} mixes per layout, not {R.N}")
            covered += 1
            for k, (c, d) in enumerate(zip(F.CELLS, data)):
                q05, q25, q50, q75, q95 = np.quantile(d, [0.05, 0.25, 0.5, 0.75, 0.95])
                ax.add_patch(plt.Rectangle((k - 0.35, q25), 0.7, q75 - q25, facecolor=F.adj_color(c), edgecolor="0.25", lw=0.6,
                                           hatch=F.PROPS_HATCH[F.settings(c)[0]]))
                ax.plot([k - 0.35, k + 0.35], [q50, q50], color="black", lw=1.4)
                ax.plot([k, k], [q05, q25], color="0.25", lw=0.8)
                ax.plot([k, k], [q75, q95], color="0.25", lw=0.8)
                ax.plot([k], [max(d)], marker="o", ms=2.5, color="0.25")
                w = wins[(b, m, c)]
                if w:
                    ax.text(k, 0.93, f"{w}", ha="center", va="top", fontsize=7, transform=ax.get_xaxis_transform())
            g = F.CELLS.index(geo[(b, m)])
            ax.scatter([g], [1.0], marker="*", s=150, color="gold", edgecolor="black", lw=0.7, zorder=5, clip_on=False)
            ax.set_yscale("log")
            top = max(max(d) for d in data)
            ax.set_ylim(0.95, top * 3)
            ticks = [t for t in (1, 1.5, 2, 3, 5, 10, 20, 50, 100, 200, 500) if t <= top * 3]
            ax.set_yticks(ticks, [f"{t:g}x" for t in ticks]); ax.minorticks_off()
            ax.set_xlim(-0.6, len(F.CELLS) - 0.4)
            ax.set_xticks(range(len(F.CELLS)), [F.cell_label(c) for c in F.CELLS], rotation=90, fontsize=8)
            ax.grid(True, axis="y", color="0.9", lw=0.6); ax.set_axisbelow(True)
            gw = wins[(b, m, geo[(b, m)])]
            ax.set_title(f"{b}, {F.MODEL_LABEL[m]}\ngeomean's pick {F.cell_label(geo[(b, m)])}: best on {gw} of {R.N} mixes")
            if j == 0:
                ax.set_ylabel("cost over the mix's best\nuniform layout (log)")
    handles = [Patch(facecolor=format_color(k), edgecolor="0.25", label=f"{lab} adjacency") for lab, k in F.ADJ.values()]
    handles += [Patch(facecolor="0.85", hatch=h, edgecolor="0.25", label=f"{p.capitalize()} properties") for p, h in F.PROPS_HATCH.items()]
    handles += [plt.Line2D([], [], marker="*", ls="", ms=11, mfc="gold", mec="black", label="geomean's pick (equal contribution)")]
    fig.legend(handles=handles, loc="outside upper center", ncol=7, frameon=False, fontsize=8)
    F.caption(fig, f"{R.N} random mixes per benchmark: a cost share per query on Sp-Co, flat Dir({R.MAIN_ALPHA:g}) over its queries "
              f"(shares in {F.MODEL_LABEL[R.SHARE_MODEL]}). Box: 25-75% of the mixes, line: median, whiskers: "
              f"5-95%, dot: the worst mix. The number at the top: on how many mixes the layout is the best. "
              + ", ".join(DATA[b] for b in benchmarks) + "." + (f" Pending (no capture yet): {', '.join(a.pending)}." if a.pending else "")
              + f" {F.WEIGHTS} {F.SUM} {a.source}", width=190)
    F.save(fig, a.out, "random_mix_robustness", f"{covered} panels x {len(F.CELLS)} layouts x {R.N} mixes")


if __name__ == "__main__":
    main()
