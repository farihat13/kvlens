#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/uniform-i01-chunk-sizes-and-front/fig_uniform_49_layouts_w16_by_part.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3, the 49 uniform layouts' wops and bytes split by part, one column, linear.

  figures/evaluation/uniform-49-layouts-w16-by-part.pdf

Two rows: (a) wops, (b) bytes, each over the row's cheapest layout (outlined; its label is 1). Bars grouped by the
adjacency setting (7 groups, named above (a)), the properties setting inside each group (the tick labels). Each bar is
stacked bottom up: adjacency lists (the 15 edge types' parts), vertex properties (the 6 vertex types'), the index; the
label on top is the bar's total over the cheapest (the cheapest's 1 bold and upright). Adjacency lists in the bar's
adjacency colour, vertex properties a lighter tint of it, the index dark grey. Dgraph's layout (Sp-Co) and NebulaGraph's (Co-Sp) are marked DG and NG (paper_style's engine marks):
the name after the tick label, the bar outlined in the engine's colour.
wops = G + 1.1 S + 0.01 N + P + D; bytes = read (misses included) + written. Deterministic counts: no error bars.

Reads only its table: uniform-i01-chunk-sizes-and-front-by-part.csv (one row per layout and part: layout, props,
adjacency, kind = adj, props or index, wops, bytes; the parts of a layout sum to its total in
uniform-i01-chunk-sizes-and-front.csv, checked here).

    python fig_uniform_49_layouts_w16_by_part.py --data <dir of the tables> --out <dir>
"""
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.legend_handler import HandlerTuple  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from common import args, read_table, use_paper_font  # noqa: E402
from paper_style import (COLUMN_IN, cell_id, engine_legend, colour_engine_legend, mark_engine_ticks, outline_engine_bar, swatch,  # noqa: E402
                         two_tones)

OUT = "figures/evaluation/uniform-49-layouts-w16-by-part.pdf"
PT, PT_SMALL = 7, 6
PT_VALUE = 6.5                                  # the value on a bar (neighbours alternate in height: place_values)
PT_LABEL = 6.5                                  # ticks, legend, group names
PT_XTICK = 5.5                                  # the 49 rotated x labels: larger ones touch
HEAD_PT = 9                                     # (a): the band for the group names, in points
SETTINGS = ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k", "chunked", "chunked_c256k")
SHORT = {"split": "Sp", "condensed": "Co", "chunked_c1k": "Ch1k", "chunked_c4k": "Ch4k", "chunked_c16k": "Ch16k",
         "chunked": "Ch64k", "chunked_c256k": "Ch256k"}
KINDS = ("adj", "props", "index")                                            # bottom up
INDEX_GREY = "0.25"
PART_LABEL = {"adj": "adjacency lists", "props": "vertex properties", "index": "index"}
GAP = 0.4                                                                    # between groups, in bar widths
WIDTH = 0.9                                                                  # a bar, of its slot


def part_color(adj, kind):
    """adjacency lists in the adjacency setting's colour, vertex properties a lighter tint of it, the index grey"""
    if kind == "index":
        return INDEX_GREY
    return two_tones(cell_id("split", adj))[0 if kind == "adj" else 1]


def costs(data):
    """{(props, adjacency): {kind: (wops, bytes)}} of the 49; each layout's parts checked against its total"""
    cost, layout_of = defaultdict(dict), {}
    for r in read_table(data, "uniform-i01-chunk-sizes-and-front-by-part"):
        cost[(r["props"], r["adjacency"])][r["kind"]] = (float(r["wops"]), float(r["bytes"]))
        layout_of[(r["props"], r["adjacency"])] = r["layout"]
    by_layout = {r["layout"]: r for r in read_table(data, "uniform-i01-chunk-sizes-and-front")}
    if len(cost) != 49 or any(set(d) != set(KINDS) for d in cost.values()):
        raise SystemExit(f"{len(cost)} layouts, want 49 with {KINDS} each")
    for k, d in cost.items():
        t = by_layout[layout_of[k]]
        for i, c in enumerate(("wops", "bytes")):
            if abs(sum(v[i] for v in d.values()) / float(t[c]) - 1) > 1e-9:
                raise SystemExit(f"{layout_of[k]}: its parts' {c} do not sum to its total")
    return cost


def points_to_data(fig, ax):
    """data units of the y axis per printed point"""
    return (ax.get_ylim()[1] - ax.get_ylim()[0]) / (ax.get_window_extent(fig.canvas.get_renderer()).height * 72 / fig.dpi)


def place_values(fig, ax, values, pad=1.0):
    """each bar's value just above it; a value that would overlap its neighbour's in the group (the one before or
    after) sits just above that one instead: neighbours alternate, so each value has two bars of width.
    values: [(x, top, text, text keywords, group, slot)]; (re)draws them, returns the highest value's top"""
    for t in getattr(ax, "_values", []):
        t.remove()
    ax._values, placed = [], {}
    renderer, per_pt = fig.canvas.get_renderer(), points_to_data(fig, ax)
    for parity in (0, 1):                                                    # even slots first, then the odd ones
        for x, top, text, kw, group, slot in values:
            if slot % 2 != parity:
                continue
            t = ax.text(x, 0, text, ha="center", va="bottom", **kw)
            h = t.get_window_extent(renderer).height * 72 / fig.dpi * per_pt
            y = top + pad * per_pt
            for n in (slot - 1, slot + 1):
                lo, hi = placed.get((group, n), (None, None))
                if lo is not None and y < hi and y + h > lo:
                    y = hi + pad * per_pt
            t.set_y(y)
            placed[(group, slot)] = (y, y + h)
            ax._values.append(t)
    return max(hi for _, hi in placed.values())


def main():
    data, out_dir = args(__doc__)
    cost = costs(data)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig, axes = plt.subplots(2, 1, figsize=(COLUMN_IN, 2.2), sharex=True)
    fig.subplots_adjust(left=0.075, right=0.998, top=0.985, bottom=0.15, hspace=0.1)
    xs = {(p, a): gi * (len(SETTINGS) + GAP) + pi for gi, a in enumerate(SETTINGS) for pi, p in enumerate(SETTINGS)}
    for i, (ax, name) in enumerate(zip(axes, ("(a) wops", "(b) bytes"))):
        total = {k: sum(v[i] for v in d.values()) for k, d in cost.items()}
        best = min(total, key=total.get)
        unit, top = total[best], max(total.values()) / total[best]
        values = []                                                          # (x, top, text, kw, group, slot)
        for k, d in cost.items():
            y = 0.0
            for kind in KINDS:
                v = d[kind][i] / unit
                ax.bar(xs[k], v, bottom=y, width=WIDTH, color=part_color(k[1], kind), edgecolor="white",
                       lw=0 if kind == "index" else 0.3, zorder=3)
                y += v
            label = "1" if k == best else (f"{y:.1f}" if y < 9.95 else f"{y:.0f}")
            is_best = k == best                                              # its "1" bold and upright
            values.append((xs[k], y, label, dict(rotation=0 if is_best else 90, fontsize=PT_VALUE + (0.5 if is_best else 0),
                                                 fontweight="bold" if is_best else "normal", color="0.1", zorder=5),
                           SETTINGS.index(k[1]), SETTINGS.index(k[0])))
            outline_engine_bar(ax, xs[k], y, cell_id(*k), WIDTH)
            if k == best:
                ax.bar(xs[k], y, width=WIDTH, fill=False, edgecolor="black", lw=0.8, zorder=4)
        head = HEAD_PT if i == 0 else 1.5                                    # (a): room for the group names
        ylim = top * 1.3
        for _ in range(20):                                                  # the axis top that holds every value
            ax.set_ylim(0, ylim)
            highest = place_values(fig, ax, values)
            need = highest + head * points_to_data(fig, ax)
            if abs(need - ylim) < 1e-3 * ylim:
                break
            ylim = need
        for gi, a in enumerate(SETTINGS):
            x0 = gi * (len(SETTINGS) + GAP) - 0.5
            if gi % 2:
                ax.axvspan(x0 - GAP / 2 + 0.05, x0 + len(SETTINGS) + GAP / 2 - 0.05, color="0.95", zorder=0, lw=0)
            if i == 0:                                                       # the group's adjacency setting, once
                ax.text(x0 + len(SETTINGS) / 2, 1, SHORT[a], ha="center", va="top", fontsize=PT_LABEL,
                        color=part_color(a, "adj"), fontweight="bold", transform=ax.get_xaxis_transform())
        ax.set_ylabel(name, fontsize=PT, labelpad=1)
        ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
        ax.tick_params(labelsize=PT_LABEL, pad=1, length=2, width=0.5)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        print(f"{name}: cheapest {SHORT[best[0]]}-{SHORT[best[1]]}, tallest {top:.1f}x")
    order = sorted(xs, key=xs.get)
    axes[-1].set_xticks([xs[k] for k in order], [SHORT[p] for p, _ in order], rotation=90, fontsize=PT_XTICK)
    axes[-1].tick_params(axis="x", length=0, pad=1)
    axes[-1].set_xlim(-0.8, max(xs.values()) + 0.6)
    mark_engine_ticks(fig, axes[-1], [cell_id(*k) for k in order])
    hs = [swatch(part_color(a, k) for a in SETTINGS) if k != "index" else Patch(fc=INDEX_GREY) for k in KINDS]
    axes[0].legend(hs, [PART_LABEL[k] for k in KINDS], handler_map={tuple: HandlerTuple(ndivide=None, pad=0)}, ncol=2,
                   loc="upper right", bbox_to_anchor=(0.998, 1 - HEAD_PT / 63), columnspacing=0.8, labelspacing=0.25, frameon=False, fontsize=PT_LABEL, handlelength=3.2,
                   handletextpad=0.3, borderaxespad=0, borderpad=0)
    eh, el = engine_legend()
    colour_engine_legend(axes[1].legend(eh, el, loc="upper left", bbox_to_anchor=(0.005, 1.0), frameon=False, fontsize=PT_LABEL,
                   handlelength=0.8, handletextpad=0.3, labelspacing=0.2, borderaxespad=0, borderpad=0))
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
