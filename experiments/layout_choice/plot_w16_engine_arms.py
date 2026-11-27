#!/usr/bin/env python3
"""The named formats against their nearest single layouts (the I01 timed run): per regime and class (every read; every read
but IC3), one pair of bars per (format, layout) -- the reads' time per 100,000 updates -- with the scorer's word and the
ratio above the pair and both stores' steady hit rates under it. Reads w16_latency_score.py engine's engine_arms.csv per
regime; draws nothing it computes itself. A pair whose format was not timed is named in the caption, not drawn. A format
with no pair in the scorer (Per-type CSR: no single layout is registered as its nearest) is not in the file.

    python3 experiments/layout_choice/plot_w16_engine_arms.py --sf 3 --root <latency root> [--out <png>]
"""
import argparse
import csv
import os
import sys
import textwrap

import matplotlib
from kvevaluator.paths import ROOT, figure_dir, refuse_results
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.style import apply_house_style, format_color
from kvcost.plotting.formatting import number_to_human

CELL_COLOR = "0.55"       # a cell of the configurable format has no palette entry: one neutral grey for every cell

from kvcost.layout_cost import settings as cells  # (the cells' short names)
from w16_latency_score import FORMAT_LABEL  # (one name per store, the scorer's)
REGIME_TITLE = {"steady": "steady (bounded cache)", "warm": "warm", "cold": "cold"}


CLASSES = (("I02", "every read (I02)"), ("W17_noIC3", "every read but IC3"))
VERDICT = {"prediction": "registered: {v}", "descriptive": "descriptive", "unregistered": "reported"}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--root", required=True, help="the latency root: <root>/analysis/<regime>/engine_arms.csv")
    ap.add_argument("--out", default=None)
    ap.add_argument("--extralarge", type=int, default=3)
    ap.add_argument("--note", default="", help="a line in the caption (what a first pass has not measured yet)")
    a = ap.parse_args()
    apply_house_style(extralarge=a.extralarge)
    xl = a.extralarge
    base = os.path.join(a.root, "analysis")
    regimes = [d for d in sorted(os.listdir(base)) if os.path.exists(os.path.join(base, d, "engine_arms.csv"))]
    if not regimes:
        raise SystemExit(f"no analysis dir with engine_arms.csv under {base}")
    regimes.sort(key=lambda r: ("steady", "warm", "cold").index(r.split("-")[0]))
    data = {r: list(csv.DictReader(open(os.path.join(base, r, "engine_arms.csv")))) for r in regimes}
    npair = max(sum(1 for r in rows if r["class"] == "I02" and r["cell_ns"] and r["format_ns"]) for rows in data.values())
    fig, axes = plt.subplots(len(regimes), len(CLASSES), figsize=(len(CLASSES) * (2.7 * max(npair, 1) + 1.6), 5.6 * len(regimes) + 1.5),
                             squeeze=False, sharey="row")
    left_out = set()
    for i, regime in enumerate(regimes):
        for j, (cls, cls_name) in enumerate(CLASSES):
            ax, x, ticks, labels = axes[i][j], 0, [], []
            for r in data[regime]:
                if r["class"] != cls and not (cls == "W17_noIC3" and not r["class"]):
                    continue
                if not r["cell_ns"] or not r["format_ns"]:              # no capture on one side: said in the caption
                    left_out.add(f"the {FORMAT_LABEL.get(r['format'], r['format'])}{' with unsplit lists' if r['format'] == 'dgraph' else ''} ({r['word']})")
                    continue
                c, f = float(r["cell_ns"]) / 1e9, float(r["format_ns"]) / 1e9
                ax.bar(x, c, 0.8, color=CELL_COLOR, edgecolor="white")
                ax.bar(x + 1, f, 0.8, color=format_color(r["format"].split("-")[0]), edgecolor="black", linewidth=1.2)
                word = r["word"].replace("cell", "the single layout is").replace("no measurable difference", "no measurable diff.")
                ax.annotate(f"{word}\n{VERDICT[r['kind']].format(v=r['verdict'].replace(' (', chr(10) + '('))}\nlayout / format = {c / f:.2f}",
                            (x + 0.5, 0.985), xycoords=("data", "axes fraction"), ha="center", va="top", fontsize=6.5 + xl, color="0.2")
                if regime.startswith("steady") and r.get("hit_rate_cell") not in ("", "nan", None):
                    ax.annotate(f"hit rate {float(r['hit_rate_cell']):.2f} | {float(r['hit_rate_format']):.2f}", (x + 0.5, 0), xycoords=("data", "axes fraction"),
                                xytext=(0, -46 - 4 * xl), textcoords="offset points", ha="center", va="top", fontsize=6.5 + xl, color="0.35")
                ticks += [x, x + 1]
                labels += [cells.short_names(r["cell_label"]),       # two lines each: the pair's labels sit one bar apart
                           FORMAT_LABEL.get(r["format"], r["format_label"]).replace(" (", "\n(").replace(" format", "\nformat")]
                x += 3
            ax.set_xticks(ticks, labels, fontsize=7 + xl)
            ax.set_xlim(-0.8, max(x - 1.2, 1.8))
            ax.set_ylim(0, ax.get_ylim()[1] * 1.38)         # room for the word inside the axes, above the pair
            ax.set_title(f"{cls_name}, {REGIME_TITLE[regime.split('-')[0]]}", fontsize=9 + xl)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
            ax.grid(axis="y", alpha=0.3)
            if j == 0:
                ax.set_ylabel("the reads' time, s per 100,000 updates", fontsize=8 + xl)
    fig.suptitle(f"LDBC Interactive, LDBC frequencies, reads only, at SF{a.sf}:\neach named format on its own store (colour) against the single layout nearest to it (grey)",
                 fontsize=9.5 + xl)
    cap = ("A bar = the sum over the queries of (runs per 100,000 updates x the median over 5 params of one run). The word is the scorer's, under the "
           "class's tie band; a registered row was predicted before the run. Layout / format below 1: the single layout is faster. "
           "IC3 is left out in the right column because the Dgraph format's date index makes IC3 another query there."
           + (" Not drawn: " + "; ".join(sorted(left_out)) + "." if left_out else "") + (" " + a.note if a.note else ""))
    fig.text(0.5, 0.012, textwrap.fill(cap, 150), ha="center", va="bottom", fontsize=6.5 + xl)
    fh = fig.get_figheight()
    fig.subplots_adjust(top=1 - 1.15 / fh, bottom=2.35 / fh, left=1.1 / fig.get_figwidth(), right=0.985, wspace=0.06, hspace=0.75)
    if a.out:                                       # --out is the figure's file
        out = os.path.join(refuse_results(os.path.dirname(os.path.abspath(a.out))), os.path.basename(a.out))
    else:
        out = os.path.join(figure_dir(os.path.join(a.root, "analysis")), "w16_engine_arms.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    fig.savefig(out, dpi=150)
    fig.savefig(out[:-4] + ".pdf")
    print(f"-> {out}; pairs not drawn: {sorted(left_out) or 'none'}")


if __name__ == "__main__":
    main()
