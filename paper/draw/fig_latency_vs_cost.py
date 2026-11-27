#!/usr/bin/env python3
# released from gs-paper at 62acd0b (evidence/latency-steady-ldbc/fig_latency_vs_cost.py); edit it there, then run evidence/release_draw.py
"""Paper figure: steady latency against combined cost of the 12 timed layouts, one panel per workload (one column).

  figures/evaluation/latency-vs-cost.pdf

(a) Interactive (LDBC Interactive's 19 reads and IU1-8 at W16's frequencies), (b) LDBC BI, (c) JOB; BI and JOB weight each
query 1 / its combined cost on Sp-Co. x: combined cost (wops + bytes / 4,096) over the panel's cheapest layout; y: the
steady wall (per query the mean over its params of the mean over measured passes 1-9) over its fastest; both log, one
decade the same length on both axes, so the dashed y = x is the diagonal; x to 10x, y to 4x (refused if a point lies
past either). Colour = the adjacency setting, marker = the properties setting. The mixes are not drawn.

Reads only its tables: latency-steady-ldbc.csv (cost_ratio, wall_ratio; kind = layout) and latency-steady-job.csv
(share_cost_ratio, share_wall_ratio; its controls left out).

    python fig_latency_vs_cost.py --data <dir of the tables> --out <dir>
"""
import argparse
import math
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedFormatter, FixedLocator, NullFormatter

from common import args, use_paper_font
from paper_style import ADJ_COLOR, CHUNK_SHADE, COLUMN_IN, PROPS_MARKER, cell_style

OUT = "figures/evaluation/latency-vs-cost.pdf"
PT, PT_SMALL = 7, 6
MIXES = (("measured", "mix", dict(marker="*", s=40, facecolor="black", edgecolor="black")),
         ("predicted", "predicted mix", dict(marker="*", s=40, facecolor="none", edgecolor="black")),
         ("bill", "bill mix", dict(marker="*", s=40, facecolor="none", edgecolor="0.55")))


def ranks(v):
    """average ranks (ties share the mean of their places)"""
    order = sorted(range(len(v)), key=v.__getitem__)
    r = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    """Spearman's rho: Pearson's r of the average ranks"""
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    return cov / (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5


def with_blocks(pts, path):
    """the same points with y = the store's block reads over Sp-Co's"""
    y = {(r["workload"], r["store"]): float(r["block_ratio"]) for r in csv.DictReader(open(path))}
    w = {"Interactive": "interactive", "BI": "bi", "JOB": "job"}
    return {k: [(kind, s, x, y[(w[k], s)]) for kind, s, x, _ in v] for k, v in pts.items()}


def load(data):
    """{workload: [(kind, cell or mix name, cost ratio, wall ratio)]}"""
    out = {"Interactive": [], "BI": [], "JOB": []}
    for r in csv.DictReader(open(data / "latency-steady-ldbc.csv")):
        w = {"interactive": "Interactive", "bi": "BI"}[r["workload"]]
        if r["kind"] == "layout":
            out[w].append(("layout", r["store"], float(r["cost_ratio"]), float(r["wall_ratio"])))
        else:
            for key, _, _ in MIXES:
                if r["store"].endswith(f"_{key}_mix_any_size"):
                    out[w].append((key, r["store"], float(r["cost_ratio"]), float(r["wall_ratio"])))
    job = data / "latency-steady-job.csv"            # a run that did not time JOB: its panel is drawn empty
    for r in (csv.DictReader(open(job)) if job.is_file() else []):
        if "control" in r["timed"]:
            continue
        kind = "layout" if r["store"].startswith("props_") else next(k for k, _, _ in MIXES if f"_{k}_mix" in r["store"])
        out["JOB"].append((kind, r["store"], float(r["share_cost_ratio"]), float(r["share_wall_ratio"])))
    return out


def to_best(pts, blocks):
    """x over the cheapest store drawn; latency (not block reads) over the fastest"""
    bx = min(p[2] for p in pts)
    by = 1.0 if blocks else min(p[3] for p in pts)
    cheapest = min(pts, key=lambda p: p[2])[1]
    fastest = min(pts, key=lambda p: p[3])[1]
    return [(k, s, x / bx, y / by) for k, s, x, y in pts], cheapest, fastest


NO_RHO = False


def panel(ax, pts, title, blocks, best=False):
    lo, hi = (0.85, 12) if best else (0.17, 4.5)
    ylo, yhi, zero = (0.0035, 14, 0.005) if blocks else (lo, hi, None)
    hi = max(hi, max(p[2] for p in pts) * 1.3)                # the x axis takes every point in
    lo = min(lo, min(p[2] for p in pts) / 1.3)
    pts = [(k, s, x, y if y > 0 else zero) for k, s, x, y in pts]
    lay = [p for p in pts if p[0] == "layout"]
    for _, cell, x, y in lay:
        s = cell_style(cell)
        ax.scatter(x, y, s=12, facecolor="none", edgecolor=s["color"], marker=s["marker"], linewidths=0.7, zorder=3)
    for key, _, kw in MIXES:
        for _, _, x, y in (p for p in pts if p[0] == key):
            ax.scatter(x, y, linewidths=0.6, zorder=4, **kw)
    if not (best and blocks):                         # y = x only when both axes share a baseline kind
        ax.plot([0.01, 1000], [0.01, 1000], ls="--", lw=0.6, color="grey", zorder=2)
    else:                                             # else slope 1 through Sp-Co: block reads in proportion to cost
        x0 = next(x for _, s, x, _ in pts if s == "props_split_adj_condensed")
        ax.plot([lo, hi], [lo / x0, hi / x0], ls="--", lw=0.6, color="grey", zorder=2)
    if blocks:
        ax.axhspan(ylo, 0.0065, color="0.93", lw=0, zorder=0)       # the "0" row: stores that read no block
    if not best:
        ax.axhline(1, lw=0.4, color="0.75", zorder=1)
        ax.axvline(1, lw=0.4, color="0.75", zorder=1)
    ax.set_xscale("log"); ax.set_yscale("log")
    ticks = [t for t in ([1, 2, 5, 10, 20, 50, 100] if best else [0.05, 0.1, 0.2, 0.5, 1, 2, 4, 10, 20]) if lo <= t <= hi]
    yticks = [zero, 0.01, 0.1, 1, 10] if blocks else [t for t in ticks if ylo <= t <= yhi]
    ylabels = ["0", "0.01x", "0.1x", "1x", "10x"] if blocks else [f"{t:g}x" for t in yticks]
    for axis, t, lab in ((ax.xaxis, ticks, [f"{t:g}x" for t in ticks]), (ax.yaxis, yticks, ylabels)):
        axis.set_major_locator(FixedLocator(t))
        axis.set_major_formatter(FixedFormatter(lab))
        axis.set_minor_formatter(NullFormatter())
    ax.set_xlim(lo, hi)
    if best and not blocks:
        ytop = max(6.5, max(p[3] for p in pts) * 1.3)          # every point inside
        yt = [t for t in (1, 1.5, 2, 3, 5, 10, 20, 50, 100) if t <= ytop and (ytop < 12 or t not in (1.5, 3))]
        ax.yaxis.set_major_locator(FixedLocator(yt))
        ax.yaxis.set_major_formatter(FixedFormatter([f"{t:g}x" for t in yt]))
        ax.set_ylim(0.9, ytop)
    else:
        ax.set_ylim(ylo, yhi)
    rho = spearman([p[2] for p in lay], [p[3] for p in lay])
    zeros = sum(p[3] == zero for p in pts) if blocks else 0
    ax.set_title(title, fontsize=PT, pad=2)
    if not NO_RHO:
        ax.text(0.04, 0.96, f"ρ {rho:.2f}" + (f"; {zeros} at 0" if zeros else ""), transform=ax.transAxes, ha="left",
                va="top", fontsize=PT_SMALL - 0.5, bbox=dict(boxstyle="square,pad=0.1", fc="white", ec="none"), zorder=6)
    ax.grid(True, which="major", alpha=0.3, lw=0.4)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)
    print(f"{title}: {len(lay)} layouts, {len(pts) - len(lay)} mixes, Spearman {rho:.3f}")


def main():
    data, out_dir = args(__doc__)
    # the paper's settings, fixed here (the exploratory draws took them as flags)
    a = argparse.Namespace(data=data, blocks=None, best=True, no_mixes=True, xtop=10.0, ytop=4.0, no_rho=True,
                           ylabel="latency / fastest", cost_labels=None, out=str(out_dir / OUT))
    global NO_RHO
    NO_RHO = a.no_rho
    pts = load(Path(a.data))
    if a.blocks:
        pts = with_blocks(pts, a.blocks)
    if a.no_mixes:
        pts = {w: [p for p in v if p[0] == "layout"] for w, v in pts.items()}
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = (2.0 if a.blocks else 1.6) - (0.1 if a.no_mixes else 0)
    foot = 0.42 if a.no_mixes else 0.52              # inches under the axes: tick labels, x label, legend rows
    if a.best and not a.blocks:                       # square panels: the panel width, plus the title and the foot
        H = (0.975 - 0.1) * COLUMN_IN / (3 + 2 * 0.17) + 0.16 + foot
        HEIGHT_FOR = lambda top: (0.975 - 0.1) * COLUMN_IN / (3 + 2 * 0.17) * (  # noqa: E731
            math.log(a.ytop / 0.9) / math.log(top / 0.9) if a.ytop else 1) + 0.16 + foot
    fig, axes = plt.subplots(1, 3, figsize=(COLUMN_IN, H))
    fig.subplots_adjust(left=0.1, right=0.975, top=1 - 0.16 / H, bottom=foot / H, wspace=0.17)
    pts_best = {}
    for i, (ax, w) in enumerate(zip(axes, ("Interactive", "BI", "JOB"))):
        p = pts[w]
        if a.best:
            p, cheapest, fastest = to_best(p, bool(a.blocks))
            pts_best[w] = p
            print(f"{w}: cheapest {cheapest}, fastest {fastest}")
        title = f"({'abc'[i]}) {w}"
        labels = dict(x.split("=") for x in a.cost_labels.split(",")) if a.cost_labels else {}
        if labels:
            title += f": +{labels['W16' if w == 'Interactive' else w]}"
        panel(ax, p, title, bool(a.blocks), a.best)
        if i:
            ax.tick_params(labelleft=False)
    if a.best and not a.blocks:                       # square panels, x and y over the same range: 1x at the corner, y = x the diagonal
        top = max(max(p[2] for p in pts_best[w]) for w in pts_best)
        top = max(top, max(max(p[3] for p in pts_best[w]) for w in pts_best)) * 1.25
        t = [v for v in ((1, 10, 50) if top > 30 else (1, 2, 5, 10, 20)) if v <= top]
        if a.xtop:
            far = max(max(p[2] for p in pts_best[w]) for w in pts_best)
            if far > a.xtop:
                raise SystemExit(f"--xtop {a.xtop}: a point lies at {far:.2f}x")
            top = a.xtop
        H = HEIGHT_FOR(top)                           # the legend below is placed in this height
        fig.set_size_inches(COLUMN_IN, H)
        fig.subplots_adjust(top=1 - 0.16 / HEIGHT_FOR(top), bottom=foot / HEIGHT_FOR(top))
        for ax in axes:
            ax.set_xlim(0.9, top)
            ax.set_ylim(0.9, a.ytop or top)
            ax.set_box_aspect(math.log((a.ytop or top) / 0.9) / math.log(top / 0.9))   # one decade = one length
            for axis in (ax.xaxis, ax.yaxis):
                axis.set_major_locator(FixedLocator(t))
                axis.set_major_formatter(FixedFormatter([f"{v:g}x" for v in t]))
                # the minor ticks from 1x up (none at the 0.9x margin left of and under 1x)
                axis.set_minor_locator(FixedLocator([m * 10 ** e for e in range(3) for m in range(2, 10) if m * 10 ** e <= top]))
            if a.ytop:                                # the cut y axis: its top labelled too
                yt = [v for v in (1, 2, 3, 4, 5) if v <= a.ytop and (v != 3 or a.ytop < 4)]
                ax.yaxis.set_major_locator(FixedLocator(yt))
                ax.yaxis.set_major_formatter(FixedFormatter([f"{v:g}x" for v in yt]))
                far = max(max(p[3] for p in pts_best[w]) for w in pts_best)
                if far > a.ytop:
                    raise SystemExit(f"--ytop {a.ytop}: a point lies at {far:.2f}x")
    axes[0].set_ylabel(a.ylabel if a.ylabel else "SSD block reads / Sp-Co" if a.blocks else
                       ("steady latency / fastest" if a.best else "steady latency / Sp-Co"), fontsize=PT, labelpad=1)
    axes[1].set_xlabel("combined cost / cheapest" if a.best else "combined cost / Sp-Co", fontsize=PT, labelpad=0)
    names = [("split", "Sp"), ("condensed", "Co")] + [(k, f"Ch{k}k") for k in (1, 4, 16, 64, 256)]
    adj = [Line2D([], [], ls="", marker="o", ms=3.2, color=ADJ_COLOR[k] if isinstance(k, str) else CHUNK_SHADE[k],
                  label=n) for k, n in names]
    props = [Line2D([], [], ls="", marker=PROPS_MARKER[k], ms=3.2, mfc="none", mec="0.2", mew=0.7, label=n)
             for k, n in names]
    mixes = [Line2D([], [], ls="", marker="*", ms=5, mfc=kw["facecolor"], mec=kw["edgecolor"], mew=0.6, label=lab)
             for _, lab, kw in MIXES]
    kw = dict(ncol=7, frameon=False, fontsize=PT_SMALL, handletextpad=0.05, columnspacing=0.5, borderaxespad=0,
              borderpad=0, handlelength=1.0)
    up = 0.0 if a.no_mixes else 0.09                  # the mixes' row, under the other two
    first = fig.legend(handles=adj, loc="lower left", bbox_to_anchor=(0.16, (0.09 + up) / H), **kw)
    second = fig.legend(handles=props, loc="lower left", bbox_to_anchor=(0.16, up / H), **kw)
    if not a.no_mixes:
        fig.legend(handles=mixes, loc="lower left", bbox_to_anchor=(0.16, 0.0), **kw)
    fig.add_artist(first); fig.add_artist(second)
    for y, t in ((0.135 + up, "adjacency"), (0.045 + up, "properties")):
        fig.text(0.155, y / H, t, ha="right", va="center", fontsize=PT_SMALL)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
