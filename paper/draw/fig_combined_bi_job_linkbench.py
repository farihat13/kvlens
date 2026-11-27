#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/combined-bi-job-linkbench/fig_combined_bi_job_linkbench.py); edit it there, then run evidence/release_draw.py
"""Paper figure: LDBC BI, IMDb/JOB and LinkBench at SF3, the nine uniform layouts and the fewest-combined mix (one
paper column).

  figures/evaluation/combined-bi-job-linkbench.pdf

Three panels, one per benchmark, the combined cost (wops + bytes / 4,096 per query) only. In each, a bar = an option's
score / Sp-Co's (the dotted line at 1; the y label gives Sp-Co's absolute score), log. BI and JOB add their queries by
the geomean (equal contribution: they define no frequencies), LinkBench by its request log's counts. A uniform bar's
colour is its adjacency setting and its hatch its properties setting (the repo's cell theme); the best uniform layout
has a black outline and a bold label. The pink bar is the fewest-combined mix (per vertex type and per edge type and
direction, no byte limit): exact for BI and LinkBench; for JOB the mix a two-step search found (so its gain is at
least what the bar shows). Its label is bold when it beats the best uniform layout; when the two round to one label,
both take three decimals. Counts are deterministic: no error bars.

The nine are ordered by adjacency setting first (Sp, Co, Ch64k; named above each group of three), then by properties
within each group (the tick under each bar).

Reads only its table, combined-bi-job-linkbench.csv (the artifact's paper/derive/combined_bi_job_linkbench.py makes it
from the snapshot sf3/mix-search-20261103, its combined-other-benchmarks): per benchmark the nine, the mix and the ceiling, each with
its score and ratio to Sp-Co under the benchmark's form.

    python fig_combined_bi_job_linkbench.py --data <dir of combined-bi-job-linkbench.csv> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from common import args, read_table, use_paper_font
from paper_style import cell_bar_kw, cell_id, side_name

MIX = "#d81b60"             # mixcol, as uniform-and-mixed-i01
SETTINGS = tuple((s, side_name(s)) for s in ("split", "condensed", "chunked"))     # Sp, Co, Ch64k: the grid's nine
CELLS = [(cell_id(p, a), f"{ps}-{as_}") for a, as_ in SETTINGS for p, ps in SETTINGS]   # adjacency first
REF = "props_split_adj_condensed"   # the 1x reference: Sp-Co, Dgraph's settings (as the design-grid table)
# (benchmark in the table, panel name, the form its score takes)
PANELS = (("LDBC BI", "BI", "geomean"), ("IMDb / JOB", "JOB", "geomean"), ("LinkBench", "LinkBench", "sum"))
PT = 9                      # the figure's text size (axes, ticks, headers), in points at one paper column
PT_BAR = 7                  # the bar labels
PT_REF = 7                  # the 'Sp-Co = ...' line beside each panel's name


def ratio_label(v):
    """a ratio to Sp-Co, no "x" (the axis says it); 1.02 is not a tie: two decimals below 1.1"""
    return f"{v:.2f}" if v < 1.1 else (f"{v:.1f}" if v < 10 else f"{v:.0f}")


def main():
    data, out_dir = args(__doc__)
    table = read_table(data, "combined-bi-job-linkbench")
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig, axes = plt.subplots(3, 1, figsize=(3.33, 3.1), sharex=True)
    MX = 9.3                                            # the mix's x, close after the nine (the divider at 8.65)
    for ax, (bench, name, form) in zip(axes, PANELS):
        rows = {r["option"]: r for r in table if r["benchmark"] == bench}
        if any(c not in rows for c, _ in CELLS) or "mix" not in rows:
            raise SystemExit(f"combined-bi-job-linkbench.csv: {bench} lacks the nine or the mix")
        ratio = [float(rows[c][f"{form}_ratio_to_spco"]) for c, _ in CELLS]
        mix = float(rows["mix"][f"{form}_ratio_to_spco"])
        ref = float(rows[REF][f"{form}_score"])
        best = min(ratio)                               # the best uniform layout, as a ratio to Sp-Co
        is_best = [v <= best * 1.0001 for v in ratio]
        # a mix and a best uniform layout that round to one label take three decimals, so a gain does not read as a tie
        lab = (lambda v: f"{v:.3f}") if ratio_label(mix) == ratio_label(best) and mix < best * 0.995 else ratio_label
        for j, (c, _) in enumerate(CELLS):              # the cell theme: colour = adjacency, hatch = properties
            ax.bar(j, ratio[j], width=0.7, zorder=3, **cell_bar_kw(c))
            if is_best[j]:                              # the best uniform layout: a black outline
                ax.bar(j, ratio[j], width=0.7, zorder=4, fill=False, edgecolor="black", linewidth=0.7)
            ax.text(j, ratio[j] * 1.1, "1" if c == REF else (lab(ratio[j]) if is_best[j] else ratio_label(ratio[j])),
                    ha="center", va="bottom", fontsize=PT_BAR, fontweight="bold" if is_best[j] else "normal")
        ax.bar(MX, mix, color=MIX, width=0.7, zorder=3)
        ax.text(MX, mix * 1.1, lab(mix), ha="center", va="bottom", fontsize=PT_BAR, color=MIX,
                fontweight="bold" if mix < best * 0.995 else "normal")
        ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
        ax.set_yscale("log")
        ax.set_ylim(min(ratio + [mix]) / 1.5, max(ratio + [mix]) * 1.95)   # room for the labels only
        lo, hi = ax.get_ylim()
        # a narrow panel (under 30x) also labels 2 and 5 within each decade, or it shows a single tick
        ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0,) if hi / lo > 30 else (1.0, 2.0, 5.0)))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.set_ylabel(name)
        ax.yaxis.set_label_coords(-0.17, 0.5)
        ax.text(-0.112, 0.5, f"Sp-Co = {ref / 1e3:.0f}K", transform=ax.transAxes, rotation=90, ha="center",
                va="center", fontsize=PT_REF)           # Sp-Co's absolute score, small, between the name and the ticks
        ax.set_xlim(-0.6, 9.9)
        for x in (2.5, 5.5):                            # between the three adjacency settings
            ax.axvline(x, color="0.5", lw=0.5)
        ax.axvline(8.65, color="0.2", lw=0.7)           # uniform | mixed
        ax.tick_params(labelsize=PT, pad=1, length=2, width=0.5)
        ax.tick_params(which="minor", length=1, width=0.4)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
        ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
        print(f"{name}: Sp-Co {ref:,.0f}, best uniform {best:.3f}, mix {mix:.3f}; "
              + " ".join(f"{n}={v:.2f}" for (_, n), v in zip(CELLS, ratio)))
    for g, (_, s) in enumerate(SETTINGS):               # the adjacency setting heads each group of three
        axes[0].text(3 * g + 1, 1.015, f"adj. {s}", transform=axes[0].get_xaxis_transform(), ha="center", va="bottom")
    axes[-1].set_xticks(list(range(9)) + [MX], [n.split("-")[0] for _, n in CELLS] + ["mix"], rotation=90)
    axes[-1].get_xticklabels()[9].set_color(MIX)
    axes[-1].text(4, -0.34, "properties setting", transform=axes[-1].get_xaxis_transform(), ha="center", va="top")
    fig.subplots_adjust(left=0.185, right=0.997, top=0.955, bottom=0.165, hspace=0.06)
    OUT = out_dir / "figures/evaluation/combined-bi-job-linkbench.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
