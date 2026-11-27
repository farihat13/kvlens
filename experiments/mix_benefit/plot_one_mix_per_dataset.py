#!/usr/bin/env python3
"""Draw one mixed layout for the whole LDBC dataset: per workload (W01-W15, I01, I02, BI), a layout's cost over that workload's
own best uniform layout: the ONE mix solved for all of them, the best fixed uniform layout, the other eight uniform
layouts, and the workload's own best mix. Reads saved CSVs only.

    python3 experiments/mix_benefit/one_mix_per_dataset.py          (writes one_mix_by_workload.csv, one_mix_layouts.csv)
    python3 experiments/mix_benefit/plot_one_mix_per_dataset.py [--root <snapshot>/sf3] [--draft]
        -> <out>/summary/ldbc/sf3/mix-benefit/one-mix-per-dataset/plots/one_mix_per_dataset.{png,pdf}
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

import figure_common as F
from kvcost.plotting.style import apply_house_style

SUB = "one-mix-per-dataset"
SET = "per type and direction"                 # the headline option set
MIX = "one mix, worst case"                    # the headline objective
MIX_COLOR = "#1f77b4"                          # the one mixed layout (no format's palette colour)
TICK = {"i01": "I01\n(LDBC\nfreq.)", "i02": "I02\n(LDBC\nfreq.,\nreads)", "bi": "BI\n(once\neach)"}


def read(*path):
    with open(os.path.join(*path)) as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=F.ROOT, help=f"the dir holding {SUB}/")
    ap.add_argument("--out", default=None, help=f"default <root>/{SUB}/plots")
    ap.add_argument("--draft", action="store_true", help="mark the figure DRAFT in its title and file name")
    ap.add_argument("--source", default="SF3. Counts are deterministic: no error bars.",
                    help="the caption's last sentences: which data")
    a = ap.parse_args()
    rows = [r for r in read(a.root, SUB, "one_mix_by_workload.csv") if r["option_set"] == SET]
    n_label = next(r["workloads"] for r in rows if r["workloads"].startswith("18"))
    rows = [r for r in rows if r["workloads"] == n_label]
    layouts = [r for r in read(a.root, SUB, "one_mix_layouts.csv") if r["workloads"] == n_label]
    head = {r["layout"]: r for r in layouts if r["option_set"] == SET}
    per_type = {r["layout"]: r for r in layouts if r["option_set"] == "per type"}
    best_layout = min((r for r in head.values() if r["layout_kind"] == "uniform"), key=lambda r: float(r["max_r_w"]))["layout"]
    best_cell = F.cells.short_names(best_layout)    # one_mix_layouts.csv names a uniform layout 'Condensed/Split'; the figure 'Co-Sp'
    if best_cell == best_layout:
        raise SystemExit(f"one_mix_layouts.csv: the best uniform layout {best_layout!r} is not a 'Props/Adjacency' name")
    ids = list(dict.fromkeys(r["workload_id"] for r in rows))
    x = {w: i for i, w in enumerate(ids)}
    by = {}
    for r in rows:
        by.setdefault(r["layout"], {})[r["workload_id"]] = r
    cell_of = lambda key: F.cell_label(key)

    apply_house_style(extralarge=1)
    fig, ax = plt.subplots(figsize=(12.5, 5.6), constrained_layout=True)
    for lay, per in by.items():                                                   # the uniform cells: grey dots
        if lay.startswith("props_") and cell_of(lay) != best_cell:
            ax.scatter([x[w] for w in per], [float(r["r_w"]) for r in per.values()], s=14, color="0.72", zorder=2, linewidths=0)
    cell_key = next(lay for lay in by if lay.startswith("props_") and cell_of(lay) == best_cell)
    ax.scatter([x[w] for w in by[cell_key]], [float(r["r_w"]) for r in by[cell_key].values()], s=58, marker="D", facecolors="white",
               edgecolors=F.adj_color(cell_key), linewidths=1.6, zorder=3)
    mix = by[MIX]
    ax.scatter([x[w] for w in mix], [float(r["r_w"]) / float(r["cost_over_own_best_mix"]) for r in mix.values()], s=120, marker="_",
               color="black", linewidths=1.6, zorder=4)                           # the workload's own best mix
    ax.scatter([x[w] for w in mix], [float(r["r_w"]) for r in mix.values()], s=62, color=MIX_COLOR, edgecolors="black",
               linewidths=0.6, zorder=5)
    for w, r in mix.items():                                                      # the mix's value above each point over 1.05
        v = float(r["r_w"])
        if v > 1.05:
            ax.annotate(f"{v:.2f}x", (x[w], v), textcoords="offset points", xytext=(0, 7), ha="center", fontsize=8)
    ax.axhline(1, color="black", lw=0.9, zorder=1)
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_xticks(range(len(ids)), [TICK.get(w, w.upper()) for w in ids])
    ax.set_xlim(-0.6, len(ids) - 0.4)
    ax.set_ylabel("ops + bytes / 4 KB, over the\nworkload's best uniform layout", fontsize=10)
    ax.tick_params(axis="both", labelsize=9.5)
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi * 2.6)                                                     # head room for the legend
    ax.grid(axis="y", alpha=0.3)
    m, c = head[MIX], head[best_layout]
    title = ("One mixed layout for the whole LDBC dataset (17 workloads and BI, SF3):\n"
             f"at most {float(m['max_r_w']):.2f}x a workload's own best uniform layout, where the best fixed uniform layout "
             f"({best_cell}) reaches {float(c['max_r_w']):.1f}x")
    ax.set_title(("DRAFT. " if a.draft else "") + title, loc="left", fontsize=10.5, fontweight="bold")
    handles = [Line2D([], [], lw=0, marker="o", ms=8, mfc=MIX_COLOR, mec="black", label="the one mixed layout, solved for all 18 (worst case)"),
               Line2D([], [], lw=0, marker="D", ms=7, mfc="white", mec=F.adj_color(cell_key), mew=1.6, label=f"the best fixed uniform layout: {best_cell}"),
               Line2D([], [], lw=0, marker="o", ms=4, mfc="0.72", mec="0.72", label="the other eight uniform layouts"),
               Line2D([], [], lw=0, marker="_", ms=11, mec="black", mew=1.6, label="the workload's own best mixed layout")]
    ax.legend(handles=handles, loc="upper left", frameon=False, fontsize=9, ncol=2)
    pt = per_type.get(MIX)
    F.caption(fig, f"One point per workload and layout. 1x = the cheapest of the nine uniform layouts for THAT workload. The one mix sets a "
              f"layout per type and direction and minimizes the largest ratio over the 18 workloads; it is at or under 1x on "
              f"{m['workloads_at_or_under_1.00']} of {m['n_workloads']} and its mean ratio is {float(m['mean_r_w']):.2f} "
              f"({best_cell}: {float(c['mean_r_w']):.2f})."
              + (f" Set per type only, the one mix reaches {float(pt['max_r_w']):.2f}x at most." if pt else "")
              + " Queries are counted with each workload's frequencies (I01, I02: LDBC frequencies; W01-W15: stated; BI: every "
              "query once each, in-sample). Every cost is composed from the nine uniform captures; nothing is built. "
              + F.WEIGHTS + " " + a.source, width=175)
    F.save(fig, a.out or os.path.join(a.root, SUB, "plots"), ("draft_" if a.draft else "") + "one_mix_per_dataset",
           f"{len(ids)} workloads; the one mix at most {float(m['max_r_w']):.2f}x, {best_cell} {float(c['max_r_w']):.1f}x")


if __name__ == "__main__":
    main()
