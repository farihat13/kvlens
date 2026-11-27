"""One figure shape shared by the studies: what each database reads from its KV store on a few queries."""
import matplotlib
import matplotlib.pyplot as plt

from kvcost.plotting.formatting import bytes_to_human, number_to_human
from kvcost.plotting.style import apply_house_style, bar_kw, db_label, op_color, op_label

OPS = ("G", "S", "N")                              # stack order, bottom up
BYTES = (("G", "GET bytes"), ("N", "scan bytes"))


def db_reads_grid(data, titles, dbs, by_op=False, normalize=False):
    """A 2 x len(titles) grid: top row KV operations, bottom row bytes read, one bar per database.

    data[db][query] = ({"G", "S", "N": count}, {"G", "N": bytes}); titles = {query: panel title}, in panel order.
    by_op stacks each bar by operation (bytes: GET, scan) instead of colouring it by database. normalize draws every
    panel as a share of its largest bar (= 100%), one shared axis per row; the bars keep their absolute labels.
    Returns the figure."""
    apply_house_style(extralarge=1)
    fig, axes = plt.subplots(2, len(titles), figsize=(3.4 * len(titles) + (1.7 if by_op else 0), 4.4),
                             constrained_layout=True, squeeze=False, sharey="row" if normalize else False)
    for j, q in enumerate(titles):
        for row, kinds in enumerate((OPS, [o for o, _ in BYTES])):
            ax = axes[row][j]
            totals = [sum(data[db][q][row][o] for o in kinds) for db in dbs]
            # bytes: one unit per panel, so the ticks are plain numbers
            div, unit = (1, "") if row == 0 else next((d, u) for d, u in ((2**20, "MB"), (2**10, "KB"), (1, "B"))
                                                      if max(totals) >= d)
            if normalize:
                div = max(totals) / 100
            for i, db in enumerate(dbs):
                vals = data[db][q][row]
                if by_op:
                    bottom = 0
                    for o in kinds:
                        ax.bar(i, vals[o] / div, 0.62, bottom=bottom, color=op_color(o), edgecolor="white", linewidth=0.6)
                        bottom += vals[o] / div
                else:
                    bottom = totals[i] / div
                    ax.bar(i, bottom, 0.62, **bar_kw(db))
                label = (number_to_human(totals[i]) if row == 0 else
                         f"{totals[i]:.0f} B" if totals[i] < 1024 else bytes_to_human(totals[i]))
                ax.text(i, bottom, label, ha="center", va="bottom")
            ax.set_ylim(0, max(totals) / div * 1.18)
            ax.grid(axis="y", color="0.85", linewidth=0.8)
            ax.set_axisbelow(True)
            ax.set_xticks(range(len(dbs)))
            ax.set_xticklabels([db_label(d) for d in dbs] if row == 1 else [""] * len(dbs), rotation=20, ha="right")
            if normalize:
                ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=0))
            elif row == 0:
                ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: number_to_human(v) if v >= 10000 else f"{v:g}"))
            if j == 0:                 # no unit on the bytes axis: each bar carries its value with the unit
                ax.set_ylabel((("KV operations" if by_op else "KV pairs read") if row == 0 else "bytes read")
                              + ("\n(% of the most)" if normalize else ""))
            if row == 0:
                ax.set_title(f"Q{j + 1}: {titles[q]}")     # numbered, so the text can say "on Q2"
    if by_op:
        for row, (keys, labels) in enumerate(((OPS, [op_label(o) for o in OPS]), zip(*BYTES))):
            handles = [plt.Rectangle((0, 0), 1, 1, color=op_color(o)) for o in keys]
            axes[row][-1].legend(handles[::-1], list(labels)[::-1], loc="upper left", bbox_to_anchor=(1.02, 1.0), frameon=False)
    fig.align_ylabels(axes[:, 0])
    return fig
