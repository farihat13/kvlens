#!/usr/bin/env python3
"""Draw the ratio sweeps (sweep.py's CSVs): does the best layout change with the write:read ratio, and does a
per type mix beat every single layout along the way? One figure per swept workload, two panels (total ops,
total bytes), log-log. Lines: the 9 single layouts (the 4 existing formats' coloured, the other 5 grey), each
labelled at its right end, and the best per type mix at each ratio (dashed black), with a marker and a short label
where its assignment changes. Vertical lines: the workload's own writes per read (and any `sweep_marks` it declares).

    python3 experiments/layout_choice/plot_sweep.py --sf 0.1   -> <workload dir>/plots/sweep.{png,pdf}
"""
import argparse
import csv
import os
import sys
import textwrap
from collections import defaultdict

import matplotlib
from kvevaluator.paths import ROOT
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.style import apply_house_style, format_color
from kvcost.plotting.formatting import decimal_bytes  # (moved to the lib; importers kept)
from kvcost.plotting.formatting import number_to_human
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import axis_label, display_title, rule_label

# (label, the palette key of the colour of the cell's adjacency setting: compose.ADJ_COLOR_KEY)
KNOWN = {"props_split_adj_condensed": ("Split/Condensed", "all_condensed"),
         "props_condensed_adj_split": ("Condensed/Split", "nebula"),
         "props_condensed_adj_condensed": ("Condensed/Condensed", "all_condensed"),
         "props_chunked_adj_chunked": ("Chunked/Chunked", "pertype_csr")}
PROPS_LS = {"split": "-", "condensed": "--", "chunked": ":"}


def cell_name(config):
    """'Split/Split' of props_split_adj_split (properties/adjacency); anything else (a mix's name) as it is"""
    if not config.startswith("props_") or "_adj_" not in config:
        return config
    p, a_ = config[len("props_"):].split("_adj_")
    return f"{p.capitalize()}/{a_.capitalize()}"


CURRENCY = {"ops": ("total weighted ops", "G:S:N:P:D = 1:1.1:0.01:1:1, reads and the writes' own reads", number_to_human),
            "bytes": ("total bytes", "bytes read + written + read by the writes first", decimal_bytes)}


def write_read_ratio(w, r):
    """the swept ratio as the reader sees it: write operations per read operation of the workload"""
    scaled = set(w["sweep"])
    writes = sum(f for _, q, f, _ in w["items"] if q in scaled)
    reads = sum(f for _, q, f, _ in w["items"] if q not in scaled)
    return r * writes / reads


def parse(label):
    """'knows=chunked, Message(p)=split' -> {part: layout}"""
    return dict(kv.split("=", 1) for kv in label.split(", ")) if label else {}


def change_label(prev, cur):
    """what changed between two assignments, short: 'knows -> Chunked; +2 more'"""
    a, b = parse(prev), parse(cur)
    moved = [f"{k.replace('(p)', ' props')} -> {v.capitalize()}" for k, v in b.items() if a.get(k) != v]
    return "; ".join(moved[:2]) + (f"; +{len(moved) - 2} more" if len(moved) > 2 else "")


def spread(pys, row, lo, hi):
    """pixel positions pys (sorted) -> positions at least `row` apart, kept inside [lo, hi]: push up, then back down
    from the top when the stack runs past it"""
    out = []
    for p in pys:
        out.append(max(p, out[-1] + row) if out else max(p, lo))
    if out and out[-1] > hi:
        out[-1] = hi
        for i in range(len(out) - 2, -1, -1):
            out[i] = min(out[i], out[i + 1] - row)
    return out


def label_right_ends(ax, ends, fontsize=7):
    """ends = [(y at the right edge, text, colour)]: labels just outside the right edge, pushed apart in pixel space
    and kept within the axes' height. Call it after every other artist (suptitle, legend) is on the figure: the
    spread is measured on a drawn layout, and the labels are pinned in axes fractions so a later layout pass that
    shrinks the axes keeps them apart (pinned in data units they slide back together)"""
    fig = ax.figure
    fig.canvas.draw()
    row = fontsize * 1.25 * fig.dpi / 72
    lo, hi = ax.transAxes.transform((0, 0))[1], ax.transAxes.transform((0, 1))[1]
    ends = sorted(ends, key=lambda e: e[0])
    x_right = ax.get_xlim()[1]
    pys = spread([ax.transData.transform((x_right, y))[1] for y, _, _ in ends], row, lo, hi)
    for (_, text, color), py in zip(ends, pys):
        _, yf = ax.transAxes.inverted().transform((0, py))
        ax.annotate(text, (1.0, yf), xycoords="axes fraction", xytext=(4, 0), textcoords="offset points",
                    va="center", fontsize=fontsize, color=color, annotation_clip=False)


def label_changes(ax, points, fontsize=6.5):
    """points = [(x, y, text)]: each label at the first offset (below, above, further out) whose box overlaps no
    earlier label; boxes estimated in pixels (a DejaVu character is ~0.6 em)"""
    fig = ax.figure
    fig.canvas.draw()
    px = fig.dpi / 72
    h = fontsize * 1.3 * px
    boxes = []
    for x, y, text in points:
        cx, cy = ax.transData.transform((x, y))
        wdt = 0.6 * fontsize * px * len(text)
        for dx, dy in ((4, -12), (4, 6), (4, -24), (4, 18), (4, -36), (4, 30), (-4 - wdt / px, -12), (-4 - wdt / px, 6)):
            box = (cx + dx * px, cy + dy * px, cx + dx * px + wdt, cy + dy * px + h)
            if not any(b[0] < box[2] and box[0] < b[2] and b[1] < box[3] and box[1] < b[3] for b in boxes):
                break
        boxes.append(box)
        ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points", fontsize=fontsize, zorder=7,
                    arrowprops=dict(arrowstyle="-", lw=0.5, color="0.4") if abs(dy) > 12 else None)

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    benchmarks.add_workload_arg(ap)
    benchmarks.add_root_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    benchmarks.add_out_arg(ap)
    a = ap.parse_args()
    benchmarks.use_plots_out(a.out)
    apply_house_style(extralarge=3)
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        d = benchmarks.workload_dir(a.dataset, a.sf, name, w, a.root, criterion=a.criterion, param_set=a.param_set)
        path = os.path.join(d, "sweep.csv")
        os.makedirs(benchmarks.plots_dir(d), exist_ok=True)
        if "sweep" not in w or not os.path.exists(path):
            continue
        series = defaultdict(list)
        for row in csv.DictReader(open(path)):
            series[row["config"]].append(row)
        fig, axes = plt.subplots(1, 2, figsize=(17, 5.6), constrained_layout=True)
        base = write_read_ratio(w, 1.0)
        marks = [(base, "this workload")] + [(base * m, lab) for m, lab in w.get("sweep_marks", [])]
        pending = []                                    # (ax, ends, changes): labelled after the legend and title
        for ax, cur in zip(axes, ("ops", "bytes")):
            title, what, human = CURRENCY[cur]
            ends, changes = [], []
            for config in sorted(series):              # sorted: the draw order must not follow the hash seed
                rows = series[config]
                if config.startswith("per_type_fewest_") and config != f"per_type_fewest_{cur}":
                    continue
                xs = [write_read_ratio(w, float(r["r"])) for r in rows]
                ys = [float(r[cur]) for r in rows]
                if config.startswith("per_type_fewest_"):
                    ax.plot(xs, ys, color="black", lw=2.4, ls=(0, (4, 2)), label="best per type mix (exact)", zorder=5)
                    prev = rows[0]["assignment"]
                    for x, y, r in zip(xs[1:], ys[1:], rows[1:]):   # where the best mix changes its assignment
                        if r["assignment"] != prev:
                            ax.plot([x], [y], marker="o", ms=6, color="black", zorder=6, ls="",
                                    label=None if changes else "the best mix changes here")
                            changes.append((x, y, change_label(prev, r["assignment"])))
                            prev = r["assignment"]
                    continue
                named = config in KNOWN
                color = format_color(KNOWN[config][1]) if named else "#9a9a9a"
                # a named cell: the colour of its adjacency setting, the line style of its properties setting (two cells
                # with the same adjacency, Split/Condensed and Condensed/Condensed, share a colour and differ in style)
                ls = PROPS_LS[config[len("props_"):].split("_adj_")[0]] if named else "-"
                ax.plot(xs, ys, color=color, lw=1.8 if named else 0.9, ls=ls, zorder=4 if named else 2)
                ends.append((ys[-1], KNOWN[config][0] if named else cell_name(config), color))
            for x, lab in marks:                     # the workload's own ratio(s): a label along each line
                ax.axvline(x, color="#d62728", lw=1.0, ls=":", zorder=1)
                ax.text(x, 0.5, f"{lab}: {x:.2g} writes per read", transform=ax.get_xaxis_transform(), rotation=90,
                        ha="right", va="center", fontsize=6.5, color="#d62728")
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_xlabel("writes per read")
            ax.set_ylabel(title)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _, h=human: h(v)))
            # where the best mix IS a single layout, say so (its dashed line then lies on that layout's line)
            mix = {r["r"]: r["assignment"] for r in series[f"per_type_fewest_{cur}"]}
            same = {c for c, rows in series.items() if not c.startswith("per_type_fewest_")
                    and all(mix[r["r"]] == r["assignment"] for r in rows)}
            first = min(same) if same else None       # min, not next(iter()): a set's order follows the hash seed
            note = f"\nbest mix = the {KNOWN[first][0] if first in KNOWN else first} at every ratio" if same else ""
            ax.set_title(what + note, fontsize="small")
            ax.grid(alpha=0.3, which="major")
            pending.append((ax, ends, changes))
        handles, labels = axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="outside lower center", ncol=3, frameon=False)
        head = display_title(w).split(" (")[0]   # the title without its parenthetical, then wrapped to the figure
        fig.suptitle(textwrap.fill(f"{head} ({axis_label(a.dataset, a.sf, a.param_set)}; {rule_label(a.criterion)}): which layout is cheapest as writes grow?", 80))
        for ax, ends, changes in pending:               # last: the labels are spread on the final layout
            label_right_ends(ax, ends)
            label_changes(ax, changes)
        for ext in ("png", "pdf"):
            fig.savefig(os.path.join(benchmarks.plots_dir(d), f"sweep.{ext}"), dpi=150)
        plt.close(fig)
        print(f"wrote {os.path.join(benchmarks.plots_dir(d), 'sweep.png')}")


if __name__ == "__main__":
    main()
