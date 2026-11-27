#!/usr/bin/env python3
"""The equal-bytes figure of one workload, from equal_bytes_front.py's files: the workload's weighted ops against a byte
limit B (x Sp-Co's bytes, the reference; Co-Sp's in a front in Co-Sp units). Always: the nine uniform layouts as points and the best
uniform layout that fits as a staircase. When the front was solved with mixes: the composed mix per byte limit (per type
and direction, and per type), the built mixes as stars, and below it the ratio best uniform layout that fits / the mix.
With --no-mix files (the uniform layouts only) the figure is the top panel alone. Counts are deterministic: no error bars.

    python3 experiments/layout_choice/plot_equal_bytes_front.py --sf 3 [--dataset ldbc] [--workload i01,i02] [--root DIR]
        reads <root>/<workload dir>/{equal_bytes_front,uniform_layouts}.csv [, built_points*.csv]
        (default <root>: <the analysis root>/equal-bytes-front) -> <root>/plots/equal-bytes-curve_<workload dir>.{png,pdf}
"""
import argparse
import csv
import glob
import os
import sys

import matplotlib
from kvevaluator.paths import ROOT, figure_dir
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.formatting import number_to_human
from kvcost.plotting.style import apply_house_style, format_color
from plot_workload_story import caption, pair_name
from kvcost.layout_cost.compose import CELLS
from kvcost.layout_cost import benchmarks
from kvcost.layout_search.front import ref_bytes_of, unit_text, x_of
from kvcost.layout_cost.labels import axis_label, display_title

BY_DIR, PER_TYPE = "per type and direction", "per type"
PER_TYPE_COLOR = "#1f77b4"
# a uniform layout is coloured by its adjacency: the palette's colour of the format with that adjacency
ADJ = {"condensed": ("Condensed", "all_condensed"), "split": ("Split", "nebula"), "chunked": ("Chunked", "pertype_csr")}


def read(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def draw(wdir, w, root, out, dataset, sf, param_set=None):
    front = read(os.path.join(root, wdir, "equal_bytes_front.csv"))
    nine = {r["cell"]: (float(r["bytes_x_reference"]), float(r["total_ops"])) for r in read(os.path.join(root, wdir, "uniform_layouts.csv"))}
    assert sorted(nine) == sorted(CELLS), f"{wdir}: uniform_layouts.csv holds {len(nine)} layouts"
    line = lambda oset: sorted((x_of(r, "cap"), float(r["total_ops"]), r["ops_single_over_mix"]) for r in front
                               if r["option_set"] == oset and r["option"] == "fewest_ops_within_cap")
    mix, per_type = line(BY_DIR), line(PER_TYPE)
    built = [r for f in sorted(glob.glob(os.path.join(root, wdir, "built_points*.csv"))) for r in read(f)]
    off = [r["mix"] for r in built if float(r["measured_ops"]) != float(r["composed_ops"])]
    if off:
        raise SystemExit(f"{wdir}: built mixes whose measured ops differ from the composed ops: {off}")
    stair, best = [], None            # the best uniform layout that fits: a staircase over the layouts in order of their bytes
    for c in sorted(nine, key=lambda c: nine[c]):
        if best is None or nine[c][1] < best:
            best = nine[c][1]
            stair.append((nine[c][0], best))
    xs = [v[0] for v in nine.values()] + [m[0] for m in mix + per_type]
    lo, hi = min(xs), max(max(v[0] for v in nine.values()), 3.0)
    logx = hi / lo > 40                                    # LinkBench: a Chunked layout has 100x the bytes
    xmin, xmax = (lo / 1.3, hi * 1.3) if logx else (max(0.0, lo - 0.05 * (hi - lo)), hi + 0.09 * (hi - lo))
    has_mix = bool(mix or per_type)
    fig, axes = plt.subplots(2 if has_mix else 1, 1, figsize=(7.4, 7.9 if has_mix else 5.2), sharex=True, squeeze=False,
                             gridspec_kw={"height_ratios": [1.25, 1]} if has_mix else None)
    top = axes[0][0]
    for pts, color, lw in ((per_type, PER_TYPE_COLOR, 1.2), (mix, "black", 2.0)):
        if pts:
            top.step([m[0] for m in pts] + [xmax], [m[1] for m in pts] + [pts[-1][1]], where="post", color=color, lw=lw, zorder=3)
    top.step([s[0] for s in stair] + [xmax], [s[1] for s in stair] + [stair[-1][1]], where="post", color="0.45", lw=1.5, ls="--", zorder=2)
    for c in CELLS:
        top.scatter([nine[c][0]], [nine[c][1]], s=52, color=format_color(ADJ[c.split("_adj_")[1]][1]), edgecolor="black", linewidth=0.6, zorder=4)
        top.annotate(pair_name(c), nine[c], textcoords="offset points", xytext=(6, 5), ha="left", fontsize=6.5, color="0.2")
    if built:
        top.scatter([x_of(r, "bytes") for r in built], [float(r["measured_ops"]) for r in built], marker="*", s=150,
                    color="gold", edgecolor="black", linewidth=0.7, zorder=5)
    top.set_yscale("log")
    top.yaxis.set_major_formatter(FuncFormatter(lambda v, _: number_to_human(v)))
    top.grid(True, which="major", color="0.9", lw=0.6)
    top.yaxis.set_minor_formatter(FuncFormatter(lambda v, _: number_to_human(v) if str(v)[0] in "25" else ""))
    top.tick_params(labelsize=8.5, which="both")
    top.set_ylabel("weighted ops of the workload (log)", fontsize=9)
    said = []
    if has_mix:
        bot = axes[1][0]
        for pts, color, lw, label in ((per_type, PER_TYPE_COLOR, 1.2, PER_TYPE), (mix, "black", 2.0, BY_DIR)):
            ratio = [(b, float(x)) for b, _, x in pts if x]
            if not ratio:
                continue
            bot.step([r[0] for r in ratio] + [xmax], [r[1] for r in ratio] + [ratio[-1][1]], where="post", color=color, lw=lw, zorder=3)
            k = max(range(len(ratio)), key=lambda i: ratio[i][1])
            said.append(f"{label}: {len(pts)} limits, peak {ratio[k][1]:.2f}x at B = {ratio[k][0]:.2f}")
            if color == "black" or not mix:
                bot.annotate(f"peak {ratio[k][1]:.1f}x at B = {ratio[k][0]:.2f}", ratio[k], textcoords="offset points", xytext=(-8, 5), ha="right", fontsize=8)
        if built:
            bot.scatter([x_of(r, "bytes") for r in built], [float(r["ops_single_over_measured"]) for r in built if r["ops_single_over_measured"]],
                        marker="*", s=150, color="gold", edgecolor="black", linewidth=0.7, zorder=5)
        bot.axhline(1.0, color="black", lw=1.0, ls=":", zorder=1)
        bot.set_yscale("log")
        top_ratio = max([float(x) for _, _, x in mix + per_type if x] + [1.0])
        ticks = [t for t in (1, 1.5, 2, 3, 4, 6, 8, 12, 20, 30, 50) if t <= top_ratio * 1.4]
        bot.set_ylim(0.85, max(2.0, top_ratio * 1.4))
        bot.set_yticks(ticks, [f"{t:g}x" for t in ticks]); bot.minorticks_off()
        bot.grid(True, which="major", color="0.9", lw=0.6)
        bot.tick_params(labelsize=8.5)
        bot.set_ylabel("best uniform layout that fits:\nits weighted ops over the mix's (log)", fontsize=9)
    last = axes[-1][0]
    if logx:
        last.set_xscale("log")
        last.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    last.set_xlim(xmin, xmax)
    last.tick_params(labelsize=8.5)
    last.set_xlabel(f"byte limit B, x {unit_text(ref_bytes_of(front)[1])}", fontsize=9)
    handles = []
    if mix:
        handles.append(Line2D([], [], color="black", lw=2.0, label="the mix, composed, per type and direction"))
    if per_type:
        handles.append(Line2D([], [], color=PER_TYPE_COLOR, lw=1.2, label="the mix, composed, per type"))
    handles.append(Line2D([], [], color="0.45", lw=1.5, ls="--", label="the best uniform layout that fits"))
    if built:
        handles.append(Line2D([], [], color="black", lw=0, marker="*", ms=11, mfc="gold", mec="black", label="a mixed layout built and measured"))
    handles += [Line2D([], [], color="black", lw=0, marker="o", ms=6.5, mfc=format_color(k), mec="black", mew=0.5, label=f"a uniform layout, {lab} adjacency")
                for lab, k in ADJ.values()]
    fh = fig.get_figheight()
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.95 / fh), ncol=2, frameon=False, fontsize=8.5)
    fig.suptitle(f"{display_title(w)} ({axis_label(dataset, sf, param_set)})\n"
                 + ("at the same bytes: a mixed layout against the best uniform layout that fits" if has_mix else
                    "the nine uniform layouts: weighted ops against bytes, and the best one within each byte limit"), fontsize=10.5, y=1 - 0.15 / fh)
    caption(fig, "Uniform layouts are named properties/adjacency (Split/Condensed = Split properties, Condensed adjacency). Weighted ops = GET + 1.1 SEEK + 0.01 NEXT "
            "on reads; a write adds its own reads, PUTs and DELETEs; bytes = read + read by a write first + written. " +
            ("Each step of a mix's line is one byte limit, solved exactly (the fewest weighted ops within the limit); between two limits the line holds "
             "the left value. " if has_mix else "") + "Counts are deterministic.", y=0.8 / fh)
    fig.subplots_adjust(top=1 - 0.75 / fh, bottom=2.25 / fh, left=0.14, right=0.97, hspace=0.06)
    os.makedirs(out, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"equal-bytes-curve_{wdir}.{ext}"), dpi=130)
    plt.close(fig)
    print(f"{wdir}: 9 uniform layouts, {len(stair)} steps of the best that fits, {len(built)} built mixes; " + ("; ".join(said) if said else "no mix (uniform layouts only)"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    benchmarks.add_workload_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    ap.add_argument("--root", default=None, help="equal_bytes_front.py's --root (default: <the analysis root>/equal-bytes-front)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    root = a.root or os.path.join(benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set), "equal-bytes-front")
    out = figure_dir(os.path.join(root, "plots"), a.out)
    apply_house_style(extralarge=1)
    n = 0
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        wdir = os.path.basename(benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=a.criterion, param_set=a.param_set))
        if not os.path.exists(os.path.join(root, wdir, "equal_bytes_front.csv")):
            continue                                       # the front ran for the headline workloads only
        draw(wdir, w, root, out, a.dataset, a.sf, a.param_set)
        n += 1
    print(f"wrote {n} figure(s) to {out}")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
