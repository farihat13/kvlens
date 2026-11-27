#!/usr/bin/env python3
# released from gs-paper at 42ebba2 (evidence/uniform-49-layouts-workloads/fig_uniform_49_layouts_workloads_wops_bytes.py); edit it there, then run evidence/release_draw.py
"""Paper figure: the 49 uniform layouts' combined cost on each workload, each bar split into wops and bytes (one column).

  figures/evaluation/uniform-49-layouts-workloads-cc-wops-bytes-no-mix-no-once.pdf

Rows: LDBC Interactive SF3 (W16's frequencies), LDBC BI SF3 (equal share), JOB (equal share), LinkBench (its
frequencies); no SF30 rows. Each bar is a layout's combined cost (wops +
bytes / 4,096) over the row's cheapest layout (outlined, 1), split into its wops part (bottom, in the bar's adjacency
colour) and its bytes / 4,096 (a lighter tint) part (top); linear y per row. Groups: the adjacency setting (named above
the top row, every other group on a light background); bars in a group: the properties setting.
Deterministic counts: no error bars.

Reads only its tables: uniform-49-layouts-workloads-per-query.csv (model combined: per query and layout, wops and
bytes; a layout's parts sum frequency x wops and frequency x bytes / 4,096), checked against each layout's score in
uniform-49-layouts-workloads.csv.

    python fig_uniform_49_layouts_workloads_wops_bytes.py --data <dir of the tables> --out <dir>
"""
import csv
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from matplotlib.legend_handler import HandlerTuple  # noqa: E402
import common  # noqa: E402
from common import args, use_paper_font  # noqa: E402
from paper_style import (COLUMN_IN, cell_id, engine_legend, colour_engine_legend, mark_engine_ticks, outline_engine_bar, swatch,  # noqa: E402
                         tint, two_tones)

SETTINGS = (("split", "Sp"), ("condensed", "Co"), ("chunked_c1k", "Ch1k"), ("chunked_c4k", "Ch4k"),
            ("chunked_c16k", "Ch16k"), ("chunked", "Ch64k"), ("chunked_c256k", "Ch256k"))
ROWS = (("ldbc-sf3-interactive", "sum", "Int. {sf}"), ("ldbc-sf3-bi", "spco-share", "BI {sf}"),
        ("imdb-job", "spco-share", "JOB"), ("linkbench", "sum", "LinkBench"))
OUT = "figures/evaluation/uniform-49-layouts-workloads-cc-wops-bytes-no-mix-no-once.pdf"
REF = "props_split_adj_condensed"
GAP = 0.3
PT, PT_BAR = 7, 6.5                          # axis names; the values on the bars, the y ticks, the legend
PT_TICK = 5.25                               # the 50 rotated x labels: larger ones touch
WIDTH = 0.9                                  # a bar, of its slot


def tones(cell):
    """(wops, bytes) colours of a cell's bar: two_tones' base, and a darker tint than its light one (the bytes part
    must read on paper)"""
    base = two_tones(cell)[0]
    return base, tint(base, 0.45 if sum(to_rgb(base)) / 3 > 0.55 else 0.42)


def label(v):
    return f"{v:.2f}" if v < 0.995 else (f"{v:.1f}" if v < 9.95 else f"{v:.0f}")


def parts(data):
    """{(workload, form): {cell: (wops, bytes / 4096)}}, checked against the table's scores"""
    out = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in csv.DictReader(open(data / "uniform-49-layouts-workloads-per-query.csv")):
        if r["model"] != "combined":
            continue
        wo, by, f = float(r["wops"]), float(r["bytes"]) / 4096, float(r["frequency"])
        form = {"ldbc-sf3-bi": "spco-share", "imdb-job": "spco-share"}.get(r["workload"], "sum")
        a = out[(r["workload"], form)][r["cell"]]
        a[0] += f * wo
        a[1] += f * by
    score = {(r["workload"], r["form"], r["cell"]): float(r["score"])
             for r in csv.DictReader(open(data / "uniform-49-layouts-workloads.csv")) if r["model"] == "combined"}
    for (wl, form), cells in out.items():
        for c, (wo, by) in cells.items():
            s = score.get((wl, form, c))
            if s is not None and abs(wo + by - s) > 1e-9 * s:
                raise SystemExit(f"{wl} {form} {c}: parts {wo + by!r}, score {s!r}")
    return out


def draw(cells, rows, out):
    """the figure: one row per workload"""
    n = len(SETTINGS)
    names = [cell_id(p, a) for a, _ in SETTINGS for p, _ in SETTINGS]
    xs = [g * (n + GAP) + i for g in range(n) for i in range(n)]
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = 0.13 + 0.505 * len(rows) + 0.33
    fig = plt.figure(figsize=(COLUMN_IN, H))
    gs = fig.add_gridspec(len(rows), 1, hspace=0.08, left=0.075, right=0.997, top=1 - 0.13 / H, bottom=0.33 / H)
    axes = [fig.add_subplot(gs[i]) for i in range(len(rows))]
    for ax, (wl, form, name) in zip(axes, rows):
        c = cells[(wl, form)]
        assert len(c) == 49, (wl, form, len(c))
        ref = min(sum(c[k]) for k in names)                        # the row's cheapest layout is 1
        tot = [sum(c[k]) / ref for k in names]
        best = min(tot)
        for x, k in zip(xs, names):
            wo, by = c[k][0] / ref, c[k][1] / ref
            dark, light = tones(k)
            ax.bar(x, wo, width=WIDTH, color=dark, lw=0, zorder=3)
            ax.bar(x, by, bottom=wo, width=WIDTH, color=light, edgecolor="white", lw=0.3, zorder=3)
            outline_engine_bar(ax, x, wo + by, k, WIDTH)
            if wo + by <= best * 1.0001:
                ax.bar(x, wo + by, width=WIDTH, fill=False, edgecolor="black", lw=0.7, zorder=4)
        top = max(tot)
        ib, ir = tot.index(best), names.index(REF)
        done = []
        for i in [ib, ir] + [i for i in range(len(xs)) if i % n in (0, n - 1)]:      # the best, Sp-Co, each group's ends
            if i in done or any(abs(i - j) <= 3 and i // n == j // n for j in done):
                continue
            done.append(i)
            x, ha = {0: (xs[i] - 0.45, "left"), n - 1: (xs[i] + 0.45, "right")}.get(i % n, (xs[i], "center"))
            ax.text(x, tot[i] + top * 0.04, "1" if i == ib else label(tot[i]), ha=ha, va="bottom", fontsize=PT_BAR,
                    fontweight="bold" if i == ib else "normal")
        ax.set_ylim(0, top * 1.34)
        for g in range(1, n, 2):                                       # every other adjacency group on a light background
            ax.axvspan(g * (n + GAP) - (1 + GAP) / 2, g * (n + GAP) + n - 1 + (1 + GAP) / 2, color="0.94", lw=0, zorder=0)
        ax.set_xlim(-0.6, xs[-1] + 0.6)
        ax.set_ylabel(name, fontsize=PT - 0.5, labelpad=1, linespacing=0.95)
        ax.yaxis.set_label_coords(-0.054, 0.5)
        ax.tick_params(labelsize=PT_BAR, pad=1, length=2, width=0.5)
        ax.locator_params(axis="y", nbins=3)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))      # 0, 2.5, 5: no trailing .0
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=1)
        print(f"{name}: best {names[ib]} {best:.3f}, Sp-Co {sum(c[REF]) / ref:.2f}x")
    for a in axes[:-1]:
        a.tick_params(labelbottom=False)
    for g, (_, s) in enumerate(SETTINGS):
        axes[0].text(g * (n + GAP) + (n - 1) / 2, 1.02, s, transform=axes[0].get_xaxis_transform(), ha="center",
                     va="bottom", fontsize=PT_BAR, fontweight="bold", color=two_tones(names[g * n])[0])
    axes[-1].set_xticks(xs, [s for _ in SETTINGS for _, s in SETTINGS], rotation=90)
    axes[-1].tick_params(axis="x", labelsize=PT_TICK, pad=1)
    # the legend in the first row's empty upper right (its Chunked bars stay under 3x)
    mark_engine_ticks(fig, axes[-1], names)
    adj = [names[g * n] for g in range(n)]
    kw = dict(frameon=False, fontsize=PT_BAR, columnspacing=0.8, handletextpad=0.3, borderaxespad=0, borderpad=0)
    first = axes[0].legend([swatch(tones(c)[0] for c in adj), swatch(tones(c)[1] for c in adj)],
                           ["wops", "bytes / 4,096"], handler_map={tuple: HandlerTuple(ndivide=None, pad=0)}, ncol=2,
                           loc="upper right", bbox_to_anchor=(0.99, 0.95), handlelength=3.2, **kw)
    eh, el = engine_legend()
    colour_engine_legend(axes[1].legend(eh, el, ncol=2, loc="upper right", bbox_to_anchor=(0.99, 0.95),
                                        handlelength=1, **kw))
    axes[0].add_artist(first)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


def main():
    data, out_dir = args(__doc__)
    cells = parts(data)
    missing = [r for r in ROWS if len(cells.get(r[:2], ())) != 49]   # a run that did not measure that dataset
    for wl, form, _ in missing:
        print(f"{wl} {form}: not in this run ({len(cells.get((wl, form), ()))} of the 49 layouts): its row is left out")
    draw(cells, [(wl, form, name.format(sf=common.sf_label())) for wl, form, name in ROWS if (wl, form, name) not in missing],
         out_dir / OUT)


if __name__ == "__main__":
    main()
