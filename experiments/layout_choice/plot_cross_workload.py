#!/usr/bin/env python3
"""The layout-choice figure across workloads, from each workload's options.csv: single_layout_heatmap.png (each
single layout's cost relative to the workload's best single layout, in three panels: total weighted ops, total bytes,
and ops + bytes / 4,096) in <out root>/plots/. What a mix buys over the
single layouts is plot_budget_sweep.py's budget_gain.png.

    python3 experiments/layout_choice/plot_cross_workload.py --sf 0.1 [--dataset ldbc|linkbench]
"""
import argparse
import os
import sys
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle
from matplotlib.ticker import FuncFormatter, NullFormatter

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.plotting.style import apply_house_style
from plot_workload_story import NAMED, PAIR_NOTE, pair_name, read_csv
from kvcost.layout_cost import cost_models
from kvcost.layout_cost.compose import CELLS, LAYOUTS
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import SUM_LABEL, axis_label, display_title, rule_label

# total_ops4k is derived here (options.csv keeps the two raw totals): total_ops + total_bytes / 4,096
CURRENCIES = (("total_ops", "total weighted ops", "ops"), ("total_bytes", "total bytes", "bytes"),
              ("total_ops4k", SUM_LABEL, "ops4k"))
NOTE = ("Total ops = read + write ops, weighted G:S:N:P:D = 1 : 1.1 : 0.01 : 1 : 1; total bytes = read + written + "
        "read by writes. Third panel: total ops + total bytes / 4 KB, the rule that picks each query's plan, applied "
        "to the workload's totals. Counts are deterministic (no error bars).")
WITHIN = 1.01            # a single layout within 1% of the best is boxed as a best too


def load(dataset, sf, root=None, *, criterion, param_set=None):
    """[(workload title, options rows)] in the suite's order, skipping a workload with no options.csv yet"""
    out = []
    for name, w in benchmarks.workloads(dataset, sf).items():
        path = os.path.join(benchmarks.workload_dir(dataset, sf, name, w, root, criterion=criterion, param_set=param_set), "options.csv")
        if os.path.exists(path):
            out.append((display_title(w), read_csv(path)))
        else:
            print(f"  (no {path}: skipped)")
    return out


def save(fig, out, name):
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {os.path.join(out, name)}.png")


def cost_of(o, col):
    """one option's cost in a currency of CURRENCIES"""
    if col == "total_ops4k":
        return float(o["total_ops"]) + float(o["total_bytes"]) / cost_models.BYTES_PER_OP
    return float(o[col])


def plot_heatmap(rows, out, head, note="", *, criterion):
    """workloads x the 9 single layouts, one panel per currency: cost / the workload's best single layout"""
    ratios = {}                                        # (currency, workload index) -> [9 ratios in CELLS order]
    for col, _, _ in CURRENCIES:
        for i, (_, opts) in enumerate(rows):
            cost = {o["option"]: cost_of(o, col) for o in opts if o["option_set"] == "single"}
            best = min(cost.values())
            ratios[(col, i)] = [cost[c] / best for c in CELLS]
    top = max(max(v) for v in ratios.values())
    norm = LogNorm(vmin=1, vmax=max(top, 10))          # one colour scale for the three panels
    n = len(rows)
    fig, axes = plt.subplots(1, len(CURRENCIES), figsize=(8 * len(CURRENCIES), 0.42 * n + 3.2), sharey=True)
    for ax, (col, title, _) in zip(axes, CURRENCIES):
        grid = [ratios[(col, i)] for i in range(n)]
        im = ax.imshow(grid, norm=norm, cmap="Reds", aspect="auto")
        for i in range(n):
            for j, v in enumerate(grid[i]):
                if v <= WITHIN:
                    ax.add_patch(Rectangle((j - 0.47, i - 0.47), 0.94, 0.94, fill=False, ec="black", lw=2))
                dark = v > norm.vmax ** 0.6                # white text on the darker reds
                txt = f"{v:.2f}" if v < 1.1 else (f"{v:.1f}" if v < 10 else f"{v:,.0f}")   # 1.03 is not boxed: say so
                ax.text(j, i, txt, ha="center", va="center", fontsize=8,
                        color="white" if dark else "black")
        # a column = a single layout, named properties-adjacency as in every layout-choice figure
        labels = [pair_name(c) + (f"\n({NAMED[c]})" if c in NAMED else "") for c in CELLS]
        ax.set_xticks(range(len(CELLS)), labels, rotation=90, fontsize=10)
        for k in range(1, len(LAYOUTS)):                   # a line between the three props layouts
            ax.axvline(3 * k - 0.5, color="black", lw=1.2)
        ax.set_title(title, pad=10)
        ax.set_xlabel("single layout (properties-adjacency)")
        ax.tick_params(length=0)
    axes[0].set_yticks(range(n), [t for t, _ in rows], fontsize=9)
    h = fig.get_figheight()                            # placed in inches: a 4-row figure (LinkBench) is short
    fig.subplots_adjust(right=0.88, wspace=0.08, top=1 - 0.55 / h, bottom=2.0 / h)       # the bottom: room for the spelled-out cell names
    cb = fig.colorbar(im, cax=fig.add_axes([0.9, 0.3, 0.012, 0.5]))
    cb.set_label("x the workload's best single layout (log)")
    cb.ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    cb.ax.yaxis.set_minor_formatter(NullFormatter())
    fig.suptitle(f"{rule_label(criterion)}. How much each single layout costs, relative to the workload's best single layout ({head})\n"
                 f"(boxed = the best, or within 1% of it)", y=1 + 0.5 / h)
    fig.text(0.5, -0.4 / h, textwrap.fill(NOTE + " Single layouts are measured (the grid), " + PAIR_NOTE
                                      + "; the ratio is per workload, per currency." + (" " + note if note else ""), 75 * len(CURRENCIES)), ha="center", va="top", fontsize=9)
    save(fig, out, "single_layout_heatmap")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    benchmarks.add_root_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    ap.add_argument("--note", default="", help="the caption's last sentence, e.g. which snapshot the counts come from")
    benchmarks.add_out_arg(ap)
    a = ap.parse_args()
    benchmarks.use_plots_out(a.out)
    os.environ.setdefault("SOURCE_DATE_EPOCH", "0")  # PDFs: no creation timestamp, byte-stable re-renders
    apply_house_style(extralarge=1)
    rows = load(a.dataset, a.sf, a.root, criterion=a.criterion, param_set=a.param_set)
    if not rows:
        raise SystemExit(f"no options.csv under {a.root or benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set)}: run workload_story.py first")
    out = benchmarks.plots_dir(a.root or benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set))
    os.makedirs(out, exist_ok=True)
    head = axis_label(a.dataset, a.sf, a.param_set)
    plot_heatmap(rows, out, head, a.note, criterion=a.criterion)


if __name__ == "__main__":
    main()
