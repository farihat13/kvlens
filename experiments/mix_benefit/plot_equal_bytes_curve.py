#!/usr/bin/env python3
"""Draw way 1: at the same bytes, how many times fewer weighted ops does a mixed layout need than the best uniform
layout that fits? One column per workload (I01, I02). Top: weighted ops against the byte limit B, the composed mix as
a step line, the best uniform layout that fits as a dashed staircase, the nine uniform layouts as points. Bottom:
their ratio, with the registered limits, the peak and the built mixes marked. Reads saved CSVs only.

    python3 experiments/layout_choice/equal_bytes_front.py ...   (the search side writes equal-bytes-front/)
    python3 experiments/mix_benefit/plot_equal_bytes_curve.py [--root <snapshot>/sf3]
        -> <out>/summary/ldbc/sf3/mix-benefit/equal-bytes-front/plots/equal_bytes_curve.{png,pdf}
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
from kvcost.plotting.style import apply_house_style, format_color
from kvcost.plotting.formatting import number_to_human
from kvcost.layout_cost.settings import B_UNIT, NAMED_BUDGETS, OLD_B_UNIT
from kvcost.layout_search.front import ref_bytes_of, unit_text, x_of
from kvevaluator.layouts import SETTINGS

# (the front's dir, the workload's id in joint_regret.csv, the panel title, its built-mix files)
WORKLOADS = [
    ("i01-rw-ldbc-mix", "i01", "I01: LDBC Interactive, LDBC frequencies", ("built_points.csv", "built_points_after_curve.csv")),
    ("i02-r-ldbc-mix-reads", "i02", "I02: the same, reads only", ("built_points.csv",)),
    ("w04-rw-profile-editing", "w04", "W04: profile editing", ()),
    ("w15-r-control-person-only", "w15", "W15: Person only", ()),
]
DEFAULT = "i01,i02"
TUNED = ("i01", "i02")                 # the workloads whose labels are placed by hand (LABEL_AT)
OPTION_SET = "per type and direction"
PER_TYPE = "per type"                  # the thinner line: what a layout per type alone reaches
PER_TYPE_COLOR = "#1f77b4"
# the named limits the x range shows: settings.NAMED_BUDGETS (B x Sp-Co's bytes); a front file from before
# is in Co-Sp's bytes, with the limits fixed. The x range covers the same bytes on I01 either way.
REGISTERED = {OLD_B_UNIT: (0.5, 1.0, 2.0, 3.0)}
XRANGE = {OLD_B_UNIT: (0.12, 3.15)}
XRANGE_DEFAULT = (0.5, 13.0)
ADJS = SETTINGS
# where a uniform layout's name sits beside its point (offset in points, alignment); two pairs of layouts nearly coincide
LABEL_AT = {"props_split_adj_split": ((-6, 4), "right"), "props_condensed_adj_split": ((6, -10), "left"),
            "props_split_adj_condensed": ((6, -11), "left"), "props_condensed_adj_condensed": ((6, 5), "left"),
            "props_split_adj_chunked": ((6, 5), "left"), "props_condensed_adj_chunked": ((6, -11), "left"),
            "props_chunked_adj_condensed": ((6, -11), "left")}


def read(*path):
    with open(os.path.join(*path)) as f:
        return list(csv.DictReader(f))


def merged(cells):
    """[(label, representative cell, bytes, ops)]: the uniform layouts, those at one point under one label
    (a workload that reads no edge list has three points, 'Split/any adjacency')"""
    at = {}
    for c in F.CELLS:
        at.setdefault((round(cells[c][0], 6), round(cells[c][1], 6)), []).append(c)
    out = []
    for group in at.values():
        props = {F.settings(c)[0] for c in group}
        if len(group) == 1:
            label = F.cell_label(group[0])
        elif len(props) == 1 and {F.settings(c)[1] for c in group} == set(ADJS):
            label = f"{props.pop().capitalize()}/any adjacency"
        else:
            label = ", ".join(F.cell_label(c) for c in group)
        out.append((label, group[0], len(group), *cells[group[0]]))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=F.ROOT, help="the dir holding equal-bytes-front/ and joint-mix/")
    ap.add_argument("--out", default=os.path.join(F.ROOT, "equal-bytes-front", "plots"))
    ap.add_argument("--workloads", default=DEFAULT, help="the columns, by id (i01,i02,w04,w15)")
    ap.add_argument("--front", action="append", default=[], metavar="ID=DIR", help="a workload's front from another dir "
                    "(equal_bytes_front.py's --root) than <root>/equal-bytes-front; repeatable")
    ap.add_argument("--xmax", type=float, default=None, help="the largest byte limit B on the x axis (default: XRANGE's, by the "
                    "front's unit)")
    ap.add_argument("--name", default="equal_bytes_curve", help="the figure's file name")
    ap.add_argument("--source", default=F.SOURCE, help="the caption's last sentences: which data")
    ap.add_argument("--no-built", action="store_true", help="draw no built mixes (a root whose code state built none)")
    a = ap.parse_args()
    known = {w[1]: w for w in WORKLOADS}
    WORKLOADS[:] = [known[x] if not a.no_built else (*known[x][:3], ()) for x in a.workloads.split(",")]
    fronts = dict(x.split("=", 1) for x in a.front)
    default = a.workloads == DEFAULT
    apply_house_style(extralarge=1)
    plt.rcParams.update({"font.size": 9, "axes.titlesize": 10, "axes.labelsize": 9, "xtick.labelsize": 8, "ytick.labelsize": 8,
                         "legend.fontsize": 8})
    # two workloads of another size (W04, W15) keep their own ops axis; the ratio row has one fixed range
    fig, axes = plt.subplots(2, len(WORKLOADS), figsize=(6.2 * len(WORKLOADS), 7.6), sharex=True, sharey="row" if default else False,
                             constrained_layout=True, gridspec_kw={"height_ratios": [1.25, 1]})
    uniform = read(a.root, "joint-mix", "joint_regret.csv")
    drawn, limits, off_range = [], set(), []
    for j, (wdir, wid, title, built_files) in enumerate(WORKLOADS):
        front = read(fronts.get(wid, os.path.join(a.root, "equal-bytes-front")), wdir, "equal_bytes_front.csv")
        ref, unit = ref_bytes_of(front)                  # B = 1 in bytes: Sp-Co's (Co-Sp's in a file in Co-Sp units)
        if j == 0:
            units = unit
            XMIN, XMAX = XRANGE.get(unit, XRANGE_DEFAULT)
            XMAX = a.xmax or XMAX
            registered = tuple(b for b in REGISTERED.get(unit, NAMED_BUDGETS) if b <= XMAX)
        elif unit != units:
            raise SystemExit(f"{wdir}: B in {unit_text(unit)}, the first column's in {unit_text(units)}: one unit per figure")
        cells = {r["option"]: (float(r["total_bytes"]), float(r["total_ops"])) for r in uniform
                 if r["currency"] == "ops" and r["id"] == wid and r["option"] in F.CELLS}
        if len(cells) != len(F.CELLS) or abs(cells[unit][0] / ref - 1) > 1e-9:
            raise SystemExit(f"{wdir}: the limit B = 1 is not {unit_text(unit)}, or a uniform layout is missing")
        line = lambda option_set: sorted((float(r["cap_bytes"]) / ref, float(r["total_ops"]), r["ops_single_over_mix"]) for r in front
                                         if r["option_set"] == option_set and r["option"] == "fewest_ops_within_cap")
        mix, per_type = line(OPTION_SET), line(PER_TYPE)
        tuned = wid in TUNED                            # I01 and I02 keep their hand-placed labels and their axes
        # a limit right of the x range is not drawn (I01 and I02 keep their lines as they were: the figure's pixels are pinned)
        shown = (lambda pts: pts) if tuned else (lambda pts: [p for p in pts if p[0] <= XMAX])
        if not per_type:
            raise SystemExit(f"{wdir}: no '{PER_TYPE}' rows in equal_bytes_front.csv")
        built = [r for name in built_files for r in read(a.root, "equal-bytes-front", wdir, name)]
        if any(("bytes_x_reference" in r) != (unit == B_UNIT) for r in built):    # a built point is in its own front's unit
            raise SystemExit(f"{wdir}: the built points under {a.root} are in another B unit than the front ({unit_text(unit)}): --no-built")
        off = [r["mix"] for r in built if float(r["measured_ops"]) != float(r["composed_ops"])]
        if off:
            raise SystemExit(f"{wdir}: built mixes whose measured ops differ from the composed ops: {off}")
        # the best uniform layout that fits: a staircase over the layouts in order of their bytes
        stair, best = [], None
        for c in sorted(cells, key=lambda c: cells[c]):
            if best is None or cells[c][1] < best:
                best = cells[c][1]
                stair.append((cells[c][0] / ref, best))
        top = axes[0][j]
        top.step([m[0] for m in shown(per_type)] + [XMAX], [m[1] for m in shown(per_type)] + [shown(per_type)[-1][1]], where="post", color=PER_TYPE_COLOR, lw=1.1, zorder=3)
        top.step([m[0] for m in shown(mix)] + [XMAX], [m[1] for m in shown(mix)] + [shown(mix)[-1][1]], where="post", color="black", lw=2.0, zorder=3)
        top.step([s[0] for s in shown(stair)] + [XMAX], [s[1] for s in shown(stair)] + [shown(stair)[-1][1]], where="post", color="0.45", lw=1.5, ls="--", zorder=2)
        points = [(F.cell_label(c), c, 1, *cells[c]) for c in F.CELLS] if tuned else sorted(merged(cells), key=lambda t: t[3])
        ys = [m[1] for m in shown(mix) + shown(stair)]
        side = 0                                        # the other panels: labels above and below in turn, in x order
        for label, c, n, cb, ops in points:
            b = cb / ref
            if b > XMAX:
                off_range.append(f"{wid.upper()} {label} at B = {b:.3g} ({number_to_human(ops)} ops)")
                continue
            top.scatter([b], [ops], s=52, color=F.adj_color(c) if n == 1 else "0.6", edgecolor="black", linewidth=0.6, zorder=4)
            ys.append(ops)
            xy, ha = LABEL_AT.get(c, ((6, 5), "left")) if tuned else (((6, 6), (6, -12))[side % 2], "left")
            if not tuned and b > 0.85 * XMAX:
                xy, ha = (-xy[0], xy[1]), "right"       # at the right edge: the label goes left of its point
            side += 1
            top.annotate(label, (b, ops), textcoords="offset points", xytext=xy, ha=ha, fontsize=6.5, color="0.2")
        first = mix[0][0]
        for ax in (top, axes[1][j]):
            ax.axvspan(first, stair[0][0], color="0.88", zorder=0)
        if tuned:
            top.annotate("in the grey band\nonly a mix fits", (stair[0][0], mix[0][1]), textcoords="offset points", xytext=(10, -62),
                         ha="left", va="top", fontsize=7, color="0.25", arrowprops=dict(arrowstyle="-", color="0.5", lw=0.6))
        top.scatter([x_of(r, "bytes") for r in built], [float(r["measured_ops"]) for r in built], marker="*", s=150,
                    color="gold", edgecolor="black", linewidth=0.7, zorder=5)
        top.set_yscale("log")
        if not tuned:                                   # room for the labels under the lowest and over the highest point
            top.set_ylim(min(ys) / 1.22, max(ys) * 1.13)
        top.set_title(title)
        top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: number_to_human(v)))
        top.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: number_to_human(v) if str(v)[0] in ("25" if tuned else "2358") else ""))
        top.grid(True, which="major", color="0.9", lw=0.6)
        bot = axes[1][j]
        ratio = [(b, float(x)) for b, _, x in mix if x]
        ratio_pt = [(b, float(x)) for b, _, x in per_type if x]
        bot.step([r[0] for r in shown(ratio_pt)] + [XMAX], [r[1] for r in shown(ratio_pt)] + [shown(ratio_pt)[-1][1]], where="post", color=PER_TYPE_COLOR, lw=1.1, zorder=3)
        bot.step([r[0] for r in shown(ratio)] + [XMAX], [r[1] for r in shown(ratio)] + [shown(ratio)[-1][1]], where="post", color="black", lw=2.0, zorder=3)
        bot.scatter([x_of(r, "bytes") for r in built], [float(r["ops_single_over_measured"]) for r in built], marker="*",
                    s=150, color="gold", edgecolor="black", linewidth=0.7, zorder=5)
        bot.axhline(1.0, color="black", lw=1.0, ls=":", zorder=1)
        marks = []
        for lim in registered:
            at = [r for r in ratio if abs(r[0] - lim) < 1e-9]
            if len(at) != 1:
                raise SystemExit(f"{wdir}: no solved limit at B = {lim}")
            marks.append(at[0][1])
            bot.axvline(lim, color="0.6", lw=0.8, ls=":", zorder=1)
            bot.annotate(f"{at[0][1]:.2f}x", (lim, at[0][1]), textcoords="offset points", xytext=(-4, 9), ha="right", fontweight="bold",
                         bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.85), zorder=6)
        k = max(range(len(ratio)), key=lambda i: ratio[i][1])
        if tuned or ratio[k][1] > 1.005:
            bot.annotate(f"peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}", (ratio[k][0], ratio[k][1]), textcoords="offset points",
                         xytext=(-8, 5), ha="right", fontsize=8)
            bot.annotate(f"starts at {ratio[0][1]:.2f}x", (ratio[0][0], ratio[0][1]), textcoords="offset points", xytext=(4, -12),
                         ha="left", fontsize=7, color="0.25")
        bot.set_yscale("log")
        bot.set_ylim(0.85, 17)
        bot.set_yticks([1, 2, 4, 8, 12]); bot.set_yticklabels(["1x", "2x", "4x", "8x", "12x"]); bot.minorticks_off()
        bot.set_xlim(XMIN, XMAX)
        bot.grid(True, which="major", color="0.9", lw=0.6)
        bot.set_xlabel(f"byte limit B, x {unit_text(unit)}")
        if j == 0:
            top.set_ylabel("weighted ops of the workload (log)")
            bot.set_ylabel("best uniform layout that fits:\nits weighted ops over the mix's (log)")
        in_range = lambda pts: [p for p in pts if p[0] <= XMAX][-1][1]  # (the last value the x range shows)
        if abs(ratio[-1][1] - in_range(ratio)) > 1e-9 :
            raise SystemExit(f"{wdir}: the ratio still moves after B = {XMAX:g}; the x range hides it")
        limits.add(len(mix))
        at_pt = [next(r[1] for r in ratio_pt if abs(r[0] - lim) < 1e-9) for lim in registered]
        kp = max(range(len(ratio_pt)), key=lambda i: ratio_pt[i][1])
        drawn.append(f"{wid.upper()} per type: {len(per_type)} limits ({len(ratio_pt)} with a uniform layout that fits), at B = " + " / ".join(f"{b:g}" for b in registered) + " "
                     + " / ".join(f"{m:.2f}" for m in at_pt) + f"x, peak {ratio_pt[kp][1]:.1f}x at B = {ratio_pt[kp][0]:.2f}, starts at {ratio_pt[0][1]:.2f}x")
        drawn.append(f"{wid.upper()}: {len(mix)} limits, {len(built)} built mixes, at B = " + " / ".join(f"{b:g}" for b in registered) + " "
                     + " / ".join(f"{m:.2f}" for m in marks) + f"x, peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}, starts at {ratio[0][1]:.2f}x")
    handles = [Line2D([], [], color="black", lw=2.0, label="the mix, composed, per type and direction"),
               Line2D([], [], color=PER_TYPE_COLOR, lw=1.1, label="the mix, composed, per type"),
               Line2D([], [], color="0.45", lw=1.5, ls="--", label="the best uniform layout that fits"),
               Line2D([], [], color="black", lw=0, marker="*", ms=11, mfc="gold", mec="black", label="a mixed layout built and measured at SF3")]
    if not any(w[3] for w in WORKLOADS):
        handles.pop()                                   # no built mix on these workloads
    handles += [Line2D([], [], color="black", lw=0, marker="o", ms=6.5, mfc=format_color(k), mec="black", mew=0.5,
                       label=f"a uniform layout, {lab} adjacency") for lab, k in F.ADJ.values()]
    fig.legend(handles=handles, loc="outside upper center", ncol=3, frameon=False, columnspacing=1.4)
    freq = "both workloads use LDBC's frequencies" if default else "frequencies as the workload defines them"
    extra = ""
    if off_range:
        extra += " Right of the x range: " + "; ".join(off_range) + "."
    F.caption(fig, f"A workload's cost adds up each query's cost x its frequency; {freq}. Each step of "
              "a mix's line is one byte limit, solved exactly (" + " and ".join(str(n) for n in sorted(limits)) + " per workload; between two "
              f"limits the line holds the left value; the bold ratios are the per type and direction line's; every limit above B = {XMAX:g} gives the same as B = {XMAX:g}). Uniform layouts are named properties/adjacency."
              + (" A built mix's measured ops equal its composed ops." if any(w[3] for w in WORKLOADS) else "")
              + f"{extra} {F.WEIGHTS} {a.source}", width=165)
    F.save(fig, a.out, a.name, "; ".join(drawn))


if __name__ == "__main__":
    main()
