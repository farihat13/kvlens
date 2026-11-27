#!/usr/bin/env python3
# released from gs-paper at ebdd331 (evidence/latency-steady-ldbc/fig_latency_steady_common_set.py); edit it there, then run evidence/release_draw.py
"""Paper figure: steady latency over Sp-Co of the 15 stores timed on all three workloads, one column.

  figures/evaluation/latency-steady-common-set.pdf

Three rows on one x axis: (a) LDBC Interactive at SF3 as W16 (its 19 reads and the updates IU1-8, each at W16's frequency), (b) LDBC BI's 17 queries at SF3,
(c) JOB's 113 queries on IMDb; BI and JOB weight each query 1 / its combined cost on Sp-Co (equal share). The 15 stores:
12 layouts ordered by the adjacency setting (a faint line between settings), then three mixes over the 7 settings per
type: the fewest combined cost from measured costs, the same from predicted costs, and the fewest bill. A bar is the
store's steady wall over Sp-Co's (per query the mean over its params of the mean over measured passes 1-9; an update's
one apply per param; summed at the row's weights). Log y.

Reads only its tables: latency-steady-ldbc.csv (Interactive, BI) and latency-steady-job.csv (JOB; its controls left out).

    python fig_latency_steady_common_set.py --data <dir of the tables> --out <dir>
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter  # noqa: E402

from common import args, read_table, use_paper_font  # noqa: E402
from paper_style import CELL_SHORT, COLUMN_IN, cell_bar_kw, cell_parts  # noqa: E402

OUT = "figures/evaluation/latency-steady-common-set.pdf"
PT, PT_SMALL = 7, 6
REFERENCE = "props_split_adj_condensed"
# the 12 layouts, adjacency-major (properties inside); the stores timed on all three workloads
LAYOUTS = ["props_split_adj_split", "props_condensed_adj_split",
           "props_split_adj_condensed", "props_condensed_adj_condensed",
           "props_chunked_c1k_adj_chunked_c1k",
           "props_chunked_c1k_adj_chunked_c4k", "props_chunked_c4k_adj_chunked_c4k",
           "props_chunked_c1k_adj_chunked_c16k", "props_chunked_c4k_adj_chunked_c16k",
           "props_chunked_c16k_adj_chunked_c16k",
           "props_chunked_adj_chunked",
           "props_chunked_c256k_adj_chunked_c256k"]
MIXES = (("measured_mix_any_size", "mix", "#d81b60", "#d81b60"), ("predicted_mix_any_size", "pred. mix", "#f48fb1", "#c2185b"),
         ("bill_mix_any_size", "bill mix", "#880e4f", "#880e4f"))   # suffix, label, bar colour, text colour
ROWS = (("interactive", "(a) Interactive", "lat_i02_"), ("bi", "(b) BI", "lat_b01_"), ("job", "(c) JOB", "lat_j01_"))


def short(cell):
    """'Ch1k-Ch16k': the properties then the adjacency setting (Chunked at 64 KB is 'Ch64k')"""
    return "-".join(CELL_SHORT[p] + (f"{kb}k" if kb else "") for p, kb in cell_parts(cell))


def adjacency(cell):
    return cell_parts(cell)[1]


def values(data):
    """per workload, the 15 wall ratios in drawing order; refuses a missing store or a reference off 1"""
    ldbc = read_table(data, "latency-steady-ldbc")
    job = [r for r in read_table(data, "latency-steady-job") if "control" not in r["timed"]]
    wall = {(r["workload"], r["store"]): float(r["wall_ratio"]) for r in ldbc}
    wall.update({("job", r["store"]): float(r["share_wall_ratio"]) for r in job})
    out = {}
    for wl, _, prefix in ROWS:
        stores = LAYOUTS + [prefix + m for m, _, _, _ in MIXES]
        missing = [s for s in stores if (wl, s) not in wall]
        if missing or abs(wall[(wl, REFERENCE)] - 1) > 1e-9:
            raise SystemExit(f"{wl}: missing {missing}, or its reference is not at 1")
        out[wl] = [wall[(wl, s)] for s in stores]
    return out


def draw_row(ax, v):
    n = len(LAYOUTS)
    floor, top = min(min(v) / 1.25, 0.55), max(v) * 1.6
    for x, val in enumerate(v):
        if x < n:
            kw, colour = cell_bar_kw(LAYOUTS[x]), "0.1"
        else:
            kw, colour = {"color": MIXES[x - n][2]}, MIXES[x - n][3]
        with plt.rc_context({"hatch.linewidth": 0.6}):
            ax.bar(x, val, width=0.8, zorder=3, **kw)
        ax.text(x, val * 1.05, f"{val:.2f}", ha="center", va="bottom", fontsize=PT_SMALL, rotation=90, color=colour)
    for x in range(1, n):
        if adjacency(LAYOUTS[x]) != adjacency(LAYOUTS[x - 1]):
            ax.axvline(x - 0.5, color="0.75", lw=0.4, zorder=1)              # between adjacency settings
    ax.axvline(n - 0.5, color="0.2", lw=0.6)                                 # the layouts, then the mixes
    ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
    ax.set_yscale("log")
    ax.set_ylim(floor, top)
    ax.set_xlim(-0.6, len(v) - 0.4)
    ax.yaxis.set_major_locator(FixedLocator([t for t in (0.5, 0.7, 1, 1.5, 2, 3, 4) if floor <= t <= top]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda t, _: f"{t:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)


def main():
    data, out_dir = args(__doc__)
    vals = values(data)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig, axes = plt.subplots(3, 1, figsize=(COLUMN_IN, 3.0), sharex=True)
    fig.subplots_adjust(left=0.115, right=0.995, top=0.995, bottom=0.2, hspace=0.08)
    for ax, (wl, label, _) in zip(axes, ROWS):
        draw_row(ax, vals[wl])
        ax.set_ylabel(f"{label}\nover Sp-Co", fontsize=PT, labelpad=1, linespacing=1.0)
        lay = vals[wl][:len(LAYOUTS)]
        print(f"{wl}: 15 stores; fastest layout {short(LAYOUTS[lay.index(min(lay))])} {min(lay):.3f}, "
              f"mix {vals[wl][len(LAYOUTS)]:.3f}")
    labels = [short(c) for c in LAYOUTS] + [name for _, name, _, _ in MIXES]
    axes[-1].set_xticks(range(len(labels)), labels, rotation=90)
    axes[-1].tick_params(axis="x", labelsize=PT_SMALL)
    for t, (_, _, _, colour) in zip(axes[-1].get_xticklabels()[len(LAYOUTS):], MIXES):
        t.set_color(colour)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
