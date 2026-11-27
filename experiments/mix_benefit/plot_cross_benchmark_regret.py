#!/usr/bin/env python3
"""Draw "no uniform layout is good on every benchmark": each uniform layout's cost over that benchmark's best uniform
layout, per benchmark and way of counting its queries. Three outputs, all from cross_benchmark_frequencies.csv:
  cross_benchmark_regret                    the heat map: one panel per model, a "worst of the columns" column
  cross_benchmark_regret_bars               the same ratios as bars, grouped by layout, under ops + bytes / 4 KB
  cross_benchmark_regret_bars_by_benchmark  one panel per benchmark, each on its own scale (SCALE below), same model

    python3 experiments/mix_benefit/cross_benchmark_frequencies.py [--root <snapshot>/sf3]
    python3 experiments/mix_benefit/plot_cross_benchmark_regret.py
        -> <out>/summary/ldbc/sf3/mix-benefit/cross-benchmark-frequencies/plots/cross_benchmark_regret*.{png,pdf}
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.patches import Patch

import figure_common as F
from kvcost.plotting.style import SEQUENTIAL_CMAP, apply_house_style, format_color

MODELS = ("wops", "bytes", "wops_bytes4k")
# the columns, in order: (benchmark, frequencies) of the CSV -> the two-line column name. The first of each benchmark is its
# headline (its own frequencies; BI and JOB define none, so equal contribution = the geomean over queries); the "worst of"
# column is over these four. The "once each" columns are the robustness check.
COLUMNS = [(("LDBC Interactive", "LDBC frequencies"), "LDBC\nInteractive", "LDBC freq."),
           (("LDBC Interactive", "once each"), "LDBC\nInteractive", "once each"),
           (("LDBC BI", "equal contribution"), "LDBC\nBI", "equal contr."),
           (("LDBC BI", "once each"), "LDBC\nBI", "once each"),
           (("IMDb / JOB", "equal contribution"), "IMDb /\nJOB", "equal contr."),
           (("IMDb / JOB", "once each"), "IMDb /\nJOB", "once each"),
           (("LinkBench", "its log's frequencies"), "Link-\nBench", "its log's freq."),
           (("LinkBench", "once each"), "Link-\nBench", "once each")]
HEADLINE = [0, 2, 4, 6]       # the column index of each benchmark's headline
# the by-benchmark bars: "zero_to_one" = (cost - best) / (worst - best) per panel; "ratio_to_best" = cost / best, own y axis
SCALE = "zero_to_one"
VMAX = 100            # the heat map's colour scale ends here; a larger ratio keeps the darkest colour and its number
# benchmarks have no colour in the style library: fixed ones for the bars grouped by layout (a paler one = once each)
COLUMN_COLOR = ("#4c72b0", "#9ecae1", "#55a868", "#a1d99b", "#c44e52", "#f4a6a6", "#8172b2", "#c5b0d5")


def freq_note(v):
    """the caption sentence on how each column counts its queries; LinkBench's number of request types from the data"""
    n = v[("LinkBench", "once each", F.CELLS[0])]["queries"]
    return ("How each benchmark's queries are counted is given with its name: LDBC freq. = each query x its LDBC frequency (I01); once each "
            f"= every query once (LinkBench: one request of each of its {n} request types, at the type's mean cost per request in its log); "
            "its log's freq. = each request type as often as in LinkBench's log; equal contr. = equal contribution, for BI and JOB, which "
            "define no frequencies: the geomean of the per-query costs, so a 2x win and a 2x loss on any query count the same.")

DATA = {"LDBC": "LDBC at SF3", "IMDb / JOB": "IMDb / JOB on the full data", "LinkBench": "LinkBench at 1 M nodes"}
DATA_NOTE = ", ".join(DATA.values()) + "."


def fmt(x):
    return f"{x:.2f}" if x < 10 else f"{x:.1f}" if x < 100 else f"{x:.0f}"


def load(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    v = {(r["benchmark"], r["frequencies"], r["cell"]): r for r in rows}
    for key, *_ in COLUMNS:
        if any((*key, c) not in v for c in F.CELLS):
            raise SystemExit(f"{path}: no rows for {key}")
    return v


def heat_map(v, out):
    fig, axes = plt.subplots(1, len(MODELS), figsize=(20.5, 5.8), constrained_layout=True, sharey=True)
    n, said = len(COLUMNS), []
    for ax, m in zip(axes, MODELS):
        grid = [[float(v[(*key, c)][f"{m}_ratio"]) for key, *_ in COLUMNS] for c in F.CELLS]
        grid = [row + [max(row[k] for k in HEADLINE)] for row in grid]
        im = ax.imshow(grid, cmap=SEQUENTIAL_CMAP, norm=LogNorm(1, VMAX), aspect="auto")
        for i, row in enumerate(grid):
            for k, x in enumerate(row):
                ax.text(k, i, fmt(x), ha="center", va="center", fontsize=8, color="white" if x > 18 else "black",
                        fontweight="bold" if k == n else "normal")
                if k < n and v[(*COLUMNS[k][0], F.CELLS[i])][f"{m}_best_cell"] == F.CELLS[i]:
                    ax.add_patch(plt.Rectangle((k - 0.5, i - 0.5), 1, 1, fill=False, ec="black", lw=1.8))
        ax.axvline(n - 0.5, color="white", lw=4)
        for x in HEADLINE[1:]:
            ax.axvline(x - 0.5, color="white", lw=1.5)
        for y in (2.5, 5.5):
            ax.axhline(y, color="white", lw=2)
        ax.set_xticks(range(n + 1))
        ax.set_xticklabels([f"{name}\n({freq})" for _, name, freq in COLUMNS] + [f"worst of\nthe {len(HEADLINE)}\nheadline\ncolumns"], fontsize=7)
        ax.set_yticks(range(len(F.CELLS)))
        ax.set_yticklabels([F.cell_label(c) for c in F.CELLS])
        ax.tick_params(length=0)
        k = min(range(len(F.CELLS)), key=lambda i: grid[i][-1])
        h = len(HEADLINE)
        claim = (f"{F.cell_label(F.CELLS[k])} is within {grid[k][-1]:.2f}x on all {h} benchmarks" if grid[k][-1] < 2
                 else f"every layout costs at least {grid[k][-1]:.2f}x the best on one of the {h} benchmarks")
        ax.set_title(f"{F.MODEL_LABEL[m]}\n{claim}")
        said.append(f"{m}: {claim}")
    axes[0].set_ylabel("uniform layout (properties-adjacency)")
    fig.colorbar(im, ax=axes, shrink=0.8, pad=0.01, label="cost over the benchmark's best uniform layout", ticks=[1, 2, 5, 10, 20, 50, 100], format="%gx")
    F.caption(fig, f"Each cell: the layout's cost on that benchmark over the cost of the benchmark's best uniform layout (framed, 1.00). "
              f"{freq_note(v)} The colour scale ends at {VMAX}x. {DATA_NOTE} {F.WEIGHTS} {F.SUM} {F.SOURCE}", width=200)
    F.save(fig, out, "cross_benchmark_regret", f"{len(MODELS)} models x {len(F.CELLS)} layouts x {n} columns; " + "; ".join(said))


def bars_by_layout(v, out, m="wops_bytes4k"):
    fig, ax = plt.subplots(figsize=(14, 4.8), constrained_layout=True)
    w = 0.8 / len(COLUMNS)
    for k, ((key, name, freq), col) in enumerate(zip(COLUMNS, COLUMN_COLOR)):
        ax.bar([i + (k - (len(COLUMNS) - 1) / 2) * w for i in range(len(F.CELLS))], [float(v[(*key, c)][f"{m}_ratio"]) for c in F.CELLS],
               w, label=f"{name.replace(chr(10), ' ').replace('- ', '')} ({freq})", color=col, edgecolor="white", linewidth=0.5)
    ax.set_yscale("log")
    ax.set_yticks([1, 2, 5, 10, 20]); ax.set_yticklabels(["1x", "2x", "5x", "10x", "20x"]); ax.minorticks_off()
    ax.set_ylim(0.9, 40)
    ax.axhline(1, color="black", lw=0.8)
    ax.set_xticks(range(len(F.CELLS))); ax.set_xticklabels([F.cell_label(c).replace("/", "/\n") for c in F.CELLS], fontsize=8)
    ax.set_xlabel("uniform layout (properties-adjacency)")
    ax.set_ylabel(f"cost over the benchmark's best\nuniform layout ({F.MODEL_LABEL[m]}, log)")
    ax.grid(True, axis="y", color="0.9", lw=0.6); ax.set_axisbelow(True)
    fig.legend(loc="outside upper center", ncol=4, frameon=False, fontsize=8, columnspacing=1.2)
    F.caption(fig, f"A bar at 1x: that layout is the benchmark's best uniform layout. {freq_note(v)} {DATA_NOTE} {F.WEIGHTS} {F.SUM} {F.SOURCE}", width=160)
    F.save(fig, out, "cross_benchmark_regret_bars", f"{len(F.CELLS)} layouts x {len(COLUMNS)} columns, {m}")


def bars_by_benchmark(v, out, scale, m="wops_bytes4k"):
    fig, axes = plt.subplots(1, len(COLUMNS), figsize=(2.6 * len(COLUMNS), 4.6), constrained_layout=True, sharey=(scale == "zero_to_one"))
    for ax, (key, name, freq) in zip(axes, COLUMNS):
        ratio = [float(v[(*key, c)][f"{m}_ratio"]) for c in F.CELLS]
        cost = [float(v[(*key, c)][m]) for c in F.CELLS]
        lo, hi = min(cost), max(cost)
        height = [(x - lo) / (hi - lo) for x in cost] if scale == "zero_to_one" else ratio
        base = 0.0 if scale == "zero_to_one" else 1.0
        for i, c in enumerate(F.CELLS):
            ax.bar(i, height[i] - base, 0.8, bottom=base, color=F.adj_color(c), hatch=F.PROPS_HATCH[F.settings(c)[0]], edgecolor="white", linewidth=0.5)
            best, worst = cost[i] == lo, cost[i] == hi
            ax.annotate("best" if best else f"{fmt(ratio[i])}x", (i, height[i]), textcoords="offset points", xytext=(0, 10 if best else 3), ha="center", va="bottom",
                        rotation=90, fontsize=7 if best or worst else 6, fontweight="bold" if best or worst else "normal",
                        color="black" if best or worst else "0.35")
            if best:
                ax.scatter([i], [base], marker="*", s=130, color="gold", edgecolor="black", linewidth=0.7, zorder=5, clip_on=False)
        ax.set_title(f"{name.replace(chr(10), ' ').replace('- ', '')}\n({freq})")
        ax.set_xticks(range(len(F.CELLS))); ax.set_xticklabels([F.cell_label(c) for c in F.CELLS], fontsize=6.5, rotation=90)
        ax.grid(True, axis="y", color="0.9", lw=0.6); ax.set_axisbelow(True)
        if scale == "zero_to_one":
            ax.set_ylim(0, 1.3); ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
        else:
            ax.set_ylim(1.0, max(ratio) * 1.25)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda y, _: f"{y:g}x"))
    how = ("0 = the benchmark's best uniform layout, 1 = its worst: (cost - best) / (worst - best)" if scale == "zero_to_one"
           else "cost over the benchmark's best uniform layout (each panel has its own y axis)")
    axes[0].set_ylabel(f"{F.MODEL_LABEL[m]}, scaled per benchmark\n(0 = its best layout, 1 = its worst)" if scale == "zero_to_one"
                       else f"cost over the benchmark's best\nuniform layout ({F.MODEL_LABEL[m]})")
    handles = [Patch(facecolor=format_color(k), edgecolor="white", label=f"{lab} adjacency") for lab, k in F.ADJ.values()]
    handles += [Patch(facecolor="0.75", hatch=h, edgecolor="white", label=f"{p.capitalize()} properties") for p, h in F.PROPS_HATCH.items()]
    fig.legend(handles=handles, loc="outside upper center", ncol=6, frameon=False, fontsize=8, columnspacing=1.2)
    F.caption(fig, f"One panel per benchmark, the nine uniform layouts in a fixed order (properties-adjacency). Bar height: {how}. The number on "
              f"a bar is its real ratio to the best (star). {freq_note(v)} {DATA_NOTE} {F.SUM} {F.SOURCE}", width=190)
    name = "cross_benchmark_regret_bars_by_benchmark" + ("" if scale == "zero_to_one" else "_ratio")
    F.save(fig, out, name, f"{len(COLUMNS)} panels x {len(F.CELLS)} layouts, {m}, scale {scale}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=os.path.join(F.ROOT, "cross-benchmark-frequencies", "cross_benchmark_frequencies.csv"))
    ap.add_argument("--out", default=os.path.join(F.ROOT, "cross-benchmark-frequencies", "plots"))
    ap.add_argument("--scale", default=SCALE, choices=("zero_to_one", "ratio_to_best"), help="the by-benchmark bars' y scale")
    ap.add_argument("--source", default=F.SOURCE, help="the caption's last sentences: which data")
    ap.add_argument("--pending", action="append", default=[], metavar="BENCHMARK",
                    help="a benchmark not captured yet (e.g. 'IMDb / JOB'): its columns are left out and named in the caption")
    a = ap.parse_args()
    global COLUMNS, HEADLINE, COLUMN_COLOR, DATA_NOTE
    F.SOURCE = a.source
    if a.pending:
        keep = [k for k, c in enumerate(COLUMNS) if c[0][0] not in a.pending]
        HEADLINE = [keep.index(k) for k in HEADLINE if k in keep]
        COLUMNS, COLUMN_COLOR = [COLUMNS[k] for k in keep], tuple(COLUMN_COLOR[k] for k in keep)
        DATA_NOTE = ", ".join(v for k, v in DATA.items() if k not in a.pending) + f". Pending (no capture yet): {', '.join(a.pending)}."
    apply_house_style(extralarge=1)
    plt.rcParams.update({"font.size": 9, "axes.titlesize": 9.5, "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8,
                         "legend.fontsize": 8, "hatch.linewidth": 0.6})
    v = load(a.data)
    heat_map(v, a.out)
    bars_by_layout(v, a.out)
    bars_by_benchmark(v, a.out, a.scale)


if __name__ == "__main__":
    main()
