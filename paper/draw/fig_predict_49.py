#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/predict-49/fig_predict_49.py); edit it there, then run evidence/release_draw.py
"""Paper figure: predict mode at tier 1 (counts and means only, no store) against the measured cost on the 49 uniform
layouts, for LDBC Interactive (W16: its mix with its updates, SF3) beside LDBC BI (B01: 17 queries each once, SF3), in
wops_bytes4k; one column.

  figures/evaluation/predict-49.pdf

Two panels, one per workload, each on its own scale (log-log): one point per layout (colour = the adjacency
setting, marker = the properties setting), each side over its own best of the 49 (x: measured over the measured best;
y: predicted over predict's cheapest); the dashed line is y = x. The ring marks predict's pick; the line above each panel
names it against the measured best, with Spearman's rho (ρ) of the 49. A layout predicted past CAP over predict's cheapest
is drawn at the top edge (open marker) and the edge says how many and their range (BI: the 14 with Split or Condensed
adjacency).

Reads only its tables, predict-49-i01.csv and predict-49-b01.csv (the artifact's paper/derive/predict_49.py writes them
from the snapshot named in data/data.toml): per model and layout, the workload's predicted and measured totals.

    python fig_predict_49.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedFormatter, FixedLocator, NullFormatter

from common import args, read_table, use_paper_font
from paper_style import ADJ_COLOR, CHUNK_SHADE, COLUMN_IN, PROPS_MARKER, cell_style, engine_legend, colour_engine_legend

MODEL = "wops_bytes4k"      # wops + bytes read / 4096: the model plans are picked by
PANELS = (("predict-49-i01", "(a) LDBC Interactive, SF3"), ("predict-49-b01", "(b) LDBC BI, SF3"))
PT, PT_SMALL = 7, 6          # print size at one column
CAP = 30.0                  # a panel's top: a layout predicted past it is drawn at the edge, its range named
PAD = 1.15                  # the axes' top over the largest point, so its marker is inside the frame


def spearman(a, b):
    """Spearman's rho of two equal-length lists (no ties in these totals)"""
    rank = lambda v: {i: r for r, i in enumerate(sorted(range(len(v)), key=v.__getitem__))}   # noqa: E731
    ra, rb, n = rank(a), rank(b), len(a)
    return 1 - 6 * sum((ra[i] - rb[i]) ** 2 for i in range(n)) / (n * (n * n - 1))


def panel_data(rows, model=MODEL):
    """(rows, x, y, pick, best, rho) of one workload's 49 under `model`; x and y keyed by layout"""
    rows = sorted((r for r in rows if r["model"] == model), key=lambda r: r["layout"])
    if len(rows) != 49:
        raise SystemExit(f"{len(rows)} layouts under {model}, want 49")
    p = {r["layout"]: float(r["predicted"]) for r in rows}
    m = {r["layout"]: float(r["measured"]) for r in rows}
    x = {k: v / min(m.values()) for k, v in m.items()}
    y = {k: v / min(p.values()) for k, v in p.items()}
    pick, best = min(p, key=lambda k: (p[k], k)), min(m, key=lambda k: (m[k], k))
    return rows, x, y, pick, best, spearman([p[k] for k in sorted(p)], [m[k] for k in sorted(p)])


def ratio_axes(ax, hi):
    """log axes ticked as ratios: 1x .. 7x up to 10x; 1x, 3x, 10x, 30x ... up to 300x; past it one tick per decade"""
    ax.set_xscale("log")
    ax.set_yscale("log")
    ticks = [1, 1.5, 2, 3, 5, 7]
    if hi > 300:
        ticks = [t for t in (1, 10, 100, 1000, 10000, 100000) if t <= hi]
    elif hi > 10:
        ticks = [t for t in (1, 3, 10, 30, 100, 300) if t <= hi]
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(FixedLocator(ticks))
        axis.set_major_formatter(FixedFormatter([f"{t:g}x" for t in ticks]))
        axis.set_minor_formatter(NullFormatter())


def ratio_ticks(ax, hi):
    """log axes ticked as ratios: 1x, 1.5x, 2x, 3x, 5x, 7x up to 10x; 1x, 3x, 10x, 30x past it"""
    ax.set_xscale("log")
    ax.set_yscale("log")
    ticks = [1, 1.5, 2, 3, 5, 7] if hi <= 10 else [1, 3, 10, 30]
    ticks = [t for t in ticks if t <= hi]
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(FixedLocator(ticks))
        axis.set_major_formatter(FixedFormatter([f"{t:g}x" for t in ticks]))
        axis.set_minor_formatter(NullFormatter())


def draw_panel(ax, data, title):
    rows, x, y, pick, best, rho = data
    over = [r["layout"] for r in rows if y[r["layout"]] > CAP]
    inside = [v for k, v in list(x.items()) + list(y.items()) if k not in over]
    hi = max(8.0, max(inside) * PAD) if not over else CAP
    for r in rows:
        k, s = r["layout"], cell_style(r["cell"])
        if k in over:                                   # past the top: at the edge, open, not clipped
            ax.scatter(x[k], hi, s=11, zorder=3, facecolor="none", edgecolor=s["color"], marker=s["marker"],
                       linewidths=0.7, clip_on=False)
        else:
            ax.scatter(x[k], y[k], s=11, zorder=3, facecolor="none", edgecolor=s["color"], marker=s["marker"],
                       linewidths=0.7)                 # open markers, as bill-49
    ax.scatter(x[pick], y[pick], s=55, facecolors="none", edgecolors="#222222", linewidths=0.8, zorder=4, clip_on=False)
    if over:
        lo_o, hi_o = min(y[k] for k in over), max(y[k] for k in over)
        xs_o = sorted(x[k] for k in over)
        ax.annotate(f"{len(over)} layouts:\n{lo_o:,.0f}-{hi_o:,.0f}x", (xs_o[-1], hi), xytext=(5, -1),
                    textcoords="offset points", ha="left", va="top", fontsize=PT_SMALL)   # right of the open points
    ax.set_title(title, fontsize=PT, pad=11)
    line = f"pick {pick}: regret {x[pick]:.2f}x, ρ {rho:.2f}"      # the selection regret: the pick's measured cost over the best
    ax.text(0.5, 1.012, line, transform=ax.transAxes, ha="center", va="bottom", fontsize=PT_SMALL)
    ratio_ticks(ax, hi)
    ax.set_xlim(0.92, hi)
    ax.set_ylim(0.92, hi)
    ax.plot([0.92, hi], [0.92, hi], ls="--", lw=0.6, color="grey", zorder=2)
    ax.grid(True, which="major", alpha=0.3, lw=0.4)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)
    print(f"{title}: pick {pick} at {x[pick]:.4f}x the measured best ({best}); Spearman {rho:.4f}; {len(over)} past {CAP:g}x")


def legend(fig, H, engines=False):
    """the shared legend at the figure's foot: adjacency colours, properties markers (as bill-49); with engines, a third
    row below them names the engines' marks (DG, NG)"""
    names = [("split", "Sp"), ("condensed", "Co")] + [(k, f"Ch{k}k") for k in (1, 4, 16, 64, 256)]
    adj = [Line2D([], [], ls="", marker="o", ms=3.5, color=ADJ_COLOR[k] if isinstance(k, str) else CHUNK_SHADE[k],
                  label=n) for k, n in names]
    props = [Line2D([], [], ls="", marker=PROPS_MARKER[k], ms=3.5, mfc="none", mec="0.2", mew=0.7, label=n)
             for k, n in names]
    kw = dict(ncol=7, frameon=False, fontsize=PT_SMALL, handletextpad=0.05, columnspacing=0.55, borderaxespad=0,
              borderpad=0, handlelength=1.0)
    up = 0.12 if engines else 0.0                      # the two rows rise by the engines' row
    first = fig.legend(handles=adj, loc="lower left", bbox_to_anchor=(0.16, (0.115 + up) / H), **kw)
    second = fig.legend(handles=props, loc="lower left", bbox_to_anchor=(0.16, up / H), **kw)
    if engines:
        eh, el = engine_legend()
        colour_engine_legend(fig.legend(eh, el, loc="lower left", bbox_to_anchor=(0.16, 0.0),
                                        **{**kw, "ncol": 2, "handletextpad": 0.3, "columnspacing": 1.0}))
        fig.add_artist(second)
    fig.add_artist(first)
    fig.text(0.155, (0.17 + up) / H, "adjacency", ha="right", va="center", fontsize=PT_SMALL)
    fig.text(0.155, (0.055 + up) / H, "properties", ha="right", va="center", fontsize=PT_SMALL)


def main():
    data, out_dir = args(__doc__)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = 1.72
    fig, axes = plt.subplots(1, 2, figsize=(COLUMN_IN, H))
    fig.subplots_adjust(left=0.105, right=0.975, top=0.85, bottom=0.47 / H, wspace=0.3)
    for ax, (table, title) in zip(axes, PANELS):
        draw_panel(ax, panel_data(read_table(data, table)), title)
    axes[0].set_ylabel("predicted / predicted best", fontsize=PT, labelpad=1)
    for ax in axes:
        ax.set_xlabel("measured / measured best", fontsize=PT, labelpad=1)
    legend(fig, H)
    out = out_dir / "figures/evaluation/predict-49.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
