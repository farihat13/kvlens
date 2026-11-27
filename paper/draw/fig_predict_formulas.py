#!/usr/bin/env python3
"""predict-formulas (optional, not in the paper): predict mode's error on one workload's total (its reads and its writes),
per layout and level; one text-wide figure, a panel per lane group.

  figures/predict-formulas.pdf (and .png)

The lane groups of workload_totals.csv: KV operations (S + G + N + P + D, unweighted), bytes read (the 7 read lanes),
bytes written and the op-byte cost (wops_bytes4k), a write's lanes under the half chunk view. One
row per layout of the seven; one bar per level of the chain (true_chunks, true_terms, true_counts, predicted: each takes
one kind of term less from the step's truth card): the workload total's error, |T_level - T_measured| / T_measured. The x
axis is symmetric-log in percent (linear below 1%). The line under the panels is the sum check and the exact gate's line
from summary.md's first line.

Reads only its tables (kvcost.predict_accuracy.predict_formulas): workload_totals.csv and summary.md.

    python fig_predict_formulas.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from common import args, read_table, sf_label, use_paper_font  # noqa: E402
from paper_style import PRINT_PT, SMALL, TEXT_IN  # noqa: E402

OUT = "figures/predict-formulas"


def main():
    data, out = args(__doc__)
    use_paper_font()
    rows = [r for r in read_table(data, "workload_totals") if r["scope"] == "workload"]   # the whole workload
    first = (data / "summary.md").read_text().splitlines()[0]
    wid = rows[0]["workload"]
    lays = list(dict.fromkeys(r["layout"] for r in rows))
    levels = list(dict.fromkeys(r["level"] for r in rows))
    groups = list(dict.fromkeys(r["lane_group"] for r in rows))   # one panel each, in the table's order
    # the chain's levels, light (most from the truth card) to dark
    shades = [plt.get_cmap("Greys")(0.2 + 0.75 * i / max(1, len(levels) - 1)) for i in range(len(levels))]
    by = {(r["layout"], r["level"], r["lane_group"]): r for r in rows}
    errs = [100 * float(r["error"]) for r in rows if r["error"]]
    top = 2 * max([100.0, *errs])                       # past the largest error: none is clipped
    h = 0.22 * len(lays) * len(levels) / 2 + 0.9
    fig, axes = plt.subplots(1, len(groups), figsize=(TEXT_IN, h), sharey=True)
    fig.subplots_adjust(left=0.1, right=0.99, top=1 - 0.3 / h, bottom=0.62 / h, wspace=0.06)
    step = 0.8 / len(levels)
    ys = list(range(len(lays)))[::-1]
    for k, (ax, group) in enumerate(zip(axes, groups)):
        for y, lay in zip(ys, lays):
            for i, lv in enumerate(levels):
                r = by.get((lay, lv, group))
                if r and r["error"]:
                    ax.barh(y + 0.4 - step * (i + 0.5), 100 * float(r["error"]), height=step * 0.9, color=shades[i],
                            linewidth=0)
        ax.set_xscale("symlog", linthresh=1)
        ax.set_xlim(0, top)
        ticks = [t for t in (0, 1, 10, 100, 1000, 10000) if t <= top]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f"{t:,}%" if t else "0" for t in ticks])
        ax.tick_params(labelsize=PRINT_PT * SMALL, pad=1.5, length=2)
        ax.set_title(f"({chr(97 + k)}) {group}, {wid.upper()} at {sf_label()}", fontsize=PRINT_PT, pad=3)
        ax.grid(axis="x", lw=0.3, color="#cccccc")
        ax.set_axisbelow(True)
    axes[0].set_yticks(ys)
    axes[0].set_yticklabels(lays, fontsize=PRINT_PT * SMALL)
    axes[0].set_ylim(-0.6, len(lays) - 0.4)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for c in shades]
    fig.legend(handles, levels, loc="lower center", bbox_to_anchor=(0.55, 0.22 / h), ncol=len(levels),
               fontsize=PRINT_PT * SMALL, frameon=False, handlelength=1.2)
    fig.text(0.55, 0.005, "; ".join(first.split("; ")[-2:]), ha="center", va="bottom", fontsize=PRINT_PT * SMALL)
    path = out / OUT
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path.with_suffix(".pdf"), dpi=300)
    fig.savefig(path.with_suffix(".png"), dpi=200)
    print(f"wrote {path.with_suffix('.pdf')} and .png")


if __name__ == "__main__":
    main()
