#!/usr/bin/env python3
# released from gs-paper at 3c2f37e (evidence/two-fronts-i01/fig_two_fronts_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure: the two fronts of LDBC Interactive's 49 uniform layouts at SF3 side by side, one column.

  figures/evaluation/two-fronts-w16.pdf

(a) wops against bytes, each over the fewest of the 49 (log), with equal-cost curves wops + alpha x bytes through the
    cheapest at alpha = 1/256 B, 1/4 KB (combined cost) and 1/64 KB (the points and front of
    uniform-49-layouts-w16-front-log.pdf; the half chunk view, as combined cost)
(b) DynamoDB's read units (y) against write units (x; swapped 2026-11-12), each over the fewest of the 49 (log), with equal bills read + r x write
    through the cheapest at r = 5 (DynamoDB's), 33.7 (Ch4k-Sp ties Ch4k-Ch4k) and 100 (the points and front of
    bill-49-front-w16.pdf; the measured chunk view, as the bill)
Dgraph's layout (Sp-Co) and NebulaGraph's (Co-Sp) are marked DG and NG (paper_style's engine marks) beside its point.
Colour = the adjacency setting, marker = the properties setting; filled = on the panel's lower-left front (the solid
step), open = beaten on both. Below each panel: the front's adjacency and properties setting. Deterministic counts: no
error bars.

Reads only its tables: uniform-i01-chunk-sizes-and-front.csv (the 49's wops and bytes) and bill-49-i01.csv (form exact,
option set uniform), each as its own derive writes it.

    python fig_two_fronts_w16.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

from common import args, read_table, use_paper_font  # noqa: E402
from paper_style import (ADJ_COLOR, CHUNK_SHADE, COLUMN_IN, cell_id, cell_parts, cell_style,  # noqa: E402
                         engine_legend, colour_engine_legend, mark_engine_point)

OUT = "figures/evaluation/two-fronts-w16.pdf"

PT, PT_SMALL = 7, 6.5
MS = 4.0                                           # the points' and the legend's marker size, printed points
# (a): 1 / alpha, its label (how, where); (b): r, its label. "y": at that height right of the curve; "yl": left of
# it; "x": at that x, above the curve; "xb": at that x, below it
ALPHAS = ((256, ("y", 1.25)), (4096, ("yl", 3.0)), (65536, ("xb", 1.04)))
RS = ((5, ("x", 2.2)), (33.7, ("y", 2.6)), (100, ("yl", 2.0)))
DARK = {4096, 5}                                   # the dark curve on each panel: weight α = 1/4 KB (combined cost), r = 5 (DynamoDB's bill)

SETTINGS = ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked", "chunked_c256k")
SHORT = {"split": "Sp", "condensed": "Co", "chunked_c1k": "Ch1k", "chunked_c4k": "Ch4k", "chunked_c16k": "Ch16k",
         "chunked": "Ch64k", "chunked_c256k": "Ch256k"}
HUE = {"split": ADJ_COLOR["split"], "condensed": ADJ_COLOR["condensed"], "chunked_c1k": CHUNK_SHADE[1],
       "chunked_c4k": CHUNK_SHADE[4], "chunked_c16k": CHUNK_SHADE[16], "chunked": CHUNK_SHADE[64],
       "chunked_c256k": CHUNK_SHADE[256]}
FRONT = "0.2"                                      # the 49's front, as in uniform-49-layouts-w16-front-log


def front(pts):
    """the lower-left front of (x, y, cell) points, by x"""
    out, best = [], float("inf")
    for x, y, c in sorted(pts):
        if y < best:
            out.append((x, y, c))
            best = y
    return out


def times(v, _=None):
    return f"{v:g}x"


def setting(layout, kb):
    return layout if kb is None else ("chunked" if kb == 64 else f"chunked_c{kb}k")


def panel(ax, bx, pts, weights, unit_x, unit_y, cost, xticks, yticks, xlabel, ylabel, title):
    """pts: [(x, y, cell)] raw; weights: ((k, label)); cost(x, y, k) the cost of a raw point at weight k"""
    rx, ry = min(x for x, _, _ in pts), min(y for _, y, _ in pts)
    p = [(x / rx, y / ry, c) for x, y, c in pts]
    fr = front(p)
    on = {c for _, _, c in fr}
    x0, x1 = 1 / 1.1, max(x for x, _, _ in p) * 1.15
    y0, y1 = 1 / 1.3, max(y for _, y, _ in p) * 1.3
    for x, y, c in p:
        st = cell_style(c)
        ax.scatter([x], [y], s=MS ** 2, marker=st["marker"], facecolor=st["color"] if c in on else "none",
                   edgecolor="black" if c in on else st["color"], linewidth=0.3 if c in on else 0.55,
                   zorder=4 if c in on else 2)
        mark_engine_point(ax, x, y, c, *((6, 12) if x < 1.1 else (-12, 0)), leader=x < 1.1)   # at the left edge: above, with a leader
    ax.step([q[0] for q in fr] + [x1], [q[1] for q in fr] + [fr[-1][1]], where="post", color=FRONT, lw=0.8, zorder=3)
    xs = [x0 * (x1 / x0) ** (i / 400) for i in range(401)]
    for k, (how, v) in weights:
        b = min(cost(x, y, k) for x, y, _ in pts)
        who = min(pts, key=lambda q: cost(q[0], q[1], k))[2]
        # cost(x rx, y ry) = b on a line: y = (b - cost(x rx, 0)) / (cost(0, 1) ry)
        slope_y = cost(0, 1, k) * ry
        yof = lambda x: (b - cost(x * rx, 0, k)) / slope_y                      # noqa: E731
        xof = lambda y: (b - slope_y * y) / cost(1, 0, k) / rx                  # noqa: E731
        line = [(x, yof(x)) for x in xs if yof(x) > y0 / 2] + [(xof(y0 / 2), y0 / 2)]
        dark = k in DARK
        ax.plot([x for x, _ in line], [y for _, y in line], ls=(0, (4, 2)) if dark else (0, (2, 2)), lw=0.8 if dark else 0.5,
                color="0.15" if dark else "0.5", zorder=2)
        ha, va = "left", "center"
        if how == "yl":
            xl, yl, ha = xof(v) / 1.03, v, "right"
        elif how == "y":
            xl, yl = xof(v) * 1.03, v
        elif how == "xb":
            xl, yl, va = v, yof(v) / 1.04, "top"
        else:
            xl, yl, va = v, yof(v) * 1.04, "bottom"
        ax.text(xl, yl, unit_x(k), fontsize=PT_SMALL, color="0.1" if dark else "0.25", ha=ha, va=va, zorder=6,
                path_effects=[pe.withStroke(linewidth=1.5, foreground="white")])   # readable across the other lines
        print(f"{title}: {unit_x(k)}: cheapest {who}")
    for a in (ax, bx):
        a.set_xscale("log")
        a.set_xlim(x0, x1)
        a.xaxis.set_major_locator(FixedLocator([t for t in xticks if t <= x1]))
        a.xaxis.set_major_formatter(FuncFormatter(times))
        a.xaxis.set_minor_locator(NullLocator())
        a.tick_params(labelsize=PT_SMALL, pad=1, length=1.5, width=0.4)
        for s in a.spines.values():
            s.set_linewidth(0.4)
    ax.set_yscale("log")
    ax.set_ylim(y0, y1)
    ax.yaxis.set_major_locator(FixedLocator([t for t in yticks if t <= y1]))
    ax.yaxis.set_major_formatter(FuncFormatter(times))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylabel(ylabel, labelpad=0.5, fontsize=PT)
    ax.tick_params(labelbottom=False)
    ax.grid(alpha=0.25, lw=0.3, zorder=0)
    ax.set_title(title, fontsize=PT, pad=2)
    nxt = [fr[i + 1][0] if i + 1 < len(fr) else x1 for i in range(len(fr))]
    for j, kind in enumerate(("props", "adj")):
        for (x, _, c), e in zip(fr, nxt):
            (pp, pk), (a_, ak) = cell_parts(c)
            s = setting(a_, ak) if kind == "adj" else setting(pp, pk)
            bx.axvspan(x, e, ymin=j / 2 + 0.04, ymax=(j + 1) / 2 - 0.04, color=HUE[s], lw=0)
    bx.set_ylim(0, 1)
    bx.tick_params(axis="y", length=0)
    bx.set_xlabel(xlabel, labelpad=0.5, fontsize=PT)
    print(f"{title}: front {len(fr)} layouts")


def main():
    data, out_dir = args(__doc__)
    wb = [(float(q["bytes"]), float(q["wops"]), q["layout"]) for q in read_table(data, "uniform-i01-chunk-sizes-and-front")]
    ru = [(float(q["write_units"]), float(q["read_units"]), q["cell"])
          for q in read_table(data, "bill-49-i01") if q["form"] == "exact" and q["option_set"] == "uniform"]
    assert len(wb) == 49 and len(ru) == 49, (len(wb), len(ru))
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = 2.27                                   # inches; the three legend rows ROW apart at the top
    fig = plt.figure(figsize=(COLUMN_IN, H))
    gs = fig.add_gridspec(2, 2, height_ratios=(1, 0.16), hspace=0.05, wspace=0.3, left=0.1, right=0.99,
                          top=1 - 0.50 / H, bottom=0.28 / H)
    ROW = 0.12
    a1, b1, a2, b2 = (fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[0, 1]),
                      fig.add_subplot(gs[1, 1]))
    # (a) x = bytes, y = wops; cost = wops + bytes / d
    panel(a1, b1, wb, ALPHAS, lambda d: "α=1/" + (f"{d // 1024} KB" if d >= 1024 else f"{d} B"), None, lambda x, y, d: y + x / d,
          (1, 2, 5, 10, 20), (1, 2, 5, 10, 20), "bytes (log)", r"$\mathit{wops}$ (log)", "(a) KV cost")
    # (b) x = write units, y = read units (as (a): bytes on x, the operations on y); bill = read + r write
    panel(a2, b2, ru, RS, lambda r: f"r={r:g}", None, lambda x, y, r: r * x + y,
          (1, 2, 5, 10, 20, 50), (1, 1.5, 2, 3, 5), "write units (log)", "read units (log)", "(b) DynamoDB bill")
    b1.set_yticks([0.25, 0.75], ["props", "adj"], fontsize=PT_SMALL)
    b2.set_yticks([])
    kw = dict(frameon=False, fontsize=PT_SMALL, handlelength=0.9, columnspacing=0.6, handletextpad=0.25,
              borderaxespad=0, borderpad=0)
    colours = [Patch(fc=HUE[s], label=SHORT[s]) for s in SETTINGS]
    equal = Line2D([], [], color="0.3", lw=0.6, ls=(0, (2, 2)), label="equal cost")
    eh, el = engine_legend()
    marks = [Line2D([], [], ls="", marker="", label="props:")]
    marks += [Line2D([], [], ls="", marker=cell_style(cell_id(s, "split"))["marker"], ms=MS, mfc="0.45", mec="black",
                     mew=0.4, label=SHORT[s]) for s in SETTINGS]
    rest = [Line2D([], [], ls="", marker="o", ms=MS, mfc="none", mec="0.45", mew=0.6, label="open: beaten"),
            Line2D([], [], color=FRONT, lw=0.8, ls="-", label="front"), equal]
    # three rows at the top: the adjacency colours; the properties markers; the lines, open points and engines
    rows = ((colours, [h.get_label() for h in colours], {}),
            (marks, [h.get_label() for h in marks], {"columnspacing": 0.4}),
            (rest + eh, [h.get_label() for h in rest] + el, {"handlelength": 1.2, "columnspacing": 0.45}))
    for j, (hs, ls, extra) in enumerate(rows):
        leg = fig.legend(hs, ls, loc="upper center", ncol=len(hs), bbox_to_anchor=(0.54 if j < 2 else 0.5, 1 - (0.01 + j * ROW) / H),
                         **{**kw, **extra})
        if j == 2:
            colour_engine_legend(leg)
        fig.add_artist(leg)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
