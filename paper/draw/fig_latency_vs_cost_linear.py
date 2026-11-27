#!/usr/bin/env python3
# released from gs-paper at 62acd0b (evidence/latency-steady-ldbc/fig_latency_vs_cost_linear.py); edit it there, then run evidence/release_draw.py
"""Paper figure: latency-vs-cost.pdf on linear axes from 0, one column.

  figures/evaluation/latency-vs-cost-linear.pdf               (with the dashed y = x)

The same 12 layouts per workload as latency-vs-cost.pdf: x = combined cost (wops + bytes / 4,096) over the cheapest
layout, y = steady latency over the fastest; x cut at 6x in (a) Interactive and (b) BI and at 10x in (c) JOB (its
Ch256k is at 8.7x), y at 4x (a point past a cut stops the draw). One x scale in every panel: (c) is wider by its range,
so the dashed line y = x (labelled in (c); equal ratios: a point under it is slower by a smaller factor than it is
costlier) has the same slope in all three; (a) and (b) square; colour = adjacency, marker = properties; Dgraph's layout (Sp-Co) and NebulaGraph's (Co-Sp) are marked DG and NG (paper_style's engine marks), each with a leader line in
the engine's colour. The ring is the pick,
the layout with the fewest combined cost; the panel's corner gives its latency over the fastest and Spearman's rho (ρ)
of the 12 (cost against latency).

Reads only its tables, as latency-vs-cost.pdf (load() of fig_latency_vs_cost.py).

    python fig_latency_vs_cost_linear.py --data <dir of the tables> --out <dir>
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

import fig_latency_vs_cost as L  # noqa: E402
import common  # noqa: E402
from common import args, empty_panel, use_paper_font  # noqa: E402
from paper_style import (ADJ_COLOR, CHUNK_SHADE, COLUMN_IN, PROPS_MARKER, cell_style, engine_legend,  # noqa: E402
                         colour_engine_legend, mark_engine_point)

OUTS = ((True, "figures/evaluation/latency-vs-cost-linear.pdf"),)
SP_CO, CO_SP = "props_split_adj_condensed", "props_condensed_adj_split"
OFFSET = {("Interactive", CO_SP): (0, -12), ("Interactive", SP_CO): (-8, 12), ("BI", SP_CO): (6, -12), ("JOB", CO_SP): (0, -12),
          ("JOB", SP_CO): (4, 12)}                                       # where each engine letter has room (points)
XTOP, YTOP = {"Interactive": 6.0, "BI": 6.0, "JOB": 10.0}, 4.0          # x cut per panel: JOB's Ch256k is at 8.7x


def draw(pts, diagonal, out):
    """one version of the figure, with or without the dashed y = x"""
    use_paper_font()
    plt.rcParams.update({"font.size": L.PT})
    foot, head = 0.54, 0.16
    widths = [XTOP[w] for w in ("Interactive", "BI", "JOB")]               # one x scale: a panel's width is its range
    gap = 0.17 * min(widths)                                               # between panels, in x units
    panel_h = (0.975 - 0.1) * COLUMN_IN * min(widths) / (sum(widths) + 2 * gap)   # (a)'s width: (a) square
    H = panel_h + head + foot
    fig, axes = plt.subplots(1, 3, figsize=(COLUMN_IN, H), gridspec_kw={"width_ratios": widths})
    fig.subplots_adjust(left=0.1, right=0.975, top=1 - head / H, bottom=foot / H, wspace=gap / (sum(widths) / 3))
    for i, (ax, w) in enumerate(zip(axes, ("Interactive", "BI", "JOB"))):
        if not any(q[0] == "layout" for q in pts[w]):
            empty_panel(ax, f"({'abc'[i]}) {w}", pad=2)
            ax.set_box_aspect(min(widths) / XTOP[w])
            print(f"{w}: no layout timed in this run: the panel is drawn empty")
            continue
        p, cheapest, fastest = L.to_best([q for q in pts[w] if q[0] == "layout"], False)
        far_x, far_y = max(q[2] for q in p), max(q[3] for q in p)
        xtop = XTOP[w]
        if (far_x > xtop or far_y > YTOP) and common.SCALE == "3":
            raise SystemExit(f"{w}: a point past the cut ({far_x:.2f}x, {far_y:.2f}x)")
        if common.SCALE != "3":                                    # another run's points: the axes take its range
            xtop, ytop = max(xtop, far_x * 1.08), max(YTOP, far_y * 1.08)
        else:
            ytop = YTOP
        for _, cell, x, y in p:
            s = cell_style(cell)
            ax.scatter(x, y, s=12, facecolor="none", edgecolor=s["color"], marker=s["marker"], linewidths=0.7, zorder=3)
            mark_engine_point(ax, x, y, cell, *OFFSET.get((w, cell), (0, 12)), leader=True)
        if diagonal:
            ax.plot([0, xtop], [0, xtop], ls="--", lw=0.6, color="grey", zorder=2)
        if diagonal and w == "JOB":                                             # named once, where it has room: it is not a fit
            ax.text(3.3, 3.0, "y = x", rotation=0, ha="left", va="center", fontsize=L.PT_SMALL, color="0.4",
                    zorder=6)
        pick = next(q for q in p if q[1] == cheapest)              # the pick: the fewest combined cost
        ax.scatter(pick[2], pick[3], s=55, facecolors="none", edgecolors="#222222", linewidths=0.8, zorder=4)
        rho = L.spearman([q[2] for q in p], [q[3] for q in p])
        # the selection regret: the pick's latency over the fastest
        ax.text(0.05, 0.95, f"regret {pick[3]:.2f}x\nρ {rho:.2f}", transform=ax.transAxes, ha="left", va="top",
                fontsize=L.PT_SMALL, linespacing=1.1, zorder=6)
        ax.set_xlim(0, xtop)
        ax.set_ylim(0, ytop)
        ax.set_box_aspect(min(widths) / XTOP[w])                       # the same height for every panel
        xt = [v for v in (0, 2, 4, 6, 8, 10) if v <= xtop]
        ax.set_xticks(xt, ["0"] + [f"{v}x" for v in xt[1:]])
        yt = list(range(0, int(ytop) + 1, 1 if ytop <= 5 else 2))
        ax.set_yticks(yt, ["0"] + [f"{v}x" for v in yt[1:]])
        ax.set_title(f"({'abc'[i]}) {w}", fontsize=L.PT, pad=2)
        ax.grid(True, alpha=0.3, lw=0.4)
        ax.tick_params(labelsize=L.PT_SMALL, pad=1, length=2, width=0.5)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        if i:
            ax.tick_params(labelleft=False)
        print(f"{w}: {len(p)} layouts, cheapest {cheapest} at {pick[3]:.4f}x the fastest ({fastest}); Spearman {rho:.4f}")
    axes[0].set_ylabel("latency / fastest", fontsize=L.PT, labelpad=1)
    axes[1].set_xlabel("op-byte cost / cheapest", fontsize=L.PT, labelpad=0)
    names = [("split", "Sp"), ("condensed", "Co")] + [(k, f"Ch{k}k") for k in (1, 4, 16, 64, 256)]
    adj = [Line2D([], [], ls="", marker="o", ms=3.2, color=ADJ_COLOR[k] if isinstance(k, str) else CHUNK_SHADE[k],
                  label=n) for k, n in names]
    props = [Line2D([], [], ls="", marker=PROPS_MARKER[k], ms=3.2, mfc="none", mec="0.2", mew=0.7, label=n)
             for k, n in names]
    kw = dict(ncol=7, frameon=False, fontsize=L.PT_SMALL, handletextpad=0.05, columnspacing=0.5, borderaxespad=0,
              borderpad=0, handlelength=1.0)
    first = fig.legend(handles=adj, loc="lower left", bbox_to_anchor=(0.16, 0.21 / H), **kw)
    second = fig.legend(handles=props, loc="lower left", bbox_to_anchor=(0.16, 0.12 / H), **kw)
    eh, el = engine_legend()
    colour_engine_legend(fig.legend(eh, el, loc="lower left", bbox_to_anchor=(0.16, 0.0),
                                    **{**kw, "ncol": 2, "handletextpad": 0.3, "columnspacing": 1.0}))
    fig.add_artist(second)
    fig.add_artist(first)
    for y, t in ((0.255, "adjacency"), (0.165, "properties")):
        fig.text(0.155, y / H, t, ha="right", va="center", fontsize=L.PT_SMALL)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def main():
    data, out_dir = args(__doc__)
    pts = L.load(data)
    for diagonal, path in OUTS:
        draw(pts, diagonal, out_dir / path)


if __name__ == "__main__":
    main()
