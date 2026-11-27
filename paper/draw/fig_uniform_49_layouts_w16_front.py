#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/front-by-part-49-layouts-i01/fig_uniform_49_layouts_w16_front.py); edit it there, then run evidence/release_draw.py
"""Paper figure: the Pareto front of W16's 49 uniform layouts at SF3 (one column), on log and on linear axes.

  figures/evaluation/uniform-49-layouts-w16-front-log.pdf
  figures/evaluation/uniform-49-layouts-w16-front-linear.pdf

Top: wops vs bytes of the 49, each over the fewest of the 49 (1 = the smallest: Co-Co's bytes, Ch256k-Ch256k's wops);
marker = the properties setting, colour = the adjacency setting; filled = on the 49's lower-left front (the solid
step, flat past its last point: the fewest wops within that many bytes), open = beaten on both axes by another layout;
a line names the layout with the fewest combined cost (wops + bytes / 4,096) of the 49.
Below: the front's adjacency and properties setting from each of its points to the next, in the settings' colours.
wops = G + 1.1 S + 0.01 N + P + D; bytes = read (misses included) + written. Deterministic counts: no error bars.

Reads only its table: front-by-part-49-layouts-i01-points.csv (the 49: kind uniform49).

    python fig_uniform_49_layouts_w16_front.py --data <dir of the tables> --out <dir>
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

import fig_front_by_part_49_layouts_w16 as F  # noqa: E402
from common import args, read_table, use_paper_font  # noqa: E402
from paper_style import COLUMN_IN, cell_parts, cell_style, cell_id  # noqa: E402

OUT = "figures/evaluation/uniform-49-layouts-w16-front-{}.pdf"     # log, linear


def setting(layout, kb):
    return layout if kb is None else ("chunked" if kb == 64 else f"chunked_c{kb}k")


def draw(data, out_dir, linear):
    """one version: log axes, or linear from 0"""
    pts = [p for p in read_table(data, "front-by-part-49-layouts-i01-points") if p["kind"] == "uniform49"]
    rw, rb = min(float(p["wops"]) for p in pts), min(float(p["bytes"]) for p in pts)   # 1 = the smallest of the 49
    u49 = [(float(p["bytes"]) / rb, float(p["wops"]) / rw, p["name"]) for p in pts]
    assert len(u49) == 49
    fr = F.front(u49)
    on = {c for _, _, c in fr}
    # the fewest combined cost (wops + bytes / 4,096) of the 49: named by a line
    best = min(pts, key=lambda p: float(p["wops"]) + float(p["bytes"]) / 4096)["name"]
    star = next((b, w) for b, w, c in u49 if c == best)
    x0, x1 = min(b for b, _, _ in u49) / 1.1, max(b for b, _, _ in u49) * 1.15
    y0, y1 = min(w for _, w, _ in u49) / 1.3, max(w for _, w, _ in u49) * 1.3
    if linear:
        x0, x1, y0, y1 = 0, max(b for b, _, _ in u49) * 1.04, 0, max(w for _, w, _ in u49) * 1.06

    use_paper_font()
    plt.rcParams.update({"font.size": F.PT})
    fig = plt.figure(figsize=(COLUMN_IN, 2.75))
    gs = fig.add_gridspec(2, 1, height_ratios=(1, 0.16), hspace=0.06, left=0.15, right=0.985, top=0.875, bottom=0.12)
    ax, bx = fig.add_subplot(gs[0]), None
    bx = fig.add_subplot(gs[1], sharex=ax)
    for b, w, c in u49:
        st = cell_style(c)
        ax.scatter([b], [w], s=14, marker=st["marker"], facecolor=st["color"] if c in on else "none",
                   edgecolor="black" if c in on else st["color"], linewidth=0.4 if c in on else 0.7,
                   zorder=4 if c in on else 2)
    F.step(ax, fr, x1, color=F.UNIFORM7, lw=1.0, ls="-", zorder=3)
    # the fewest combined cost: a line from its point to its name, in the empty space (below the front on log axes, above it on linear)
    ax.annotate("fewest combined cost", star, xytext=(30, 45) if linear else (-14, -34), textcoords="offset points",
                fontsize=F.PT_SMALL - 0.5, ha="left" if linear else "center", va="bottom" if linear else "top", color="black", zorder=8,
                arrowprops=dict(arrowstyle="-", color="black", lw=0.5, shrinkA=1, shrinkB=3))
    short = lambda c: "-".join(F.SHORT[setting(l, kb)] for l, kb in cell_parts(c))   # noqa: E731
    for a in (ax, bx):
        a.set_xscale("linear" if linear else "log")
        a.set_xlim(x0, x1)
        a.xaxis.set_major_locator(FixedLocator((1, 5, 10, 15, 20) if linear else (1, 2, 5, 10, 20)))
        a.xaxis.set_major_formatter(FuncFormatter(F.times))
        a.xaxis.set_minor_locator(NullLocator())
        a.tick_params(labelsize=F.PT_SMALL, pad=1, length=2, width=0.5)
        for s in a.spines.values():
            s.set_linewidth(0.5)
    ax.set_yscale("linear" if linear else "log")
    ax.set_ylim(y0, y1)
    ax.yaxis.set_major_locator(FixedLocator((1, 10, 20, 30) if linear else (1, 2, 5, 10, 20, 50)))
    ax.yaxis.set_major_formatter(FuncFormatter(F.times))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_ylabel(r"x the fewest $\mathit{wops}$" + ("" if linear else " (log)"), labelpad=1, fontsize=F.PT)
    ax.tick_params(labelbottom=False)
    ax.grid(alpha=0.25, lw=0.4, zorder=0)

    # below: the front's adjacency and properties setting from each point to the next
    nxt = [fr[i + 1][0] if i + 1 < len(fr) else x1 for i in range(len(fr))]
    for j, kind in enumerate(("props", "adj")):
        for (b, _, c), e in zip(fr, nxt):
            (p, pk), (a_, ak) = cell_parts(c)
            s = setting(a_, ak) if kind == "adj" else setting(p, pk)
            bx.axvspan(b, e, ymin=j / 2 + 0.04, ymax=(j + 1) / 2 - 0.04, color=F.HUE[s], lw=0)
    bx.set_yticks([0.25, 0.75], ["properties", "adjacency"], fontsize=F.PT_SMALL - 0.5)
    bx.set_ylim(0, 1)
    bx.tick_params(axis="y", length=0)
    bx.set_xlabel("x the fewest bytes" + ("" if linear else " (log)"), labelpad=1, fontsize=F.PT)

    kw = dict(frameon=False, fontsize=F.PT_SMALL - 0.5, handlelength=0.9, columnspacing=0.4, handletextpad=0.3,
              borderaxespad=0, borderpad=0)
    colours = [Patch(fc=F.HUE[s], label=F.SHORT[s]) for s in F.SETTINGS]
    first = fig.legend(handles=colours, loc="upper center", ncol=8, bbox_to_anchor=(0.55, 0.995), **kw)
    marks = [Line2D([], [], ls="", marker="", label="props:")]
    marks += [Line2D([], [], ls="", marker=cell_style(cell_id(s, "split"))["marker"], ms=3.4, mfc="0.45", mec="black",
                     mew=0.4, label=F.SHORT[s]) for s in F.SETTINGS]
    marks += [Line2D([], [], ls="", marker="o", ms=3.4, mfc="none", mec="0.45", mew=0.7, label="open: beaten"),
              
              Line2D([], [], color=F.UNIFORM7, lw=1.0, ls="-", label="front")]
    fig.legend(handles=marks, loc="upper center", ncol=10, bbox_to_anchor=(0.55, 0.945), **{**kw, "columnspacing": 0.25})
    fig.add_artist(first)
    out = out_dir / OUT.format("linear" if linear else "log")
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"{'linear' if linear else 'log'}: the front of the 49: {len(fr)} layouts, {short(fr[0][2])} to {short(fr[-1][2])}")
    print(f"  named (the fewest combined cost): {short(best)}, {star[0]:.2f}x bytes, {star[1]:.2f}x wops, {'on' if best in on else 'off'} the front")
    print(f"wrote {out}")


def main():
    data, out_dir = args(__doc__)
    draw(data, out_dir, linear=False)
    draw(data, out_dir, linear=True)


if __name__ == "__main__":
    main()
