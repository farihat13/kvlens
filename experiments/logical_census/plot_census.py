#!/usr/bin/env python3
"""Draw the logical access census (census.py's CSVs): per query, the elements a run touches and their kinds; per
workload (I01; LDBC Interactive and BI, each with every query once and every query equal), the share of each kind.

    uv run --locked python experiments/logical_census/plot_census.py --in DIR
Writes <in>/census_per_query.{png,pdf} and <in>/census_workloads.{png,pdf}.
"""
import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker
from kvcost.plotting.formatting import number_to_human
from kvcost.plotting.style import OP_COLOR_BESIDE_CELLS, apply_house_style

# drawn bottom-up; the colors of read_parts (adjacency in blues: edges by access path, then its edge-props slice;
# vertex props, index) plus label checks and writes (a brown off the cell scheme's colours: Split is purple)
KINDS = ("edges_list", "edges_probe", "edge_props_list", "edge_props_probe", "label_checks", "vertex_props",
         "index_entries", "items_written")
LABEL = {"edges_list": "edges via list expansion", "edges_probe": "edges via a direct probe (one per pair)",
         "edge_props_list": "edge properties read with the list",
         "edge_props_probe": "edge properties by a direct edge read",
         "label_checks": "label checks (answered from the edge read)", "vertex_props": "vertex properties read",
         "index_entries": "index entries crossed", "items_written": "items written (vertices, edges, properties)"}
COLOR = {"edges_list": "#2171b5", "edges_probe": "#08306b", "edge_props_list": "#9ecae1", "edge_props_probe": "#c6dbef",
         "label_checks": "#fdd0a2", "vertex_props": "#e6550d", "index_entries": "#31a354",
         "items_written": OP_COLOR_BESIDE_CELLS["P"]}
DARK = {"edges_list", "edges_probe", "vertex_props", "index_entries", "items_written"}   # a white share label on these
NO_PROBE_PROPS = "No plan reads edge properties by a direct edge read (its column is 0; not in the legend)."
PANELS = (("interactive", "ic", "LDBC Interactive complex reads"), ("interactive", "is", "short reads"),
          ("writes", "iu", "updates"), ("bi", "bi", "LDBC BI"))
WORKLOADS = (("i01", "I01: LDBC Interactive\n(LDBC's frequencies)"),
             ("ldbc_once", "LDBC Interactive,\nevery query once"), ("ldbc_equal", "LDBC Interactive,\nevery query equal"),
             ("bi_once", "LDBC BI,\nevery query once"), ("bi_equal", "LDBC BI,\nevery query equal"))


def read(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def total(r):
    return sum(float(r[k]) for k in KINDS)


def drawn(rows):
    """the kinds in the legend: every kind, less edge props by a direct edge read when no row has any"""
    return [k for k in KINDS if k != "edge_props_probe" or any(float(r[k]) for r in rows)]


def per_query(rows, fig_path):
    """top row: elements per run (log; whisker = min..max over every captured body); bottom row: the kinds' shares"""
    chosen = [r for r in rows if r["chosen"] == "1"]
    spread = {}
    for r in rows:
        spread.setdefault((r["suite"], r["query"]), []).append(total(r))
    groups = []
    for suite, prefix, title in PANELS:
        qs = sorted((r for r in chosen if r["suite"] == suite and r["query"].startswith(prefix)),
                    key=lambda r: int(r["query"][len(prefix):]))
        groups.append((title, qs))
    n = sum(len(qs) for _, qs in groups)
    print(f"[per query] {n} queries drawn: " + ", ".join(f"{t} {len(qs)}" for t, qs in groups))
    if n != len(chosen):
        raise SystemExit(f"plot_census: {len(chosen)} chosen bodies, {n} drawn")
    fig, axes = plt.subplots(2, len(groups), figsize=(15, 6.2), sharey="row", squeeze=False,
                             gridspec_kw={"width_ratios": [len(qs) for _, qs in groups], "height_ratios": [1.3, 1],
                                          "wspace": 0.04, "hspace": 0.08})
    for j, (title, qs) in enumerate(groups):
        top, bot = axes[0][j], axes[1][j]
        x = list(range(len(qs)))
        for i, r in enumerate(qs):
            t = total(r)
            lo, hi = min(spread[(r["suite"], r["query"])]), max(spread[(r["suite"], r["query"])])
            top.bar(i, t, 0.7, color="0.55")
            if hi > lo:
                top.errorbar(i, t, yerr=[[t - lo], [hi - t]], color="black", lw=0.9, capsize=2.5)
            top.text(i, hi * 1.25, number_to_human(t) + ("≈" if r["approx"] == "1" else ""), ha="center",
                     va="bottom", fontsize=7, rotation=90)
            base = 0.0
            for k in KINDS:
                s = float(r[k]) / t if t else 0.0
                if s > 0:
                    bot.bar(i, s, 0.7, bottom=base, color=COLOR[k], edgecolor="white", lw=0.3)
                base += s
        top.set_yscale("log")
        top.set_ylim(1, 3e9)
        top.set_title(title, fontsize=10)
        bot.set_ylim(0, 1)
        for ax in (top, bot):
            ax.set_xlim(-0.6, len(qs) - 0.4)
            ax.set_xticks(x)
        top.set_xticklabels([])
        bot.set_xticklabels([r["query"].upper() for r in qs], rotation=90, fontsize=8)
    axes[0][0].set_ylabel("elements per run")
    axes[1][0].set_ylabel("share of the elements")
    ks = drawn(rows)
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLOR[k]) for k in ks]
    fig.legend(handles, [LABEL[k] for k in ks], loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=4,
               frameon=False, fontsize=9)
    fig.suptitle("What each query touches, the same on every layout (LDBC sf3)", y=0.97)
    fig.text(0.5, -0.115, "Counted from the plans' step rows, equal on all nine uniform layouts; the mean over the 5 "
             "reported params. Per query, its cheapest plan body valid on both Dgraph and NebulaGraph (the fewest elements). Whisker: "
             "min..max over every body we captured, layout-specific ones included. ≈: a closure or chase step (edges "
             "counted as the rows it returns). A direct probe: read_edge_exists, one edge per pair (IS7). Updates: the items "
             "their plan inserts (index entries not counted)." + ("" if "edge_props_probe" in ks else " " + NO_PROBE_PROPS),
             ha="center", va="top", fontsize=8, wrap=True)
    for ext in ("png", "pdf"):
        fig.savefig(f"{fig_path}.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"-> {fig_path}.png")


def per_workload(tot, rows, fig_path):
    """one 100% bar per workload: the share of each kind, with the elements per run (or the query count) beside it"""
    by = {r["workload"]: r for r in tot}
    fig, ax = plt.subplots(figsize=(9, 4.4))
    for y, (wl, name) in enumerate(WORKLOADS):
        r = by[wl]
        t = total(r)
        left = 0.0
        for k in KINDS:
            s = float(r[k]) / t
            if s > 0:
                ax.barh(y, s, left=left, height=0.6, color=COLOR[k], edgecolor="white", lw=0.4)
                if s >= 0.04:
                    ax.text(left + s / 2, y, f"{s:.0%}", ha="center", va="center", fontsize=8,
                            color="white" if k in DARK else "0.15")
            left += s
        side = (f"{number_to_human(t / float(r['n_runs']))} per run" if r["measure"] == "elements"
                else f"{r['n_queries']} queries, shares averaged")
        if float(r["items_written"]) > 0:
            side += f"\nwrites {float(r['items_written']) / t * 100:.2g}%"
        ax.text(1.01, y, side, va="center", fontsize=8)
        print(f"[workloads] {wl}: " + ", ".join(f"{k} {float(r[k]) / t:.4f}" for k in KINDS) + f"; {side!r}")
    ax.set_yticks(range(len(WORKLOADS)))
    ax.set_yticklabels([n for _, n in WORKLOADS], fontsize=9)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.tick_params(axis="x", labelsize=9)
    ax.set_xlabel("share of the elements the workload touches", fontsize=10)
    ks = drawn(rows)
    handles = [plt.Rectangle((0, 0), 1, 1, color=COLOR[k]) for k in ks]
    ax.legend(handles, [LABEL[k] for k in ks], loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3,
              frameon=False, fontsize=8)
    ax.set_title("Which part of the graph each workload touches (LDBC sf3, the same on every layout)", fontsize=10)
    fig.text(0.5, -0.2, "Every query once: each query's elements summed, so the big queries dominate. Every query equal: "
             "each query's shares of its own elements, averaged over the queries (not elements summed). LDBC Interactive "
             "includes the 8 updates (IU1-IU8): their elements are the items they write. I01: LDBC's frequencies (the "
             "reference)." + ("" if "edge_props_probe" in ks else " " + NO_PROBE_PROPS), ha="center", va="top", fontsize=8,
             wrap=True)
    for ext in ("png", "pdf"):
        fig.savefig(f"{fig_path}.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"-> {fig_path}.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--in", dest="inp", required=True, help="census.py's --out dir")
    a = ap.parse_args()
    apply_house_style(3)
    per_query(read(os.path.join(a.inp, "census.csv")), os.path.join(a.inp, "census_per_query"))
    per_workload(read(os.path.join(a.inp, "census_totals.csv")), read(os.path.join(a.inp, "census.csv")),
                 os.path.join(a.inp, "census_workloads"))


if __name__ == "__main__":
    main()
