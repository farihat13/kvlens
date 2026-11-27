#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/uniform-i01-chunk-sizes-and-front/fig_uniform_w16_chunk_sizes.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3 on the 49 uniform layouts with Chunked at 1, 4, 16, 64 and 256 KB, under three models (full width).

  figures/evaluation/uniform-w16-chunk-sizes.pdf

Each side (properties, adjacency) is Split, Condensed or Chunked at one of five sizes: 7 x 7 = 49 layouts, grouped by
the adjacency setting (headed above each group), the properties setting under each bar. One panel per model: wops,
bytes, combined (wops + bytes / 4,096). A bar = the layout's cost / Sp-Co's (the dotted line at 1; Sp-Co's absolute
cost is beside each panel's name), log. Colour = the adjacency setting, hatch = the properties setting (the repo's cell
theme); the model's best layout has a black outline and a bold label. Labelled: the best, Sp-Co, and the first, middle
and last bar of each group (a bar beside a labelled one is not). wops = G + 1.1 S + 0.01 N + P + D (a write's own reads
included); bytes = read (misses included) + written. Counts are deterministic: no error bars.

Not in the paper on its own: uniform-i01-chunk-sizes-and-front draws these panels (and imports this module).

Reads only its table, uniform-i01-chunk-sizes-and-front.csv (the artifact's paper/derive/uniform_i01_chunk_sizes_and_front.py
makes it from the snapshot sf3/mix-benefit-20261103, its chunk-size-uniform, and checks each row's wops): every row of
layouts.csv (17 of the 49 measured, the rest composed per part from the same captures).

    python fig_uniform_w16_chunk_sizes.py --data <dir of the table> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from common import args, read_table, use_paper_font
from paper_style import cell_bar_kw, cell_id

BYTES_PER_OP = 4096         # the combined cost's byte price: one 4 KiB block = one op (the wops_bytes4k rule)
SETTINGS = (("split", "Sp"), ("condensed", "Co"), ("chunked_c1k", "Ch1k"), ("chunked_c4k", "Ch4k"),
            ("chunked_c16k", "Ch16k"), ("chunked", "Ch64k"), ("chunked_c256k", "Ch256k"))   # unsized Chunked = 64 KB
CELLS = [cell_id(p, a) for a, _ in SETTINGS for p, _ in SETTINGS]   # adjacency outer, properties inner
MODELS = ((r"$\mathit{wops}$", lambda w, b: w, lambda v: f"{v / 1e6:.0f}M"),
          ("bytes", lambda w, b: b, lambda v: f"{v / 1e9:.0f}GB"),
          ("combined", lambda w, b: w + b / BYTES_PER_OP, lambda v: f"{v / 1e6:.0f}M"))
REF = "props_split_adj_condensed"   # the 1 line: Sp-Co, as uniform-and-mixed-i01
LABELLED = (0, 3, 6)        # per adjacency group: the first, middle and last properties setting get a label
GAP = 0.5                   # the space between two adjacency groups, in bar widths
PT = 8                      # the figure's text size, in points at full page width


def load(data):
    """{layout: (wops, bytes)}, the table's rows in their file's order; checks all 49 are there"""
    cells = {}
    for r in sorted(read_table(data, "uniform-i01-chunk-sizes-and-front"), key=lambda r: r["line"]):
        cells[r["layout"]] = (float(r["wops"]), float(r["bytes"]))
    missing = [c for c in CELLS if c not in cells]
    if missing:
        raise SystemExit(f"uniform-i01-chunk-sizes-and-front.csv: no row for {missing}")
    return cells


def ratio_label(v):
    """a ratio to Sp-Co, no "x" (the axis says it); 1.02 is not a tie: two decimals below 1.1"""
    return f"{v:.2f}" if v < 1.1 else (f"{v:.1f}" if v < 10 else f"{v:.0f}")


def main():
    data, out_dir = args(__doc__)
    cells = load(data)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    n = len(SETTINGS)
    fig, axes = plt.subplots(3, 1, figsize=(7.0, 3.0), sharex=True)
    xs = [g * (n + GAP) + a for g in range(n) for a in range(n)]
    for ax, (name, cost, human) in zip(axes, MODELS):
        ref = cost(*cells[REF])
        ratio = [cost(*cells[c]) / ref for c in CELLS]
        best = min(ratio)
        for x, v, c in zip(xs, ratio, CELLS):
            ax.bar(x, v, width=0.85, zorder=3, **cell_bar_kw(c))
            if v <= best * 1.01:                            # the best layout (within 1%): a black outline
                ax.bar(x, v, width=0.85, zorder=4, fill=False, edgecolor="black", linewidth=0.8)
        ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
        ib, ir = ratio.index(best), CELLS.index(REF)
        done = []
        for i in [ib, ir] + [i for i in range(len(xs)) if i % n in LABELLED]:
            if i in done or any(abs(i - j) == 1 and i // n == j // n for j in done):
                continue                                    # a bar beside a labelled one: the labels would touch
            done.append(i)
            ax.text(xs[i], ratio[i] * 1.1, "1" if i == ir else ratio_label(ratio[i]), ha="center", va="bottom",
                    fontsize=PT - 2, fontweight="bold" if i == ib else "normal")
        ax.set_yscale("log")
        ax.set_ylim(min(ratio) / 1.4, max(ratio) * 1.75)   # room for the labels only
        lo, hi = ax.get_ylim()
        # a narrow panel (under 30x) also labels 2 and 5 within each decade, or it shows a single tick
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0,) if hi / lo > 30 else (1.0, 2.0, 5.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_ylabel(name)
        ax.yaxis.set_label_coords(-0.052, 0.5)
        ax.text(-0.034, 0.5, f"Sp-Co = {human(ref)}", transform=ax.transAxes, rotation=90, ha="center", va="center",
                fontsize=PT - 1.5)                          # Sp-Co's absolute cost: the bars are ratios to it
        for g in range(1, n):
            ax.axvline(g * (n + GAP) - (1 + GAP) / 2, color="0.6", lw=0.4)
        ax.set_xlim(-0.6, xs[-1] + 0.6)
        ax.tick_params(labelsize=PT - 1, pad=1, length=2, width=0.5)
        ax.tick_params(which="minor", length=1, width=0.4)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
        print(f"{name}: Sp-Co {human(ref)}; best {CELLS[ib]} {best:.3f}")
    for g, (_, s) in enumerate(SETTINGS):
        axes[0].text(g * (n + GAP) + (n - 1) / 2, 1.01, f"adjacency {s}", transform=axes[0].get_xaxis_transform(),
                     ha="center", va="bottom")
    axes[-1].set_xticks(xs, [s for _ in SETTINGS for _, s in SETTINGS], rotation=90)
    axes[-1].tick_params(axis="x", labelsize=PT - 1.5, pad=1)
    axes[-1].set_xlabel("properties setting", labelpad=0)
    fig.subplots_adjust(left=0.062, right=0.997, top=0.94, bottom=0.165, hspace=0.05)
    OUT = out_dir / "figures/evaluation/uniform-w16-chunk-sizes.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
