#!/usr/bin/env python3
"""LinkBench's whole request log, measured steady time against the two count models, two figures:
  linkbench-whole-log-time-vs-models.png     the nine layouts: the steady time of a pass, weighted ops (wops) and
                                             ops + bytes / 4,096 (wops_bytes4k), each relative to Split/Split
  linkbench-whole-log-chunk-64k-vs-4k.png    the five layouts with a Chunked side, at 64 KB and at 4 KB chunks, the same
                                             three measures
Reads steady_whole_log_table.py's CSV (the 14 cells). LinkBench's reference here is Split/Split (the table's own).

    python3 experiments/linkbench/plot_whole_log_time_vs_models.py --table <steady_whole_log_table.csv> --out DIR"""
import argparse
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost.settings import name_of as pair_name  # ('Co-Sp': a cell's short name)

REF = "props_split_adj_split"
PANELS = (("time", "measured: steady time of a pass", "#d95f02"), ("wops", "model: weighted ops (wops)", "#7570b3"),
          ("wops_bytes4k", "model: ops + bytes / 4,096 (wops_bytes4k)", "#1b9e77"))
FOUR_KB = {"props_split_adj_chunked_c4k": "props_split_adj_chunked", "props_condensed_adj_chunked_c4k": "props_condensed_adj_chunked",
           "props_chunked_c4k_adj_split": "props_chunked_adj_split", "props_chunked_c4k_adj_condensed": "props_chunked_adj_condensed",
           "props_chunked_c4k_adj_chunked_c4k": "props_chunked_adj_chunked"}
NOTE = ("Steady, 102 MB cache (direct I/O); one pass = every read request of the log once, in the log's order; the time is the "
        "median total of the three measured passes. Counts over the same requests (deterministic). A layout is named "
        "properties/adjacency. Measured on the build of the n1m re-measure.")


def style(ax, title):
    ax.set_yscale("log")
    ax.set_ylim(0.4, 22)
    ax.set_yticks([0.5, 1, 2, 5, 10], ["0.5x", "1x", "2x", "5x", "10x"])
    ax.minorticks_off()
    ax.axhline(1, color="0.5", lw=0.8, ls=":")
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.grid(axis="y", alpha=0.3)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--table", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    t = {}
    with open(a.table) as f:
        for r in csv.DictReader(f):
            t[r["cell"]] = {"time": float(r["pass_total_s"]), "wops": float(r["wops"]), "wops_bytes4k": float(r["wops_bytes4k"])}
    nine = [c for c in t if c not in FOUR_KB]
    if len(nine) != 9 or sorted(c for c in t if c in FOUR_KB) != sorted(FOUR_KB):
        sys.exit(f"the table holds {len(nine)} grid cells and {len(t) - len(nine)} 4 KB cells; 9 and 5 expected")
    ref = t[REF]
    os.makedirs(a.out, exist_ok=True)

    fig, axes = plt.subplots(1, 3, figsize=(15, 6.4), sharey=True, gridspec_kw={"wspace": 0.07})
    for ax, (key, title, color) in zip(axes, PANELS):
        for x, c in enumerate(nine):
            v = t[c][key] / ref[key]
            lab = (f"{ref[key]:.2f} s" if key == "time" else f"{ref[key] / 1e3:,.0f} K") if c == REF else f"{v:.2f}x"
            ax.bar(x, v, 0.8, color=color)
            ax.text(x, v * 1.06, lab, ha="center", va="bottom", rotation=90, fontsize=9.5, fontweight="bold" if c == REF else None)
        ax.set_xticks(range(9), [pair_name(c) for c in nine], rotation=90, fontsize=10)
        style(ax, title)
    axes[0].set_ylabel("relative to Split/Split (log)", fontsize=11)
    fig.suptitle("LinkBench (1 M nodes): the whole request log's 68,994 reads on the nine layouts, measured time against the two count models",
                 fontsize=13, y=0.975)
    fig.text(0.5, 0.012, NOTE + "\nBar label: Split/Split's absolute value (bold), every other bar's ratio to it (LinkBench's reference here is "
             "Split/Split). Chunked = 64 KB chunks.", ha="center", va="bottom", fontsize=8.5, wrap=True)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.87, bottom=0.36)
    fig.savefig(os.path.join(a.out, "linkbench-whole-log-time-vs-models.png"), dpi=130)
    plt.close(fig)

    col = {"64 KB": "#8c8c8c", "4 KB": "#1b9e77"}
    best = min(nine, key=lambda c: t[c]["time"])
    fig, axes = plt.subplots(1, 3, figsize=(15, 6.2), sharey=True, gridspec_kw={"wspace": 0.07})
    for ax, (key, title, _) in zip(axes, PANELS):
        for x, (c4, c64) in enumerate(FOUR_KB.items()):
            for i, (size, c) in enumerate((("64 KB", c64), ("4 KB", c4))):
                v = t[c][key] / ref[key]
                ax.bar(x + (i - 0.5) * 0.4, v, 0.38, color=col[size])
                ax.text(x + (i - 0.5) * 0.4, v * 1.06, f"{v:.2f}x", ha="center", va="bottom", rotation=90, fontsize=9.5)
        v = t[best][key] / ref[key]
        ax.axhline(v, color="#d95f02", lw=1.2, ls="--")
        ax.set_xticks(range(len(FOUR_KB)), [pair_name(c).replace("/", "/\n") for c in FOUR_KB.values()], fontsize=9.5)
        style(ax, title)
    axes[0].set_ylabel("relative to Split/Split (log)", fontsize=11)
    fig.legend(handles=[Patch(color=col["64 KB"], label="Chunked = 64 KB chunks"), Patch(color=col["4 KB"], label="Chunked = 4 KB chunks"),
                        Line2D([], [], color="#d95f02", lw=1.2, ls="--", label=f"{pair_name(best)}, the fastest of the nine")],
               loc="lower center", bbox_to_anchor=(0.5, 0.115), ncol=3, frameon=False, fontsize=10)
    fig.suptitle("LinkBench (1 M nodes), the whole log's 68,994 reads: the five layouts with a Chunked side, at 64 KB and at 4 KB chunks",
                 fontsize=13, y=0.975)
    fig.text(0.5, 0.012, NOTE + f"\n1x = Split/Split ({ref['time']:.2f} s a pass), LinkBench's reference here.",
             ha="center", va="bottom", fontsize=8.5, wrap=True)
    fig.subplots_adjust(left=0.06, right=0.99, top=0.87, bottom=0.26)
    fig.savefig(os.path.join(a.out, "linkbench-whole-log-chunk-64k-vs-4k.png"), dpi=130)
    print(f"wrote 2 figures to {a.out}; 9 + 5 cells; Split/Split {ref['time']:.2f} s a pass; fastest of the nine {pair_name(best)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
