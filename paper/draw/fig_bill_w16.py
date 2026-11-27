#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/bill-i01/fig_bill_w16.py); edit it there, then run evidence/release_draw.py
"""Paper figure: W16 at SF3 under a billed store's rule (DynamoDB's request units): the nine uniform layouts and the mix
with the lowest bill, at 64 KB chunks (one paper column).

  figures/evaluation/bill-w16.pdf

Bar = the exact bill of one pass of W16 (the table also holds its lower and upper forms; they are not drawn).
Every bar is over Sp-Co's exact bill (the dotted line at 1), log; the style of uniform-and-mixed-i01: colour = the
adjacency setting, hatch = the properties setting, grouped by adjacency; the best uniform layout has a black outline
and a bold label; the mix is dark pink (its label bold when it beats the best uniform layout). The mix sets the layout per vertex type and per edge
type (both directions alike), solved under the exact form.

Reads only its table, bill-i01.csv (the artifact's paper/derive/bill_i01.py makes it from the snapshot
sf3/mix-benefit-20261103, layout-choice/wops_bytes4k/bill, and the mix from sf3/mix-search-20261103): per option and form, the bill and bill_x_sp_co, its ratio to Sp-Co's exact bill.

    python fig_bill_w16.py --data <dir of bill-i01.csv> --out <dir>     # -> <out>/figures/evaluation/bill-w16.pdf
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

from common import args, read_table, use_paper_font
from paper_style import cell_bar_kw, cell_id, side_name

MIX = "#d81b60"
FORMS = ("lower", "exact", "upper")
SETTINGS = tuple((s, side_name(s)) for s in ("split", "condensed", "chunked"))     # Sp, Co, Ch64k: the grid's nine
CELLS = [(cell_id(p, a), f"{ps}-{as_}") for a, as_ in SETTINGS for p, ps in SETTINGS]   # adjacency first
REF = "props_split_adj_condensed"   # the 1x reference: Sp-Co, as uniform-and-mixed-i01
MX = 9.4                    # the mix's x, close after the nine (the divider at 8.6)
PT, PT_BAR = 9, 7           # the text size, and the bar labels, in points at one paper column (as uniform-and-mixed-i01)


def ratio_label(v):
    """a ratio to Sp-Co, no "x" (the axis says it); two decimals below 1.1"""
    return f"{v:.2f}" if v < 1.1 else f"{v:.1f}"


def load(data):
    """({option: {form: bill / Sp-Co's exact bill}} for the nine uniform layouts and the mix, Sp-Co's exact bill)"""
    out, ref = {}, None
    for r in read_table(data, "bill-i01"):
        key = r["option"] if r["option_set"] == "single" else "mix"
        if key in out and r["form"] in out[key]:
            raise SystemExit(f"bill-i01.csv: two rows for {key}, {r['form']}")
        out.setdefault(key, {})[r["form"]] = float(r["bill_x_sp_co"])
        if r["option"] == REF and r["form"] == "exact":
            ref = float(r["bill"])
    want = [c for c, _ in CELLS] + ["mix"]
    if sorted(out) != sorted(want) or any(sorted(out[k]) != sorted(FORMS) for k in want):
        raise SystemExit(f"bill-i01.csv: options {sorted(out)}")
    return out, ref


def main():
    data, out_dir = args(__doc__)
    out = out_dir / "figures/evaluation/bill-w16.pdf"
    d, ref = load(data)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig, ax = plt.subplots(figsize=(3.33, 1.65))
    keys = [c for c, _ in CELLS] + ["mix"]
    ex = [d[k]["exact"] for k in keys]
    best = min(ex[:9])                                  # the best uniform layout, as a ratio to Sp-Co
    xs = list(range(9)) + [MX]
    for j, (c, _) in enumerate(CELLS):                  # the cell theme: colour = adjacency, hatch = properties
        ax.bar(j, ex[j], width=0.7, zorder=3, **cell_bar_kw(c))
        if ex[j] <= best * 1.01:                        # the best uniform layout: a black outline
            ax.bar(j, ex[j], width=0.7, zorder=4, fill=False, edgecolor="black", linewidth=0.7)
    ax.bar(MX, ex[9], color=MIX, width=0.7, zorder=3)
    for j, (x, v) in enumerate(zip(xs, ex)):
        mix = j == 9
        ax.text(x, v * 1.06, "1" if keys[j] == REF else ratio_label(v), ha="center", va="bottom", fontsize=PT_BAR,
                color=MIX if mix else "black",
                fontweight="bold" if (v <= best * 1.01 if not mix else v < best * 0.995) else "normal")
    ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
    ax.set_yscale("log")
    ax.set_ylim(min(ex) / 1.4, max(ex) * 1.45)          # room for the labels only
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_ylabel("bill")
    ax.yaxis.set_label_coords(-0.135, 0.5)
    ax.text(-0.088, 0.5, f"Sp-Co = {ref / 1e6:.0f}M", transform=ax.transAxes, rotation=90, ha="center", va="center",
            fontsize=PT_BAR)                            # Sp-Co's bill in request units: the bars are ratios to it
    ax.set_xlim(-0.6, MX + 0.6)
    for x in (2.5, 5.5):                                # between the three adjacency settings
        ax.axvline(x, color="0.5", lw=0.5)
    ax.axvline(8.6, color="0.2", lw=0.7)                # uniform | mixed
    for g, (_, s) in enumerate(SETTINGS):
        ax.text(3 * g + 1, 1.015, f"adj. {s}", transform=ax.get_xaxis_transform(), ha="center", va="bottom")
    ax.set_xticks(xs, [n.split("-")[0] for _, n in CELLS] + ["mix"], rotation=90)
    ax.get_xticklabels()[9].set_color(MIX)
    ax.annotate("properties setting", xy=(4, 0), xycoords=("data", "axes fraction"), xytext=(0, -23),
                textcoords="offset points", ha="center", va="top")   # under the nine's ticks, in points
    ax.tick_params(labelsize=PT, pad=1, length=2, width=0.5)
    ax.tick_params(which="minor", length=1, width=0.4)
    for side in ax.spines.values():
        side.set_linewidth(0.5)
    ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
    fig.subplots_adjust(left=0.16, right=0.997, top=0.89, bottom=0.27)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    print(f"Sp-Co {ref / 1e6:.1f}M request units; best uniform {best:.3f}, the mix {ex[9]:.3f} "
          f"({ex[9] / best:.3f} of the best uniform); " + " ".join(f"{n}={v:.2f}" for (_, n), v in zip(CELLS, ex)))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
