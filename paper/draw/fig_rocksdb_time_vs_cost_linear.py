#!/usr/bin/env python3
# released from gs-paper at 533e223 (evidence/latency-steady-ldbc/fig_rocksdb_time_vs_cost_linear.py); edit it there, then run evidence/release_draw.py
"""Paper figure: latency-vs-cost-linear.pdf with y = the time inside RocksDB calls instead of the whole latency, one
column.

  figures/evaluation/rocksdb-time-vs-cost-linear.pdf

The same 12 layouts per workload: x = combined cost (wops + bytes / 4,096) over the cheapest layout, y = RocksDB time
over the layout with the least (latency-steady-rocksdb-parts.csv: rocksdb_ns, from the KV-timer pass; Interactive: its
19 reads at LDBC's frequencies, the inserts not KV-timed); y = x dashed. Each panel has its own axes, from 0 to just
past its farthest point (a point past a cut stops the draw). The ring is the pick (the fewest combined cost); the
corner gives its regret (its RocksDB time over the least) and Spearman's rho of the 12. Colour = adjacency, marker =
properties.

Reads only its tables, as latency-vs-cost.pdf (load() of fig_latency_vs_cost.py) plus latency-steady-rocksdb-parts.csv.

    python fig_rocksdb_time_vs_cost_linear.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

import csv  # noqa: E402
import fig_latency_vs_cost as L  # noqa: E402
from common import args, use_paper_font  # noqa: E402
from paper_style import ADJ_COLOR, CHUNK_SHADE, COLUMN_IN, PROPS_MARKER, cell_style  # noqa: E402

OUT = "figures/evaluation/rocksdb-time-vs-cost-linear.pdf"
XTOP = {"Interactive": 4.5, "BI": 5.5, "JOB": 9.5}            # just past the largest cost: 3.96x, 5.11x, 8.68x
YTOP = {"Interactive": 55.0, "BI": 4.0, "JOB": 2.5}            # just past the most RocksDB time: 51.4x, 3.49x, 2.26x
XTICKS = {"Interactive": (0, 1, 2, 3, 4), "BI": (0, 1, 2, 3, 4, 5), "JOB": (0, 2, 4, 6, 8)}
YTICKS = {"Interactive": (0, 10, 20, 30, 40, 50), "BI": (0, 1, 2, 3, 4), "JOB": (0, 1, 2)}


def main():
    data, out_dir = args(__doc__)
    pts = L.load(data)
    rocks = {(r["workload"], r["store"]): float(r["rocksdb_ns"])
             for r in csv.DictReader(open(data / "latency-steady-rocksdb-parts.csv"))}
    key = {"Interactive": "interactive", "BI": "bi", "JOB": "job"}
    pts = {w: [(k, s, x, rocks[(key[w], s)]) for k, s, x, _ in v] for w, v in pts.items()}
    use_paper_font()
    plt.rcParams.update({"font.size": L.PT})
    foot, head = 0.42, 0.16
    panel_w = (0.975 - 0.1) * COLUMN_IN / (3 + 2 * 0.17)
    H = panel_w + head + foot
    fig, axes = plt.subplots(1, 3, figsize=(COLUMN_IN, H))
    fig.subplots_adjust(left=0.1, right=0.975, top=1 - head / H, bottom=foot / H, wspace=0.17)
    for i, (ax, w) in enumerate(zip(axes, ("Interactive", "BI", "JOB"))):
        p, cheapest, fastest = L.to_best([q for q in pts[w] if q[0] == "layout"], False)
        far_x, far_y = max(q[2] for q in p), max(q[3] for q in p)
        xtop, ytop = XTOP[w], YTOP[w]
        if far_x > xtop or far_y > ytop:
            raise SystemExit(f"{w}: a point past the cut ({far_x:.2f}x, {far_y:.2f}x)")
        for _, cell, x, y in p:
            s = cell_style(cell)
            ax.scatter(x, y, s=12, facecolor="none", edgecolor=s["color"], marker=s["marker"], linewidths=0.7, zorder=3)
        ax.plot([0, min(xtop, ytop)], [0, min(xtop, ytop)], ls="--", lw=0.6, color="grey", zorder=2)
        ax.set_xlim(0, xtop)
        ax.set_ylim(0, ytop)
        ax.set_box_aspect(1)
        ax.set_aspect("auto")
        xt = XTICKS[w]
        ax.set_xticks(xt, ["0"] + [f"{v}x" for v in xt[1:]])
        ax.set_yticks(YTICKS[w], ["0"] + [f"{v}x" for v in YTICKS[w][1:]])
        ax.set_title(f"({'abc'[i]}) {w}", fontsize=L.PT, pad=2)
        ax.grid(True, alpha=0.3, lw=0.4)
        ax.tick_params(labelsize=L.PT_SMALL, pad=1, length=2, width=0.5)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        r = L.spearman([q[2] for q in p], [q[3] for q in p])
        pick = next(q for q in p if q[1] == cheapest)          # the pick: the fewest combined cost
        ax.scatter(pick[2], pick[3], s=55, facecolors="none", edgecolors="#222222", linewidths=0.8, zorder=4)
        # the selection regret: the pick's RocksDB time over the least
        ax.text(0.05, 0.95, f"regret {pick[3]:.2f}x\nρ {r:.2f}", transform=ax.transAxes, ha="left", va="top",
                fontsize=L.PT_SMALL, linespacing=1.1, zorder=6)
        print(f"{w}: {len(p)} layouts, cheapest {cheapest} at {pick[3]:.3f}x the least RocksDB time ({fastest}); y to {far_y:.2f}x; rho {r:.2f}")
    axes[0].set_ylabel("RocksDB time / least", fontsize=L.PT, labelpad=1)
    axes[1].set_xlabel("combined cost / cheapest", fontsize=L.PT, labelpad=0)
    names = [("split", "Sp"), ("condensed", "Co")] + [(k, f"Ch{k}k") for k in (1, 4, 16, 64, 256)]
    adj = [Line2D([], [], ls="", marker="o", ms=3.2, color=ADJ_COLOR[k] if isinstance(k, str) else CHUNK_SHADE[k],
                  label=n) for k, n in names]
    props = [Line2D([], [], ls="", marker=PROPS_MARKER[k], ms=3.2, mfc="none", mec="0.2", mew=0.7, label=n)
             for k, n in names]
    kw = dict(ncol=7, frameon=False, fontsize=L.PT_SMALL, handletextpad=0.05, columnspacing=0.5, borderaxespad=0,
              borderpad=0, handlelength=1.0)
    first = fig.legend(handles=adj, loc="lower left", bbox_to_anchor=(0.16, 0.09 / H), **kw)
    fig.legend(handles=props, loc="lower left", bbox_to_anchor=(0.16, 0.0), **kw)
    fig.add_artist(first)
    for y, t in ((0.135, "adjacency"), (0.045, "properties")):
        fig.text(0.155, y / H, t, ha="right", va="center", fontsize=L.PT_SMALL)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
