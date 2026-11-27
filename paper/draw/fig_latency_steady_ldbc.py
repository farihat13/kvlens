#!/usr/bin/env python3
# released from gs-paper at ebdd331 (evidence/latency-steady-ldbc/fig_latency_steady_ldbc.py); edit it there, then run evidence/release_draw.py
"""Paper figure: steady latency of the timed layouts over Sp-Co in the 49 slots, LDBC Interactive and BI at SF3, one column.

  figures/evaluation/latency-steady-ldbc.pdf

Two rows on one x axis: (a) LDBC Interactive as W16 (its 19 reads and the updates IU1-8, each at W16's frequency), (b) LDBC BI's 17 queries, each weighted
1 / its combined cost on Sp-Co (the equal-share form of uniform-49-layouts-workloads). The 49 slots are grouped by the
adjacency setting, the properties setting inside; a slot timed in either row is wide, the others narrow with a grey dot (not timed). A bar
is the store's steady wall time over Sp-Co's (per query the mean over params of the mean over measured passes 1-9;
an update's one apply per param; summed at the row's weights); the mix (the measured pick of each workload) is at the right. Log y.

Reads only its table, latency-steady-ldbc.csv (one row per workload and timed store: the ratios over Sp-Co).

    python fig_latency_steady_ldbc.py --data <dir of the table> --out <dir>
"""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, NullFormatter  # noqa: E402

from common import args, read_table, use_paper_font  # noqa: E402
from paper_style import CELL_SHORT, COLUMN_IN, cell_bar_kw, cell_id, cell_parts  # noqa: E402

NAME = "latency-steady-ldbc"
OUT = "figures/evaluation/latency-steady-ldbc.pdf"
SETTINGS = [("split", "Sp"), ("condensed", "Co"), ("chunked_c1k", "Ch1k"), ("chunked_c4k", "Ch4k"),
            ("chunked_c16k", "Ch16k"), ("chunked", "Ch64k"), ("chunked_c256k", "Ch256k")]
PT, PT_SMALL = 7, 6
MIX = "#d81b60"                                  # the mix's colour, as in uniform-49-layouts-workloads
ROWS = (("interactive", "(a) Interactive", "lat_i02_measured_mix_any_size", 15),
        ("bi", "(b) BI", "lat_b01_measured_mix_any_size", 15))           # workload, label, the mix drawn, timed stores expected
WIDE, NARROW, GROUP_GAP = 1.0, 0.32, 0.45                     # a timed slot, an untimed one, between groups
REFERENCE = "props_split_adj_condensed"


def short_props(cell):
    """the properties setting's short name of a cell ('Ch4k'; Chunked at 64 KB is 'Ch64k')"""
    (p, kb), _ = cell_parts(cell)
    return CELL_SHORT[p] + (f"{kb}k" if kb else "")


def slots(timed):
    """the 49 cells adjacency-major, their x centres, and each adjacency group's (start, end)"""
    names = [cell_id(p, a) for a, _ in SETTINGS for p, _ in SETTINGS]
    xs, edges, x = [], [], 0.0
    for g in range(len(SETTINGS)):
        start = x
        for i in range(len(SETTINGS)):
            w = WIDE if names[g * len(SETTINGS) + i] in timed else NARROW
            xs.append(x + w / 2)
            x += w
        edges.append((start, x))
        x += GROUP_GAP
    return names, xs, edges, x + 0.9


def draw_row(ax, v, mix, names, xs, edges, mix_x):
    lo, hi = min(v.values()), max(v.values())
    floor, top = lo / 1.2, hi * 1.75
    for x, c in zip(xs, names):
        if c not in v:
            ax.plot([x], [floor * 1.1], marker=".", color="0.7", ms=1.5, zorder=2)        # a slot not timed
            continue
        with plt.rc_context({"hatch.linewidth": 0.6}):
            ax.bar(x, v[c], width=0.86 * WIDE, zorder=3, **cell_bar_kw(c))
        ax.text(x, v[c] * 1.05, f"{v[c]:.2f}", ha="center", va="bottom", fontsize=PT_SMALL, rotation=90)
    ax.bar(mix_x, v[mix], width=1.2, zorder=3, color=MIX)
    ax.text(mix_x, v[mix] * 1.05, f"{v[mix]:.2f}", ha="center", va="bottom", fontsize=PT_SMALL, color=MIX,
            fontweight="bold", rotation=90)
    ax.axvline(edges[-1][1] + GROUP_GAP / 2, color="0.2", lw=0.6)
    for g in range(1, len(SETTINGS)):
        ax.axvline(edges[g][0] - GROUP_GAP / 2, color="0.6", lw=0.4)
    ax.axhline(1, color="0.4", lw=0.5, ls=":", zorder=2)
    ax.set_yscale("log")
    ax.set_ylim(floor, top)
    ax.yaxis.set_major_locator(FixedLocator([t for t in (0.5, 0.7, 1, 1.5, 2, 3) if floor <= t <= top]))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda t, _: f"{t:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.grid(axis="y", alpha=0.3, lw=0.4, zorder=0)
    ax.tick_params(labelsize=PT_SMALL, pad=1, length=2, width=0.5)
    for side in ax.spines.values():
        side.set_linewidth(0.5)


def main():
    data, out_dir = args(__doc__)
    table = read_table(data, NAME)
    vals = {}
    for wl, _, mix, want in ROWS:
        rows = [r for r in table if r["workload"] == wl]
        if len(rows) != want:
            raise SystemExit(f"{NAME}.csv: {wl} has {len(rows)} stores, want {want}")
        vals[wl] = {r["store"]: float(r["wall_ratio"]) for r in rows if r["kind"] == "layout" or r["store"] == mix}
        if abs(vals[wl][REFERENCE] - 1) > 1e-9 or mix not in vals[wl]:
            raise SystemExit(f"{NAME}.csv: {wl}'s reference is not at 1, or its mix {mix} is missing")
    timed = {c for v in vals.values() for c in v}
    names, xs, edges, mix_x = slots(timed)
    use_paper_font()
    plt.rcParams.update({"font.size": PT})
    fig, axes = plt.subplots(2, 1, figsize=(COLUMN_IN, 2.25), sharex=True)
    fig.subplots_adjust(left=0.115, right=0.995, top=0.875, bottom=0.175, hspace=0.1)
    for ax, (wl, label, mix, _) in zip(axes, ROWS):
        draw_row(ax, vals[wl], mix, names, xs, edges, mix_x)
        ax.set_ylabel(f"{label}\nover Sp-Co", fontsize=PT, labelpad=1, linespacing=1.0)
        print(f"{wl}: {len(vals[wl]) - 1} timed layouts and the mix ({vals[wl][mix]:.3f}); fastest layout "
              f"{min((v, c) for c, v in vals[wl].items() if c != mix)[1]}")
    top = axes[0]
    for (lo, hi), (_, s) in zip(edges, SETTINGS):
        top.text((lo + hi) / 2, 1.02, s, transform=top.get_xaxis_transform(), ha="center", va="bottom",
                 fontsize=PT_SMALL, fontweight="bold")
    top.text(mix_x, 1.02, "mix", transform=top.get_xaxis_transform(), ha="center", va="bottom", fontsize=PT_SMALL,
             fontweight="bold", color=MIX)
    top.text(0.5, 1.13, "adjacency setting", transform=top.transAxes, ha="center", va="bottom", fontsize=PT)
    ticks = [x for x, c in zip(xs, names) if c in timed]
    axes[1].set_xticks(ticks + [mix_x], [short_props(c) for c in names if c in timed] + ["mix"], rotation=90)
    axes[1].tick_params(axis="x", labelsize=PT_SMALL)
    axes[1].set_xlim(-0.2, mix_x + 0.9)
    fig.text(0.55, 0.0, "properties setting", ha="center", va="bottom", fontsize=PT)
    out = out_dir / OUT
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
