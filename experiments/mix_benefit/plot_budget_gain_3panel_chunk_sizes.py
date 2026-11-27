#!/usr/bin/env python3
"""budget-gain.png's three panels (layout_choice/plot_budget_sweep.py) with one line per chunk size of the Chunked
setting, and one thicker line "any chunk size" (owner: mix_benefit). 1: the reference layout (--ref, default Sp-Co,
python/kvcost/layout_cost/settings.py) over the best uniform layout that fits the byte limit; 2: the best uniform layout over the best mix,
same limit; 3: the reference over the best mix
(= 1 x 2). A size's line uses that size's uniform layouts and that size's mix; "any chunk size" takes the best uniform
layout of any drawn size and the lowest mix of any drawn size. Reads equal_bytes_front.py's CSVs only.

    python3 experiments/mix_benefit/plot_budget_gain_3panel_chunk_sizes.py --workload i01 \\
        --front "64 KB=<front dir>" --front "16 KB=<front dir>" --front "4 KB=<front dir>" \\
        [--by-direction] [--more-front "LABEL=<front dir>"] [--ref CELL] [--out DIR] [--source TEXT]
B, the byte limit, stays in units of the front's B = 1 (cells.B_UNIT's bytes, Sp-Co; Co-Sp's in a front in Co-Sp units),
whatever --ref is.
--more-front: one more mix from another solve (its parts may each take their own chunk size), read for the same
the mixes' freedom (per type, or per type and direction with --by-direction): its own line in panels 2 and 3, against the best uniform layout of any drawn size, so the gap to the
thick line (the lowest mix of ONE size) is what a size per part adds. Its legend says whether it is exact.
Output: <out>/budget-gain-3panel-chunk-sizes[-per-direction]_<workload dir>.{png,pdf} and
budget_gain_3panel_chunk_sizes[_per_direction]_<workload id>.csv
"""
import argparse
import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

import plot_equal_bytes_curve_chunk_sizes as E
from kvcost.plotting.formatting import decimal_bytes, short_bytes  # (the budget figure's markers, axis and text helpers)
from kvcost.plotting.style import apply_house_style, caption, log_axis  # (the budget figure's markers, axis and text helpers)
from kvcost.layout_cost.labels import B_STYLE, gain_text  # (the budget figure's markers, axis and text helpers)
from kvcost.layout_cost import settings as cells  # (the reference layout and the cells' short names)
from kvcost.layout_search.front import ref_bytes_of
from kvevaluator.output_tree import Tree

SETS = {"per type": ("", "2. Mixing per type", "a layout per vertex type and per edge type"),
        "per type and direction": ("-per-direction", "2. Mixing per type and direction", "a layout per vertex type and per edge type and list direction")}
ANY, ANY_COLOR, ANY_LW = "any chunk size", "#e7298a", 3.6
UNIT = None                    # the fronts' B unit (one per figure): its named limits and x range are E.BY_UNIT's


def style(b):
    """a named limit's (colour, marker); B = 0.5, a named limit only in a front in Co-Sp units, keeps its own colour"""
    return B_STYLE.get(b, ("#253494", "v"))


def load(front_dir, wdir, option_set, reference):
    """(B = 1 in bytes, [(cap, best uniform ops or None)], [(cap, mix ops)], (the reference's ops, its bytes) or None)"""
    rows = E.read(front_dir, wdir, "equal_bytes_front.csv")
    global UNIT
    ref, unit = ref_bytes_of(rows)       # B = 1 in bytes: Sp-Co's (Co-Sp's in a file in Co-Sp units)
    if UNIT not in (None, unit):
        raise SystemExit(f"{front_dir}/{wdir}: B in {cells.name_of(unit)}'s bytes, the first front's in {cells.name_of(UNIT)}'s")
    UNIT = unit
    mix = sorted((float(r["cap_bytes"]), float(r["total_ops"]), float(r["best_single_ops"]) if r["best_single_ops"] else None)
                 for r in rows if r["option_set"] == option_set and r["option"] == "fewest_ops_within_cap")
    if not mix:
        raise SystemExit(f"{front_dir}/{wdir}: no '{option_set}' rows")
    path = os.path.join(front_dir, wdir, "uniform_layouts.csv")
    cs = next(((float(r["total_ops"]), float(r["total_bytes"])) for r in E.read(path) if r["cell"] == reference), None) if os.path.exists(path) else None
    return ref, [(c, s) for c, _, s in mix if s], [(c, o) for c, o, _ in mix], cs


def at(pts, c):
    """a step line's value at c: the last point at or left of c (None before the first)"""
    v = None
    for x, y in pts:
        if x <= c * (1 + 1e-9):
            v = y
    return v


def lowest(lines, c):
    vals = [v for v in (at(p, c) for p in lines) if v is not None]
    return min(vals) if vals else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--workload", default="i01", choices=sorted(E.WORKLOADS))
    ap.add_argument("--front", action="append", default=[], metavar="SIZE=DIR", help="a chunk size's front; repeatable, drawn in this order")
    ap.add_argument("--more-front", action="append", default=[], metavar="LABEL=DIR", help="one more mix from another front; repeatable")
    ap.add_argument("--by-direction", action="store_true", help="the mixes per type and direction (search-mix "
                    "--by-direction); default per type, what budget-gain.png draws")
    ap.add_argument("--ref", default=cells.REF, choices=cells.CELLS, help="the layout panels 1 and 3 are over (default Sp-Co)")
    ap.add_argument("--out", default=str(Tree().summary("ldbc", "3", "chunk-size-report", "plots")))
    ap.add_argument("--source", default="Counts are deterministic (no error bars).", help="the caption's last sentences: which data")
    a = ap.parse_args()
    a.option_set = "per type and direction" if a.by_direction else "per type"     # the fronts' rows name it so
    wdir, title = E.WORKLOADS[a.workload]
    tag, head2, mix_is = SETS[a.option_set]
    sizes = [x.split("=", 1) for x in a.front]
    if not sizes or any(s not in E.COLORS for s, _ in sizes):
        raise SystemExit(f"--front SIZE=DIR with SIZE one of {', '.join(E.COLORS)}")
    apply_house_style()
    rname = cells.name_of(a.ref)
    loaded = [(size, E.COLORS[size], 2.0, *load(d, wdir, a.option_set, a.ref)) for size, d in sizes]
    refs, cs = {round(r, 0) for _, _, _, r, _, _, _ in loaded}, {(round(c[0], 3), round(c[1], 0)) for *_, c in loaded if c}
    if len(refs) != 1 or len(cs) != 1:
        raise SystemExit(f"the fronts disagree on B = 1 ({sorted(refs)}) or on {rname} ({sorted(cs)}), or none has uniform_layouts.csv")
    ref, (cs_ops, rb) = loaded[0][3], cs.pop()           # ref: B = 1 in bytes (UNIT's); rb: the reference layout's own bytes
    NAMED, _, xmax = E.BY_UNIT[UNIT]                     # the named limits; the x range's end in B
    uname = cells.name_of(UNIT)
    more, full = [], {}                                  # full: the label with its exactness, for the legend
    for label, d in (x.split("=", 1) for x in a.more_front):
        r, _, mix, _ = load(d, wdir, a.option_set, a.ref)
        if round(r, 0) not in refs:
            raise SystemExit(f"{label}: its B = 1 is not the other fronts'")
        more.append((label, mix))
        full[label] = E.named(label, d, wdir, a.option_set)
    # "any chunk size", at every limit where a line steps
    singles, mixes = [s for *_, s, _, _ in loaded], [m for *_, m, _ in loaded]
    caps = sorted({c for line in singles + mixes + [m for _, m in more] for c, _ in line})
    lines = loaded + [(ANY, ANY_COLOR, ANY_LW, ref, [(c, lowest(singles, c)) for c in caps if lowest(singles, c)],
                       [(c, lowest(mixes, c)) for c in caps if lowest(mixes, c)], cs_ops)]
    any_single = lines[-1][4]
    # an added mix: against the best uniform layout of any drawn size (panel 2) and the reference (panel 3); not in panel 1
    lines += [(label, E.MORE_COLOR, 2.4, ref, any_single, mix, cs_ops) for label, mix in more]
    added = {label for label, _ in more}
    # a text row per line in each panel: past 4 lines, each one more makes the panels taller, so the plot keeps its height
    tall = 8.6 + 0.5 * len(more) + 0.9 * max(0, len(lines) - 4)
    fig, axes = plt.subplots(3, 1, figsize=(8.4, tall), sharex=True, sharey=True, gridspec_kw={"hspace": 0.1})
    ty = lambda y: 1 - (1 - y) * 8.6 / tall  # a text row's height, kept in inches
    a1, a2, a3 = axes
    a1.set_xscale("log")
    a1.set_yscale("log")
    vals, notes, rows = [1.0], ([], [], []), []
    for z, (name, color, lw, _, single, mix, _) in enumerate(lines):
        grid = sorted({c for c, _ in single + mix})
        g1 = [(c, cs_ops / at(single, c)) for c in grid if at(single, c)]
        g2 = [(c, at(single, c) / at(mix, c)) for c in grid if at(single, c) and at(mix, c)]
        g3 = [(c, cs_ops / at(mix, c)) for c in grid if at(mix, c)]
        kw = dict(where="post", color=color, lw=lw, zorder=2 if name == ANY else 3 + 0.1 * (len(lines) - z), alpha=0.55 if name == ANY else 1.0,
                  solid_capstyle="butt")
        for k, (ax, g) in enumerate(((a1, g1), (a2, g2), (a3, g3))):
            if k == 0 and name in added:             # panel 1 holds no mix: the added line would repeat "any chunk size"
                continue
            if k == 1:
                ax.step([c for c, _ in g] + [caps[-1]], [v for _, v in g] + [g[-1][1]], **kw)
            else:                                        # dashed below the reference layout's own bytes: it does not fit there
                below, above = [(c, v) for c, v in g if c < rb * (1 - 1e-9)], [(c, v) for c, v in g if c >= rb * (1 - 1e-9)]
                if below:
                    ax.step([c for c, _ in below] + [min(rb, caps[-1])], [v for _, v in below] + [below[-1][1]], **{**{k_: v_ for k_, v_ in kw.items() if k_ != "solid_capstyle"}, "ls": (0, (3, 1.6))})
                if below and above:
                    ax.plot([rb, rb], [below[-1][1], above[0][1]], color=color, lw=lw, alpha=kw["alpha"], zorder=kw["zorder"])
                if above:
                    ax.step([c for c, _ in above] + [caps[-1]], [v for _, v in above] + [above[-1][1]], **kw)
            vals += [v for _, v in g]
            named = [(b, at(g, b * ref)) for b in NAMED if at(g, b * ref)]
            cmax, gmax = max(g, key=lambda t: t[1])
            notes[k].append((color, f"{name}:  " + ",  ".join(f"{b:g}x: {gain_text(v)}" for b, v in named)
                             + f";  up to {gain_text(gmax)} (within {short_bytes(cmax)})"))
            rows += [{"workload_id": a.workload, "option_set": a.option_set, "panel": k + 1, "line": name, "cap_bytes": round(c, 1),
                      "b": round(c / ref, 6), "improvement": round(v, 6)} for c, v in g]
        if name != ANY and name not in added:            # at the reference layout's own bytes, as the known figure's dot
            for ax, g in ((a1, g1), (a3, g3)):
                if at(g, rb):
                    ax.plot([rb], [at(g, rb)], "o", color=color, ms=5, zorder=5)
    heads = (("1. Choosing among the 9 single layouts", f"operations of {rname} (the reference layout) / operations of the best of the 9 layouts that fits the byte limit"),
             (head2, "operations of the best of the 9 layouts / operations of the best mix, same byte limit"),
             ("1 and 2 together", f"operations of {rname} / operations of the best mix that fits the byte limit  (= panel 1 x panel 2)"))
    for ax, (head, what), ns in zip(axes, heads, notes):
        ax.axhline(1, color="black", lw=0.9)
        ax.grid(alpha=0.3, which="major")
        for b in NAMED:
            ax.axvline(b * ref, color=style(b)[0], lw=1.0, ls=":")
        ax.text(0.012, ty(0.955), head, transform=ax.transAxes, fontsize=10, va="top", fontweight="bold")
        ax.text(0.012, ty(0.86), what, transform=ax.transAxes, fontsize=8, va="top", style="italic")
        ax.text(0.012, ty(0.775), f"at B x {uname}'s bytes:", transform=ax.transAxes, fontsize=8, va="top")
        for i, (color, text) in enumerate(ns):
            ax.text(0.012, ty(0.70 - 0.068 * i), text, transform=ax.transAxes, fontsize=8, va="top", color=color, fontweight="bold")
        ax.set_ylabel("improvement,\nin operations", fontsize=10)
        ax.tick_params(axis="y", labelsize=10)
    a1.set_xlim(caps[0] / 1.08, min(caps[-1], max(1.08 * xmax * ref, 1.2 * rb)) * 1.03)
    lo = min(vals) / 1.4
    room = ty(0.70 - 0.068 * max(len(ns) for ns in notes)) - 0.02
    a1.set_ylim(lo, lo * (max(vals) / lo) ** (1 / min(0.46, room)))    # the lines stay under each panel's text block
    a1.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    vmax = max(vals)
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x" if v <= 2.2 * vmax else ""))   # no labels in the text block's room
    a1.yaxis.set_minor_formatter(NullFormatter())
    a3.set_xlabel("byte limit: total bytes allowed", fontsize=10.5)
    log_axis(a3.xaxis, decimal_bytes, *a1.get_xlim())
    a3.tick_params(axis="x", labelsize=10)
    any_label = f"{ANY}: the best uniform layout of any size; the lowest mix of ONE size"
    handles = [Line2D([], [], color=c, lw=lw, alpha=0.55 if n == ANY else 1.0,
                      label=any_label if n == ANY else f"{full[n]}: the best uniform layout of any size; a mix whose parts each take their own size" if n in added
                      else f"Chunked = {n}") for n, c, lw, *_ in lines]
    handles += [Line2D([], [], color=style(b)[0], lw=1.0, ls=":", label=f"byte limit = {b:g}x {uname}'s bytes") for b in NAMED]
    a1.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=1 if more else 2, frameon=False, fontsize=8.5, columnspacing=1.2)
    fh = fig.get_figheight()
    fig.suptitle(f"{title} (SF3), per chunk size of the Chunked setting\nwhat a byte limit buys: the improvement in operations "
                 "(5x = one fifth of the operations; higher is better)", fontsize=10.5, y=1 + (0.42 + (0.185 * (len(handles) - (len(handles) + 1) // 2) if more else 0)) / fh)
    caption(fig, f"One line per chunk size of the Chunked setting: the 9 single layouts and the mix ({mix_is}) at that size. A mix is the exact "
              "fewest-ops mix within the limit, priced from the measured single layouts. The thick line lets both sides take any drawn chunk size, one size "
              "for the whole store. " + ("The added line (panels 2 and 3) is a mix whose parts each take their own chunk size, against the same best uniform layout. " if more else "") +
              
              f"Each line holds its value until the next limit. In panels 1 and 3 the fixed layout is {rname} (the reference), drawn solid from its own "
              "bytes (the dot; the same for every chunk size). Left of the dot (dashed) the limit is below the reference layout's own "
              f"bytes: the layout that fits uses fewer bytes than the reference and the ratio says how its ops compare. B is in units of {uname}'s bytes. " + cells.NAMES_NOTE + f" A named limit is B x the bytes of {uname} for this workload "
              f"(B = {', '.join(f'{b:g}' for b in NAMED)}); a limit past the fewest-ops mix's bytes buys nothing more."
              + " Total ops = read + write ops, weighted G:S:N:P:D = 1 : 1.1 : 0.01 : 1 : 1; total bytes = read (misses included) + written + read "
              f"by writes. {a.source}", y=0.11 - 0.62 / fh)
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, f"budget_gain_3panel_chunk_sizes{tag.replace('-', '_')}_{a.workload}.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)
    base = os.path.join(a.out, f"budget-gain-3panel-chunk-sizes{tag}_{wdir}")
    for ext in ("png", "pdf"):
        fig.savefig(f"{base}.{ext}", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {base}.png ({a.option_set}; {len(caps)} limits)")
    for k, ns in enumerate(notes):
        for _, text in ns:
            print(f"  panel {k + 1}  {text}")


if __name__ == "__main__":
    main()
