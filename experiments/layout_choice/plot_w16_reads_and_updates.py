#!/usr/bin/env python3
"""I01's time per store, reads and updates: the reads' steady time (I02: the 19 read queries, 307 MB cache) stacked with
the updates' timed apply (IU: the 8 updates applied once on a copy of the store, warm), and the updates alone below.
Reads the scorer's w16_latency.csv (w16_latency_score.py score --all-arms): W17_median and IU_median per store, in ns
per 100,000 updates of the mix. Stores without a row in the file are left out.

    python3 experiments/layout_choice/plot_w16_reads_and_updates.py --latency <w16_latency.csv> [--latency-4k <w16_latency.csv>]
        [--latency-extra <w16_latency.csv> ...] --out DIR
        -> <out>/i01-time-reads-and-updates.png"""
import argparse
import csv
import os
import sys

from kvevaluator.paths import refuse_results
from kvevaluator.layouts import SETTINGS, cell_id

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

from plot_workload_story import pair_name

LAYOUTS = SETTINGS
REF = "meta-props_condensed_adj_split"                     # the bars' ratio base (1x) of every I01 figure; not B's unit
FOUR_KB = (("meta-props_split_adj_chunked_c4k", "props_split_adj_chunked"), ("meta-props_condensed_adj_chunked_c4k", "props_condensed_adj_chunked"),
           ("meta-props_chunked_c4k_adj_split", "props_chunked_adj_split"), ("meta-props_chunked_c4k_adj_condensed", "props_chunked_adj_condensed"),
           ("meta-props_chunked_c4k_adj_chunked_c4k", "props_chunked_adj_chunked"))
GROUPS = [("named formats", [("dgraph-c512k", "Dgraph format\n(512 KB)"), ("nebula", "NebulaGraph\nformat")]),
          ("the nine layouts (Chunked = 64 KB)", [("meta-" + cell_id(p, a), pair_name(cell_id(p, a))) for p in LAYOUTS for a in LAYOUTS]),
          ("Chunked = 4 KB", [(arm, pair_name(cell)) for arm, cell in FOUR_KB]),
          ("mixed layout, built\n(B x Co-Sp's bytes)", [(f"meta-w16_b{t}", f"within B = {b}") for t, b in (("05", "0.5"), ("1", "1"), ("2", "2"), ("3", "3"))]
           + [("meta-w16_b1_per_part_4k_16k_64k", "within B = 1,\na chunk size\nper part")])]
READS, UPDATES = "#9ecae1", "#e6550d"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--latency", required=True, help="the scorer's w16_latency.csv (the stores of the steady run)")
    ap.add_argument("--latency-4k", default=None, help="the same file for the five cells with Chunked at 4 KB")
    ap.add_argument("--latency-extra", action="append", default=[], help="the same file for another set of stores (repeatable)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    R = {}
    for path in filter(None, [a.latency, a.latency_4k] + a.latency_extra):
        with open(path) as f:
            for r in csv.DictReader(f):
                if r["W17_median"] and r["IU_median"]:
                    R[r["arm"]] = (float(r["W17_median"]) / 1e9, float(r["IU_median"]) / 1e9)
    bars, edges, heads, left_out = [], [], [], []
    for g, items in GROUPS:
        have = [it for it in items if it[0] in R]
        left_out += [it[0] for it in items if it[0] not in R]
        if have:
            heads.append((len(bars), len(bars) + len(have), g))
            bars += have
            edges.append(len(bars) - 0.5)
    if REF not in R:
        sys.exit(f"the reference {REF} has no row")
    fig, axes = plt.subplots(2, 1, figsize=(15.5, 11.4), sharex=True, gridspec_kw={"hspace": 0.08, "height_ratios": (1.25, 1)})
    ax = axes[0]
    ref, top = sum(R[REF]), max(sum(R[s]) for s, _ in bars)
    for x, (s, _) in enumerate(bars):
        rd, up = R[s]
        ax.bar(x, rd, 0.8, color=READS, edgecolor="0.3", linewidth=0.4)
        ax.bar(x, up, 0.8, bottom=rd, color=UPDATES, edgecolor="0.3", linewidth=0.4)
        ax.text(x, rd + up + top * 0.012, f"{rd + up:,.0f}\n" + ("1x" if s == REF else f"{(rd + up) / ref:.2f}x"), ha="center", va="bottom",
                fontsize=9, fontweight="bold" if s == REF else None)
    ax.axhline(ref, color="#1f77b4", lw=1, ls="--")
    ax.set_ylim(0, top * 1.16)
    ax.set_ylabel("I01: reads + updates\ns per 100,000 updates", fontsize=11)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax = axes[1]
    ref = R[REF][1]
    for x, (s, _) in enumerate(bars):
        up = R[s][1]
        ax.bar(x, up, 0.8, color=UPDATES, edgecolor="0.3", linewidth=0.4)
        ax.text(x, up * 1.12, f"{up:,.1f}\n" + ("1x" if s == REF else f"{up / ref:.2g}x" if up / ref < 99.5 else f"{up / ref:.0f}x"), ha="center",
                va="bottom", fontsize=9, fontweight="bold" if s == REF else None)
    ax.axhline(ref, color="#1f77b4", lw=1, ls="--")
    ax.set_yscale("log")
    ax.set_ylim(min(v[1] for v in R.values()) / 2, max(v[1] for v in R.values()) * 8)
    ax.set_ylabel("the updates alone (log)\ns per 100,000 updates", fontsize=11)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}" if v >= 1 else f"{v:g}"))
    ax.set_xticks(range(len(bars)), [b[1] for b in bars], rotation=90, fontsize=10)
    for x in axes:
        for e in edges[:-1]:
            x.axvline(e, color="black", lw=1)
        x.grid(axis="y", alpha=0.3)
        x.set_axisbelow(True)
        x.set_xlim(-0.6, len(bars) - 0.4)
    for lo, hi, g in heads:
        axes[0].text((lo + hi - 1) / 2, 1.02, g, transform=axes[0].get_xaxis_transform(), ha="center", va="bottom", fontsize=11, fontweight="bold")
    fig.legend(handles=[Patch(color=READS, label="the 19 read queries (I02): steady, 307 MB cache"),
                        Patch(color=UPDATES, label="the 8 updates: timed apply, warm")], loc="lower center", bbox_to_anchor=(0.5, 0.062), ncol=2,
               frameon=False, fontsize=10)
    fig.suptitle("I01 (LDBC's own mix) at SF3: time of the whole mix per store, reads and updates", fontsize=13.5, y=0.975)
    fig.text(0.5, 0.01, "Bar label: seconds per 100,000 updates of the mix, and the multiple of the reference layout Condensed/Split in that row (the dashed line; "
             "below 1x = faster). A layout is named properties/adjacency.\nReads: steady, 307 MB cache (direct I/O), the scorer's median total of the 19 read queries "
             "at the mix's frequencies. Updates: timed apply, warm: the 8 updates applied once, in the mix's sequence,\non a copy of the store "
             "(4 GB memtable, no flush), their apply time at the mix's frequencies. The same store and build for both (the SF3 re-measure's build, for the updates, "
             "the 4 KB cells and the mix with a chunk size per part).\nThe NebulaGraph format's keys are vid-first on this build; under a bounded cache that order reads about twice the blocks at equal counts "
             "(snapshot nebula-key-order-steady-20261011).", ha="center", va="bottom", fontsize=8.5)
    fig.subplots_adjust(left=0.075, right=0.99, top=0.92, bottom=0.295)
    out = refuse_results(a.out)
    os.makedirs(out, exist_ok=True)
    fig.savefig(os.path.join(out, "i01-time-reads-and-updates.png"), dpi=130)
    print(f"wrote i01-time-reads-and-updates.png: {len(bars)} stores" + (f"; no row for {', '.join(left_out)}" if left_out else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
