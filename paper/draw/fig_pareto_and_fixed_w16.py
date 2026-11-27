#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/pareto-and-fixed-i01/fig_pareto_and_fixed_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3 (64 KB chunks), the front of the mixes per type and what they are made of (one paper column).

  figures/evaluation/pareto-and-fixed-w16.pdf

A mix per type sets one layout per vertex type and per edge type (21 parts, each Split, Condensed or Chunked: 3^21
mixes). (a) The complete front of the mixes per type (every mix no other one beats in both wops and bytes), the best
uniform layout and the nine uniform layouts, over the density of all 3^21 mixes; the faint line is the front per type
and direction (each edge type's two directions apart). Below it, the setting each type gets along the front (types
under 1% of every front mix's wops and bytes left out). (b), (c) The same axes and lines; under each front mix a bar to
its wops (b), left of it a bar to its bytes (c), cut by part in proportion to each part's share, in (a)'s row order:
(a)'s top row next to the front line, the index and the parts (a) leaves out at the axis. A and B: the best mix within
2x and 5x Sp-Co's bytes, marked in every panel. (d), (e) A's and B's wops (d) and bytes (e) as one bar each, cut by
part in (b)'s order, each total at its bar's end (one decimal); a part is named with its share of the bar where the name
fits. Axes are multiples of Sp-Co's (Dgraph's settings), log in (a)-(c), linear in (d), (e). Colour =
the part's setting, in the cell theme (Split purple, Condensed teal, Chunked red; vertex parts lighter). wops = G +
1.1 S + 0.01 N + P + D (reads and writes, a write's own reads included); bytes = read (misses included) + written.
Counts are deterministic: no error bars.

Reads only its tables (the artifact's paper/derive/pareto_and_fixed_i01.py makes them from the snapshots named in
data/data.toml): pareto-and-fixed-i01.csv (the front mixes, the nine, the front per
type and direction, each front and uniform row with its parts' settings, wops and bytes), pareto-and-fixed-i01-density.csv
(the density's non-empty bins) and pareto-and-fixed-i01-density-grid.csv (its extent and parts).

    python fig_pareto_and_fixed_w16.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PolyCollection
from matplotlib.colors import LogNorm, to_hex, to_rgb
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from common import args, read_table, use_paper_font
from paper_style import ADJ_COLOR, CHUNK_SHADE, DEFAULT_CHUNK_KB, side_name

PT = 6.5                    # the text size in the figure, in points at one paper column
MIX, MIX_FAINT, UNIFORM = "#d81b60", "#f2b6cc", "0.35"   # mixcol, a light mixcol, grey (as the other front figures)
GUIDE = "0.5"               # A and B's guide lines: thin mid grey, quieter than the data
X_TIMES = (1, 2, 3, 4, 5, 10)
Y_TIMES = (0.1, 0.2, 0.5, 1, 2)
MARKS = (("A", 2.0), ("B", 5.0))   # the best mix per type within 2x and 5x Sp-Co's bytes
SETTINGS = ("split", "condensed", "chunked")
SHORT = {s: side_name(s) for s in ("split", "condensed", "chunked")}     # Sp, Co, Ch64k: the grid's nine
PROPS = ("Person", "Message", "Forum", "Organisation", "Place", "Tag")
LISTS = ("knows", "hasCreator", "likes", "replyOf", "containerOf", "hasMember", "hasModerator", "hasTag", "hasInterest",
         "hasType", "isLocatedIn", "isPartOf", "isSubclassOf", "studyAt", "workAt")
UNITS = [("index", None)] + [("props", p) for p in PROPS] + [("adj", lst) for lst in LISTS]
INDEX = "#bdbdbd"


def tint(c, f=0.55):
    """c mixed with white: f = 0 is c, 1 is white"""
    return to_hex([x + (1 - x) * f for x in to_rgb(c)])


HUE = {**ADJ_COLOR, "chunked": CHUNK_SHADE[DEFAULT_CHUNK_KB]}      # the cell theme: Sp purple, Co teal, Ch red
COLOR = {s: (tint(HUE[s]), HUE[s]) for s in SETTINGS}              # (vertex part, edge part)


def share(m, u, cur):
    """a part's wops or bytes in a mix (part keys as complete_front.json writes them; the index is 'None')"""
    v = m["units"].get("None" if u[0] == "index" else str(u))
    return v[cur] if v else 0.0


def color(m, u):
    if u[0] == "index":
        return INDEX
    return COLOR[m["assign"][f"{u[0]}|{u[1]}"]][0 if u[0] == "props" else 1]


def rects(ax, x0, x1, y0, y1, colors):
    """many filled rectangles as one collection (one bar patch each takes minutes); zero-size ones dropped"""
    verts, cs = [], []
    for a, b, c, d, col in zip(x0, x1, y0, y1, colors):
        if b > a and d > c:
            verts.append(((a, c), (a, d), (b, d), (b, c)))
            cs.append(col)
    ax.add_collection(PolyCollection(verts, facecolors=cs, edgecolors="none", linewidths=0, zorder=3,
                                     rasterized=True))   # thousands of bars: pixels in the PDF, lines and text stay vector


def split(lo, top, fracs):
    """a bar from the axis edge lo to top on a log axis, cut in proportion to fracs along its drawn length"""
    edges, c = [lo], 0.0
    for f in fracs:
        c += f
        edges.append(lo * (top / lo) ** c)
    return edges


def times(v, _=None):
    return f"{v:g}x"


def as_mix(r):
    """a front or uniform row back in complete_front.json's shape: ops, bytes, units (by part key), assign"""
    m = {"name": r["name"], "ops": float(r["ops"]), "bytes": float(r["bytes"]), "units": {}, "assign": {}}
    for u in UNITS:
        key, col = ("None", "index") if u[0] == "index" else (str(u), f"{u[0]}_{u[1]}")
        if r[f"ops_{col}"] != "":
            m["units"][key] = {"ops": float(r[f"ops_{col}"]), "bytes": float(r[f"bytes_{col}"])}
        if u[0] != "index":
            m["assign"][f"{u[0]}|{u[1]}"] = r[f"setting_{col}"]
    return m


def main():
    data, out_dir = args(__doc__)
    table = sorted(read_table(data, "pareto-and-fixed-i01"), key=lambda r: (r["kind"], r["line"]))
    mixes = [as_mix(r) for r in table if r["kind"] == "front"]
    uni = {m["name"]: m for m in (as_mix(r) for r in table if r["kind"] == "uniform")}
    if len(uni) != 9:
        raise SystemExit(f"pareto-and-fixed-i01.csv: {len(uni)} uniform layouts, not the nine")
    rb, ro = uni["Sp-Co"]["bytes"], uni["Sp-Co"]["ops"]              # 1x on both axes
    for m in mixes:
        m["X"], m["Y"] = m["bytes"] / rb, m["ops"] / ro
    nine = {n: (u["bytes"] / rb, u["ops"] / ro) for n, u in uni.items()}
    ylim = (mixes[-1]["Y"] / 1.8, 2.2)
    x0, x1 = mixes[0]["X"] / 1.05, max([m["X"] for m in mixes] + [b for b, _ in nine.values()]) * 1.05
    dirs = sorted((float(r["bytes"]) / rb, float(r["ops"]) / ro) for r in table if r["kind"] == "direction")
    if not dirs:
        raise SystemExit("pareto-and-fixed-i01.csv: no 'direction' rows")
    marks = [(name, [m for m in mixes if m["X"] <= lim * (1 + 1e-9)][-1]) for name, lim in MARKS]
    nxt = [mixes[i + 1]["X"] if i + 1 < len(mixes) else x1 for i in range(len(mixes))]

    def mark(a, vertical=True, label=True):
        """A and B on a panel: a dot and its name, and a guide line through it (vertical, or horizontal in (c))"""
        for name, m in marks:
            (a.axvline if vertical else a.axhline)(m["X"] if vertical else m["Y"], color=GUIDE, lw=0.45,
                                                   ls=(0, (2, 1.5)), zorder=8)
            if label:
                a.plot([m["X"]], [m["Y"]], "o", color=MIX, ms=3.4, mec="0.25", mew=0.5, zorder=9)
                a.annotate(name, (m["X"], m["Y"]), textcoords="offset points", xytext=(3, 3), fontsize=PT - 1,
                           fontweight="bold", color=MIX, zorder=10)

    def lines(a):
        """the front per type (a white edge, so it shows over the bars) and the best uniform layout within the bytes"""
        a.step([m["X"] for m in mixes] + [x1], [m["Y"] for m in mixes] + [mixes[-1]["Y"]], where="post", color=MIX,
               lw=1.1, zorder=6, path_effects=[pe.Stroke(linewidth=2.0, foreground="white"), pe.Normal()])
        pts, best, xs, ys = sorted(nine.values()), float("inf"), [], []
        for i, (b, o) in enumerate(pts):
            best = min(best, o)
            xs += [b, pts[i + 1][0] if i + 1 < len(pts) else x1]
            ys += [best, best]
        a.plot(xs, ys, color=UNIFORM, lw=0.8, ls="--", zorder=5)

    def axes(a, small=False):
        """the one pair of axes every panel uses: log-log multiples of Sp-Co, the same range and ticks, the crosshair"""
        a.axvline(1, color="0.45", lw=0.5, ls=":", zorder=1)
        a.axhline(1, color="0.45", lw=0.5, ls=":", zorder=1)
        a.set_xscale("log")
        a.set_yscale("log")
        a.set_xlim(x0, x1)
        a.set_ylim(*ylim)
        a.xaxis.set_major_locator(FixedLocator(X_TIMES))
        a.xaxis.set_major_formatter(FuncFormatter(lambda v, _: times(v) if not small or v in (1, 2, 5, 10) else ""))
        a.xaxis.set_minor_locator(NullLocator())
        a.yaxis.set_major_locator(FixedLocator(Y_TIMES))
        a.yaxis.set_major_formatter(FuncFormatter(times))
        a.yaxis.set_minor_locator(NullLocator())
        a.set_ylabel(r"x Sp-Co's $\mathit{wops}$", labelpad=1)
        if small:
            a.set_xlabel("x Sp-Co's bytes", labelpad=1)

    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = 4.4                                                       # inches; the margins below are in inches over H
    fig = plt.figure(figsize=(3.33, H))
    # (a)-(c) on top: (a) on the left, (b) over (c) on the right; (d) and (e) below, in one row across the full width
    outer = fig.add_gridspec(1, 2, width_ratios=(1.45, 1), wspace=0.36, left=0.158, right=0.99,
                             top=1 - 0.4455 / H, bottom=1.38 / H)
    left = outer[0].subgridspec(2, 1, height_ratios=(1.1, 1.0), hspace=0.05)
    right = outer[1].subgridspec(2, 1, hspace=0.62)
    bottom = fig.add_gridspec(1, 2, width_ratios=(0.6, 1.4), wspace=0.1, left=0.045, right=0.99, top=0.92 / H,
                              bottom=0.28 / H)
    ax = fig.add_subplot(left[0])
    bx = fig.add_subplot(left[1], sharex=ax)

    # (a) the cloud of all mixes, the two fronts, the nine
    grid = read_table(data, "pareto-and-fixed-i01-density-grid")[0]
    h = np.zeros((int(grid["nx"]), int(grid["ny"])))
    for r in read_table(data, "pareto-and-fixed-i01-density"):
        h[int(r["ix"]), int(r["iy"])] = int(r["count"])
    xl, xh, yl, yh = (float(grid[k]) for k in ("xl", "xh", "yl", "yh"))
    n_parts = len(grid["parts"].split(";"))                       # each part takes one of 3 settings: 3^parts mixes
    if int(h.sum()) != 3 ** n_parts:
        raise SystemExit(f"the density holds {int(h.sum()):,} mixes, not 3^{n_parts}")
    k = 4                                                         # 400 thin bins leave empty stripes: draw 100 x 100
    h = h.reshape(h.shape[0] // k, k, h.shape[1] // k, k).sum(axis=(1, 3))
    xe = 10 ** np.linspace(xl, xh, h.shape[0] + 1) / rb
    ye = 10 ** np.linspace(yl, yh, h.shape[1] + 1) / ro
    ax.pcolormesh(xe, ye, np.ma.masked_equal(h.T, 0), cmap="Greys", norm=LogNorm(1, h.max()), alpha=0.22,
                  shading="flat", zorder=0, rasterized=True, linewidth=0)
    ax.step([b for b, _ in dirs] + [x1], [o for _, o in dirs] + [dirs[-1][1]], where="post", color=MIX_FAINT, lw=0.9,
            zorder=5.5)
    lines(ax)
    for b, o in nine.values():
        ax.plot([b], [o], "o", mfc="white", mec="0.1", mew=0.8, ms=3.6, zorder=7)
    # every uniform layout named; two pairs share a spot (Sp-Co with Co-Co, Sp-Sp with Co-Sp)
    for n, text, (ox, oy, ha) in (("Sp-Co", "Sp-Co, Co-Co", (3, 3, "left")), ("Co-Sp", "Sp-Sp, Co-Sp", (0, 4, "center")),
                                  ("Ch64k-Co", "Ch64k-Co", (0, -7.5, "center")), ("Ch64k-Sp", "Ch64k-Sp", (4, -1, "left")),
                                  ("Sp-Ch64k", "Sp-Ch64k", (3, 2, "left")), ("Co-Ch64k", "Co-Ch64k", (-3, -6, "right")),
                                  ("Ch64k-Ch64k", "Ch64k-Ch64k", (-3, -7, "right"))):
        ax.annotate(text, nine[n], textcoords="offset points", xytext=(ox, oy), ha=ha, fontsize=PT - 1, color="0.1")
    mark(ax)
    axes(ax)
    ax.tick_params(labelbottom=False)
    ax.set_title("(a) the front, and each type's setting", fontsize=PT + 0.5, pad=2)

    # (a, below) each type's setting at each point of the front: vertex types, then edge types, each by its top share
    rows = [u for u in UNITS[1:] if any(share(m, u, c) / m[c] >= 0.01 for m in mixes for c in ("ops", "bytes"))]
    weight = {u: max(share(m, u, "ops") / m["ops"] for m in mixes) for u in rows}
    rows.sort(key=lambda u: (u[0] != "props", -weight[u]))
    nv = sum(u[0] == "props" for u in rows)
    for j, u in enumerate(rows):
        rects(bx, [m["X"] for m in mixes], nxt, [j - 0.46] * len(mixes), [j + 0.46] * len(mixes),
              [color(m, u) for m in mixes])
    bx.set_yticks(range(len(rows)), [u[1] for u in rows], fontsize=PT - 2.5)
    bx.axhline(nv - 0.5, color="0.2", lw=0.6)
    mark(bx, label=False)
    bx.set_ylim(len(rows) - 0.5, -0.5)
    bx.xaxis.set_major_locator(FixedLocator(X_TIMES))
    bx.xaxis.set_major_formatter(FuncFormatter(times))
    bx.xaxis.set_minor_locator(NullLocator())
    bx.set_xlabel("x Sp-Co's bytes", labelpad=1)

    # (b), (c) stack the parts in (a)'s row order, read from the front line inward: (a)'s top row next to the line,
    # its bottom row next, and the index and the parts (a) leaves out at the axis
    stack = [UNITS[0]] + [u for u in UNITS[1:] if u not in rows] + rows[::-1]
    assert sorted(map(str, stack)) == sorted(map(str, UNITS))

    # (b) under each front mix a bar to its wops, cut by part in proportion to the part's share
    cx = fig.add_subplot(right[0])
    cuts = [split(ylim[0], m["Y"], [share(m, u, "ops") / m["ops"] for u in stack]) for m in mixes]
    for i, u in enumerate(stack):
        rects(cx, [m["X"] for m in mixes], nxt, [c[i] for c in cuts], [c[i + 1] for c in cuts], [color(m, u) for m in mixes])
    lines(cx)
    mark(cx)
    axes(cx, small=True)
    cx.set_title(r"(b) $\mathit{wops}$ by part", fontsize=PT + 0.5, pad=2)

    # (c) left of each front mix a bar to its bytes, cut by part; each band from the mix's wops to the next higher mix's
    dx = fig.add_subplot(right[1])
    hs = [(mixes[i - 1]["Y"] if i > 0 else m["Y"] * 1.03) - m["Y"] for i, m in enumerate(mixes)]
    cuts = [split(x0, m["X"], [share(m, u, "bytes") / m["bytes"] for u in stack]) for m in mixes]
    for i, u in enumerate(stack):
        rects(dx, [c[i] for c in cuts], [c[i + 1] for c in cuts], [m["Y"] for m in mixes],
              [m["Y"] + hh for m, hh in zip(mixes, hs)], [color(m, u) for m in mixes])
    lines(dx)
    mark(dx, vertical=False)
    axes(dx, small=True)
    dx.set_title("(c) bytes by part", fontsize=PT + 0.5, pad=2)

    # (d), (e) A's and B's wops and bytes as one bar each, in multiples of Sp-Co's, cut by part in (b)'s order
    renderer = fig.canvas.get_renderer()
    ex, fx = fig.add_subplot(bottom[0]), fig.add_subplot(bottom[1])
    for a, cur, title in ((ex, "ops", r"(d) $\mathit{wops}$ of A and B"), (fx, "bytes", "(e) bytes of A and B")):
        ref = ro if cur == "ops" else rb
        lim = max(m[cur] for _, m in marks) / ref * 1.2   # room for the total at the bar's end
        a.set_xlim(0, lim)                     # the limits first: the label-fit test reads them
        a.set_ylim(len(marks) - 0.5, -0.5)
        for yi, (name, m) in enumerate(marks):
            left_x = 0.0
            for u in stack:
                v = share(m, u, cur) / ref
                a.barh(yi, v, left=left_x, height=0.8, color=color(m, u), edgecolor="white", lw=0.25)
                sh = share(m, u, cur) / m[cur]
                if sh >= 0.05 and u[0] != "index":
                    t = a.text(left_x + v / 2, yi, f"{u[1]}\n{100 * sh:.0f}%", ha="center", va="center",
                               fontsize=7, fontweight="bold", color="white" if u[0] == "adj" else "0.1",
                               linespacing=0.9)
                    if t.get_window_extent(renderer).transformed(a.transData.inverted()).width > v * 0.95:
                        t.remove()
                left_x += v
            a.text(left_x + lim * 0.015, yi, f"{left_x:.1f}x", va="center", fontsize=PT - 0.5, fontweight="bold")
        if lim > 1:                            # Sp-Co, when it is in range
            a.axvline(1, color="0.45", lw=0.5, ls=":", zorder=0.5)   # behind the bars
        a.set_yticks(range(len(marks)), [n for n, _ in marks])
        a.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        a.tick_params(axis="y", length=0)
        a.set_xlabel("x Sp-Co's " + (r"$\mathit{wops}$" if cur == "ops" else "bytes"), labelpad=1)
        a.set_title(title, fontsize=PT + 0.5, pad=2)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)

    for a in (ax, bx, cx, dx, ex, fx):
        a.tick_params(labelsize=PT - 0.5, pad=1, length=2, width=0.5)
        for side in a.spines.values():
            side.set_linewidth(0.5)
    for a in (ax, cx, dx):
        a.grid(alpha=0.3, lw=0.4)
    # two rows, filled column by column: the lines and the cloud in two columns, then one column per setting
    handles = [Line2D([], [], color=MIX, lw=1.1, label="best mix per type"),
               Line2D([], [], color=MIX_FAINT, lw=0.9, label="per type and direction"),
               Line2D([], [], color=UNIFORM, lw=0.8, ls="--", label="best uniform"),
               Patch(fc="0.85", label=f"all $3^{{{n_parts}}}$ mixes")]
    for s in SETTINGS:
        handles += [Patch(fc=COLOR[s][1], label=f"{SHORT[s]} edge"), Patch(fc=COLOR[s][0], label=f"{SHORT[s]} vertex")]
    fig.legend(handles=handles, loc="upper center", ncol=5, frameon=False, fontsize=PT - 0.5, bbox_to_anchor=(0.5, 1.0),
               handlelength=1.3, columnspacing=0.8, handletextpad=0.3)
    OUT = out_dir / "figures/evaluation/pareto-and-fixed-w16.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    for name, m in marks:
        print(f"{name}: {m['X']:.2f}x Sp-Co's bytes, {m['Y']:.3f}x its wops")
    print(f"{len(mixes)} front mixes, {len(rows)} type rows, {len(dirs)} per-direction limits; stack {len(stack)} parts")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
