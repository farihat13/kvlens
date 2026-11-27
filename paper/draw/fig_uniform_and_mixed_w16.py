#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/uniform-and-mixed-i01/fig_uniform_and_mixed_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3 (64 KB chunks), the nine uniform layouts and three mixes under three models (one paper column).

  figures/evaluation/uniform-and-mixed-w16.pdf

Three panels, one per model: wops, bytes, combined (wops + bytes / 4,096). In each, a bar = an option's cost / Sp-Co's
(the dotted line at 1; the panel's y label gives Sp-Co's absolute cost), log. A uniform bar's colour is its adjacency setting
and its hatch its properties setting (the repo's cell theme); that model's best uniform layout has a black outline and a
bold label. The three mixes set the layout per vertex type and per edge type and direction, with no byte limit: each is
the fewest under one model (dark pink in that model's panel; its label bold when it beats the best uniform layout). wops = G + 1.1 S + 0.01 N + P + D (reads and writes, a write's own reads included); bytes = read (misses
included) + written + read by writes. Counts are deterministic: no error bars.

The nine are ordered by adjacency setting first (Sp, Co, Ch64k; named above each group of three), then by properties
within each group (the tick under each bar).

Reads only its table, uniform-and-mixed-i01.csv (the artifact's paper/derive/uniform_and_mixed_i01.py makes it from
the mix-benefit snapshot sf3/mix-benefit-20261024): the nine and the fewest-wops and fewest-bytes mixes (source
options.csv), the fewest-combined mix (source sum_optimum.csv), each with total_ops and total_bytes.

    python fig_uniform_and_mixed_w16.py --data <dir of uniform-and-mixed-i01.csv> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from common import args, read_table, use_paper_font
from paper_style import cell_bar_kw, cell_id, side_name

BYTES_PER_OP = 4096         # the combined cost's byte price: one 4 KiB block = one op (the wops_bytes4k rule)
MIX, MIX_FAINT = "#d81b60", "#f2b6cc"   # mixcol (the options map, the TikZ figures) and a light mixcol
SETTINGS = tuple((s, side_name(s)) for s in ("split", "condensed", "chunked"))     # Sp, Co, Ch64k: the grid's nine
CELLS = [(cell_id(p, a), f"{ps}-{as_}") for a, as_ in SETTINGS for p, ps in SETTINGS]   # adjacency first
MODELS = ((r"$\mathit{wops}$", lambda r: float(r["total_ops"]), lambda v: f"{v / 1e6:.0f}M"),
          ("bytes", lambda r: float(r["total_bytes"]), lambda v: f"{v / 1e9:.0f}GB"),
          ("combined", lambda r: float(r["total_ops"]) + float(r["total_bytes"]) / BYTES_PER_OP, lambda v: f"{v / 1e6:.0f}M"))
REF = "props_split_adj_condensed"   # the 1x reference: Sp-Co, Dgraph's settings (as the design-grid table)
PT = 9                      # the figure's text size (axes, ticks, headers), in points at one paper column
PT_BAR = 7                  # the bar labels
PT_REF = 7                  # the 'Sp-Co = ...' line under each panel's name


def ratio_label(v):
    """a ratio to Sp-Co, no "x" (the axis says it); 1.02 is not a tie: two decimals below 1.1"""
    return f"{v:.2f}" if v < 1.1 else (f"{v:.1f}" if v < 10 else f"{v:.0f}")


def main():
    data, out_dir = args(__doc__)
    table = read_table(data, "uniform-and-mixed-i01")
    rows = {r["option"]: r for r in table if r["source"] == "options.csv"}
    comb = [r for r in table if r["source"] == "sum_optimum.csv"]
    if len(comb) != 1 or any(c not in rows for c, _ in CELLS):
        raise SystemExit("uniform-and-mixed-i01.csv: not the nine, the two mixes from options.csv and one from sum_optimum.csv")
    mixes = [(rows["per_type_direction_fewest_ops"], r"$\mathit{wops}$"),
             (rows["per_type_direction_fewest_bytes"], "bytes"), (comb[0], "combined")]

    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig, axes = plt.subplots(3, 1, figsize=(3.33, 3.1), sharex=True)
    MX = (9.2, 10.2, 11.2)                              # the mixes' x, close after the nine (the divider at 8.6)
    for k, (ax, (name, cost_of, human)) in enumerate(zip(axes, MODELS)):
        cost = [cost_of(rows[c]) for c, _ in CELLS]
        ref = cost_of(rows[REF])
        ratio = [v / ref for v in cost]
        best = min(ratio)                               # the best uniform layout, as a ratio to Sp-Co
        is_ref = [c == REF for c, _ in CELLS]
        is_best = [v <= best * 1.01 for v in ratio]
        mix_ratio = [cost_of(m) / ref for m, _ in mixes]
        for j, (c, _) in enumerate(CELLS):              # the cell theme: colour = adjacency, hatch = properties
            ax.bar(j, ratio[j], width=0.7, zorder=3, **cell_bar_kw(c))
            if is_best[j]:                              # the best uniform layout: a black outline
                ax.bar(j, ratio[j], width=0.7, zorder=4, fill=False, edgecolor="black", linewidth=0.7)
        ax.bar(MX, mix_ratio, color=[MIX if j == k else MIX_FAINT for j in range(3)], width=0.7, zorder=3)
        ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
        for j, v in enumerate(ratio):
            ax.text(j, v * 1.1, "1" if is_ref[j] else ratio_label(v), ha="center", va="bottom", fontsize=PT_BAR,
                    fontweight="bold" if is_best[j] else "normal")
        for j, v in zip(MX, mix_ratio):
            ax.text(j, v * 1.1, ratio_label(v), ha="center", va="bottom", fontsize=PT_BAR, color=MIX,
                    fontweight="bold" if v < best * 0.995 else "normal")
        ax.set_yscale("log")
        ax.set_ylim(min(ratio + mix_ratio) / 1.5, max(ratio + mix_ratio) * 1.95)   # room for the labels only
        lo, hi = ax.get_ylim()
        # a narrow panel (under 30x) also labels 2 and 5 within each decade, or it shows a single tick
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0,) if hi / lo > 30 else (1.0, 2.0, 5.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_ylabel(name)
        ax.yaxis.set_label_coords(-0.15, 0.5)
        ax.text(-0.098, 0.5, f"Sp-Co = {human(ref)}", transform=ax.transAxes, rotation=90, ha="center", va="center",
                fontsize=PT_REF)                        # Sp-Co's absolute cost, small, between the name and the ticks
        ax.set_xlim(-0.6, 11.8)
        for x in (2.5, 5.5):                            # between the three adjacency settings
            ax.axvline(x, color="0.5", lw=0.5)
        ax.axvline(8.6, color="0.2", lw=0.7)            # uniform | mixed
        ax.tick_params(labelsize=PT, pad=1, length=2, width=0.5)
        ax.tick_params(which="minor", length=1, width=0.4)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
        print(f"{name}: Sp-Co {human(ref)}, best uniform {best:.3f}; " + " ".join(f"{n}={v:.2f}" for (_, n), v in zip(CELLS, ratio))
              + " | mixes " + " ".join(f"{v:.3f}" for v in mix_ratio))
    # the adjacency setting heads each group of three; a bar's tick names its properties setting
    for g, (_, s) in enumerate(SETTINGS):
        axes[0].text(3 * g + 1, 1.015, f"adj. {s}", transform=axes[0].get_xaxis_transform(), ha="center", va="bottom")
    axes[0].text(MX[1], 1.015, "mix for", transform=axes[0].get_xaxis_transform(), ha="center", va="bottom", color=MIX)
    axes[-1].set_xticks(list(range(9)) + list(MX), [n.split("-")[0] for _, n in CELLS] + [n for _, n in mixes],
                        rotation=90)
    for label in axes[-1].get_xticklabels()[9:]:
        label.set_color(MIX)
    axes[-1].text(4, -0.36, "properties setting", transform=axes[-1].get_xaxis_transform(), ha="center", va="top")
    fig.subplots_adjust(left=0.155, right=0.997, top=0.955, bottom=0.185, hspace=0.06)
    OUT = out_dir / "figures/evaluation/uniform-and-mixed-w16.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
