#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/uniform-i01-chunk-sizes-and-front/fig_uniform_w16_chunk_sizes_and_front.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3, the 49 uniform layouts with Chunked at 1-256 KB and the fronts they give (full width).

  figures/evaluation/uniform-w16-chunk-sizes-and-front.pdf
  figures/evaluation/uniform-w16-chunk-sizes-and-front-boxes.pdf   (the same, each adjacency setting's range boxed)
  figures/evaluation/uniform-w16-chunk-sizes-1k-64k-and-front.pdf  (the same with Chunked at 1-64 KB: 36 layouts)
  figures/evaluation/uniform-49-layouts-w16.pdf            (the left part's wops and bytes rows, one column, each row
                                                            over its cheapest layout)

Left: uniform-w16-chunk-sizes' three panels (wops, bytes, combined = wops + bytes / 4,096), each bar a layout's cost /
Sp-Co's, log; colour = the adjacency setting, hatch = the properties setting; the best has a black outline. Right:
wops vs bytes, both / Sp-Co's, log-log: the front of the mixes per type and the best uniform layout over Sp/Co/Ch64k
(pareto-and-fixed-i01's two lines), and the same two over Sp/Co/Ch1k-256k; the 49 layouts as points (marker = properties,
colour = adjacency; smaller = beaten by another uniform layout in both wops and bytes).
The 1-256 KB mix line is the exact front over those options: at each of 128 bytes limits (104 until 2026-11-07), the mix per type with the
fewest wops within it, solved over Sp, Co and Ch1k-256k per part (chunk-size-front's "any size" line); the 3-option
front's mixes and the 49 layouts are mixes over the same options, so the line takes them in too. In the 1-64 KB variant,
past the last limit whose mix uses no 256 KB part the line is dotted: there it is the lower front of the mixes drawn.
wops = G + 1.1 S + 0.01 N + P + D; bytes = read (misses included) + written. Deterministic counts: no error bars.
All three sources are the 2026-11-03 SF3 grid; the script checks Sp-Co is the same in the 49 and the nine.

Reads only its two tables (the artifact's paper/derive/uniform_i01_chunk_sizes_and_front.py makes them from the
snapshot sf3/mix-benefit-20261103: its chunk-size-uniform, w16-front and chunk-size-front):
uniform-i01-chunk-sizes-and-front.csv (the uniform layouts) and uniform-i01-chunk-sizes-and-front-front.csv (the front
points, by file).

    python fig_uniform_w16_chunk_sizes_and_front.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, LogLocator, NullFormatter, NullLocator

import fig_uniform_w16_chunk_sizes as U
from common import args, read_table, use_paper_font
from paper_style import PROPS_MARKER, cell_bar_kw, cell_id, cell_scatter_kw

OUT = "figures/evaluation/uniform-w16-chunk-sizes-and-front.pdf"
OUT_BOXES = "figures/evaluation/uniform-w16-chunk-sizes-and-front-boxes.pdf"   # the same, each adjacency's range boxed
OUT_64K = "figures/evaluation/uniform-w16-chunk-sizes-1k-64k-and-front.pdf"     # Chunked at 1-64 KB only
OUT_COLUMN = "figures/evaluation/uniform-49-layouts-w16.pdf"            # the wops and bytes rows alone, one column
PT = 9                      # axis labels, ticks, headers, legend
PT_BAR = 7                  # the labels on the bars, Sp-Co's cost and the properties names under the bars
MIX, UNIFORM = "#d81b60", "0.35"            # pareto-and-fixed-i01's two lines
MIX7, UNIFORM7 = "#1565c0", "#1e88e5"       # the 7-option lines: blue, a lighter blue
SIZES = ("split", "condensed", 1, 4, 16, 64, 256)   # the PROPS_MARKER key of each of U.SETTINGS


def front(pts):
    """the lower-left front of (bytes, wops) points, by bytes"""
    out, best = [], float("inf")
    for b, w, tag in sorted(pts):
        if w < best:
            out.append((b, w, tag))
            best = w
    return out


def opts(text):
    """the settings a mix's assignment uses, named as U.SETTINGS (64 KB is "chunked")"""
    return {{"chunked_c64k": "chunked"}.get(s, s) for s in (p.split("=", 1)[1].strip() for p in text.split(";") if "=" in p)}


def step(ax, pts, x1, **kw):
    ax.step([p[0] for p in pts] + [x1], [p[1] for p in pts] + [pts[-1][1]], where="post", **kw)


def cells_of(settings):
    return [cell_id(p, a) for a, _ in settings for p, _ in settings]   # adjacency outer, properties inner


def bars(axes, cells, settings, base="Sp-Co"):
    """the left part: uniform-w16-chunk-sizes' three panels, narrower; each row over Sp-Co's cost, or with
    base="best" over the row's cheapest layout (its bar is 1)"""
    n = len(settings)
    names = cells_of(settings)
    xs = [g * (n + U.GAP) + a for g in range(n) for a in range(n)]
    for ax, (name, cost, human) in zip(axes, U.MODELS):
        ref = cost(*cells[U.REF])
        unit = ref if base == "Sp-Co" else min(cost(*cells[c]) for c in names)
        ratio = [cost(*cells[c]) / unit for c in names]
        best = min(ratio)
        for x, v, c in zip(xs, ratio, names):
            ax.bar(x, v, width=0.85, zorder=3, **cell_bar_kw(c))
            if v <= best * 1.01:
                ax.bar(x, v, width=0.85, zorder=4, fill=False, edgecolor="black", linewidth=0.7)
        if base == "Sp-Co":
            ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
        ib, ir = ratio.index(best), names.index(U.REF)
        done = []
        for i in [ib, ir] + [i for i in range(len(xs)) if i % n in (0, n - 1)]:   # the first and last bar of each group
            if i in done or any(abs(i - j) <= 2 and i // n == j // n for j in done):
                continue
            done.append(i)
            # a group's edge labels lean inward, so they do not run into the next group's
            x, ha = {0: (xs[i] - 0.45, "left"), n - 1: (xs[i] + 0.45, "right")}.get(i % n, (xs[i], "center"))
            ax.text(x, ratio[i] * 1.1, "1" if i == (ir if base == "Sp-Co" else ib) else U.ratio_label(ratio[i]), ha=ha, va="bottom",
                    fontsize=PT_BAR, fontweight="bold" if i == ib else "normal")
        ax.set_yscale("log")
        ax.set_ylim(min(ratio) / 1.3, max(ratio) * 2.1)
        lo, hi = ax.get_ylim()
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0,) if hi / lo > 30 else (1.0, 2.0, 5.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_ylabel(name)
        ax.yaxis.set_label_coords(-0.098, 0.5)
        ax.text(-0.066, 0.5, f"Sp-Co = {human(ref)}", transform=ax.transAxes, rotation=90, ha="center", va="center",
                fontsize=PT_BAR)
        for g in range(1, n):
            ax.axvline(g * (n + U.GAP) - (1 + U.GAP) / 2, color="0.6", lw=0.4)
        ax.set_xlim(-0.6, xs[-1] + 0.6)
        ax.tick_params(labelsize=PT, pad=1, length=2, width=0.5)
        ax.tick_params(which="minor", length=1, width=0.4)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
    for g, (_, s) in enumerate(settings):
        axes[0].text(g * (n + U.GAP) + (n - 1) / 2, 1.01, f"adj. {s}", transform=axes[0].get_xaxis_transform(),
                     ha="center", va="bottom", fontsize=PT - 1)   # 9 pt: neighbouring headers touch
    axes[-1].set_xticks(xs, [s for _ in settings for _, s in settings], rotation=90)
    axes[-1].tick_params(axis="x", labelsize=PT_BAR, pad=1)
    axes[-1].set_xlabel("properties setting", labelpad=0, fontsize=PT)


def draw(data, out, boxes, settings=U.SETTINGS):
    """the figure; boxes: shade each adjacency setting's range over the properties settings; settings: a prefix of
    U.SETTINGS (Sp, Co and the chunk sizes up to the largest drawn)"""
    cells = U.load(data)                                           # {layout: (wops, bytes)}
    front_rows = sorted(read_table(data, "uniform-i01-chunk-sizes-and-front-front"), key=lambda r: (r["file"], r["line"]))
    of = lambda name: [r for r in front_rows if r["file"] == name]                          # noqa: E731
    allowed = {s for s, _ in settings}
    names = cells_of(settings)
    span = f"Ch1k-{settings[-1][1].removeprefix('Ch')}"            # "Ch1k-256k"
    rw, rb = cells[U.REF]
    uni = {u["name"]: {"ops": float(u["ops"]), "bytes": float(u["bytes"])} for u in of("uniform_layouts.json")}
    if abs(uni["Sp-Co"]["ops"] / rw - 1) > 1e-9 or abs(uni["Sp-Co"]["bytes"] / rb - 1) > 1e-9:
        raise SystemExit("Sp-Co differs between the 49 and the nine: the two scales are not one")
    mix3 = [(float(m["bytes"]) / rb, float(m["ops"]) / rw, "mix") for m in of("complete_front.json")]
    nine = [(u["bytes"] / rb, u["ops"] / rw, n) for n, u in uni.items()]
    u49 = [(b / rb, w / rw, c) for c, (w, b) in cells.items() if c in names]   # 49 (or 36) uniform layouts
    rows = [r for r in of("equal_bytes_front.csv") if r["assignment"]]
    if len(rows) != 128:
        raise SystemExit(f"equal_bytes_front.csv: {len(rows)} solved limits with an assignment, not 128")
    solved = [(float(r["bytes"]) / rb, float(r["wops"]) / rw, "mix") for r in rows if opts(r["assignment"]) <= allowed]
    full8 = front([(float(r["bytes"]) / rb, float(r["wops"]) / rw, "mix" if opts(r["assignment"]) <= allowed else "out")
                   for r in rows])
    last_exact = max(p[0] for p in full8 if p[2] != "out")       # beyond: the front needed a part not drawn (256 KB)
    # the 3-option mixes and the uniform layouts are mixes over the same options: the line takes them in
    f3, fu3, f7, fu7 = front(mix3 + nine), front(nine), front(solved + u49 + mix3 + nine), front(u49)
    print(f"mixes: 3-option {len(mix3)} (complete front), {len(settings)}-option {len(solved)} solved limits within "
          f"{span} of {len(rows)}; uniform {len(u49)}")
    if len(u49) != len(names):
        raise SystemExit(f"{len(u49)} uniform layouts, not {len(names)}")
    for name, f in (("mix 3", f3), ("uniform 9", fu3), ("mix wide", f7), ("uniform wide", fu7)):
        print(f"{name}: {len(f)} points, fewest wops {f[-1][1]:.4f} at {f[-1][0]:.2f}x bytes; at <=1x bytes "
              f"{min(w for b, w, _ in f if b <= 1.000001):.4f}")
    print(f"{span} mix line exact up to {last_exact:.2f}x Sp-Co bytes")

    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig = plt.figure(figsize=(7.0, 3.1))
    gs = fig.add_gridspec(1, 2, width_ratios=(2.25, 1), wspace=0.175, left=0.085, right=0.995, top=0.94, bottom=0.175)
    left = gs[0].subgridspec(3, 1, hspace=0.07)
    axes = [fig.add_subplot(left[0])]
    axes += [fig.add_subplot(left[i], sharex=axes[0]) for i in (1, 2)]
    for a in axes[:-1]:
        a.tick_params(labelbottom=False)
    bars(axes, cells, settings)

    col = gs[1].get_position(fig)                                  # the right column runs down to the figure's foot:
    right = fig.add_gridspec(2, 1, left=col.x0, right=col.x1, top=col.y1, bottom=0.015,   # its keys sit beside
                             height_ratios=(1, 0.42), hspace=0.22)                        # the bars' names
    ax = fig.add_subplot(right[0])                                 # about square, as pareto-and-fixed-i01 (a)
    allx = [p[0] for p in f3 + f7 + nine + u49]
    x0, x1 = min(allx) / 1.08, max(allx) * 1.08
    on = {p[2] for p in fu7}
    if boxes:                    # each adjacency setting: the box its 7 layouts span (a range, not reachable layouts)
        by = {c: (b, w) for b, w, c in u49}
        for a, _ in settings:
            pts = [by[cell_id(p, a)] for p, _ in settings]
            bl, bh = min(b for b, _ in pts), max(b for b, _ in pts)
            wl, wh = min(w for _, w in pts), max(w for _, w in pts)
            col = cell_scatter_kw(cell_id("split", a))["color"]
            ax.fill_between([bl, bh], wl / 1.04, wh * 1.04, color=col, alpha=0.22, lw=0, zorder=1)
            ax.fill_between([bl, bh], wl / 1.04, wh * 1.04, facecolor="none", edgecolor=col, lw=0.5, zorder=1)
    for b, w, c in u49:          # marker = the properties setting, colour = the adjacency; smaller = off the uniform front
        ax.scatter([b], [w], s=16 if c in on else 10, alpha=1, zorder=4 if c in on else 2,
                   **{**cell_scatter_kw(c), "linewidths": 0.3})
    halo = [pe.Stroke(linewidth=2.0, foreground="white"), pe.Normal()]
    step(ax, fu3, x1, color=UNIFORM, lw=0.8, ls="--", zorder=5)
    step(ax, f3, x1, color=MIX, lw=1.1, zorder=6, path_effects=halo)
    step(ax, fu7, x1, color=UNIFORM7, lw=1.0, ls="--", zorder=5)
    exact = [p for p in f7 if p[0] <= last_exact * (1 + 1e-9)]
    rest = [exact[-1]] + [p for p in f7 if p[0] > last_exact * (1 + 1e-9)]
    ax.step([p[0] for p in exact] + [rest[1][0] if len(rest) > 1 else x1], [p[1] for p in exact] + [exact[-1][1]],
            where="post", color=MIX7, lw=1.1, zorder=6, path_effects=halo)
    if len(rest) > 1:
        step(ax, rest[1:], x1, color=MIX7, lw=1.1, ls=(0, (1, 1)), zorder=6)
        ax.plot([rest[1][0], rest[1][0]], [exact[-1][1], rest[1][1]], color=MIX7, lw=1.1, ls=(0, (1, 1)), zorder=6)
    ax.axvline(1, color="0.45", lw=0.5, ls=":", zorder=1)
    ax.axhline(1, color="0.45", lw=0.5, ls=":", zorder=1)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(x0, x1)
    ys = [p[1] for p in f3 + f7 + nine + u49]
    ax.set_ylim(min(ys) / 1.4, max(ys) * 1.4)
    for axis, ticks in ((ax.xaxis, (1, 2, 5, 10, 20, 50)), (ax.yaxis, (0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5))):
        axis.set_major_locator(FixedLocator(ticks))
        axis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        axis.set_minor_locator(NullLocator())
    ax.set_xlabel("bytes / Sp-Co's", labelpad=1)
    ax.set_ylabel(r"$\mathit{wops}$ / Sp-Co's", labelpad=1)
    ax.tick_params(labelsize=PT, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)
    ax.grid(alpha=0.25, lw=0.4, zorder=0)
    key = fig.add_subplot(right[1])
    key.axis("off")
    ax.legend(handles=[Line2D([], [], color=MIX, lw=1.1, label="mix, Ch64k"),
                       Line2D([], [], color=UNIFORM, lw=0.8, ls="--", label="uniform, 9"),
                       Line2D([], [], color=MIX7, lw=1.1, label=f"mix, {span}"),
                       Line2D([], [], color=UNIFORM7, lw=1.0, ls="--", label=f"uniform, {len(names)}")],
              loc="lower left", fontsize=PT_BAR, frameon=False, handlelength=1.6, handletextpad=0.4,
              labelspacing=0.25, borderaxespad=0.3)
    def rows(handles):
        """Sp and Co on the first line, the chunk sizes on the second (a legend fills column by column)"""
        blank = Line2D([], [], ls="", label=" ")       # the chunk sizes say only the size: the title says Ch
        top = handles[:2] + [blank] * (len(handles) - 4)
        return [h for pair in zip(top, handles[2:]) for h in pair]

    kw = dict(title_fontsize=PT_BAR, ncol=len(settings) - 2, fontsize=PT_BAR, frameon=False, handletextpad=0.25, columnspacing=0.45,
              labelspacing=0.25, borderaxespad=0)
    props = key.legend(handles=rows([Line2D([], [], ls="", marker=PROPS_MARKER[k], color="0.35", mec="black", mew=0.3,
                                            ms=4.4, label=name.removeprefix("Ch")) for (_, name), k in
                                     zip(settings, SIZES)]),
                       title=f"marker: properties (Ch at {span[2:]})", loc="upper center", bbox_to_anchor=(0.5, 1.02), handlelength=0.8, **kw)
    key.add_artist(props)
    key.legend(handles=rows([Patch(color=cell_scatter_kw(cell_id("split", a))["color"], label=name.removeprefix("Ch"))
                             for a, name in settings]),
               title=f"colour: adjacency (Ch at {span[2:]})", loc="lower center", bbox_to_anchor=(0.5, -0.06), handlelength=0.9,
               handleheight=0.7, **kw)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


def draw_bars_column(data, out):
    """the left part's wops and bytes panels alone (no combined row, no Sp-Co totals), at one column; the figure's front
    is front-by-part-49-layouts-i01"""
    global PT, PT_BAR
    keep = PT, PT_BAR
    PT, PT_BAR = 8, 6.5                                            # bars() reads these
    cells = U.load(data)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = 1.85                                      # the bands above (0.31 in) and below (0.43 in) the rows in inches
    fig = plt.figure(figsize=(3.33, H))
    gs = fig.add_gridspec(2, 1, hspace=0.08, left=0.085, right=0.995, top=1 - 0.31 / H, bottom=0.43 / H)
    axes = [fig.add_subplot(gs[0])]
    axes.append(fig.add_subplot(gs[1], sharex=axes[0]))
    axes[0].tick_params(labelbottom=False)
    bars(axes, cells, U.SETTINGS, base="best")                     # two axes: the wops and bytes rows of U.MODELS
    for a in axes:                                                 # bars() places these for the full-width figure
        for t in [t for t in a.texts if t.get_text().startswith("Sp-Co =")]:
            t.remove()
        a.yaxis.set_label_coords(-0.052, 0.5)
        a.tick_params(labelsize=7, pad=1)
    for t in axes[0].texts:                                        # one "adjacency setting" title over bare headers
        if t.get_text().startswith("adj. "):
            t.set_text(t.get_text().removeprefix("adj. "))
            t.set_fontsize(7)
            t.set_fontweight("bold")
    axes[0].text(0.5, 1.15, "adjacency setting", transform=axes[0].transAxes, ha="center", va="bottom", fontsize=PT)
    axes[-1].tick_params(axis="x", labelsize=5)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    PT, PT_BAR = keep
    print(f"wrote {out}")


def main():
    data, out_dir = args(__doc__)
    draw(data, out_dir / OUT, boxes=False)
    draw_bars_column(data, out_dir / OUT_COLUMN)
    draw(data, out_dir / OUT_BOXES, boxes=True)
    draw(data, out_dir / OUT_64K, boxes=False, settings=U.SETTINGS[:6])           # Sp, Co, Ch1k-64k


if __name__ == "__main__":
    main()
