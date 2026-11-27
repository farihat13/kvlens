#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/other-workloads-heatmap/fig_other_workloads_heatmap.py); edit it there, then run evidence/release_draw.py
"""Paper figure: the nine uniform layouts on four benchmarks under three models (full text width).

  figures/evaluation/other-workloads-heatmap.pdf

Rows: LDBC Interactive (SF3, LDBC's frequencies), LDBC BI (SF3, every query once), IMDb with the JOB queries (every
query once), LinkBench (1M nodes, its request log's frequencies). A cell = a uniform layout's cost / the benchmark's best
uniform layout under that model (log colour, saturating at 100x; within 1% of the best boxed). wops = G + 1.1 S + 0.01 N
+ P + D; bytes = read (misses included) + written + read by writes; combined = wops + bytes / 4,096. Counts are
deterministic: no error bars.

Reads only its table, other-workloads-heatmap.csv (the artifact's paper/derive/other_workloads_heatmap.py makes it from
the cross-benchmark snapshot sf3/mix-benefit-job-20261025, and checks each ratio against the summed costs): per
benchmark and layout, the three models' summed costs and their ratios to the best uniform layout.

    python fig_other_workloads_heatmap.py --data <dir of other-workloads-heatmap.csv> --out <dir>
"""
import csv

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap, LogNorm
from matplotlib.patches import Rectangle

from common import args, read_table, use_paper_font
from paper_style import cell_id, side_name

# (benchmark, how its queries are counted, the row's label)
ROWS = (("LDBC Interactive", "LDBC frequencies", "LDBC Interactive"), ("LDBC BI", "once each", "LDBC BI"),
        ("IMDb / JOB", "once each", "IMDb (JOB)"), ("LinkBench", "its log's frequencies", "LinkBench"))
SETTINGS = tuple((s, side_name(s)) for s in ("split", "condensed", "chunked"))     # Sp, Co, Ch64k: the grid's nine
CELLS = [(cell_id(p, a), f"{ps}-{as_}") for p, ps in SETTINGS for a, as_ in SETTINGS]   # properties first, as the grid
MODELS = ((r"(a) $\mathit{wops}$", "wops"), ("(b) bytes", "bytes"),
          (r"(c) combined: $\mathit{wops}$ + bytes/4096", "wops_bytes4k"))
WITHIN = 1.01               # a cell within 1% of the best is boxed as a best
CAP = 100                   # the colour saturates at 100x; the number in the cell gives the value
CMAP = LinearSegmentedColormap.from_list("paper_blue", ["#f7fbff", "#6f9cc9"])   # the workload heat map's blue
PT = 7                      # the text size in the figure, in points at the text width


def label(v):
    return f"{v:.2f}" if v < 1.1 else (f"{v:.1f}" if v < 10 else (f"{v:.0f}" if v < 1000 else f"{v / 1000:.1f}K"))


def load(data):
    """{(benchmark, frequencies): {cell: row}} for the four rows drawn"""
    by = {}
    for r in read_table(data, "other-workloads-heatmap"):
        by.setdefault((r["benchmark"], r["frequencies"]), {})[r["cell"]] = r
    out = {}
    for bench, freq, _ in ROWS:
        rows = by.get((bench, freq), {})
        if sorted(rows) != sorted(c for c, _ in CELLS):
            raise SystemExit(f"{bench}, {freq}: {len(rows)} layouts, not the nine")
        out[bench, freq] = rows
    return out


def main():
    data_dir, out_dir = args(__doc__)
    data = load(data_dir)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    norm = LogNorm(vmin=1, vmax=CAP, clip=True)
    H = 0.95 + 0.16 * len(ROWS)
    fig, axes = plt.subplots(1, 3, figsize=(7.0, H), sharey=True)
    for ax, (title, model) in zip(axes, MODELS):
        grid = [[float(data[bench, freq][c][f"{model}_ratio"]) for c, _ in CELLS] for bench, freq, _ in ROWS]
        ax.imshow(grid, norm=norm, cmap=CMAP, aspect="auto")
        for i, g in enumerate(grid):
            for j, v in enumerate(g):
                best = v <= WITHIN
                if best:
                    ax.add_patch(Rectangle((j - 0.47, i - 0.45), 0.94, 0.9, fill=False, ec="black", lw=0.8, zorder=3))
                ax.text(j, i, label(v), ha="center", va="center", fontsize=PT - 2, color="black" if best else "0.25")
        for x in (2.5, 5.5):                             # between the three property settings
            ax.axvline(x, color="0.5", lw=0.4)
        ax.set_xticks(range(9), [n for _, n in CELLS], rotation=90)
        ax.tick_params(length=0, pad=1.5, labelsize=PT - 0.5)
        ax.set_title(title, fontsize=PT + 0.5, pad=3)
        for side in ax.spines.values():
            side.set_linewidth(0.4)
            side.set_color("0.4")
        for (bench, _, name), g in zip(ROWS, grid):
            print(f"{model:13s} {name:17s} " + " ".join(f"{n}={label(v)}{'*' if v <= WITHIN else ''}"
                                                       for (_, n), v in zip(CELLS, g)))
    axes[0].set_yticks(range(len(ROWS)), [n for _, _, n in ROWS])
    cax = fig.add_axes([0.946, 0.36 / H, 0.008, 1 - 0.56 / H])      # one colour range for the three panels
    cb = fig.colorbar(plt.cm.ScalarMappable(norm=norm, cmap=CMAP), cax=cax, extend="max")
    cb.set_ticks([1, 2, 5, 10, 20, 50, 100], labels=["1x", "2x", "5x", "10x", "20x", "50x", "100x"])
    cb.ax.tick_params(labelsize=PT - 1.5, length=1.5, width=0.4, pad=1)
    cb.outline.set_linewidth(0.4)
    cb.set_label("x the best uniform", fontsize=PT - 1, labelpad=1)
    fig.subplots_adjust(left=0.105, right=0.935, top=1 - 0.2 / H, bottom=0.36 / H, wspace=0.035)
    OUT = out_dir / "figures/evaluation/other-workloads-heatmap.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
