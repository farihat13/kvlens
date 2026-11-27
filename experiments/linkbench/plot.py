"""LinkBench wide-read plots -- dgraph vs nebula vs arango per access pattern, in the repo house
style (kvcost.plotting.style), inspired by the kvstats stacked-op figures. Two figures, each with an
n1m + n10m panel so the scale-invariance reads visually:

  linkbench-ops.png    weighted KV ops (110·S+100·G+N) per pattern, one stacked bar per db
                       (color = op S/G/N, hatch = db), log y. Shows getNode dgraph≫nebula (per-field
                       GETs vs 1 row GET) and getLinkList's nebula NEXT fan-out.
  linkbench-bytes.png  total bytes read per pattern, grouped bars by db, log y.

Reads each scale's wide-read capture (the output tree WIDE_READ: grid_bodies --format on the four anchored queries,
run-linkbench.sh), median over params.
Run with the ROOT venv:  python3 experiments/linkbench/plot.py [--out DIR]   (default output/figures/linkbench-wide-read/<date>)
"""
import datetime
import os
import statistics as st
import sys

import matplotlib
from kvevaluator.output_tree import Tree
from kvevaluator.paths import ROOT
matplotlib.use("Agg")                                    # headless: savefig only
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

_EXP = str(ROOT)   # experiment root
from kvcost.plotting.style import apply_house_style, op_color, op_label, db_color, db_label, bar_kw, bar_offsets
from kvcost.plotting.formatting import number_to_human, bytes_to_human
from kvevaluator import weights
from kvevaluator.measure import kvstats_layout as L

EL = 3
apply_house_style(extralarge=EL)

WEIGHT = weights.DEFAULT                                 # G:S:N = 100:110:1
OPS = ["G", "S", "N"]                                    # G at the bottom of the stack
PATTERNS = ["getnode", "composed_getnode", "getlinklist_link1", "getlinklist_link2"]
PLABEL = {"getnode": "getNode", "composed_getnode": "composed\ngetNode (link1)",
          "getlinklist_link1": "getLinkList\n(link1)", "getlinklist_link2": "getLinkList\n(link2)"}
DBS = ["dgraph", "nebula", "arangodb"]


WIDE_READ = Tree().study("linkbench-wide-read")   # run-linkbench.sh's grid_bodies --root


def wide_read(scale):
    """the scale's wide-read capture (run-linkbench.sh: grid_bodies --format --queries <PATTERNS> --root WIDE_READ)"""
    return WIDE_READ.capture("linkbench", scale)


def _present(scale):
    """the DBS the scale's wide-read capture holds"""
    return [db for db in DBS if os.path.exists(wide_read(scale).path_of("counts", db))]


# the study's scales (a smoke/tiny run's capture is deliberately NOT plotted); dated OUT
_ALL_SCALES = [("n1m", "1M"), ("n10m", "10M")]
SCALES = [(s, l) for s, l in _ALL_SCALES if _present(s)]
OUT = str(Tree().figure("linkbench-wide-read", datetime.date.today().strftime("%Y%m%d")))


def load(scale):
    """MEDIAN S/G/N + median bytes read over params, per (pattern, db) -- the same aggregation
    the results report (a mean would hub-inflate getLinkList to hide the typical-node near-tie).
    Reads the scale's formats capture (what the driver writes)."""
    rows = [{**r, "db": db} for db in _present(scale)
            for r in L.store_counts(scale, "linkbench", db, wide_read(scale).path_of("counts", db))]
    agg = {}
    for q in PATTERNS:
        for db in DBS:
            rs = [r for r in rows if r["query"] == q and r["db"] == db]
            if not rs:
                continue
            agg[(q, db)] = {op: st.median([int(r[op]) for r in rs]) for op in ("S", "G", "N")}
            agg[(q, db)]["bytes"] = st.median([float(r["read_bytes"]) for r in rs])
    return agg


def plot_ops():
    fig, axes = plt.subplots(1, len(SCALES), figsize=(6.4 * len(SCALES), 5.2), squeeze=False)
    bw = 0.8 / len(DBS)
    offs = bar_offsets(len(DBS), bw)
    floor = 1.0                                          # log y can't start a bar at 0
    for ax, (scale, slab) in zip(axes[0], SCALES):
        agg = load(scale)
        xs = range(len(PATTERNS))
        for db, off in zip(DBS, offs):
            for qi, q in enumerate(PATTERNS):
                a = agg.get((q, db))
                if not a:
                    continue
                cum = 0.0
                for op in OPS:
                    h = WEIGHT[op] * a[op]
                    base = max(cum, floor)
                    ax.bar(qi + off, max(cum + h - base, 0), bw, bottom=base,
                           color=op_color(op), **bar_kw(db))
                    cum += h
                if cum > 0:
                    ax.text(qi + off, cum, f" {number_to_human(cum)}", ha="center", va="bottom",
                            rotation=90, fontsize=6 + EL)
        ax.set_yscale("log")
        pmax = max((sum(WEIGHT[op] * agg[(q, db)][op] for op in OPS) for (q, db) in agg), default=1)
        ax.set_ylim(1, pmax * 4)                         # headroom for the rotated top-value labels
        ax.set_xticks(list(xs))
        ax.set_xticklabels([PLABEL[q] for q in PATTERNS])
        ax.set_title(f"LinkBench {slab} nodes")
        ax.yaxis.set_major_formatter(lambda v, _: number_to_human(v))
        ax.grid(axis="y", ls=":", alpha=0.4)
    axes[0][0].set_ylabel("weighted KV ops per read\n(110·S + 100·G + N, median over params, log)")
    legend = [Patch(facecolor=op_color(o), label=op_label(o)) for o in OPS] + \
             [Patch(facecolor="0.7", label=db_label(d), **bar_kw(d)) for d in DBS]
    axes[0][0].legend(handles=legend, ncol=len(OPS) + len(DBS), loc="lower center",
                      bbox_to_anchor=(1.0, 1.06), frameon=False)
    fig.suptitle("LinkBench wide-read: weighted KV ops per access pattern (dgraph / nebula / arango)",
                 y=1.12, fontsize=15 + EL)
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "linkbench-ops.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def plot_bytes():
    fig, axes = plt.subplots(1, len(SCALES), figsize=(6.4 * len(SCALES), 5.0), squeeze=False)
    bw = 0.8 / len(DBS)
    offs = bar_offsets(len(DBS), bw)
    for ax, (scale, slab) in zip(axes[0], SCALES):
        agg = load(scale)
        xs = range(len(PATTERNS))
        for db, off in zip(DBS, offs):
            vals = [agg[(q, db)]["bytes"] if (q, db) in agg else None for q in PATTERNS]
            xpos = [qi + off for qi in xs]
            xp = [x for x, v in zip(xpos, vals) if v]
            vv = [v for v in vals if v]
            ax.bar(xp, vv, bw, color=db_color(db), label=db_label(db), **bar_kw(db))
            for x, v in zip(xp, vv):
                ax.text(x, v, f" {bytes_to_human(v)}", ha="center", va="bottom",
                        rotation=90, fontsize=6 + EL)
        ax.set_yscale("log")
        pmax = max((agg[(q, db)]["bytes"] for (q, db) in agg), default=1)
        ax.set_ylim(top=pmax * 1.7)                      # headroom for the rotated top-value labels
        ax.set_xticks(list(xs))
        ax.set_xticklabels([PLABEL[q] for q in PATTERNS])
        ax.set_title(f"LinkBench {slab} nodes")
        ax.yaxis.set_major_formatter(lambda v, _: bytes_to_human(v))
        ax.grid(axis="y", ls=":", alpha=0.4)
    axes[0][0].set_ylabel("bytes read per access\n(median over params, log)")
    axes[0][0].legend(ncol=len(DBS), loc="lower center", bbox_to_anchor=(1.0, 1.02), frameon=False)
    fig.suptitle("LinkBench wide-read: bytes read per access pattern (dgraph / nebula / arango)",
                 y=1.08, fontsize=15 + EL)
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "linkbench-bytes.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


if __name__ == "__main__":
    if "--out" in sys.argv:   # a snapshot's render (snapshot-meta.json): the figures go into its scratch copy
        OUT = sys.argv[sys.argv.index("--out") + 1]
    if not SCALES:   # only a smoke/tiny run so far: nothing the figures draw
        print(f"no wide-read capture for {', '.join(s for s, _ in _ALL_SCALES)} yet -- figures skipped")
        raise SystemExit(0)
    for f in (plot_ops(), plot_bytes()):
        print("wrote", os.path.relpath(f, _EXP))
