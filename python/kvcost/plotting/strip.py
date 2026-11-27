"""Strip figures of the uniform-layout cells: one strip of points per column, each point a cell (colour: its adjacency
layout; marker: its properties layout), placed by a jitter that is the same on every run, and a factored legend.

    from kvcost.plotting.strip import strip_x, strip_point, separators, layout_legend_handles
"""
from matplotlib.lines import Line2D

from kvcost.plotting.style import cell_order, cell_scatter_kw, cell_short


def strip_x(centre, j, n, slot, frac=1.0):
    """the x of the j-th of n points in a strip centred at `centre`, spread over slot * frac (no RNG: sort the points)"""
    return centre + (j / (n - 1) - 0.5) * slot * frac


def strip_point(ax, x, y, cell):
    """one cell's point: its colour and marker, a thinner outline than the library's (strips hold many small points)"""
    ax.scatter(x, y, s=13, zorder=3, **{**cell_scatter_kw(cell), "linewidths": 0.25})


def separators(ax, xs, lw, color):
    """a vertical line at each x, under the points"""
    for x in xs:
        ax.axvline(x, lw=lw, color=color, zorder=1)


def layout_legend_handles(cells):
    """the cells' legend, factored: a row of adjacency colours, a row of properties markers (from the cells drawn);
    returns (handles, ncol) for ax.legend"""
    adj, props = {}, {}
    for cell in cell_order(cells):
        p, a = cell_short(cell).split("-")
        kw = cell_scatter_kw(cell)
        adj.setdefault(a, Line2D([], [], ls="", marker="s", ms=8, color=kw["color"], label=a))
        props.setdefault(p, Line2D([], [], ls="", marker=kw["marker"], ms=7, mfc="0.8", mec="black", mew=0.6, label=p))
    blank = Line2D([], [], ls="", marker="")
    rows = [[Line2D([], [], ls="", marker="", label="adjacency (colour):")] + list(adj.values()),
            [Line2D([], [], ls="", marker="", label="properties (marker):")] + list(props.values())]
    width = max(len(r) for r in rows)
    rows = [r + [blank] * (width - len(r)) for r in rows]
    # a legend fills column by column: interleave the two rows
    return [h for pair in zip(*rows) for h in pair], width
