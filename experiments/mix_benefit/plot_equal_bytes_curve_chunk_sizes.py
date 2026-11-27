#!/usr/bin/env python3
"""The equal-bytes curve of one workload with one mix line per chunk size of the Chunked setting (owner: mix_benefit).
Top: weighted ops against the byte limit B; per size, the mixed layout per type and direction as a step line, the best
uniform layout of that size that fits as a thin dashed staircase of the same colour, and the size's Chunked/Chunked
layout as a point. Bottom: per size, the best uniform layout that fits over the mix. Reads saved CSVs only.

    python3 experiments/mix_benefit/plot_equal_bytes_curve_chunk_sizes.py --workload i01 \\
        --front "64 KB=<front dir>" --front "16 KB=<front dir>" --front "4 KB=<front dir>" \\
        [--more-front "LABEL=<front dir>"] [--out DIR] [--source TEXT]
A front dir is equal_bytes_front.py's --root (one folder per workload: equal_bytes_front.csv, uniform_layouts.csv).
--more-front: one more mix line from another solve (a mixed layout whose parts may each take their own chunk size,
layout_choice/chunk_size_front.py); in the bottom panel it is divided into the best uniform layout of ANY drawn size
that fits. Its legend says whether every point is the exact optimum (the front's chunk_size_front.csv).
Output: <out>/equal-bytes-curve_<workload dir>.{png,pdf}
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

import figure_common as F
from kvcost.plotting.style import apply_house_style
from kvcost.plotting.formatting import number_to_human
import sys
from kvcost.layout_cost import settings as cells  # (the cells' short names; B's unit)
from kvcost.layout_search.front import ref_bytes_of
from kvcost.layout_cost.settings import name_of  # (load() has a local named cells)
from kvevaluator.layouts import family_name
from kvevaluator.output_tree import Tree

BU = cells.name_of(cells.B_UNIT)    # set again by the first front read (Co-Sp in a file in Co-Sp units)
CC = family_name("chunked", "chunked")       # ChK-ChK: the chunk size stands beside it

WORKLOADS = {"i01": ("i01-rw-ldbc-mix", "I01: LDBC Interactive, LDBC frequencies"), "i02": ("i02-r-ldbc-mix-reads", "I02: the same, reads only")}
OPTION_SET = "per type and direction"
# the named limits and the x range, by the fronts' unit: settings.NAMED_BUDGETS x Sp-Co's bytes; a front in
# Co-Sp units is in Co-Sp's bytes, with the limits fixed. The x range covers the same bytes on I01 either way.
BY_UNIT = {cells.B_UNIT: (cells.NAMED_BUDGETS, 0.5, 13.0), cells.OLD_B_UNIT: ((0.5, 1.0, 2.0, 3.0), 0.12, 3.15)}
REGISTERED, XMIN, XMAX = BY_UNIT[cells.B_UNIT]
UNIT = None                            # the fronts' B unit: one per figure
# study-local: a chunk size has no repo-wide color; 64 KB is black, as the mix line of equal_bytes_curve.png
COLORS = {"256 KB": "#66a61e", "128 KB": "#e7298a", "64 KB": "black", "16 KB": "#d95f02", "4 KB": "#1b9e77", "1 KB": "#7570b3"}
MORE_COLOR = "#1f78b4"      # the added mix (--more-front), the same in the three chunk-size figures
CHUNKED = "props_chunked_adj_chunked"


def read(*path):
    with open(os.path.join(*path)) as f:
        return list(csv.DictReader(f))


def load(front_dir, wdir):
    """(B = 1 in bytes, the mix line [(B, ops, best uniform over mix or None)], the staircase [(B, ops)], Chunked/Chunked (B, ops))"""
    global UNIT, BU, REGISTERED, XMIN, XMAX
    rows = read(front_dir, wdir, "equal_bytes_front.csv")
    ref, unit = ref_bytes_of(rows)       # B = 1 in bytes: Sp-Co's (Co-Sp's in a file in Co-Sp units)
    if UNIT is None:
        UNIT, BU = unit, name_of(unit)
        REGISTERED, XMIN, XMAX = BY_UNIT[unit]
    elif unit != UNIT:
        raise SystemExit(f"{front_dir}/{wdir}: B in {name_of(unit)}'s bytes, the first front's in {BU}'s: one unit per figure")
    mix = sorted((float(r["cap_bytes"]) / ref, float(r["total_ops"]), float(r["ops_single_over_mix"]) if r["ops_single_over_mix"] else None)
                 for r in rows if r["option_set"] == OPTION_SET and r["option"] == "fewest_ops_within_cap")
    if not mix:
        raise SystemExit(f"{front_dir}/{wdir}: no '{OPTION_SET}' rows")
    path = os.path.join(front_dir, wdir, "uniform_layouts.csv")
    if os.path.exists(path):
        cells = {r["cell"]: (float(r["total_bytes"]) / ref, float(r["total_ops"])) for r in read(path)}
    else:                                   # an older front: its 'single' rows name the best uniform layout at each limit
        cells = {r["option"]: (float(r["total_bytes"]) / ref, float(r["total_ops"])) for r in rows if r["option_set"] == "single"}
    stair, best = [], None
    for c in sorted(cells, key=lambda c: cells[c]):
        if best is None or cells[c][1] < best:
            best = cells[c][1]
            stair.append((cells[c][0], best))
    if CHUNKED not in cells:
        raise SystemExit(f"{front_dir}/{wdir}: no Chunked/Chunked layout")
    return ref, mix, stair, cells[CHUNKED]


def exact_note(front_dir, wdir, option_set):
    """"exact" or "best found; exact at N of M limits" for an added front's line, from its chunk_size_front.csv (the
    "any size" line's proven_optimum per limit); "" when the front has no such file"""
    path = os.path.join(front_dir, wdir, "chunk_size_front.csv")
    if not os.path.exists(path):
        return ""
    flags = [r["proven_optimum"] == "yes" for r in read(path) if r["unit_set"] == option_set and r["line"] == "any size"]
    if not flags:
        raise SystemExit(f"{path}: no 'any size' rows for '{option_set}'")
    return "exact" if all(flags) else f"best found; exact at {sum(flags)} of {len(flags)} limits"


def named(label, front_dir, wdir, option_set):
    note = exact_note(front_dir, wdir, option_set)
    return f"{label} ({note})" if note else label


def at(pts, b):
    """a step line's value at b: the last point at or left of b (None before the first)"""
    v = None
    for x, y in pts:
        if x <= b + 1e-9:
            v = y
    return v


def lowest(lines, b):
    vals = [v for v in (at(p, b) for p in lines) if v is not None]
    return min(vals) if vals else None


def step(ax, pts, **kw):
    ax.step([p[0] for p in pts] + [XMAX], [p[1] for p in pts] + [pts[-1][1]], where="post", **kw)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workload", default="i01", choices=sorted(WORKLOADS))
    ap.add_argument("--front", action="append", default=[], metavar="SIZE=DIR", help="a chunk size's front (64 KB, 16 KB, 4 KB, 1 KB); "
                    "repeatable, drawn in this order")
    ap.add_argument("--more-front", action="append", default=[], metavar="LABEL=DIR", help="one more mix line from another front; repeatable")
    ap.add_argument("--out", default=str(Tree().summary("ldbc", "3", "chunk-size-report", "plots")))
    ap.add_argument("--source", default="Counts are deterministic: no error bars.", help="the caption's last sentences: which data")
    a = ap.parse_args()
    wdir, title = WORKLOADS[a.workload]
    sizes = [x.split("=", 1) for x in a.front]
    if not sizes or any(s not in COLORS for s, _ in sizes):
        raise SystemExit(f"--front SIZE=DIR with SIZE one of {', '.join(COLORS)}")
    apply_house_style(extralarge=1)
    plt.rcParams.update({"font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8, "legend.fontsize": 8})
    fig, (top, bot) = plt.subplots(2, 1, figsize=(7.6, 8.4), sharex=True, constrained_layout=True, gridspec_kw={"height_ratios": [1.25, 1]})
    refs, notes, table, handles, stairs = set(), [], [], [], []
    for z, (size, d) in enumerate(sizes):
        ref, mix, stair, (cb, cops) = load(d, wdir)
        refs.add(round(ref, 3))
        stairs.append(stair)
        color = COLORS[size]
        step(top, mix, color=color, lw=2.0, zorder=3 + 0.1 * (len(sizes) - z))
        step(top, stair, color=color, lw=1.0, ls="--", zorder=2)
        top.scatter([cb], [cops], s=58, marker="s", color=color, edgecolor="black", linewidth=0.7, zorder=5)
        top.annotate(f"{CC}, {size}", (cb, cops), textcoords="offset points", xytext=(7, 6), ha="left", fontsize=6.5, color=color, zorder=6,
                     bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.8))   # above its own flat line; no box (it would hide a line)
        ratio = [(b, x) for b, _, x in mix if x]
        step(bot, ratio, color=color, lw=2.0, zorder=3 + 0.1 * (len(sizes) - z))
        reg_at = [next((x for b, x in ratio if abs(b - lim) < 1e-9), None) for lim in REGISTERED]
        ops_at = [next((o for b, o, _ in mix if abs(b - lim) < 1e-9), None) for lim in REGISTERED]
        if None in reg_at or None in ops_at:
            raise SystemExit(f"{size}: a registered limit is not solved, or no uniform layout fits it")
        k = max(range(len(ratio)), key=lambda i: ratio[i][1])
        table.append((size, color, reg_at, ratio[k]))
        notes.append(f"{size}: {len(mix)} limits, mix ops at B = {' / '.join(f'{b:g}' for b in REGISTERED)} " + " / ".join(number_to_human(o) for o in ops_at)
                     + "; best uniform over mix " + " / ".join(f"{x:.2f}" for x in reg_at) + f"x, peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}; "
                     f"{CC} at B = {cb:.2f}, {number_to_human(cops)} ops")
        handles.append(Line2D([], [], color=color, lw=2.0, label=f"Chunked = {size}: the mixed layout, per type and direction"))
    if len(refs) != 1:
        raise SystemExit(f"the fronts disagree on {BU}'s bytes (B = 1): {sorted(refs)}")
    for label, d in (x.split("=", 1) for x in a.more_front):
        ref, mix, _, _ = load(d, wdir)
        if round(ref, 3) not in refs:
            raise SystemExit(f"{label}: its B = 1 is not the other fronts'")
        full = named(label, d, wdir, OPTION_SET)
        step(top, mix, color=MORE_COLOR, lw=2.0, ls="-", zorder=4)
        # the fair pair: the best uniform layout of ANY drawn size that fits, over this mix
        ratio = [(b, lowest(stairs, b) / o) for b, o, _ in mix if lowest(stairs, b)]
        step(bot, ratio, color=MORE_COLOR, lw=2.0, zorder=4)
        k = max(range(len(ratio)), key=lambda i: ratio[i][1])
        reg = [at(ratio, lim) for lim in REGISTERED]
        table.append((f"{label} (any-size uniform)", MORE_COLOR, reg, ratio[k]))
        handles.append(Line2D([], [], color=MORE_COLOR, lw=2.0, label=f"{full}: the mixed layout; below, the best uniform layout of any size over it"))
        notes.append(f"{full}: {len(mix)} limits, " + " / ".join(number_to_human(at([(b, o) for b, o, _ in mix], lim)) for lim in REGISTERED)
                     + f" ops at B = {' / '.join(f'{b:g}' for b in REGISTERED)}; the best uniform of any size over it " + " / ".join(f"{x:.2f}" for x in reg)
                     + f"x, peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}")
    handles += [Line2D([], [], color="0.3", lw=1.0, ls="--", label="the best uniform layout of that chunk size that fits"),
                Line2D([], [], color="0.3", lw=0, marker="s", ms=6, mec="black", label=f"{CC} at that chunk size")]
    for ax in (top, bot):
        for lim in REGISTERED:
            ax.axvline(lim, color="0.6", lw=0.8, ls=":", zorder=1)
        ax.grid(True, which="major", color="0.9", lw=0.6)
    top.set_yscale("log")
    top.set_ylim(top.get_ylim()[0] / 1.15, top.get_ylim()[1])       # room for the lowest point's label
    top.set_title(title)
    top.set_ylabel("weighted ops of the workload (log)")
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: number_to_human(v)))
    top.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: number_to_human(v) if str(v)[0] in "25" else ""))
    bot.axhline(1.0, color="black", lw=1.0, ls=":", zorder=1)
    bot.set_yscale("log")
    bot.set_ylim(0.85, 17)
    bot.set_yticks([1, 2, 4, 8, 12])
    bot.set_yticklabels(["1x", "2x", "4x", "8x", "12x"])
    bot.minorticks_off()
    bot.set_xlim(XMIN, XMAX)
    bot.set_xlabel(f"byte limit B, x {BU}'s bytes")
    bot.set_ylabel("best uniform layout that fits:\nits weighted ops over the mix's (log)")
    # the bottom row's values, written in the top panel's free corner
    top.text(0.985, 0.965, f"best uniform layout over the mix\nat B = {' / '.join(f'{b:g}' for b in REGISTERED)}; its peak", transform=top.transAxes, ha="right", va="top", fontsize=7, color="0.2",
             bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none", alpha=0.85), zorder=7)
    for i, (size, color, reg_at, peak) in enumerate(table):
        top.text(0.985, 0.875 - 0.05 * i, f"{size}: " + " / ".join(f"{x:.2f}" for x in reg_at) + f"x; {peak[1]:.1f}x at B = {peak[0]:.2f}",
                 transform=top.transAxes, ha="right", va="top", fontsize=7, color=color, fontweight="bold",
                 bbox=dict(boxstyle="square,pad=0.15", fc="white", ec="none", alpha=0.85), zorder=7)
    fig.legend(handles=handles, loc="outside upper center", ncol=1 if a.more_front else 2, frameon=False, columnspacing=1.4)
    F.caption(fig, "One mix line per chunk size of the Chunked setting: every part takes Split, Condensed or Chunked at that one size. A workload's "
              "cost adds up each query's cost x its frequency (LDBC's frequencies). Each step of a line is one byte limit, solved exactly; between "
              "two limits the line holds the left value. B is in units of " + BU + "'s bytes, which do not depend on the chunk size. Bottom: "
              "each size's mix against the best uniform layout of the same size that fits" + ("; the added mix, whose parts each take their own chunk size, against the best "
              "uniform layout of any drawn size that fits" if a.more_front else "") + ". Every size's grid is priced by one rule: per store and "
              f"query, the plan picked on the held-out params. {cells.NAMES_NOTE} {F.WEIGHTS} {a.source}", width=128)
    F.save(fig, a.out, f"equal-bytes-curve_{wdir}", "; ".join(notes))


if __name__ == "__main__":
    main()
