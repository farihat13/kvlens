#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/predict-49/fig_predict_49_w16_bi_job.py); edit it there, then run evidence/release_draw.py
"""Paper figure: predict-49 with JOB as a third panel: predict mode at tier 1 (counts and means only, no store) against
the measured cost on the 49 uniform layouts; one column.

  figures/evaluation/predict-49-w16-bi-job.pdf

(a) LDBC Interactive (W16: its mix with its updates, SF3), (b) LDBC BI (B01: 17 queries, SF3) and (c) JOB (J01: its 113
queries on the full IMDb, each 1 / its measured combined cost on Sp-Co), all in wops_bytes4k. (c) was drawn in wops until
before the 2026-11-11 snapshot (most common values and highest degrees), tier 1 priced job1b and job1d about
30,000x their measured cost and the combined-cost ranking had Spearman -0.01; now it is 0.955, the pick 1.038x the best.
Each panel: one point per layout (colour = adjacency, marker = properties; Dgraph's layout (Sp-Co) and NebulaGraph's (Co-Sp) are marked DG and NG (paper_style's engine marks), each with a leader line in the
engine's colour), x = measured over the measured best, y =
predicted over predict's cheapest, log-log; dashed y = x; the ring is predict's pick, and the line above gives its
measured cost over the measured best and Spearman's rho (ρ) of the 49. A layout predicted past CAP sits
at the top edge (open), the count and range written in the panel. The x axis ends just past 5x in (b); (a) and (c) end at the y axis's top.

Reads only its tables: predict-49-i01.csv, predict-49-b01.csv, predict-49-j01.csv (per model and layout: predicted,
measured).

    python fig_predict_49_w16_bi_job.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedFormatter, FixedLocator, NullFormatter  # noqa: E402

import common  # noqa: E402
from common import args, empty_panel, has_table, read_table, use_paper_font  # noqa: E402
from fig_predict_49 import CAP, PAD, PT, PT_SMALL, legend, panel_data  # noqa: E402
from paper_style import COLUMN_IN, cell_style, mark_engine_point  # noqa: E402

PANELS = (("predict-49-i01", "wops_bytes4k", "(a) LDBC Interactive, {sf}", None),
          ("predict-49-b01", "wops_bytes4k", "(b) LDBC BI, {sf}", 5.3),      # x to just past 5x (Sp-Sp at 5.11x)
          ("predict-49-j01", "wops_bytes4k", "(c) JOB", None))
OUT = "figures/evaluation/predict-49-w16-bi-job.pdf"
NOTE, NOTE_IN = "predict-49-set", 0.14      # predict_49.py's table for another predicted set; its line's height (in)
SP_CO, CO_SP = "props_split_adj_condensed", "props_condensed_adj_split"
OFFSET = {("(b)", CO_SP): (-1, -13), ("(b)", SP_CO): (10, -7), ("(c)", CO_SP): (10, -9),
          ("(c)", SP_CO): (17, -16)}                 # below the diagonal, where nothing is drawn (points)


def ticks(ax, hi):
    """log axes ticked as ratios: 1x, 2x, 3x, 5x, 7x up to 10x (no 1.5x: it meets 2x at a third of a column); 1x, 3x,
    10x, 30x past it"""
    ax.set_xscale("log")
    ax.set_yscale("log")
    t = [v for v in ([1, 2, 3, 5, 7] if hi <= 10 else [1, 3, 10, 30]) if v <= hi]
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(FixedLocator(t))
        axis.set_major_formatter(FixedFormatter([f"{v:g}x" for v in t]))
        axis.set_minor_formatter(NullFormatter())


def draw_panel(ax, data, title, xtop=None):
    """xtop: the x axis ends there (else at the y axis's top); a point past it stops the draw"""
    rows, x, y, pick, best, rho = data
    over = [r["layout"] for r in rows if y[r["layout"]] > CAP]
    inside = [v for k, v in list(x.items()) + list(y.items()) if k not in over]
    hi = max(8.0, max(inside) * PAD) if not over else CAP
    if xtop and max(x.values()) > xtop:
        raise SystemExit(f"{title}: a layout is measured past the x axis's end ({max(x.values()):.2f}x > {xtop:g}x)")
    for r in rows:
        k, s = r["layout"], cell_style(r["cell"])
        ax.scatter(x[k], hi if k in over else y[k], s=11, zorder=3, facecolor="none", edgecolor=s["color"],
                   marker=s["marker"], linewidths=0.7, clip_on=k not in over)
        mark_engine_point(ax, x[k], y[k], r["cell"], *OFFSET.get((title[:3], r["cell"]), (8, -10)), leader=True)
    ax.scatter(x[pick], y[pick], s=55, facecolors="none", edgecolors="#222222", linewidths=0.8, zorder=4, clip_on=False)
    if over:
        lo_o, hi_o = min(y[k] for k in over), max(y[k] for k in over)
        rng = f"{lo_o:,.0f}x" if round(lo_o) == round(hi_o) else f"{lo_o:,.0f}-{hi_o:,.0f}x"
        ax.text(0.97, 0.6, f"{len(over)} at top:\n{rng}", transform=ax.transAxes, ha="right", va="center",
                fontsize=PT_SMALL - 0.5)
    ax.set_title(title, fontsize=PT - 0.5, pad=9)
    line = f"regret {x[pick]:.2f}x, ρ {rho:.2f}"              # the selection regret: the pick's measured cost over the best
    ax.text(0.5, 1.012, line, transform=ax.transAxes, ha="center", va="bottom", fontsize=PT_SMALL)
    ticks(ax, hi)
    ax.set_xlim(0.92, xtop or hi)
    ax.set_ylim(0.92, hi)
    ax.plot([0.92, hi], [0.92, hi], ls="--", lw=0.6, color="grey", zorder=2)
    if xtop:
        t = [v for v in ((1, 2, 3, 5) if xtop < 7 else (1, 2, 3, 5, 10)) if v <= xtop]
        ax.xaxis.set_major_locator(FixedLocator(t))
        ax.xaxis.set_major_formatter(FixedFormatter([f"{v:g}x" for v in t]))
    ax.grid(True, which="major", alpha=0.3, lw=0.4)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)
    print(f"{title}: pick {pick} at {x[pick]:.4f}x the measured best ({best}); Spearman {rho:.4f}; {len(over)} past {CAP:g}x")


def main():
    data, out_dir = args(__doc__)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    # another predicted set than the paper's (predict_49.py --set wrote predict-49-set.csv): its note in a line above
    # the panels, on a figure taller by that line; without the table the figure is the paper's, unchanged
    note = read_table(data, NOTE)[0]["note"] if has_table(data, NOTE) else None
    H, top = (1.54 + NOTE_IN, 0.81 * 1.54 / (1.54 + NOTE_IN)) if note else (1.54, 0.81)
    fig, axes = plt.subplots(1, 3, figsize=(COLUMN_IN, H))
    fig.subplots_adjust(left=0.095, right=0.975, top=top, bottom=0.62 / H, wspace=0.38)
    if note:
        fig.text(0.54, 1 - 0.03 / H, f"predictions from {note}", ha="center", va="top", fontsize=PT, style="italic")
    for ax, (table, model, title, xtop) in zip(axes, PANELS):
        title = title.format(sf=common.sf_label())
        if not has_table(data, table):
            empty_panel(ax, title)
            print(f"{title}: {table}.csv not in this run: the panel is drawn empty")
            continue
        draw_panel(ax, panel_data(read_table(data, table), model), title, xtop if common.SCALE == "3" else None)
    axes[0].set_ylabel("predicted / its best (log)", fontsize=PT, labelpad=1)
    fig.text(0.54, 0.485 / H, "measured / measured best (log)", ha="center", va="top", fontsize=PT)
    legend(fig, H, engines=True)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
