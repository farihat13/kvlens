#!/usr/bin/env python3
"""The budget gain of one workload per chunk size of the Chunked setting (owner: mix_benefit): how many times fewer
weighted ops the mixed layout does than the best uniform layout that fits the same byte limit B. Top: per size, that
size's best uniform layout over that size's mix. Bottom: one common pair, the best uniform layout of ANY size that fits
over the lowest mix line of any size. Reads the fronts plot_equal_bytes_curve_chunk_sizes.py reads; CSVs only.

    python3 experiments/mix_benefit/plot_budget_gain_chunk_sizes.py --workload i01 \\
        --front "64 KB=<front dir>" --front "16 KB=<front dir>" --front "4 KB=<front dir>" \\
        [--more-front "LABEL=<front dir>"] [--out DIR] [--source TEXT]
--more-front: one more mix from another solve (its parts may each take their own chunk size): a second line in the
bottom panel, the same best uniform layout of any size over that mix, so the gap between the two bottom lines is what
a chunk size per part adds over picking one size.
Output: <out>/budget-gain-chunk-sizes_<workload dir>.{png,pdf} and budget_gain_chunk_sizes_<workload id>.csv
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

import figure_common as F
import plot_equal_bytes_curve_chunk_sizes as E
from kvcost.plotting.style import apply_house_style
from kvevaluator.output_tree import Tree

COMMON_COLOR = "0.15"


at, lowest = E.at, E.lowest


def named_text():
    """'1 / 2 / 3 / 5': the fronts' named limits (E.REGISTERED, set by the first front read)"""
    return " / ".join(f"{b:g}" for b in E.REGISTERED)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workload", default="i01", choices=sorted(E.WORKLOADS))
    ap.add_argument("--front", action="append", default=[], metavar="SIZE=DIR", help="a chunk size's front; repeatable, drawn in this order")
    ap.add_argument("--more-front", action="append", default=[], metavar="LABEL=DIR", help="one more mix from another front; repeatable")
    ap.add_argument("--out", default=str(Tree().summary("ldbc", "3", "chunk-size-report", "plots")))
    ap.add_argument("--source", default="Counts are deterministic: no error bars.", help="the caption's last sentences: which data")
    a = ap.parse_args()
    wdir, title = E.WORKLOADS[a.workload]
    sizes = [x.split("=", 1) for x in a.front]
    if not sizes or any(s not in E.COLORS for s, _ in sizes):
        raise SystemExit(f"--front SIZE=DIR with SIZE one of {', '.join(E.COLORS)}")
    apply_house_style(extralarge=1)
    plt.rcParams.update({"font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8})
    fig, (top, bot) = plt.subplots(2, 1, figsize=(7.6, 7.6), sharex=True, constrained_layout=True)
    refs, mixes, stairs, handles, notes, rows, table, more = set(), [], [], [], [], [], [], []
    for z, (size, d) in enumerate(sizes):
        ref, mix, stair, _ = E.load(d, wdir)
        refs.add(round(ref, 3))
        mixes.append([(b, o) for b, o, _ in mix])
        stairs.append(stair)
        ratio = [(b, x) for b, _, x in mix if x]
        E.step(top, ratio, color=E.COLORS[size], lw=2.0, zorder=3 + 0.1 * (len(sizes) - z))
        handles.append(Line2D([], [], color=E.COLORS[size], lw=2.0, label=f"Chunked = {size}"))
        reg = [at(ratio, lim) for lim in E.REGISTERED]
        k = max(range(len(ratio)), key=lambda i: ratio[i][1])
        table.append((size, E.COLORS[size], reg, ratio[k]))
        notes.append(f"{size}: " + " / ".join(f"{x:.2f}" for x in reg) + f"x at B = {named_text()}, peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}")
        rows += [{"workload_id": a.workload, "panel": "per chunk size", "line": size, "b": round(b, 6), "best_uniform_over_mix": round(x, 6)} for b, x in ratio]
    if len(refs) != 1:
        raise SystemExit(f"the fronts disagree on {E.BU}'s bytes (B = 1): {sorted(refs)}")
    for label, d in (x.split("=", 1) for x in a.more_front):
        ref, mix, _, _ = E.load(d, wdir)
        if round(ref, 3) not in refs:
            raise SystemExit(f"{label}: its B = 1 is not the other fronts'")
        full = E.named(label, d, wdir, E.OPTION_SET)
        ratio = [(b, lowest(stairs, b) / o) for b, o, _ in mix if lowest(stairs, b)]
        E.step(bot, ratio, color=E.MORE_COLOR, lw=2.0, zorder=4)
        k = max(range(len(ratio)), key=lambda i: ratio[i][1])
        more.append((full, [at(ratio, lim) for lim in E.REGISTERED], ratio[k]))
        handles.append(Line2D([], [], color=E.MORE_COLOR, lw=2.0, label=f"bottom: {full}, a mix whose parts each take their own chunk size"))
        notes.append(f"{full}, the best uniform of any size over it: " + " / ".join(f"{x:.2f}" for x in more[-1][1]) + f"x at B = {named_text()}, peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}")
        rows += [{"workload_id": a.workload, "panel": "common pair", "line": label, "b": round(b, 6), "best_uniform_over_mix": round(x, 6)} for b, x in ratio]
    # the common pair, at every B where any line steps
    bs = sorted({b for line in mixes + stairs for b, _ in line})
    common = [(b, lowest(stairs, b) / lowest(mixes, b)) for b in bs if lowest(stairs, b) and lowest(mixes, b)]
    E.step(bot, common, color=COMMON_COLOR, lw=2.2, zorder=3)
    k = max(range(len(common)), key=lambda i: common[i][1])
    reg = [at(common, lim) for lim in E.REGISTERED]
    handles.append(Line2D([], [], color=COMMON_COLOR, lw=2.2, label="bottom: the lowest mix of one chunk size"))
    notes.append("common pair: " + " / ".join(f"{x:.2f}" for x in reg) + f"x, peak {common[k][1]:.1f}x at B = {common[k][0]:.2f}")
    rows += [{"workload_id": a.workload, "panel": "common pair", "line": "any size", "b": round(b, 6), "best_uniform_over_mix": round(x, 6)} for b, x in common]
    # one y range for both panels, tall enough that each panel's lines stay below its text block in the top right
    ytop, vmax = 17.0, 1.0
    for ax, nlines in ((top, 1 + len(table)), (bot, 1 + len(more))):
        v = max(max(ln.get_ydata(), default=1.0) for ln in ax.get_lines())
        room = 1 - 0.035 - 0.05 * nlines - 0.03
        ytop, vmax = max(ytop, 0.85 * (v / 0.85) ** (1 / room)), max(vmax, v)
    for ax in (top, bot):
        for lim in E.REGISTERED:
            ax.axvline(lim, color="0.6", lw=0.8, ls=":", zorder=1)
        ax.axhline(1.0, color="black", lw=1.0, ls=":", zorder=1)
        ax.grid(True, which="major", color="0.9", lw=0.6)
        ax.set_yscale("log")
        ax.set_ylim(0.85, ytop)
        ticks = [t for t in (1, 2, 4, 8, 12, 25, 50, 100) if t <= vmax * 1.3 or t <= 2]
        ax.set_yticks(ticks)
        ax.set_yticklabels([f"{t}x" for t in ticks])
        ax.minorticks_off()
    top.set_title(f"{title}\nEach chunk size against itself: its best uniform layout over its own mix", fontsize=9.5)
    top.set_ylabel("best uniform layout that fits:\nits weighted ops over the mix's (log)")
    top.text(0.985, 0.965, f"at B = {named_text()}; the peak", transform=top.transAxes, ha="right", va="top", fontsize=7, color="0.2")
    for i, (size, color, reg_s, peak) in enumerate(table):
        top.text(0.985, 0.915 - 0.05 * i, f"{size}: " + " / ".join(f"{x:.2f}" for x in reg_s) + f"x; {peak[1]:.1f}x at B = {peak[0]:.2f}",
                 transform=top.transAxes, ha="right", va="top", fontsize=7, color=color, fontweight="bold")
    bot.set_title("One common pair: the best uniform layout of any chunk size over the lowest mix of any chunk size" if not more else
                  "The best uniform layout of any chunk size,\nover the lowest mix of one size and over a mix with a size per part", fontsize=9.5)
    bot.set_ylabel("best uniform layout of any size:\nits weighted ops over the lowest mix's (log)")
    bot.text(0.985, 0.965, f"the lowest mix of one size, at B = {named_text()}: " + " / ".join(f"{x:.2f}" for x in reg) + f"x; peak {common[k][1]:.1f}x at B = {common[k][0]:.2f}",
             transform=bot.transAxes, ha="right", va="top", fontsize=7, color=COMMON_COLOR, fontweight="bold")
    for i, (full, reg_m, peak) in enumerate(more):
        bot.text(0.985, 0.915 - 0.05 * i, f"{full}: " + " / ".join(f"{x:.2f}" for x in reg_m) + f"x; peak {peak[1]:.1f}x at B = {peak[0]:.2f}",
                 transform=bot.transAxes, ha="right", va="top", fontsize=7, color=E.MORE_COLOR, fontweight="bold")
    bot.set_xlim(E.XMIN, E.XMAX)
    bot.set_xlabel(f"byte limit B, x {E.BU}'s bytes")
    fig.legend(handles=handles, loc="outside upper center", ncol=3 if not more else 1, frameon=False, columnspacing=1.4)
    F.caption(fig, "The budget gain: at a byte limit B, the weighted ops of the best uniform layout that fits, over the mixed layout's (per type and "
              "direction). The two panels divide different pairs. Top: one line per chunk size of the Chunked setting, both layouts at that size. "
              "Bottom: both free to take any of the drawn sizes, so a mix is never credited for a chunk size a uniform layout could also take"
              + ("; the second line divides the same uniform layout into a mix whose parts each take their own chunk size. " if more else ". ") +
              "A line holds its left value between two limits and starts where a uniform layout first fits. A workload's cost adds up each "
              f"query's cost x its frequency (LDBC's frequencies). {F.WEIGHTS} {a.source}", width=128)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f"budget_gain_chunk_sizes_{a.workload}.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    F.save(fig, a.out, f"budget-gain-chunk-sizes_{wdir}", "; ".join(notes))


if __name__ == "__main__":
    main()
