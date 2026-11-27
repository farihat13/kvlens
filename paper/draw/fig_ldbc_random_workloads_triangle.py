#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/ldbc-random-workloads-triangle/fig_ldbc_random_workloads_triangle.py); edit it there, then run evidence/release_draw.py
"""Paper figure: LDBC Interactive at SF3, random workloads on a triangle: which uniform layout is the cheapest as the
split between complex reads (IC), short reads (IS) and updates (IU) moves (one paper column).

  figures/evaluation/ldbc-random-workloads-triangle-right.pdf   the 49 on a right-angled triangle: x = complex reads'
      share, y = updates' share, short reads the rest (faint dashed diagonals); regions only, LDBC's frequencies
      (diamond) and each query an equal share of Sp-Co's cost (square)
  figures/evaluation/ldbc-random-workloads-triangle-with-frequency.pdf   (a) the 49 as above, at the same size, and
      (b) small, top right: the same 49 where a point is each group's share of the FREQUENCY (of the runs), with a
      line where the winner changes; one legend for both (no tile counts)

A point of the triangle is a split: each group's share of the workload's cost on Sp-Co (Dgraph's layout). The triangle
is cut into 400 tiles (5% steps); in each, 100 random workloads with the tile's split (the queries inside a group
weighted at random, Dirichlet(1): the same 100 draws in every tile). A tile is filled with the layout that is the
cheapest for the most of them under wops + bytes / 4,096; a black dot = no layout is the cheapest for at least half,
or two tie. Colour = the adjacency setting, white hatch = the properties setting (the paper's cell style); the legend
(right of the triangle) gives each winner's tiles. Faint lines every 20% of each share; the 20% updates and the 20% complex lines are named. The corners name the groups;
each edge's ticks give its corner's share, the arrows the way it grows.
The diamond is LDBC's frequencies (W16); the square gives each query an equal share of Sp-Co's cost (a group's share = its
queries / all 27). Both are named in the legend only. The rest (what a tile is, the draws, the rule for a dot, the axes' unit) is
the caption's. Deterministic counts: no error bars.

The frequency tables (ldbc-random-workloads-triangle-frequency.csv, -frequency-groups.csv) are the same columns made
from the snapshot's `executions` column (each group's share of the runs) instead of `cost_sp_co`.

Reads only its tables (the artifact's paper/derive/ldbc_random_workloads_triangle.py makes them from the snapshot
sf3/random-spread-20261103): ldbc-random-workloads-triangle.csv (per layout set, tile and layout: the wins) and
ldbc-random-workloads-triangle-groups.csv (per group: its queries, W16's share), the two frequency tables, and
ldbc-random-workloads-triangle-queries.csv (per W16 query: its group, frequency and cost per run on Sp-Co; it gives the
LinkBench and TAO marks their costs).

    python fig_ldbc_random_workloads_triangle.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon

from common import args, read_table, use_paper_font
from paper_style import PRINT_PT, cell_settings, cell_style

NAME = "ldbc-random-workloads-triangle"
OUTS = {"": "sp-co-ch1k-ch4k-ch16k-ch64k-ch256k", "-25": "sp-co-ch1k-ch4k-ch16k", "-9": "sp-co-ch64k"}
SETTINGS = ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked", "chunked_c256k")
SHORT = {"split": "Sp", "condensed": "Co", "chunked": "Ch64k"}
GROUPS = ("complex", "short", "update")
STEPS, DRAWS = 20, 100
SQ3 = np.sqrt(3) / 2
PT, PT_SMALL = PRINT_PT, PRINT_PT - 1   # 7 pt (paper_style.PRINT_PT) and 6 pt: the half-height triangle
# two more real mixes as frequencies of reads and writes, both with no complex reads: LinkBench's default (69% reads,
# 31% writes; its workload config) and Facebook TAO's (99.8% reads, 0.2% writes; Bronson et al., ATC'13). Placed the way
# LDBC's diamond is: the reads' share split equally over LDBC's short reads (IS1-7), the writes' over its updates
# (IU1-8); each query's frequency times its combined cost on Sp-Co; each group's share of the total.
OTHER_MIXES = (("LinkBench's frequencies", 0.31, "^"), ("TAO's frequencies", 0.002, "v"))
NAMED = "#1a1a1a"           # the two named guides (updates 20%, complex 20%)
TRIANGLE_IN = 1.62          # the canvas height before the crop (the legend sits to the right of the triangle)
TRIANGLE_W = 0.52           # the share of the column the triangle's axes take
REGION = 40                 # a layout that fills this many tiles is named inside the triangle


def short(s):
    return SHORT.get(s) or "Ch" + s.split("_c")[1]


def label(cell):
    """properties-adjacency, each Chunked side with its size, e.g. Ch1k-Co, Sp-Ch64k"""
    p, a = cell_settings(cell)
    return f"{short(p)}-{short(a)}"


def order(cell):
    """the set's order (a tie goes to the first): properties outer, adjacency inner, settings ascending"""
    p, a = cell_settings(cell)
    return SETTINGS.index(p), SETTINGS.index(a)


def xy(c, s, u):
    """a split (complex, short, update) on the page: complex bottom left, short bottom right, updates on top"""
    return s + u / 2, u * SQ3


def tiles(n):
    """the triangle cut into n^2 small triangles, each as its 3 corners in (short, update); the snapshot's tile order"""
    out = []
    for i in range(n):
        for j in range(n - i):
            out.append([(i, j), (i + 1, j), (i, j + 1)])
            if i + j <= n - 2:
                out.append([(i + 1, j), (i, j + 1), (i + 1, j + 1)])
    return [[(a / n, b / n) for a, b in t] for t in out]


def load(data, name=NAME):
    """({layout set: {tile: {layout: wins}}}, {tile: (complex, short, update) centre}, {group: (queries, W16 share)})"""
    wins, centre = {}, {}
    for r in read_table(data, name):
        t = int(r["tile"])
        wins.setdefault(r["layout_set"], {}).setdefault(t, {})[r["layout"]] = int(r["wins"])
        centre[t] = tuple(float(r[f"share_{g}"]) for g in GROUPS)
    groups = {r["group"]: (int(r["queries"]), float(r["i01_share"])) for r in read_table(data, f"{name}-groups")}
    for s, by in wins.items():
        if sorted(by) != list(range(STEPS ** 2)) or any(sum(w.values()) != DRAWS for w in by.values()):
            raise SystemExit(f"{name}.csv: {s}: {len(by)} tiles, or a tile whose wins do not add up to {DRAWS}")
    for t, corners in enumerate(tiles(STEPS)):   # the tile order here is the snapshot's: each centre must match
        s, u = np.mean([p[0] for p in corners]), np.mean([p[1] for p in corners])
        if abs(centre[t][1] - s) > 1e-9 or abs(centre[t][2] - u) > 1e-9:
            raise SystemExit(f"{name}.csv: tile {t}'s centre {centre[t]} is not the drawn tile's ({1 - s - u}, {s}, {u})")
    return wins, groups


def fill(ax, pts, cell):
    st = cell_style(cell)
    ax.add_patch(Polygon(pts, closed=True, facecolor=st["color"], edgecolor=st["color"], lw=0.2, antialiased=False,
                         zorder=1))
    if st["hatch"]:
        with plt.rc_context({"hatch.linewidth": st["hatch_linewidth"]}):
            ax.add_patch(Polygon(pts, closed=True, facecolor="none", edgecolor="white", lw=0, hatch=st["hatch"],
                                 antialiased=False, zorder=2))


def guides(ax, named=True):
    """every 20% of each share: faint lines; the 20% updates and the 20% complex lines named (unless named=False)"""
    for v in (0.2, 0.4, 0.6, 0.8):
        for a, b in (((1 - v, 0, v), (0, 1 - v, v)), ((v, 1 - v, 0), (v, 0, 1 - v)), ((1 - v, v, 0), (0, v, 1 - v))):
            (x0, y0), (x1, y1) = xy(*a), xy(*b)
            ax.plot([x0, x1], [y0, y1], color="black", lw=0.35, alpha=0.3, zorder=3, solid_capstyle="butt")
    if not named:
        return
    for a, b in (((0.8, 0, 0.2), (0, 0.8, 0.2)), ((0.2, 0.8, 0), (0.2, 0, 0.8))):
        (x0, y0), (x1, y1) = xy(*a), xy(*b)
        ax.plot([x0, x1], [y0, y1], color=NAMED, lw=0.9, ls=(0, (3, 1.5)), zorder=4)
    x, y = xy(0, 0.8, 0.2)                            # at the right end of the updates 20% line, clear of the markers
    ax.text(x - 0.06, y - 0.012, "updates 20%", ha="right", va="top", fontsize=PT_SMALL - 0.5, zorder=7,
            bbox=dict(fc="white", ec="none", pad=0.4, alpha=0.85))
    x, y = xy(0.2, 0.35, 0.45)                        # on the complex 20% line, along it
    ax.text(x + 0.012, y + 0.007, "complex 20%", ha="center", va="bottom", fontsize=PT_SMALL - 0.5, zorder=7,
            rotation=-60, rotation_mode="anchor", bbox=dict(fc="white", ec="none", pad=0.4, alpha=0.85))


def frame(ax, groups, stubs=False):
    """the outline, each edge's 20/40/60/80 ticks and its share's name, the corners; stubs: each number at the end of a
    short tick that continues its own grid line out of the triangle (two grid lines meet at each edge point)"""
    ax.add_patch(Polygon([(0, 0), (1, 0), (0.5, SQ3)], closed=True, fill=False, lw=0.6, zorder=5))
    if stubs:
        L, gap = 0.035, 0.05
        out = {"complex": (0.5, -SQ3), "update": (-1.0, 0.0), "short": (0.5, SQ3)}   # each share's line, outward (unit)
        for v in (0.2, 0.4, 0.6, 0.8):
            for g, (x, y) in (("complex", xy(v, 1 - v, 0)), ("update", xy(1 - v, 0, v)), ("short", xy(0, v, 1 - v))):
                dx, dy = out[g]
                ax.plot([x, x + L * dx], [y, y + L * dy], color="0.35", lw=0.5, zorder=5, solid_capstyle="butt")
                ax.text(x + (L + gap) * dx, y + (L + gap) * dy, f"{v * 100:.0f}", ha="center", va="center",
                        fontsize=PT_SMALL - 0.5, color="0.25")
    for v in (0.2, 0.4, 0.6, 0.8) if not stubs else ():
        x, y = xy(v, 1 - v, 0)                   # complex, on the bottom edge
        ax.text(x, y - 0.02, f"{v * 100:.0f}", ha="center", va="top", fontsize=PT_SMALL - 0.5, color="0.25")
        x, y = xy(1 - v, 0, v)                   # updates, on the left edge
        ax.text(x - 0.02, y, f"{v * 100:.0f}", ha="right", va="center", fontsize=PT_SMALL - 0.5, color="0.25")
        x, y = xy(0, v, 1 - v)                   # short, on the right edge
        ax.text(x + 0.02, y, f"{v * 100:.0f}", ha="left", va="center", fontsize=PT_SMALL - 0.5, color="0.25")
    n = {g: groups[g][0] for g in GROUPS}
    # the corners' names below the tick row (each edge's ticks give that corner's share)
    ax.text(0.24, -0.1, f"complex reads (IC1–{n['complex']})", ha="right", va="top", fontsize=PT)
    ax.text(0.76, -0.1, f"short reads (IS1–{n['short']})", ha="left", va="top", fontsize=PT)
    ax.text(0.5, SQ3 + 0.025, f"updates (IU1–{n['update']})", ha="center", va="bottom", fontsize=PT)
    # which way each share grows: under the bottom edge between the corner names, and outside the two side edges
    ax.text(0.5, -0.1, "← complex %", ha="center", va="top", fontsize=PT_SMALL, color="0.15")
    ax.text(0.25 - 0.17 * SQ3, SQ3 / 2 + 0.085, "updates % →", ha="center", va="center", rotation=60,
            rotation_mode="anchor", fontsize=PT_SMALL, color="0.15")
    ax.text(0.75 + 0.17 * SQ3, SQ3 / 2 + 0.085, "short % →", ha="center", va="center", rotation=-60,
            rotation_mode="anchor", fontsize=PT_SMALL, color="0.15")
    ax.set_xlim(-0.08, 1.08)                          # the corner names may run past: the crop keeps them
    ax.set_ylim(-0.19, SQ3 + 0.09)
    ax.set_aspect("equal")
    ax.axis("off")


def borders(ax, edge_tops, to_xy=xy):
    """a dark line on each tile edge whose two tiles have different winners (close colours stay apart)"""
    for (a, b), tops in sorted(edge_tops.items()):
        if len(tops) == 2 and tops[0] != tops[1]:
            (x0, y0), (x1, y1) = (to_xy(1 - s - u, s, u) for s, u in (a, b))
            ax.plot([x0, x1], [y0, y1], color="black", lw=0.55, zorder=4, solid_capstyle="round")


def tops_of_tiles(wins, layout_set):
    """per tile, in the draw order: (its top layout, clear or not); a tie goes to the first in the set's order"""
    out = []
    for t in range(len(tiles(STEPS))):
        w = wins[layout_set][t]
        top = max(sorted(w, key=order), key=lambda L: w[L])
        out.append((top, w[top] >= DRAWS / 2 and list(w.values()).count(w[top]) == 1))
    return out


def tiles_into(ax, tops, dots=True, with_borders=False, to_xy=xy):
    """fill each tile with its winner; a dot where no clear winner; a line where the winner changes (optional);
    to_xy places a split on the page (the equilateral triangle by default)"""
    edge_tops = {}
    for (top, clear), corners in zip(tops, tiles(STEPS)):
        pts = [to_xy(1 - s - u, s, u) for s, u in corners]
        fill(ax, pts, top)
        for k in range(3):
            a, b = sorted((corners[k], corners[(k + 1) % 3]))
            edge_tops.setdefault((tuple(round(v, 9) for v in a), tuple(round(v, 9) for v in b)), []).append(top)
        if dots and not clear:
            ax.plot(*np.mean(pts, axis=0), ".", color="black", ms=0.7, zorder=3.5)
    if with_borders:
        borders(ax, edge_tops, to_xy)


def name_regions(ax, tops, size, to_xy=xy):
    """each layout over REGION tiles named once, where most tiles around it are its own"""
    P = np.array([np.mean([to_xy(1 - s - u, s, u) for s, u in c], axis=0) for c in tiles(STEPS)])
    names = np.array([t for t, _ in tops])
    inner = np.array([all(1 - s - u > 0 and s > 0 and u > 0 for s, u in c) for c in tiles(STEPS)])
    for c in sorted(set(names), key=order):
        mine = P[(names == c) & inner]
        if len(mine) < REGION:
            continue
        near = np.linalg.norm(P[:, None, :] - mine[None, :, :], axis=2) < 2.5 / STEPS
        score = (near & (names[:, None] == c)).sum(axis=0) - (near & (names[:, None] != c)).sum(axis=0)
        x, y = mine[int(score.argmax())]
        ax.text(x, y, label(c), ha="center", va="center", fontsize=size, fontweight="bold", zorder=7,
                bbox=dict(fc="white", ec="none", pad=0.4, alpha=0.85))


def draw_with_frequency(wins, groups, f_wins, f_groups, out):
    """(a) the 49 by share of combined cost at the paper size; (b) small, top right, by share of frequency"""
    layout_set = OUTS[""]
    cost, freq = tops_of_tiles(wins, layout_set), tops_of_tiles(f_wins, layout_set)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig = plt.figure(figsize=(3.33, TRIANGLE_IN))              # the paper figure's canvas: the same triangle size
    ax = fig.add_axes([0.0, 0.0, TRIANGLE_W, 1.0])
    tiles_into(ax, cost)
    name_regions(ax, cost, PT_SMALL - 0.5)
    guides(ax)
    frame(ax, groups)
    total = sum(groups[g][0] for g in GROUPS)
    ax.plot(*xy(*(groups[g][1] for g in GROUPS)), "D", color="black", mfc="white", ms=3.8, mew=0.8, zorder=8)
    ax.plot(*xy(*(groups[g][0] / total for g in GROUPS)), "s", color="black", mfc="white", ms=3.5, mew=0.8, zorder=8)
    ax.text(-0.08, SQ3 + 0.09, "(a) share of\ncombined cost", ha="left", va="top", fontsize=PT_SMALL, linespacing=0.95)
    # (b): outline, the region line, LDBC's diamond, the corners' names only
    bx = fig.add_axes([TRIANGLE_W + 0.08, 0.50, 0.30, 0.50])
    tiles_into(bx, freq, dots=False, with_borders=True)
    bx.add_patch(Polygon([(0, 0), (1, 0), (0.5, SQ3)], closed=True, fill=False, lw=0.5, zorder=5))
    bx.plot(*xy(*(f_groups[g][1] for g in GROUPS)), "D", color="black", mfc="white", ms=2.6, mew=0.6, zorder=8)
    bx.text(0, -0.04, "IC", ha="center", va="top", fontsize=PT_SMALL - 1)
    bx.text(1, -0.04, "IS", ha="center", va="top", fontsize=PT_SMALL - 1)
    bx.text(0.56, SQ3 - 0.02, "IU", ha="left", va="center", fontsize=PT_SMALL - 1)
    bx.text(-0.05, SQ3 + 0.12, "(b) share of\nfrequency", ha="right", va="top", fontsize=PT_SMALL, linespacing=0.95)
    bx.set_xlim(-0.05, 1.05)
    bx.set_ylim(-0.16, SQ3 + 0.12)
    bx.set_aspect("equal")
    bx.axis("off")
    # one legend for both, under (b): every layout that wins a tile in either, by adjacency then properties
    shown = sorted({t for t, _ in cost} | {t for t, _ in freq}, key=lambda c: (order(c)[1], order(c)[0]))
    handles = [Patch(facecolor=cell_style(c)["color"], hatch=cell_style(c)["hatch"], edgecolor="white", lw=0)
               for c in shown]
    handles += [Line2D([], [], marker=".", ls="", color="black", ms=3),
                Line2D([], [], marker="D", ls="", color="black", mfc="white", ms=4.5, mew=1.0),
                Line2D([], [], marker="s", ls="", color="black", mfc="white", ms=4.0, mew=1.0)]
    labels = [label(c) for c in shown] + ["no clear winner", "LDBC's freq.", "equal share"]
    with plt.rc_context({"hatch.linewidth": 0.5}):
        fig.legend(handles, labels, loc="upper left", bbox_to_anchor=(TRIANGLE_W, 0.47), ncol=2, fontsize=PT_SMALL - 0.5,
                   frameon=False, handlelength=1.1, handleheight=0.8, handletextpad=0.25, labelspacing=0.15,
                   columnspacing=0.4, borderaxespad=0.0, borderpad=0.0)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.01)
    fig.savefig(out.with_suffix(".png"), dpi=300, bbox_inches="tight", pad_inches=0.01)
    plt.close(fig)
    print(f"with frequency: (a) {len({t for t, _ in cost})} layouts fill a tile, (b) {len({t for t, _ in freq})} -> {out}")


def update_share(writes, queries):
    """a mix of short reads and updates given by its share of writes -> the updates' share of its combined cost on
    Sp-Co: each short read gets (1 - writes) / 7 of the runs, each update writes / 8, each at its own cost"""
    cost = {g: [float(r["cost_sp_co"]) for r in queries if r["group"] == g] for g in ("short", "update")}
    if (len(cost["short"]), len(cost["update"])) != (7, 8):
        raise SystemExit(f"{NAME}-queries.csv: {len(cost['short'])} short reads and {len(cost['update'])} updates")
    s = (1 - writes) * sum(cost["short"]) / len(cost["short"])
    u = writes * sum(cost["update"]) / len(cost["update"])
    return u / (s + u)


def equal_cost_runs(queries):
    """each group's share of the runs when each query takes an equal share of Sp-Co's cost (runs = 1 / cost)"""
    runs = {g: sum(1 / float(r["cost_sp_co"]) for r in queries if r["group"] == g) for g in GROUPS}
    return {g: runs[g] / sum(runs.values()) for g in GROUPS}


def draw_right_angle(wins, groups, queries, out, others=True, freq=False):
    """the 49 on a right triangle: x = complex reads' share, y = updates' share, short reads the rest (100 - x - y: the
    diagonal edge has none; faint dashed lines at 20-80%); regions only, LDBC's frequencies as a diamond, each query an
    equal share of Sp-Co's cost as a square; others: also LinkBench's and TAO's marks; freq: a share is of the runs,
    not of the cost (wins and groups are then the frequency tables)"""
    other_mixes = OTHER_MIXES if others else ()
    at = (lambda w: w) if freq else (lambda w: update_share(w, queries))     # noqa: E731  a mix's updates share
    to_xy = lambda c, s, u: (c, u)                                          # noqa: E731
    tops = tops_of_tiles(wins, OUTS[""])
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig = plt.figure(figsize=(3.33, 1.85))
    ax = fig.add_axes([0.1, 0.17, 0.5, 0.8])
    tiles_into(ax, tops, dots=False, with_borders=freq, to_xy=to_xy)     # freq: two near-alike winners, a line between
    name_regions(ax, tops, PT_SMALL - 0.5, to_xy=to_xy)
    grid = dict(color="black", lw=0.35, alpha=0.3, zorder=3)
    for v in (0.2, 0.4, 0.6, 0.8):
        ax.plot([v, v], [0, 1 - v], **grid)                                 # complex = v
        ax.plot([0, 1 - v], [v, v], **grid)                                 # updates = v
        ax.plot([1 - v, 0], [0, 1 - v], **{**grid, "ls": (0, (2, 1.5))})     # short = v
    ax.plot([1, 0], [0, 1], color="black", lw=0.6, zorder=5)                # short = 0
    w = {g: groups[g][1] for g in GROUPS}
    ax.plot(w["complex"], w["update"], "D", color="black", mfc="white", ms=3.8, mew=0.8, zorder=8, clip_on=False)
    # each query an equal share of Sp-Co's cost: a group's share is its count of queries over all queries
    total = sum(groups[g][0] for g in GROUPS)
    eq = equal_cost_runs(queries) if freq else {g: groups[g][0] / total for g in GROUPS}
    ax.plot(eq["complex"], eq["update"], "s", color="black", mfc="white", ms=3.5, mew=0.8,
            zorder=8)
    for _, writes, marker in other_mixes:
        ax.plot(0, at(writes), marker, color="black", mfc="white", ms=3.8, mew=0.8,
                zorder=8, clip_on=False)
    ticks = [0, 0.2, 0.4, 0.6, 0.8, 1]
    ax.set_xticks(ticks, [f"{t * 100:.0f}" for t in ticks])
    ax.set_yticks(ticks, [f"{t * 100:.0f}" for t in ticks])
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_aspect("equal")
    runs = ", % of runs" if freq else " %"
    ax.set_xlabel("complex reads" + runs, labelpad=1)
    ax.set_ylabel("updates" + runs, labelpad=1)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ax.spines.values():
        side.set_linewidth(0.5)
    count = {}
    for t, _ in tops:
        count[t] = count.get(t, 0) + 1
    names = sorted(count, key=lambda c: (order(c)[1], order(c)[0]))       # by adjacency, then properties
    handles = [Patch(facecolor=cell_style(c)["color"], hatch=cell_style(c)["hatch"], edgecolor="white", lw=0)
               for c in names]
    handles.append(Line2D([], [], marker="D", ls="", color="black", mfc="white", ms=4.5, mew=1.0))
    handles.append(Line2D([], [], marker="s", ls="", color="black", mfc="white", ms=4.0, mew=1.0))
    labels = [f"{label(c)} ({count[c]})" for c in names] + ["LDBC's frequencies", "equal share per query"]
    for name, _, marker in other_mixes:
        handles.append(Line2D([], [], marker=marker, ls="", color="black", mfc="white", ms=4.5, mew=1.0))
        labels.append(name)
    with plt.rc_context({"hatch.linewidth": 0.5}):
        fig.legend(handles, labels, loc="center left", bbox_to_anchor=(0.62, 0.55), fontsize=PT_SMALL, frameon=False,
                   handlelength=1.4, handleheight=0.9, handletextpad=0.35, labelspacing=0.18, borderaxespad=0,
                   borderpad=0)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    if other_mixes:
        print("  " + ", ".join(f"{n}: updates {100 * at(w):.1f}%" for n, w, _ in other_mixes))
    print(f"right triangle: {len(count)} layouts fill a tile -> {out}")


def main():
    data, out_dir = args(__doc__)
    wins, groups = load(data)
    f_wins, f_groups = load(data, f"{NAME}-frequency")
    queries = read_table(data, f"{NAME}-queries")
    draw_right_angle(wins, groups, queries, out_dir / f"figures/evaluation/{NAME}-right.pdf", others=False)
    draw_with_frequency(wins, groups, f_wins, f_groups, out_dir / f"figures/evaluation/{NAME}-with-frequency.pdf")


if __name__ == "__main__":
    main()
