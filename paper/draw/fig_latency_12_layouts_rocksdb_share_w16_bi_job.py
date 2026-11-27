#!/usr/bin/env python3
# released from gs-paper at eb47e87 (evidence/latency-steady-ldbc/fig_latency_12_layouts_rocksdb_share_w16_bi_job.py); edit it there, then run evidence/release_draw.py
"""Paper figure: steady latency of the 12 layouts timed on all three workloads, each row over the layout with the fewest
combined cost, split into the time inside RocksDB and the rest; one column, linear.

  figures/evaluation/latency-12-layouts-rocksdb-share-w16-bi-job.pdf

Rows: (a) LDBC Interactive at SF3 as W16 (its 19 reads and the updates IU1-8, each at W16's frequency), (b) LDBC BI's 17
queries at SF3, (c) JOB's 113 queries on IMDb; BI and JOB weight each query 1 / its combined cost on Sp-Co (equal share).
A bar is the store's steady wall (per query the mean over its params of the mean over measured passes 1-9; an update's
one apply per param; summed at the row's weights) over the row's pick: of the 12, the layout with the fewest combined
cost (wops + bytes / 4,096, from the same table; outlined, labelled 1; the same layout as the fewest of all 49 uniform
layouts in uniform-49-layouts-workloads). Bars grouped by the adjacency setting (named above (a), every other group on
a light background), the properties setting as the tick label (no hatch: it hid the split). The time in RocksDB in
the adjacency colour, the rest a lighter tint. Dgraph's layout (Sp-Co) and NebulaGraph's (Co-Sp) are marked DG and NG (paper_style's engine marks): the name after the tick label, the bar
outlined in the engine's colour.

The split: the lower part is the share of the store's time spent inside RocksDB calls, from the KV-timer passes (every
KV call timed at the RocksDB API, the timer's own cost taken out), applied to the steady bar: RocksDB / (RocksDB + the
rest of the steps that read the store + the other steps). Interactive's share is its 19 reads' (the updates were not
KV-timed).

Reads only its tables: latency-steady-ldbc.csv (Interactive, BI), latency-steady-job.csv (JOB; its controls left out),
latency-steady-rocksdb-parts.csv (per workload and store: rocksdb_ns, kv_other_ns, non_kv_ns, summed at the row's
weights).

    python fig_latency_12_layouts_rocksdb_share_w16_bi_job.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullLocator  # noqa: E402

from matplotlib.legend_handler import HandlerTuple  # noqa: E402
from common import args, read_table, use_paper_font  # noqa: E402
from fig_latency_steady_common_set import LAYOUTS, REFERENCE  # noqa: E402
from paper_style import (CELL_SHORT, COLUMN_IN, cell_id, cell_parts, engine_legend, colour_engine_legend,  # noqa: E402
                         mark_engine_ticks, outline_engine_bar, swatch, two_tones)

OUT = "figures/evaluation/latency-12-layouts-rocksdb-share-w16-bi-job.pdf"
PT, PT_SMALL = 7, 6.5                       # titles and axis names; values, ticks, legend, group names
ROCKS, REST = "#3f5f8a", "#a9c4e4"
GAP = 1.3                                  # between adjacency groups, in bar widths
ROWS = (("interactive", "(a) Interactive"), ("bi", "(b) BI"), ("job", "(c) JOB"))


def setting(part):
    """'Ch1k' for ('chunked', 1); Chunked at 64 KB is 'Ch64k'"""
    p, kb = part
    return CELL_SHORT[p] + (f"{kb}k" if kb else "")


def values(data):
    """per workload: the 12 wall ratios over its pick, the pick's index, the 12 RocksDB shares; refuses a missing
    store or a reference off 1"""
    ldbc = read_table(data, "latency-steady-ldbc")
    job = [r for r in read_table(data, "latency-steady-job") if "control" not in r["timed"]]
    wall = {(r["workload"], r["store"]): float(r["wall_ratio"]) for r in ldbc}
    wall.update({("job", r["store"]): float(r["share_wall_ratio"]) for r in job})
    cost = {(r["workload"], r["store"]): float(r["cost_ratio"]) for r in ldbc}
    cost.update({("job", r["store"]): float(r["share_cost_ratio"]) for r in job})
    share = {}
    for r in read_table(data, "latency-steady-rocksdb-parts"):
        parts = [float(r[k]) for k in ("rocksdb_ns", "kv_other_ns", "non_kv_ns")]
        share[(r["workload"], r["store"])] = parts[0] / sum(parts)
    out = {}
    for wl, _ in ROWS:
        missing = [s for s in LAYOUTS if (wl, s) not in wall or (wl, s) not in share]
        if missing or abs(wall[(wl, REFERENCE)] - 1) > 1e-9:
            raise SystemExit(f"{wl}: missing {missing}, or its reference is not at 1")
        pick = min(range(len(LAYOUTS)), key=lambda i: cost[(wl, LAYOUTS[i])])
        ref = wall[(wl, LAYOUTS[pick])]
        out[wl] = ([wall[(wl, s)] / ref for s in LAYOUTS], pick, [share[(wl, s)] for s in LAYOUTS])
    return out


def positions():
    """x of each layout, and the groups as [adjacency name, first x, last x]"""
    xs, groups, x = [], [], 0.0
    for c in LAYOUTS:
        adj = setting(cell_parts(c)[1])
        if groups and adj != groups[-1][0]:
            x += GAP
        if not groups or adj != groups[-1][0]:
            groups.append([adj, x, x])
        groups[-1][2] = x
        xs.append(x)
        x += 1
    return xs, groups


def draw_row(ax, v, pick, shares, xs, groups, first):
    for g, (name, a, b) in enumerate(groups):
        if g % 2:
            ax.axvspan(a - 0.5 - GAP / 2, b + 0.5 + GAP / 2, color="0.94", lw=0, zorder=0)
        if first:
            ax.text((a + b) / 2, 1.02, name, transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                    fontsize=PT_SMALL, fontweight="bold", color=two_tones(LAYOUTS[xs.index(a)])[0])
    for i, (x, val, s) in enumerate(zip(xs, v, shares)):
        rocks, rest = two_tones(LAYOUTS[i])
        ax.bar(x, val * s, width=0.8, zorder=3, color=rocks, lw=0)
        ax.bar(x, val * (1 - s), bottom=val * s, width=0.8, zorder=3, color=rest, lw=0)
        ax.plot([x - 0.4, x + 0.4], [val * s] * 2, color="white", lw=0.4, zorder=3.5)   # the split between the parts
        outline_engine_bar(ax, x, val, LAYOUTS[i], 0.8)
        if i == pick:
            ax.bar(x, val, width=0.8, fill=False, edgecolor="black", lw=0.8, zorder=4)
        ax.text(x, val + max(v) * 0.03, "1" if i == pick else f"{val:.2f}", ha="center", va="bottom",
                fontsize=PT_SMALL, rotation=0 if i == pick else 90, color="0.1", fontweight="bold" if i == pick else "normal")
    ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
    ax.set_ylim(0, max(v) * 1.3)
    ax.yaxis.set_major_locator(FixedLocator([0, 1, 2, 3, 4] if max(v) > 2.2 else [0, 0.5, 1, 1.5, 2]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda t, _: f"{t:g}"))
    ax.yaxis.set_minor_locator(NullLocator())
    ax.set_xlim(xs[0] - 0.75, xs[-1] + 1.15)            # right: room for the last group's name
    ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=1)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)


def main():
    data, out_dir = args(__doc__)
    vals = values(data)
    xs, groups = positions()
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    H = 2.68                                   # inches: the three rows, the tick labels
    fig, axes = plt.subplots(3, 1, figsize=(COLUMN_IN, H), sharex=True)
    fig.subplots_adjust(left=0.12, right=0.995, top=1 - 0.1375 / H, bottom=0.31 / H, hspace=0.08)
    for i, (ax, (wl, label)) in enumerate(zip(axes, ROWS)):
        v, pick, shares = vals[wl]
        draw_row(ax, v, pick, shares, xs, groups, i == 0)
        ax.set_ylabel(f"{label}\nover the pick", fontsize=PT, labelpad=1, linespacing=1.0)
        print(f"{wl}: over {LAYOUTS[pick]}; fastest {LAYOUTS[v.index(min(v))]} {min(v):.3f}; RocksDB share "
              f"{min(shares):.0%}-{max(shares):.0%}")
    axes[-1].set_xticks(xs, [setting(cell_parts(c)[0]) for c in LAYOUTS], rotation=90)
    axes[-1].tick_params(axis="x", labelsize=PT_SMALL, length=0)
    mark_engine_ticks(fig, axes[-1], LAYOUTS)
    adj_cells = [cell_id("split", a) for a in ("split", "condensed", "chunked_c1k", "chunked_c4k", "chunked_c16k",
                                                "chunked", "chunked_c256k")]
    kw = dict(frameon=False, fontsize=PT_SMALL, columnspacing=0.8, handletextpad=0.3, borderaxespad=0,
              borderpad=0)
    first = axes[0].legend([swatch(two_tones(c)[0] for c in adj_cells), swatch(two_tones(c)[1] for c in adj_cells)],
                           ["in RocksDB", "the rest"], handler_map={tuple: HandlerTuple(ndivide=None, pad=0)}, ncol=2,
                           loc="upper right", bbox_to_anchor=(0.99, 0.97), handlelength=3.2, **kw)
    eh, el = engine_legend()
    axes[0].add_artist(first)
    colour_engine_legend(axes[2].legend(eh, el, ncol=1, loc="upper left", bbox_to_anchor=(0.01, 0.97),
                                        handlelength=0, labelspacing=0.2, **{**kw, "handletextpad": 0}))   # (c)'s empty corner
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
