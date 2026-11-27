#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/front-by-part-49-layouts-i01/fig_front_by_part_49_layouts_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3, the front when each type picks Sp, Co or Ch at 1-256 KB, and what it is made of (one column).

  figures/evaluation/front-by-part-49-layouts-w16.pdf

pareto-and-fixed-i01's layout on the wider option set of uniform-i01-chunk-sizes-and-front. (a) The front of the mixes
per type over Sp, Co and Ch 1, 4, 16, 64, 256 KB (pink; the same line as uniform-i01-chunk-sizes-and-front's), the best
of the 49 uniform layouts (dark grey, dashed), the front over Sp, Co, Ch64k and its best uniform layout (light pink,
light grey dashed), and the 49 layouts as points (marker = properties, colour = adjacency; open = beaten by another uniform
layout in both wops and bytes). Below it, the setting each type gets along the front (types under 1% of every front
mix's wops and bytes left out). (b), (c) Under each front mix a bar to its wops (b), left of it a bar to its bytes (c),
cut by part in proportion to each part's share, in (a)'s row order. A and B: the best mix within 2x and 5x Sp-Co's
bytes. (d) The six largest parts' and knows's wops against their bytes at each setting the front gives them (one line per part,
log-log). (e), (f) A's and B's wops and bytes as one bar each, cut by part as shares of the bar (its total over Sp-Co's
above it), the largest parts named. Axes are multiples of Sp-Co's, log in (a)-(d); (e), (f) are shares. Colour = the part's setting (Split purple, Condensed teal, Chunked red, darker for larger chunks;
vertex parts lighter). wops = G + 1.1 S + 0.01 N + P + D; bytes = read (misses included) + written. Deterministic
counts: no error bars.

Reads only its two tables (the artifact's derive makes them from sf3/mix-benefit-20261103):
front-by-part-49-layouts-i01.csv (the 149 front points, one row per point and part: its setting, wops and bytes) and
front-by-part-49-layouts-i01-points.csv (the 49 uniform layouts, the nine at 64 KB, the 1,588 mixes of the 3-setting
front: wops, bytes).

    python fig_front_by_part_49_layouts_w16.py --data <dir of the tables> --out <dir>

Panel (a) alone for another workload (LDBC BI, JOB), from the tables the artifact's
experiments/mix_benefit/front_by_part.py writes (front-by-part-<name>.csv, -points.csv, -limits.csv): the best mix
and the best of the 49 (the nine at 64 KB checked, not drawn), the points and type rows, each axis over the fewest of
the 49 (log), and a staircase limit not proven the fewest (JOB: each limit's best mix found within a node limit) drawn
as an open dot, the largest gap to its bound in the legend. Drawn as a figure of its own, in the style of
uniform-49-layouts-w16-front-log.pdf: the title without "(a)", vertex types in full colour; Dgraph's layout (Sp-Co) and
NebulaGraph's (Co-Sp) marked DG and NG beside their points. Writes figures/evaluation/front-mix-vs-uniform-<w16, b01 or j01>.{pdf,png}:

    python fig_front_by_part_49_layouts_w16.py --panel a --name b01 --title "LDBC BI (B01, SF3)" --data <dir> --out <dir>
"""
import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.collections import PolyCollection
from matplotlib.colors import to_hex, to_rgb
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator

from common import args, read_table, use_paper_font
from paper_style import ADJ_COLOR, CHUNK_SHADE, cell_id, cell_style, colour_engine_legend, engine_legend, mark_engine_point

OUT = "figures/evaluation/front-by-part-49-layouts-w16.pdf"
CALLOUT = (("adj", "hasCreator"), ("adj", "hasTag"))   # (e), (f): named beside the bars, too thin to name inside
PT, PT_SMALL = 7, 6                             # titles and axis names; ticks, legend and in-bar text (as predict-49)
PT_ALONE = 6.5                                  # panel (a) alone: ticks, row names, legend
MIX7, UNIFORM7 = "#d81b60", "0.2"               # the 7-setting lines: the mix pink, as in every figure of the 49
MIX, UNIFORM = "#f48fb1", "0.6"                 # with Chunked at 64 KB only: lighter
GUIDE = "0.2"
REF = "props_split_adj_condensed"               # Sp-Co: 1x on both axes
MARKS = (("A", 2.0), ("B", 5.0))                # the best mix within 2x and 5x Sp-Co's bytes
FAINT = (1.5, 3.0)                              # faint guides in (a) at these multiples of Sp-Co's bytes
SETTINGS = ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked", "chunked_c256k")
SHORT = {"split": "Sp", "condensed": "Co", "chunked_c1k": "Ch1k", "chunked_c4k": "Ch4k", "chunked_c16k": "Ch16k",
         "chunked": "Ch64k", "chunked_c256k": "Ch256k"}
HUE = {"split": ADJ_COLOR["split"], "condensed": ADJ_COLOR["condensed"], "chunked_c1k": CHUNK_SHADE[1],
       "chunked_c4k": CHUNK_SHADE[4], "chunked_c16k": CHUNK_SHADE[16], "chunked": CHUNK_SHADE[64],
       "chunked_c256k": CHUNK_SHADE[256]}
INDEX = "#bdbdbd"
FRONT_HW = 0.5                                  # panel (a) alone: its front box's height / width (printed one column wide)
X_TIMES = (1, 2, 5, 10, 20)
TICKS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 0.7, 1, 1.5, 2, 3, 4, 5, 7, 10, 20, 50, 100)   # panel (a) alone: from the data
SPARSE = (0.7, 1.5, 3, 4, 7)                    # x ticks only (the y axis keeps 1-2-5)
Y_TIMES = (0.05, 0.1, 0.2, 0.5, 1, 2)


def tint(c, f=0.55):
    """c mixed with white: f = 0 is c, 1 is white"""
    return to_hex([x + (1 - x) * f for x in to_rgb(c)])


def color(kind, setting):
    if kind == "index":
        return INDEX
    return tint(HUE[setting]) if kind == "props" else HUE[setting]


def rects(ax, x0, x1, y0, y1, colors):
    """many filled rectangles as one collection; zero-size ones dropped"""
    verts, cs = [], []
    for a, b, c, d, col in zip(x0, x1, y0, y1, colors):
        if b > a and d > c:
            verts.append(((a, c), (a, d), (b, d), (b, c)))
            cs.append(col)
    ax.add_collection(PolyCollection(verts, facecolors=cs, edgecolors="none", linewidths=0, zorder=3, rasterized=True))


def split(lo, top, fracs):
    """a bar from the axis edge lo to top on a log axis, cut in proportion to fracs along its drawn length"""
    edges, c = [lo], 0.0
    for f in fracs:
        c += f
        edges.append(lo * (top / lo) ** c)
    return edges


def front(pts):
    """the lower-left front of (bytes, wops, tag) points, by bytes"""
    out, best = [], float("inf")
    for b, w, tag in sorted(pts):
        if w < best:
            out.append((b, w, tag))
            best = w
    return out


def step(ax, pts, x1, **kw):
    ax.step([p[0] for p in pts] + [x1], [p[1] for p in pts] + [pts[-1][1]], where="post", **kw)


def times(v, _=None):
    return f"{v:g}x"


def main():
    data, out_dir = args(__doc__)
    pts = read_table(data, "front-by-part-49-layouts-i01-points")
    ref = [p for p in pts if p["kind"] == "uniform49" and p["name"] == REF]
    if len(ref) != 1:
        raise SystemExit(f"points: {len(ref)} {REF} rows, not one")
    rw, rb = float(ref[0]["wops"]), float(ref[0]["bytes"])
    of = lambda k: [(float(p["bytes"]) / rb, float(p["wops"]) / rw, p["name"]) for p in pts if p["kind"] == k]  # noqa
    u49, nine, mix3 = of("uniform49"), of("uniform9"), of("front3")
    if (len(u49), len(nine), len(mix3)) != (49, 9, 1588):
        raise SystemExit(f"points: {len(u49)} + {len(nine)} + {len(mix3)}, not 49 + 9 + 1,588")
    f3, fu3, fu7 = front(mix3 + nine), front(nine), front(u49)

    mixes = {}
    for r in read_table(data, "front-by-part-49-layouts-i01"):
        m = mixes.setdefault(int(r["mix"]), {"X": float(r["bytes"]) / rb, "Y": float(r["wops"]) / rw,
                                             "ops": float(r["wops"]), "bytes": float(r["bytes"]), "units": {}})
        m["units"][(r["kind"], r["part"])] = (r["setting"], float(r["part_wops"]), float(r["part_bytes"]))
    mixes = [mixes[k] for k in sorted(mixes)]
    if len(mixes) != 167:
        raise SystemExit(f"front-by-part-49-layouts-i01.csv: {len(mixes)} front points, not 149")
    marks = [(n, [m for m in mixes if m["X"] <= lim * (1 + 1e-9)][-1]) for n, lim in MARKS]
    allx = [p[0] for p in u49] + [m["X"] for m in mixes]
    x0, x1 = min(allx) / 1.06, max(allx) * 1.08
    ylim = (mixes[-1]["Y"] / 1.5, 2.6)
    nxt = [mixes[i + 1]["X"] if i + 1 < len(mixes) else x1 for i in range(len(mixes))]

    units = sorted({u for m in mixes for u in m["units"]})

    def share(m, u, i):
        """a part's share of a mix's wops (i = 1) or bytes (i = 2)"""
        return m["units"][u][i] / (m["ops"] if i == 1 else m["bytes"]) if u in m["units"] else 0.0

    def col(m, u):
        return color(u[0], m["units"][u][0]) if u in m["units"] else INDEX

    # (a, below)'s rows: vertex types, then edge types, each by its top share; types under 1% everywhere left out
    rows = [u for u in units if u[0] != "index" and any(share(m, u, i) >= 0.01 for m in mixes for i in (1, 2))]
    weight = {u: max(share(m, u, 1) for m in mixes) for u in rows}
    rows.sort(key=lambda u: (u[0] != "props", -weight[u]))
    nv = sum(u[0] == "props" for u in rows)
    # (b), (c) stack the parts from the front line inward in (a)'s row order; the index and the rest at the axis
    stack = [u for u in units if u[0] == "index"] + [u for u in units if u not in rows and u[0] != "index"] + rows[::-1]
    assert sorted(stack) == units
    halo = [pe.Stroke(linewidth=2.0, foreground="white"), pe.Normal()]

    def mark(a, vertical=True, label=True, off=(3, 3), strong=True):
        """A and B: a dot and its name, and a guide line through it (vertical, or horizontal in (c))"""
        for n, m in marks:
            (a.axvline if vertical else a.axhline)(m["X"] if vertical else m["Y"], color=GUIDE if strong else "0.5",
                                                   lw=0.75 if strong else 0.45, ls=(0, (2, 1.5)), zorder=8)
            if label:
                a.plot([m["X"]], [m["Y"]], "o", color=MIX7, ms=3.2, mec="0.25", mew=0.5, zorder=9)
                a.annotate(n, (m["X"], m["Y"]), textcoords="offset points", xytext=off, fontsize=PT_SMALL,
                           fontweight="bold", color=MIX7, zorder=10,
                           path_effects=[pe.Stroke(linewidth=1.5, foreground="white"), pe.Normal()])

    def mixline(a):
        step(a, [(m["X"], m["Y"]) for m in mixes], x1, color=MIX7, lw=1.4, zorder=6, path_effects=halo)

    def axes(a, small=False, xlabel=True, ylabel=True):
        """the one pair of axes every panel uses: log-log multiples of Sp-Co, the same range and ticks"""
        a.axvline(1, color="0.45", lw=0.5, ls=":", zorder=1)
        a.axhline(1, color="0.45", lw=0.5, ls=":", zorder=1)
        a.set_xscale("log")
        a.set_yscale("log")
        a.set_xlim(x0, x1)
        a.set_ylim(*ylim)
        a.xaxis.set_major_locator(FixedLocator(X_TIMES))
        a.xaxis.set_major_formatter(FuncFormatter(lambda v, _: times(v) if not small or v in (1, 5, 20) else ""))
        a.xaxis.set_minor_locator(NullLocator())
        a.yaxis.set_major_locator(FixedLocator(Y_TIMES))
        a.yaxis.set_major_formatter(FuncFormatter(lambda v, _: times(v) if not small or v in (0.1, 1) else ""))
        a.yaxis.set_minor_locator(NullLocator())
        if ylabel:                                 # (b), (c) share (a)'s y axis and its name
            a.set_ylabel(r"x Sp-Co's $\mathit{wops}$", labelpad=1, fontsize=PT)
        if small and xlabel:
            a.set_xlabel("x Sp-Co's bytes", labelpad=1, fontsize=PT)

    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    # heights in inches: the legend, (a) over the type rows beside (b) over (c), then (d) and (e)
    head, upper, mid, low, foot = 0.53, 2.05, 0.37, 1.0, 0.24
    H = head + upper + mid + low + foot
    fig = plt.figure(figsize=(3.33, H))
    outer = fig.add_gridspec(1, 2, width_ratios=(1.45, 1), wspace=0.17, left=0.158, right=0.99,
                             top=1 - head / H, bottom=1 - (head + upper) / H)
    left = outer[0].subgridspec(2, 1, height_ratios=(0.95, 1.1), hspace=0.04)
    right = outer[1].subgridspec(2, 1, hspace=0.3)
    bottom = fig.add_gridspec(1, 2, width_ratios=(1.45, 1), wspace=0.17, left=0.158, right=0.99, top=(low + foot) / H,
                              bottom=foot / H)                  # (d) under (a), (e) under (b) and (c): the same columns

    # (a) the 49 uniform layouts, the four lines, A and B
    ax = fig.add_subplot(left[0])
    bx = fig.add_subplot(left[1], sharex=ax)
    on = {p[2] for p in fu7}
    for b, w, c in u49:
        st = cell_style(c)                       # as bill-49: filled = on the 49's front, open = beaten on both axes
        ax.scatter([b], [w], s=11, marker=st["marker"], facecolor=st["color"] if c in on else "none",
                   edgecolor="black" if c in on else st["color"], linewidth=0.4 if c in on else 0.7,
                   zorder=4 if c in on else 2)
    step(ax, fu3, x1, color=UNIFORM, lw=1.0, ls=(0, (3, 1.5)), zorder=5)
    step(ax, f3, x1, color=MIX, lw=1.3, zorder=5.5)
    step(ax, fu7, x1, color=UNIFORM7, lw=1.0, ls=(0, (3, 1.5)), zorder=5)
    mixline(ax)
    mark(ax, off=(3, -8))
    for x in FAINT:                              # faint guides between A and B's budgets
        ax.axvline(x, color="0.7", lw=0.4, ls=(0, (2, 1.5)), zorder=1)
    axes(ax)
    ax.tick_params(labelbottom=False)
    ax.set_title("(a) the front, and each type's setting", fontsize=PT, pad=2)

    # (a, below) each type's setting at each point of the front
    for j, u in enumerate(rows):
        rects(bx, [m["X"] for m in mixes], nxt, [j - 0.46] * len(mixes), [j + 0.46] * len(mixes), [col(m, u) for m in mixes])
    bx.set_yticks(range(len(rows)), [u[1] + (" (V)" if u[0] == "props" else "") for u in rows], fontsize=PT_SMALL - 0.5)
    bx.axhline(nv - 0.5, color="0.2", lw=0.6)
    mark(bx, label=False)
    for x in FAINT:
        bx.axvline(x, color="0.7", lw=0.4, ls=(0, (2, 1.5)), zorder=8)
    bx.set_ylim(len(rows) - 0.5, -0.5)
    bx.xaxis.set_major_locator(FixedLocator(X_TIMES))
    bx.xaxis.set_major_formatter(FuncFormatter(times))
    bx.xaxis.set_minor_locator(NullLocator())
    bx.set_xlabel("x Sp-Co's bytes", labelpad=1, fontsize=PT)

    # (b) under each front mix a bar to its wops, cut by part
    cx = fig.add_subplot(right[0])
    cuts = [split(ylim[0], m["Y"], [share(m, u, 1) for u in stack]) for m in mixes]
    for i, u in enumerate(stack):
        rects(cx, [m["X"] for m in mixes], nxt, [c[i] for c in cuts], [c[i + 1] for c in cuts], [col(m, u) for m in mixes])
    mixline(cx)
    mark(cx)
    axes(cx, small=True, xlabel=False, ylabel=False)
    cx.set_title(r"(b) $\mathit{wops}$ by part", fontsize=PT, pad=2)

    # (c) left of each front mix a bar to its bytes, cut by part; each band from its wops to the next higher mix's
    dx = fig.add_subplot(right[1])
    hs = [(mixes[i - 1]["Y"] if i > 0 else m["Y"] * 1.03) - m["Y"] for i, m in enumerate(mixes)]
    cuts = [split(x0, m["X"], [share(m, u, 2) for u in stack]) for m in mixes]
    for i, u in enumerate(stack):
        rects(dx, [c[i] for c in cuts], [c[i + 1] for c in cuts], [m["Y"] for m in mixes],
              [m["Y"] + hh for m, hh in zip(mixes, hs)], [col(m, u) for m in mixes])
    mixline(dx)
    mark(dx, vertical=False)
    axes(dx, small=True, ylabel=False)
    dx.set_title("(c) bytes by part", fontsize=PT, pad=2)

    # (d) each large part's own trade-off: its wops and bytes at each setting the front gives it
    ex = fig.add_subplot(bottom[0])
    TP = (("adj", "replyOf"), ("adj", "containerOf"), ("adj", "hasTag"), ("adj", "hasCreator"), ("adj", "workAt"),
          ("props", "Message"), ("adj", "knows"))
    per = {}
    for m in mixes:
        for u in TP:
            if u in m["units"]:
                st, w, by = m["units"][u]
                per.setdefault(u, {})[st] = (by / rb, w / rw)
    LC = dict(zip(TP, ("#1f78b4", "#33a02c", "#b8860b", "#6a3d9a", "#8c564b", "#555555", "#ff7f00")))
    for u in TP:
        p = [(s_, *per[u][s_]) for s_ in SETTINGS if s_ in per[u]]
        xs, ys = [q[1] for q in p], [q[2] for q in p]
        ex.plot(xs, ys, color=LC[u], lw=0.9, zorder=2)
        ex.scatter(xs, ys, c=[color(u[0], q[0]) if u[0] == "adj" else HUE[q[0]] for q in p], s=9, edgecolors="black",
                   linewidths=0.3, zorder=3)
        name = u[1] + (" (V)" if u[0] == "props" else "")
        at, dx_, dy, ha, va = {"replyOf": (0, 0, 3, "center", "bottom"), "containerOf": (-1, 3, 0, "left", "center"),
                               "workAt": (-1, 3, 0, "left", "center"), "hasTag": (-1, -2, -7, "right", "top"),
                               "hasCreator": (-1, 0, -3, "center", "top"),
                               "Message (V)": (-1, 0, 3, "center", "bottom"),
                               "knows": (0, -3, 0, "right", "center")}[name]
        ex.annotate(name, (xs[at], ys[at]), xytext=(dx_, dy), textcoords="offset points", fontsize=PT_SMALL - 0.5,
                    ha=ha, va=va, color=LC[u], fontweight="bold", path_effects=halo)
    ex.set_xscale("log")
    ex.set_yscale("log")
    for axis in (ex.xaxis, ex.yaxis):
        axis.set_major_formatter(FuncFormatter(times))
        axis.set_minor_locator(NullLocator())
    ex.xaxis.set_major_locator(FixedLocator((0.01, 0.1, 1, 10)))
    ex.yaxis.set_major_locator(FixedLocator((0.0001, 0.001, 0.01, 0.1, 1)))
    ex.set_xlim(0.003, 60)
    ex.set_ylim(0.00006, 1)
    ex.set_xlabel("the part's bytes, x Sp-Co's", labelpad=1, fontsize=PT)
    ex.set_ylabel(r"the part's $\mathit{wops}$" "\n" "x Sp-Co's", labelpad=1, fontsize=PT, linespacing=0.95)
    ex.set_title("(d) seven parts' cost at each setting", fontsize=PT, pad=2)
    ex.grid(alpha=0.3, lw=0.4)

    # (e), (f) A's and B's wops and bytes as one vertical bar each, cut by part in (b)'s order
    ef = bottom[1].subgridspec(1, 2, wspace=0.06)   # (f) shares (e)'s 0-100% axis, so the two sit close
    fx, gx = fig.add_subplot(ef[0]), fig.add_subplot(ef[1])
    renderer = fig.canvas.get_renderer()
    for a, i, cur, title in ((fx, 1, "ops", r"(e) $\mathit{wops}$"), (gx, 2, "bytes", "(f) bytes")):
        unit = rw if cur == "ops" else rb
        BAR = 0.42
        lim = 1.3                                    # each bar is 100% of its mix's cost; its total above it
        a.set_ylim(0, lim)
        a.set_xlim(-0.3, len(marks) - 0.7)
        seg = {}                                     # (part, bar) -> the segment's middle, for the callouts
        for xi, (n, m) in enumerate(marks):
            ly = 0.0
            total = sum(m["units"][u][i] for u in stack if u in m["units"]) / unit
            for u in stack:
                v = (m["units"][u][i] if u in m["units"] else 0.0) / unit / total
                a.bar(xi, v, bottom=ly, width=BAR, color=col(m, u), edgecolor="white", lw=0.25)
                seg[(u, xi)] = ly + v / 2
                sh = share(m, u, i)
                if u in CALLOUT:
                    pass
                elif sh >= 0.1 and u[0] != "index":          # the part's name and share, kept only if it fits the segment
                    t = a.text(xi, ly + v / 2, f"{u[1]}{' (V)' if u[0] == 'props' else ''}\n{100 * sh:.0f}%", ha="center", va="center", rotation=90, linespacing=0.9,
                               fontsize=PT_SMALL - 1, fontweight="bold", color="white" if u[0] == "adj" else "0.1")
                    ext = t.get_window_extent(renderer).transformed(a.transData.inverted())
                    if ext.height > v * 0.95 or ext.width > BAR * 1.4:
                        t.remove()
                ly += v
            a.text(xi, ly + lim * 0.015, f"{total:.2f}x" if total < 1 else f"{total:.1f}x", ha="center", va="bottom",
                   fontsize=PT_SMALL, fontweight="bold", rotation=90)
        # the callouts: each name in the gap between the bars, stacked apart, a line to its segment in each bar
        low = 0.01                                   # placed bottom-up, each above the last
        for u in sorted(CALLOUT, key=lambda u: sum(seg[(u, xi)] for xi in range(len(marks)))):
            y = sum(seg[(u, xi)] for xi in range(len(marks))) / len(marks)
            t = a.text(0.5, 0, u[1], ha="center", va="center", rotation=90, fontsize=PT_SMALL - 1, color="0.15")
            ext = t.get_window_extent(renderer).transformed(a.transData.inverted())
            y = max(y, low + ext.height / 2)
            t.set_y(y)
            low = y + ext.height / 2 + 0.1
            for xi in range(len(marks)):
                x0 = ext.x0 - 0.02 if xi == 0 else ext.x1 + 0.02
                a.plot([x0, xi + (BAR / 2 if xi == 0 else -BAR / 2)], [y, seg[(u, xi)]], color="0.3", lw=0.4,
                       solid_capstyle="butt", clip_on=False)
        a.set_xticks(range(len(marks)), [n for n, _ in marks])
        a.yaxis.set_major_locator(FixedLocator((0, 0.5, 1)))
        a.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{100 * v:.0f}%"))
        a.tick_params(axis="y", labelleft=cur == "ops")
        a.tick_params(axis="x", length=0)
        a.set_title(title, fontsize=PT, pad=2)
        a.set_ylabel("share" if cur == "ops" else "", fontsize=PT, labelpad=1)
        for side in ("top", "right"):
            a.spines[side].set_visible(False)

    for a in (ax, bx, cx, dx, ex, fx, gx):
        a.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
        for side in a.spines.values():
            side.set_linewidth(0.5)
    for a in (ax, cx, dx):
        a.grid(alpha=0.3, lw=0.4)
    # three legend rows: the four lines, the settings' colours, the properties' markers (the points in (a))
    lines = [Line2D([], [], color=MIX7, lw=1.4, label="best mix (Ch1k-256k)"),
             Line2D([], [], color=UNIFORM7, lw=1.0, ls=(0, (3, 1.5)), label="best uniform (49)"),
             Line2D([], [], color=MIX, lw=1.3, label="best mix (Ch64k)"),
             Line2D([], [], color=UNIFORM, lw=1.0, ls=(0, (3, 1.5)), label="best uniform (9)")]
    colours = [Patch(fc=HUE[s], label=SHORT[s]) for s in SETTINGS] + [Patch(fc=tint("#888888"), label="vertex: lighter")]
    kw = dict(frameon=False, fontsize=PT_SMALL, handlelength=1.0, columnspacing=0.45, handletextpad=0.3, borderaxespad=0,
              borderpad=0)
    first = fig.legend(handles=lines, loc="upper center", ncol=4, bbox_to_anchor=(0.5, 1.0), **kw)
    fig.legend(handles=colours, loc="upper center", ncol=8, bbox_to_anchor=(0.5, 1 - 0.15 / H),
               **{**kw, "handlelength": 0.9, "columnspacing": 0.45})
    # a point's marker is its properties setting (paper_style.PROPS_MARKER, through cell_style); filled = on the front
    prop_marks = [Line2D([], [], ls="", marker="", label="props:")]
    prop_marks += [Line2D([], [], ls="", marker=cell_style(cell_id(s, "split"))["marker"], ms=3.6, mfc="0.45", mec="black",
                    mew=0.4, label=SHORT[s]) for s in SETTINGS]
    prop_marks.append(Line2D([], [], ls="", marker="o", ms=3.6, mfc="none", mec="0.45", mew=0.7, label="open: beaten"))
    second = fig.legend(handles=prop_marks, loc="upper center", ncol=9, bbox_to_anchor=(0.5, 1 - 0.29 / H),
                        **{**kw, "handlelength": 0.7, "columnspacing": 0.35, "handletextpad": 0.15})
    fig.add_artist(first)
    fig.add_artist(second)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    for n, m in marks:
        print(f"{n}: {m['X']:.2f}x Sp-Co's bytes, {m['Y']:.3f}x its wops")
    print(f"{len(mixes)} front points, {len(rows)} type rows, stack {len(stack)} parts; wrote {out}")


def above(hi, lo, x0, x1, n=4000):
    """(the x positions, of n log-spaced over [x0, x1], where the step line through hi is above lo's; the positions
    where both have started): a front over more candidates is never above one over fewer"""
    at = lambda pts, x: next((p[1] for p in reversed(pts) if p[0] <= x * (1 + 1e-12)), None)   # noqa: E731
    xs = [x0 * (x1 / x0) ** (i / (n - 1)) for i in range(n)]
    pairs = [(at(hi, x), at(lo, x)) for x in xs]
    pairs = [(h, low) for h, low in pairs if h is not None and low is not None]
    return sum(h > low * (1 + 1e-9) for h, low in pairs), len(pairs)


def main_panel_a(argv=None):
    """panel (a) alone for one workload's tables (the docstring's second form); argv: as the command line's"""
    ap = argparse.ArgumentParser(description="panel (a) alone for one workload")
    ap.add_argument("--panel", required=True, choices=["a"])
    ap.add_argument("--name", required=True, help="the tables' name: front-by-part-<name>*.csv (b01, j01)")
    ap.add_argument("--figure", help="the figure's name: front-mix-vs-uniform-<figure>.pdf (default: --name without "
                                     "its 49-layouts- prefix)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--loose-label", default="found", help="what an open dot is, in the legend before its gap "
                                                          "(JOB: 'start-rule mix': the search improved no start)")
    a = ap.parse_args(argv)
    pal = HUE
    table = f"front-by-part-{a.name}"
    pts = read_table(a.data, f"{table}-points")
    ref = [p for p in pts if p["kind"] == "uniform49" and p["name"] == REF]
    if len(ref) != 1:
        raise SystemExit(f"points: {len(ref)} {REF} rows, not one")
    u49_raw = [p for p in pts if p["kind"] == "uniform49"]
    rw, rb = min(float(p["wops"]) for p in u49_raw), min(float(p["bytes"]) for p in u49_raw)   # 1 = the fewest of the 49
    of = lambda k: [(float(p["bytes"]) / rb, float(p["wops"]) / rw, p["name"]) for p in pts if p["kind"] == k]  # noqa
    u49, nine, mix3 = of("uniform49"), of("uniform9"), of("front3")
    if (len(u49), len(nine)) != (49, 9):
        raise SystemExit(f"points: {len(u49)} + {len(nine)}, not 49 + 9")
    fu3, fu7 = front(nine), front(u49)
    f3 = front(mix3 + nine) if mix3 else None
    limits = {r["label"]: r for r in read_table(a.data, f"{table}-limits")}
    mixes = {}
    for r in read_table(a.data, table):
        lim = limits.get(r["label"]) if r["source"] == "solved" else None
        m = mixes.setdefault(int(r["mix"]), {"X": float(r["bytes"]) / rb, "Y": float(r["wops"]) / rw, "units": {},
                                             "proven": lim is None or lim["proven"] == "1",
                                             "gap": None if lim is None or lim["gap"] == "" else float(lim["gap"])})
        m["units"][(r["kind"], r["part"])] = (r["setting"], float(r["part_wops"]), float(r["part_bytes"]))
    mixes = [mixes[k] for k in sorted(mixes)]
    allx = [p[0] for p in u49] + [m["X"] for m in mixes]
    ally = [p[1] for p in u49] + [m["Y"] for m in mixes]
    x0, x1 = min(allx) / 1.1, max(allx) * 1.15                 # the padding of uniform-49-layouts-w16-front-log
    ylim = (min(ally) / 1.3, max(ally) * 1.3)
    nxt = [mixes[i + 1]["X"] if i + 1 < len(mixes) else x1 for i in range(len(mixes))]
    mixline = [(m["X"], m["Y"]) for m in mixes]
    checks = [("mix over uniform49", *above(mixline, fu7, x0, x1))] + ([("front3 over uniform9", *above(f3, fu3, x0, x1))] if f3 else [])
    for name, bad, n in checks:                   # a front over more candidates is never above one over fewer
        print(f"check {name}: {bad} of {n} x positions above")
        if bad or not n:
            raise SystemExit(f"check {name} failed: {bad} of {n}")
    units = sorted({u for m in mixes for u in m["units"]})
    tot = lambda m, i: sum(v[i] for v in m["units"].values())                              # noqa: E731
    share = lambda m, u, i: m["units"][u][i] / tot(m, i) if u in m["units"] else 0.0      # noqa: E731
    col = lambda m, u: pal[m["units"][u][0]] if u in m["units"] else INDEX                 # noqa: E731  (alone: vertex types not lighter)
    rows = [u for u in units if u[0] != "index" and any(share(m, u, i) >= 0.01 for m in mixes for i in (1, 2))]
    weight = {u: max(share(m, u, 1) for m in mixes) for u in rows}
    rows.sort(key=lambda u: (u[0] != "props", -weight[u]))
    nv = sum(u[0] == "props" for u in rows)
    names = [u[1] + (" (V)" if u[0] == "props" else "") for u in rows]
    halo = [pe.Stroke(linewidth=2.0, foreground="white"), pe.Normal()]

    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    left = max(0.19, 0.06 + 0.0125 * max(map(len, names)))     # room for the longest row name (JOB's edge types)
    top_h = (0.97 - left) * 3.33 * FRONT_HW                    # the front's box
    rows_h, gap, head, foot = 0.105 * len(rows), 0.2, 0.5, 0.31     # gap: the front's own x tick labels
    H = head + top_h + gap + rows_h + foot
    fig = plt.figure(figsize=(3.33, H))
    gs = fig.add_gridspec(2, 1, height_ratios=(top_h, rows_h), hspace=gap / ((top_h + rows_h) / 2), left=left,
                          right=0.97, top=1 - head / H, bottom=foot / H)
    ax = fig.add_subplot(gs[0])
    bx = fig.add_subplot(gs[1], sharex=ax)
    on = {p[2] for p in fu7}
    for b, w, c in u49:
        st = cell_style(c)
        ax.scatter([b], [w], s=11, marker=st["marker"], facecolor=st["color"] if c in on else "none",
                   edgecolor="black" if c in on else st["color"], linewidth=0.4 if c in on else 0.7,
                   zorder=4 if c in on else 2)
        mark_engine_point(ax, b, w, c, *((6, 12) if c == "props_split_adj_condensed" else (-12, 0)), leader=c == "props_split_adj_condensed")
    # the nine at 64 KB and their 3-setting mixes are checked above but not drawn: this figure is the mix against the 49
    step(ax, fu7, x1, color=UNIFORM7, lw=1.0, zorder=5)
    step(ax, mixline, x1, color=MIX7, lw=1.4, zorder=6, path_effects=halo)
    loose = [m for m in mixes if not m["proven"]]
    if loose:
        ax.plot([m["X"] for m in loose], [m["Y"] for m in loose], "o", ms=2.6, mfc="white", mec=MIX7, mew=0.7, zorder=7)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(x0, x1)
    ax.set_ylim(*ylim)
    ax.xaxis.set_major_locator(FixedLocator([t for t in (1, 1.5, 2, 3, 4, 5, 10, 20, 50, 100) if x0 <= t <= x1]))
    ax.xaxis.set_major_formatter(FuncFormatter(times))
    ax.xaxis.set_minor_locator(NullLocator())
    ax.yaxis.set_major_locator(FixedLocator([t for t in (1, 2, 5, 10, 20, 50, 100) if ylim[0] <= t <= ylim[1]]))
    ax.yaxis.set_major_formatter(FuncFormatter(times))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylabel(r"x the fewest $\mathit{wops}$ (log)", labelpad=1, fontsize=PT)
    ax.tick_params(labelbottom=True)                         # the x labels under the front too, not only under the rows
    ax.set_title(a.title, fontsize=PT, pad=2, x=(0.5 - left) / (0.97 - left))     # centred on the figure
    ax.grid(alpha=0.25, lw=0.4)
    ax.grid(axis="x", which="major", color="0.85", lw=0.4)
    for j, u in enumerate(rows):
        rects(bx, [m["X"] for m in mixes], nxt, [j - 0.46] * len(mixes), [j + 0.46] * len(mixes), [col(m, u) for m in mixes])
    bx.set_yticks(range(len(rows)), names, fontsize=PT_ALONE)
    if 0 < nv < len(rows):
        bx.axhline(nv - 0.5, color="0.2", lw=0.6)
    bx.set_ylim(len(rows) - 0.5, -0.5)
    bx.set_xlabel("x the fewest bytes (log)", labelpad=1, fontsize=PT)
    for a_ in (ax, bx):
        a_.tick_params(labelsize=PT_ALONE, pad=1, length=2, width=0.5)
        for side in a_.spines.values():
            side.set_linewidth(0.5)
    gaps = [m["gap"] for m in loose if m["gap"] is not None]
    lines = [Line2D([], [], color=MIX7, lw=1.4, label="best mix found" if loose else "best mix"),
             Line2D([], [], color=UNIFORM7, lw=1.0, label="best uniform")]
    if loose:
        lines.append(Line2D([], [], marker="o", ls="", ms=2.6, mfc="white", mec=MIX7, mew=0.7,
                            label=f"{a.loose_label}, within {max(gaps):.0%} of the bound" if gaps else f"{a.loose_label}, not proven"))
    colours = [Patch(fc=pal[s], label=SHORT[s]) for s in SETTINGS]
    kw = dict(frameon=False, fontsize=PT_ALONE, handlelength=1.0, columnspacing=0.4, handletextpad=0.3,
              borderaxespad=0, borderpad=0)
    eh, el = engine_legend()
    first = fig.legend(lines + eh, [h.get_label() for h in lines] + el, loc="upper center", ncol=len(lines) + 2,
                       bbox_to_anchor=(0.5, 1 - 0.02 / H),
                       **{**kw, "columnspacing": 0.8, "handlelength": 1.6})
    colour_engine_legend(first)
    second = fig.legend(handles=colours, loc="upper center", ncol=7, bbox_to_anchor=(0.5, 1 - 0.14 / H), **{**kw, "handlelength": 0.9})
    marks_ = [Line2D([], [], ls="", marker="", label="props:")]                   # the properties markers, as the 49's front
    marks_ += [Line2D([], [], ls="", marker=cell_style(cell_id(s, "split"))["marker"], ms=3.4, mfc="0.45", mec="black",
                      mew=0.4, label=SHORT[s]) for s in SETTINGS]
    marks_ += [Line2D([], [], ls="", marker="o", ms=3.4, mfc="none", mec="0.45", mew=0.7, label="open: beaten")]
    fig.legend(handles=marks_, loc="upper center", ncol=9, bbox_to_anchor=(0.5, 1 - 0.27 / H), **{**kw, "columnspacing": 0.25, "handletextpad": 0.15})
    fig.add_artist(first)
    fig.add_artist(second)
    out = a.out / f"figures/evaluation/front-mix-vs-uniform-{a.figure or a.name.removeprefix('49-layouts-')}.pdf"   # w16, b01, j01
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=300)                     # the type rows are rasterized: at print resolution
    fig.savefig(out.with_suffix(".png"), dpi=300)
    print(f"{a.title}: {len(mixes)} front mixes ({len(loose)} not proven{f', the largest gap {max(gaps):.1%}' if gaps else ''}), "
          f"{len(mix3)} front3, 49 uniform; {len(rows)} type rows; wrote {out}")


if __name__ == "__main__":
    main_panel_a() if "--panel" in sys.argv[1:] else main()
