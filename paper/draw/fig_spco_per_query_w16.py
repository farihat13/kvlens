#!/usr/bin/env python3
# released from gs-paper at b95936d (evidence/spco-per-query-i01/fig_spco_per_query_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure (the appendix, fig:spco-per-query): Sp-Co's cost per run of each of LDBC Interactive's 27 queries
(W16) at SF3, wops above, bytes below (one column).

  figures/appendix/spco-per-query-w16.pdf

wops = G + 1.1 S + 0.01 N + P + D; bytes = read (misses included) + written; a write's last chunk charged half a
chunk (the half view). Each bar is one run, the mean over the query's params, not weighted by its frequency. Colour =
the query's group (complex reads, short reads, updates). Linear y, so the short reads' and updates' bars are
near zero; their labels give the values. Deterministic counts: no error bars.

Reads only its table, spco-per-query-i01.csv (query, group, frequency, wops, bytes; the table's order).

    python fig_spco_per_query_w16.py --data <dir of the table> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from common import args, read_table, use_paper_font
from paper_style import COLUMN_IN, bytes_to_human, number_to_human

OUT = "figures/appendix/spco-per-query-w16.pdf"
GROUPS = (("complex", "complex reads", "#4c72b0"), ("short", "short reads", "#55a868"), ("update", "updates", "#c44e52"))
COLOR = {g: c for g, _, c in GROUPS}


def small_bytes(b):
    """bytes as the paper's formatter writes them, whole bytes below 1 KB"""
    return f"{b:.0f}B" if b < 1024 else bytes_to_human(b).replace(" ", "")


def main():
    data, out_dir = args(__doc__)
    rows = read_table(data, "spco-per-query-i01")
    if len(rows) != 27 or {r["group"] for r in rows} != set(COLOR):
        raise SystemExit(f"{len(rows)} queries (want 27), groups {sorted({r['group'] for r in rows})}")
    draw(rows, out_dir / OUT)


def draw(rows, out):
    xs, gap = [], 0.0
    for i, r in enumerate(rows):
        if i and r["group"] != rows[i - 1]["group"]:
            gap += 0.5
        xs.append(i + gap)
    use_paper_font()
    plt.rcParams.update({"font.size": 7})
    fig, axes = plt.subplots(2, 1, figsize=(COLUMN_IN, 2.05), sharex=True,
                             gridspec_kw={"hspace": 0.07, "left": 0.1, "right": 0.995})    # drawn at the column's width
    for ax, key, label, fmt, ticks in ((axes[0], "wops", r"$\mathit{wops}$ / run", number_to_human, (0, 30e3, 60e3)),
                                       (axes[1], "bytes", "bytes / run", small_bytes, (0, 10 * 2**20, 20 * 2**20))):
        v = [float(r[key]) for r in rows]
        ax.bar(xs, v, width=0.8, color=[COLOR[r["group"]] for r in rows], edgecolor="white", lw=0.3)
        for x, y in zip(xs, v):
            ax.text(x, y + 0.02 * max(v), fmt(y).replace(" ", ""), ha="center", va="bottom", fontsize=4.6, rotation=90)
        ax.set_ylim(0, max(v) * 1.42)            # room for the tallest bar's label (rotated), and no more
        ax.set_yticks(ticks)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda t, _, fmt=fmt: fmt(t).replace(" ", "") if t else "0"))
        ax.set_ylabel(label, fontsize=7, labelpad=1)
        ax.tick_params(labelsize=6, pad=1, length=2, width=0.5)
        ax.grid(axis="y", alpha=0.3, lw=0.4)
        for s in ax.spines.values():
            s.set_linewidth(0.5)
        ax.set_xlim(xs[0] - 0.55, xs[-1] + 0.55)
    for g, name, color in GROUPS:
        gx = [x for x, r in zip(xs, rows) if r["group"] == g]
        axes[0].text((gx[0] + gx[-1]) / 2, 1.03, name, transform=axes[0].get_xaxis_transform(), ha="center",
                     va="bottom", fontsize=6.5, color=color)
    axes[1].set_xticks(xs)
    axes[1].set_xticklabels([r["query"].upper() for r in rows], rotation=90, fontsize=5.5)
    for ax in axes:
        ax.tick_params(axis="x", length=0, pad=2)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


if __name__ == "__main__":
    main()
