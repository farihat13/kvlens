#!/usr/bin/env python3
"""The bill figure: one pass of a workload billed by DynamoDB's rule (python/kvcost/layout_cost/compose.py bill_units) on the
nine uniform layouts and on the per type mix with the smallest bill, from bill_search.py's bill_optimum.csv. One panel per
Chunked size. A bar = read units + 5 x write units,
in units of the Co-Sp bar; the label is the ratio to it. Counts are deterministic: no error bars.

    python3 experiments/layout_choice/plot_bill.py [--sf 3] [--workload i01] [--root <a bill_search.py output dir>] [--out DIR]
reads <root> (default <out root>/bill, bill_search.py --sf <sf>) and draws every workload solved there (or --workload
ids) into <out>/bill-single-vs-mix_<workload>.{png,pdf} (default <root>/plots); a panel whose Chunked size was not
solved there is left out. run-layout-choice.sh's stage.
"""
import argparse
import csv
import os

import matplotlib
from kvevaluator.paths import ROOT, figure_dir
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.formatting import number_to_human
from kvcost.plotting.style import apply_house_style
from plot_workload_story import BLEND_OPS_COLOR, WRITTEN_COLOR, caption, pair_name  # (the bill row's two colours)
from kvcost.layout_cost.compose import CELLS
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import axis_label, display_title, rule_label
from kvcost.layout_cost import settings as cells  # (the reference)

from kvcost.layout_cost.cost_models import WRITE_PRICE, bill_at  # (a write unit is priced 5 read units)
FORM = "exact"
REF = cells.REF                 # every ratio is to the reference layout (python/kvcost/layout_cost/settings.py)
RN = cells.name_of(REF)       # its name in the figure's text
PANELS = (("chunked-64k", "Chunked items of 64 KB"), ("chunked-4k", "Chunked items of 4 KB"))


def load(path):
    """(the nine uniform rows by cell, the mix's row) of the exact form"""
    cells, mix = {}, None
    for r in csv.DictReader(open(path)):
        if r["form"] != FORM:
            continue
        if r["option_set"] == "single":
            cells[r["option"]] = r
        elif r["option"] == f"per_type_fewest_bill_{FORM}":
            mix = r
    assert sorted(cells) == sorted(CELLS) and mix, f"{path}: {len(cells)} uniform rows, mix {'found' if mix else 'missing'}"
    for r in list(cells.values()) + [mix]:   # the file's bill is the two parts added (each rounded to 3 decimals: 0.004 at small scale)
        gap = abs(bill_at(float(r["read_units"]), float(r["write_units"])) - float(r["bill"]))
        assert gap <= max(1e-6 * float(r["bill"]), 0.004), f"{path}: {r['option']}: read + {WRITE_PRICE} x write units is {gap} off the bill"
    return cells, mix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="3", help="the scale whose bill dir to draw, <out root>/bill (default 3)")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    ap.add_argument("--root", default=None)
    ap.add_argument("--workload", default=None, help="ids or names (i01); default every workload solved")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    apply_house_style(extralarge=1)
    root = a.root or os.path.join(benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set), "bill")
    out = figure_dir(os.path.join(root, "plots"), a.out)
    todo = [(os.path.basename(benchmarks.workload_dir(a.dataset, a.sf, name, w, criterion=a.criterion, param_set=a.param_set)), w) for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items()]
    sf = a.sf
    panels = [p for p in PANELS if os.path.isdir(os.path.join(root, p[0]))]
    if not panels or not todo:
        raise SystemExit(f"plot_bill: nothing to draw under {root} (no {' / '.join(p[0] for p in PANELS)} dir, or no workload)")
    for wdir, w in todo:
        draw(root, out, wdir, w, panels, a.dataset, sf, a.criterion, a.param_set)
    print(f"[plot_bill] {len(todo)} workloads x {len(panels)} panels ({', '.join(p[0] for p in panels)}) -> {out}")


def draw(root, out, wdir, w, panels, dataset, sf, criterion, param_set=None):
    """one workload's figure: a panel per Chunked size solved under `root`"""
    fig, axes = plt.subplots(1, len(panels), figsize=(5.8 * max(len(panels), 2), 7.6), sharey=True, squeeze=False)
    axes = axes[0]
    top = 0.0
    for ax, (sub, title) in zip(axes, panels):
        cells, mix = load(os.path.join(root, sub, wdir, "bill_optimum.csv"))
        unit = float(cells[REF]["bill"])
        best = min(CELLS, key=lambda c: (float(cells[c]["bill"]), c))
        bars = [(c, cells[c], pair_name(c)) for c in CELLS] + [(None, mix, "smallest bill")]
        for x, (cell, r, _) in enumerate(bars):
            rd, wr = float(r["read_units"]) / unit, bill_at(0.0, float(r["write_units"])) / unit   # the writes' part of the bill
            ax.bar(x, rd, 0.8, color=BLEND_OPS_COLOR, edgecolor="white", linewidth=0.4)
            ax.bar(x, wr, 0.8, bottom=rd, color=WRITTEN_COLOR, hatch="///", edgecolor="white", linewidth=0.4)
            ax.text(x, rd + wr + 0.02, number_to_human(unit) if cell == REF else f"{rd + wr:.2g}x", rotation=90, ha="center", va="bottom",
                    fontsize=9, fontweight="bold" if cell == REF else None)
            top = max(top, rd + wr)
        gain = float(cells[best]["bill"]) / float(mix["bill"])
        ax.text(0.985, 0.97, f"the mix: {gain:.2f}x below the best\nsingle layout ({pair_name(best)})", transform=ax.transAxes, ha="right", va="top",
                fontsize=10, fontweight="bold")
        ax.axvline(len(CELLS) - 0.5, color="black", lw=0.8)
        for x0, x1, head in ((0, len(CELLS) - 1, "single layouts (the 9)"), (len(CELLS), len(CELLS), "mix,\nper type")):
            ax.text((x0 + x1) / 2, 1.01, head, transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=8.5, fontweight="bold")
        ax.set_xticks(range(len(bars)), [b[2] for b in bars], rotation=90, fontsize=9)
        ax.set_title(title, fontsize=11, fontweight="bold", pad=30)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
        ax.grid(axis="y", alpha=0.3)
        ax.set_axisbelow(True)
    axes[0].set_ylim(0, top * 1.48)
    axes[0].set_ylabel("bill = read units\n+ 5 x write units\n(" + RN + " = 1)", fontsize=11.5)
    fh = fig.get_figheight()
    handles = [Patch(color=BLEND_OPS_COLOR, label="read units (their share of the bill)"),
               Patch(facecolor=WRITTEN_COLOR, hatch="///", edgecolor="white", label="5 x write units (their share)")]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.2 / fh), ncol=2, frameon=False, fontsize=11)
    fig.suptitle(f"{display_title(w)} ({axis_label(dataset, sf, param_set)}; {rule_label(criterion)})\n"
                 "the nine single layouts and the mix with the smallest bill, under a billed key-value service's rule (DynamoDB's request units)",
                 fontsize=11.5, y=1 - 0.2 / fh)
    caption(fig, "Bar label: the absolute value of " + RN + ", the reference layout (bold), every other bar's ratio to it. Cells are named properties-adjacency (Sp Split, Co Condensed, Ch Chunked; Sp-Co = Split properties, "
            "Condensed adjacency). The rule (bill_search.py; DynamoDB's request units): a point read costs its key + value bytes / 4 KB, rounded up; "
            "a range read (a SEEK and what it scans; a NEXT is free) its bytes / 4 KB, rounded up; a PUT its bytes / 1 KB, rounded up; a DELETE 1 unit; a write unit is "
            "priced 5 read units; the reads a write does first are read units. Bars are the exact form (every request rounded up alone). "
            "The mix: one layout per type, solved for the smallest bill (exact search). Every option keeps the pick record's plans (not re-chosen under the bill). "
            "Counts are deterministic. Nothing was measured on such a service. Data: "
            + (root[root.index("output/"):] if "output/" in root else root) + ".", y=1.0 / fh)
    fig.subplots_adjust(top=1 - 1.45 / fh, bottom=3.35 / fh, left=0.125, right=0.975, wspace=0.07)
    os.makedirs(out, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"bill-single-vs-mix_{wdir}.{ext}"), dpi=110)
    plt.close(fig)
    print(f"wrote {out}/bill-single-vs-mix_{wdir}.png and .pdf ({len(panels)} panels x {len(CELLS) + 1} bars)")


if __name__ == "__main__":
    main()
