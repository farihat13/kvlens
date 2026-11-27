#!/usr/bin/env python3
"""The byte-limit figures, from each workload's budget_sweep.csv (budget_sweep.py) and options.csv:
  <workload>/plots/budget-gain.png   What does a byte limit buy? Three panels, each an improvement in operations
                                     against the byte limit: the best of the 9 single layouts over a fixed uniform layout,
                                     the best per type mix over the best of the 9, and both together.
  <workload>/plots/budget-gain-vs-formats.png   (--known-formats) The improvement over the NAMED formats (dgraph,
                                     nebula: their own rows of named_formats.csv), not over the cells shaped like them.
  plots/budget_gain.png              Every workload against ONE fixed layout (Split/Condensed): the best of the 9
                                     and the best mix at B x the reference layout's bytes (B = 1, 2, 3, 5), and
                                     the fewest bytes at all.

    python3 experiments/layout_choice/plot_budget_sweep.py --sf 30 [--dataset ldbc|linkbench] [--root <analysis dir>]
"""
import argparse
import math
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, FuncFormatter, LogLocator, NullFormatter

HERE = os.path.dirname(os.path.abspath(__file__))
import budget_sweep
from kvcost.plotting.formatting import number_to_human
from kvcost.plotting.style import apply_house_style, format_color
from kvcost.plotting.formatting import decimal_bytes
from kvcost.plotting.style import log_axis
from kvcost.layout_cost.labels import b_style, budgets_note, gain_text
from plot_workload_story import (BUDGET_COLOR, LAYOUT_COLOR, PROPS_MARKER, caption, draw_mix, draw_single, mix_marker,
                                 pair_name, read_csv,
                                 short_bytes)
from kvcost.layout_cost.compose import ADJ_COLOR_KEY, KNOWN
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.settings import B_UNIT
from kvcost.layout_search.front import b_of, ref_bytes_of
from kvcost.layout_search.search import options_b_unit
from kvcost.layout_cost.labels import axis_label, display_title, rule_label
from kvevaluator.layouts import SETTINGS

NOTE = ("Total ops = read + write ops, weighted G:S:N:P:D = 1 : 1.1 : 0.01 : 1 : 1; total bytes = read (misses "
        "included) + written + read by writes. Counts are deterministic (no error bars). A mix chooses a layout per "
        "vertex type and per edge type; each is the exact fewest-ops mix within the limit, priced from the 9 measured "
        "single layouts (priced, not built).")


def named_budgets(cv):
    """{B: (cap bytes, gain or None)} at the named limits (B x the file's unit's bytes); gain None = no single layout fits"""
    out = {}
    for c, kind, sgl, mix in cv:
        if b_of(kind) is not None and mix:
            out[b_of(kind)] = (c, float(sgl["ops"]) / float(mix["ops"]) if sgl else None)
    return out


def layout_name(opt):
    """a uniform layout by its own name, properties/adjacency: 'Condensed/Split'; the byte limits' unit says so:
    'Split/Condensed (the reference layout)'; a cell without a short name: 'the Split-Chunk layout'"""
    if opt == B_UNIT:
        return f"{KNOWN[opt]} (the reference layout)"
    return KNOWN[opt] if opt in KNOWN else f"the {pair_name(opt)} layout"


def times_text(v):
    """'6.3x', '840x': a multiple of a layout's bytes"""
    return f"{v:,.0f}x" if v >= 100 else f"{v:.2g}x"


def curves(rows):
    """per cap, ascending: (cap, kind, single row or None, mix row or None)"""
    by = {}
    for r in rows:
        by.setdefault(float(r["cap_bytes"]), {"kind": r["cap_kind"]})[r["option_set"]] = r
    return [(c, v["kind"], v.get("single"), v.get("per type")) for c, v in sorted(by.items())]


def gains(cv):
    """[(cap, kind, gain)] where a single layout fits; 'only a mix fits' caps are left out"""
    return [(c, k, float(s["ops"]) / float(m["ops"])) for c, k, s, m in cv if s and m]


DGRAPH = "props_split_adj_condensed"
KNOWN_LAYOUTS = (DGRAPH, "props_condensed_adj_split")   # the two fixed uniform layouts the gain is drawn over:
# Split/Condensed (the reference, B's unit: settings.B_UNIT) and Condensed/Split (NebulaGraph's and ArangoDB's)


# (the palette key of the cell's ADJACENCY colour, the line style): a cell's own style, never a named format's
KNOWN_STYLE = {c: (ADJ_COLOR_KEY[c.split("_adj_")[1]], ls) for c, ls in zip(KNOWN_LAYOUTS, ("-", "--"))}


def plot_workload(d, title, rows, opts=(), xlim=None, ylim=None, out=None, xnorm=None, plain=False):
    # plain: budget-gain-plain.png, the same panels without the small text (the formula line, the notes, the caption)
    # xlim / ylim / out: one frame of a series: fixed axes, a fixed-size PNG at `out`;
    # xnorm: draw the limit as a multiple of these bytes (a series whose workload grows by orders of magnitude)
    sx = (lambda c: c / xnorm) if xnorm else (lambda c: c)
    """budget-gain.png: three stacked panels on ONE x axis (the byte limit) and ONE y axis (reduction in ops, higher
    is better), one comparison each: (1) the best of the 9 single layouts over each fixed uniform layout, (2) the best mix
    over the best of the 9, (3) both together: the best mix over each fixed uniform layout (= 1 x 2). Such a layout's lines
    start at its own bytes (a smaller limit does not hold it)."""
    cv = curves(rows)
    caps = [c for c, *_ in cv]
    gs = gains(cv)
    known = [(o["option"], float(o["total_ops"]), float(o["total_bytes"])) for k in KNOWN_LAYOUTS
             for o in opts if o["option_set"] == "single" and o["option"] == k]
    fig, axes = plt.subplots(3, 1, figsize=(8.4, 8.6), sharex=True, sharey=True, gridspec_kw={"hspace": 0.1})
    a1, a2, a3 = axes
    a1.set_xscale("log")
    a1.set_yscale("log")
    vals, notes1, notes3 = [1.0], [], []
    for opt, ops_k, bytes_k in known:
        fmt, ls = KNOWN_STYLE[opt]
        color = format_color(fmt)
        pts = [(c, sgl, mix) for c, _, sgl, mix in cv if c >= bytes_k * (1 - 1e-9) and sgl and mix]
        if not pts:
            continue
        x = [sx(c) for c, _, _ in pts]
        g1 = [ops_k / float(sgl["ops"]) for _, sgl, _ in pts]
        g3 = [ops_k / float(mix["ops"]) for _, _, mix in pts]
        for ax, g in ((a1, g1), (a3, g3)):
            ax.step(x, g, where="post", color=color, lw=2.0, ls=ls)
            ax.plot([x[0]], [g[0]], "o", color=color, ms=6, zorder=4)       # at the layout's own bytes
        vals += g1 + g3
        name = layout_name(opt)
        notes1.append(f"over {name}: {gain_text(g1[0])} at its own bytes, up to {gain_text(max(g1))} "
                      f"(at {times_text(x[g1.index(max(g1))] / sx(bytes_k))} its bytes)")
        notes3.append(f"over {name}: {gain_text(g3[0])} at its own bytes, up to {gain_text(max(g3))} "
                      f"(at {times_text(x[g3.index(max(g3))] / sx(bytes_k))} its bytes)")
    a2.step([sx(c) for c, _, _ in gs], [g for _, _, g in gs], where="post", color=BUDGET_COLOR, lw=2.0)
    a2.fill_between([sx(c) for c, _, _ in gs], 1, [g for _, _, g in gs], step="post", color=BUDGET_COLOR, alpha=0.13, lw=0)
    vals += [g for _, _, g in gs]
    named = named_budgets(cv)
    unit_cell = ref_bytes_of(rows)[1]           # B's unit: Sp-Co, or Co-Sp in a sweep in Co-Sp units
    gain = max([g for _, _, g in gs], default=1.0)
    notes2 = []
    if gs and gain > 1.005:
        cmax, _, gmax = max(gs, key=lambda t: t[2])
        at = [f"{b:g}x: {gain_text(g)}" for b, (_, g) in sorted(named.items()) if g is not None]
        if at:
            notes2.append(f"with the byte limit at B x the {cell_name(unit_cell)} cell's bytes:  " + ",  ".join(at))
        notes2.append(f"up to {gain_text(gmax)} (within {short_bytes(cmax)})")
    else:
        notes2.append("mixing alone: no reduction at any limit (the best mix is a single layout)")
    for b, (c, g) in sorted(named.items()):
        if g is not None:
            a2.plot([sx(c)], [g], b_style(b)[1], color=b_style(b)[0], ms=6.5, zorder=4, markeredgecolor="black", markeredgewidth=0.5)
    heads = (("1. Choosing among the 9 single layouts", "operations of a fixed uniform layout / operations of the best of the 9 layouts that fits the byte limit", notes1),
             ("2. Mixing per type", "operations of the best of the 9 layouts / operations of the best mix, same byte limit", notes2),
             ("1 and 2 together", "operations of a fixed uniform layout / operations of the best mix that fits the byte limit  (= panel 1 x panel 2)", notes3))
    for ax, (head, what, notes) in zip(axes, heads):
        ax.axhline(1, color="black", lw=0.9)
        ax.grid(alpha=0.3, which="major")
        for b, (c, _) in sorted(named.items()):
            ax.axvline(sx(c), color=b_style(b)[0], lw=1.0, ls=":")
        ax.text(0.012, 0.955, head, transform=ax.transAxes, fontsize=10, va="top", fontweight="bold")
        if not plain:
            ax.text(0.012, 0.86, what, transform=ax.transAxes, fontsize=8, va="top", style="italic")
            ax.text(0.012, 0.775, "\n".join(notes), transform=ax.transAxes, fontsize=8, va="top", linespacing=1.25)
        ax.set_ylabel("improvement,\nin operations", fontsize=10)
        ax.tick_params(axis="y", labelsize=10)
    lo, hi = caps[0], caps[-1]
    pad = 10 ** (0.04 * max(math.log10(hi / lo), 0.3))
    a1.set_xlim(*(xlim or (sx(lo) / pad, sx(hi) * pad)))
    head_room = (1.15, 1.8) if plain else (1.75, 2.2)                              # for each panel's text block
    a1.set_ylim(*(ylim or (min(vals) / 1.4, max(max(vals), 1.3) ** head_room[0] * head_room[1])))
    a1.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    a1.yaxis.set_minor_formatter(NullFormatter())
    if xnorm:
        a3.set_xlabel(f"byte limit, as a multiple of the fewest-bytes single layout's bytes ({short_bytes(xnorm)} here)", fontsize=10.5)
        a3.xaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
        a3.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
        a3.xaxis.set_minor_formatter(NullFormatter())
    else:
        a3.set_xlabel("byte limit: total bytes allowed", fontsize=10.5)
        log_axis(a3.xaxis, decimal_bytes, *a1.get_xlim())
    a3.tick_params(axis="x", labelsize=10)
    handles = [Line2D([], [], color=format_color(KNOWN_STYLE[o][0]), lw=2.0, ls=KNOWN_STYLE[o][1], marker="o", ms=6,
                      label=f"over {layout_name(o)} (dot: its own bytes)") for o, _, _ in known]
    handles.append(Line2D([], [], color=BUDGET_COLOR, lw=2.0, label="the best mix over the best of the 9"))
    for b in sorted(named):
        handles.append(Line2D([], [], color=b_style(b)[0], lw=1.0, ls=":", marker=b_style(b)[1], ms=6,
                              markeredgecolor="black", markeredgewidth=0.5, label=f"byte limit = {b:g}x the {cell_name(unit_cell)} cell's bytes"))
    a1.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3, frameon=False, fontsize=8.5,
              columnspacing=1.2)
    fh = fig.get_figheight()
    fig.suptitle(f"{title}\nwhat a byte limit buys: the improvement in operations (5x = one fifth of the operations; higher is better)",
                 fontsize=9.5 if out else 10.5, y=1 - (0.12 if out else 0.02) / fh)
    if not plain:
        caption(fig, "The 9 single layouts are measured; a mix (a layout per vertex type and per edge type) is the exact "
                "fewest-ops mix within the limit, priced from them, not built. Each line holds its value until the next "
                "limit (" + str(len(caps)) + " limits). A fixed uniform layout's line starts at its own bytes. " + budgets_note(unit_cell, sorted(named))
                + " Total ops = "
                "read + write ops, weighted G:S:N:P:D = 1 : 1.1 : 0.01 : 1 : 1; total bytes = read (misses included) + "
                "written + read by writes. Counts are deterministic (no error bars).",
                y=0.105 if out else 0.11 - 0.62 / fh)
    if out:                                             # a frame: the same pixels every time (no tight bounding box)
        fig.subplots_adjust(left=0.11, right=0.975, top=0.865, bottom=0.175)
        fig.savefig(out, dpi=110)
        plt.close(fig)
        return
    os.makedirs(benchmarks.plots_dir(d), exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(benchmarks.plots_dir(d), f"budget-gain{'-plain' if plain else ''}.{ext}"), dpi=150, bbox_inches="tight")
    plt.close(fig)


NAMED_FORMATS = "named_formats.csv"     # a named format priced in options.csv's currencies
CELL = {"split": "Split", "condensed": "Condensed", "chunked": "Chunked"}
MIX_SETS = (("per type", "o", "a layout per type"), ("per type and direction", "D", "per type and list direction"))


def cell_name(opt):
    """'Split/Condensed': a grid cell by its settings, props / adjacency"""
    p, a = opt[len("props_"):].split("_adj_")
    return f"{CELL[p]}/{CELL[a]}"


def plot_vs_formats(d, title, opts, named, arms=(), note=""):
    """budget-gain-vs-formats.png: the improvement in operations over each NAMED format (a panel each; the format's
    own row of named_formats.csv, not the grid cell shaped like it), at each option's own bytes: the 9 single layouts
    and the fewest-ops mix within B x the options.csv's B unit's bytes (its B rows; search.options_b_unit). Points only: the
    continuous curve of budget-gain.png needs budget_sweep.csv."""
    singles = [(o["option"], float(o["total_ops"]), float(o["total_bytes"])) for o in opts if o["option_set"] == "single"]
    unit_cell = options_b_unit(o["question"] for o in opts)   # B's unit: Sp-Co, or Co-Sp in an options.csv in Co-Sp units
    unit = next(b for c, _, b in singles if c == unit_cell)      # the limits' unit, in bytes
    mixes = {(o["option_set"], float(o["question"][2:])): (float(o["total_ops"]), float(o["total_bytes"]))
             for o in opts if o["question"].startswith("B=") and o["option_set"] in dict((s, 0) for s, _, _ in MIX_SETS)}
    budgets = sorted({b for _, b in mixes})

    def frontier(pts):
        """[(bytes, ops)] -> the fewest ops within each byte limit, as steps: [(bytes, running fewest ops)]"""
        out = []
        for by, o in sorted(pts):
            if not out or o < out[-1][1]:
                out.append((by, o))
        return out
    lines = [(frontier([(by, o) for _, o, by in singles]), "0.35", "-", 1.6)]      # exact: the 9 are all there is
    for (st, _, _), ls in zip(MIX_SETS, ("-", "--")):                               # exact at each solved limit
        pts = [(float(o["total_bytes"]), float(o["total_ops"])) for o in opts if o["option_set"] == st
               and (o["question"].startswith("B=") or o["question"] in ("fewest ops", "fewest bytes")) and o["fits"] != "no"]
        lines.append((frontier(pts), BUDGET_COLOR, ls, 1.8))
    fig, axes = plt.subplots(len(named), 1, figsize=(8.6, 3.9 * len(named) + 1.5), sharex=True, sharey=True,
                             gridspec_kw={"hspace": 0.08})
    axes = [axes] if len(named) == 1 else list(axes)
    ys, xs = [1.0], []
    for ax, n in zip(axes, named):
        ops_k, bytes_k, color = float(n["total_ops"]), float(n["total_bytes"]), format_color(n["option"])
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.axhline(1, color="black", lw=0.9)
        ax.grid(alpha=0.3, which="major")
        for b in budgets:
            ax.axvline(b * unit, color=b_style(b)[0], lw=1.0, ls=":")
        for fr, lcolor, ls, lw in lines:
            xr = [by for by, _ in fr] + [budgets[-1] * unit * 1.6]
            ax.step(xr, [ops_k / o for _, o in fr] + [ops_k / fr[-1][1]], where="post", color=lcolor, ls=ls, lw=lw, zorder=2)
        for c, o, by in singles:                        # the options map's symbols: shape = props, colour = adjacency
            draw_single(ax, by, ops_k / o, c, s=70)
            ys.append(ops_k / o)
            xs.append(by)
        for st, _, _ in MIX_SETS:                       # the options map's mix symbols: hollow, pink (a budget pick)
            for b in budgets:
                if (st, b) not in mixes:
                    continue
                o, by = mixes[(st, b)]
                draw_mix(ax, by, ops_k / o, {"set": st, "end": False})
                ys.append(ops_k / o)
                xs.append(by)
        for b in budgets:                               # one label per limit: per type / per type and direction
            vals = [(mixes[(st, b)], st) for st, _, _ in MIX_SETS if (st, b) in mixes]
            g = [ops_k / o for (o, _), _ in vals]
            text = gain_text(g[0]) if max(g) / min(g) < 1.005 else " / ".join(gain_text(v) for v in g)
            last = b == budgets[-1]                     # the largest limit's label goes right of its points
            ax.annotate(text, ((max if last else min)(by for (_, by), _ in vals), max(g)), xytext=(8, 0) if last else (-5, 5),
                        textcoords="offset points", ha="left" if last else "right", va="center" if last else "bottom",
                        fontsize=8.5, fontweight="bold", color=b_style(b)[0])
        # the panel's format at 1x, at its own bytes (the large dot); an arm of it (dgraph-c512k) as a paler dot
        where = []
        for m in [n] + [r for r in arms if r["option"].startswith(n["option"] + "-")]:
            mo, mb = float(m["total_ops"]), float(m["total_bytes"])
            own = m is n
            if not own and abs(mo / ops_k - 1) < 0.01 and abs(mb / bytes_k - 1) < 0.01:
                continue                                # the same point as its format on this workload
            ax.plot([mb], [ops_k / mo], "o", color=color, mec="black", mew=0.8, ms=10 if own else 7, zorder=6,
                    alpha=1 if own else 0.55)
            where.append(f"large dot: {m['label']}, {short_bytes(mb)} = {times_text(mb / unit)} the {cell_name(unit_cell)} cell's bytes"
                         if own else f"pale dot: {m['label']}, {short_bytes(mb)} = {times_text(mb / unit)}, "
                                     f"{ops_k / mo:.2f}x in operations")
            ys.append(ops_k / mo)
            xs.append(mb)
        ax.text(0.012, 0.955, f"over {n['label']}", transform=ax.transAxes, fontsize=10.5, va="top", fontweight="bold", color=color)
        ax.text(0.012, 0.875, f"operations of {n['label']} ({number_to_human(ops_k)}) / operations of the option",
                transform=ax.transAxes, fontsize=8, va="top", style="italic")
        ax.text(0.012, 0.815, "\n".join(where), transform=ax.transAxes, fontsize=8, va="top", color=color, linespacing=1.25)
        ax.set_ylabel("improvement,\nin operations", fontsize=10)
        ax.tick_params(axis="y", labelsize=10)
    a1, a3 = axes[0], axes[-1]
    a1.set_xlim(min(xs) / 1.5, max(xs + [budgets[-1] * unit]) * 1.6)
    a1.set_ylim(min(ys) / 1.25, max(ys) * 4.5)
    a1.set_xlim(min(xs) / 1.5, budgets[-1] * unit * 1.6)
    a1.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
    a1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    a1.yaxis.set_minor_formatter(NullFormatter())
    a3.set_xlabel("total bytes of the option (the dotted lines: the byte limits)", fontsize=10.5)
    log_axis(a3.xaxis, decimal_bytes, *a1.get_xlim())
    a3.tick_params(axis="x", labelsize=10)
    handles = [Line2D([], [], marker=PROPS_MARKER[p], color="0.75", mec="black", mew=0.6, ls="", ms=7.5, label=f"single layout: {CELL[p]} props")
               for p in SETTINGS]
    handles += [Patch(facecolor=LAYOUT_COLOR[a_], edgecolor="black", lw=0.6, label=f"single layout: {CELL[a_]} adjacency")
                for a_ in SETTINGS]
    for st, _, what in MIX_SETS:
        mk, col, sz = mix_marker({"set": st, "end": False})
        handles.append(Line2D([], [], marker=mk, color=col, mfc="none", mew=1.8, ls="", ms=math.sqrt(sz) * 0.8,
                              label=f"fewest-ops mix within a limit: {what} (priced)"))
    handles += [Line2D([], [], color="0.35", lw=1.6, label="the best of the 9 within the byte limit"),
                Line2D([], [], color=BUDGET_COLOR, lw=1.8, label="the best mix per type within the limit"),
                Line2D([], [], color=BUDGET_COLOR, lw=1.8, ls="--", label="the best mix per type and direction")]
    handles += [Line2D([], [], color=b_style(b)[0], lw=1.2, ls=":",
                       label=f"byte limit = {b:g}x the {cell_name(unit_cell)} cell's bytes") for b in budgets]
    a1.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=3, frameon=False, fontsize=8.2, columnspacing=1.0)
    fh = fig.get_figheight()
    fig.suptitle(f"{title}\nthe improvement in operations over a named format (5x = one fifth of its operations; higher is better)",
                 fontsize=10.5, y=1 + 0.42 / fh)
    caption(fig, "The known layouts here are the NAMED formats, each measured on its own store with its own plans; "
            "budget-gain.png draws the same improvement over the two grid cells shaped like them (Split/Condensed and "
            "Condensed/Split, props / adjacency). A single layout's shape is its props setting and its colour its adjacency setting, and a mix is a "
            "hollow pink mark (triangle = per type, diamond = per type and direction), as in options.png. Every point sits at its own total bytes. The limits are multiples of the "
            f"{cell_name(unit_cell)} cell's bytes ({short_bytes(unit)}); a mix is the exact fewest-ops mix within its limit, priced from "
            "the 9 measured single layouts, not built. The bold labels give the mixes' improvement (per type / per type and "
            "direction where they differ). Each line holds its value until the next option that fits: the single-layout line "
            f"is exact; a mix line is exact at the solved limits (fewest bytes, B = {', '.join(f'{b:g}' for b in budgets)}, fewest ops) and a floor between "
            "them (a finer sweep can only raise it). "
            + note + " Total ops = read + write ops, weighted G:S:N:P:D = 1 : 1.1 : 0.01 : 1 : 1; total bytes = read (misses "
            "included) + written + read by writes. Counts are deterministic (no error bars).", y=0.11 - 0.62 / fh)
    os.makedirs(benchmarks.plots_dir(d), exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(benchmarks.plots_dir(d), f"budget-gain-vs-formats.{ext}"), dpi=150, bbox_inches="tight")
    plt.close(fig)


def two_line(title):
    """'W14 (rw): Hub writes vs person analytics' -> the id and kind on one line, the description on the next"""
    head_, _, rest = title.partition(": ")
    return f"{head_}\n{rest}" if rest else title


def fixed_baseline(opts, rows, known=DGRAPH):
    """one workload against a FIXED layout: {"ops": {B: (best single, best mix)},
    "bytes": (best single, best mix)}, each value = the fixed layout's cost / the option's, None where nothing of
    that kind fits. ops: the fewest ops within B x the sweep's unit's bytes (budget_sweep.csv); bytes: the
    fewest bytes at all (no limit on ops), from options.csv."""
    singles = {o["option"]: o for o in opts if o["option_set"] == "single"}
    k_ops, k_bytes = float(singles[known]["total_ops"]), float(singles[known]["total_bytes"])
    out = {"ops": {}, "bytes": None, "unit": ref_bytes_of(rows)[1]}    # unit: B's cell (Co-Sp in a sweep in Co-Sp units)
    for r in rows:
        if b_of(r["cap_kind"]) is not None:
            if r["option_set"] not in ("single", "per type"):    # a sweep with grouped props solves: their line
                continue
            at = out["ops"].setdefault(b_of(r["cap_kind"]), [None, None])
            at[("single", "per type").index(r["option_set"])] = k_ops / float(r["ops"])
    sets = {o["option_set"] for o in opts}
    # an options.csv with grouped props solves: its solves without those options
    st = "per type, no grouping" if "per type, no grouping" in sets else "per type"
    fewest = next(o for o in opts if o["option_set"] == st and o["question"] == "fewest bytes")
    out["bytes"] = (k_bytes / min(float(o["total_bytes"]) for o in singles.values()), k_bytes / float(fewest["total_bytes"]))
    return out


def plot_across_fixed(items, out, head, known=DGRAPH):
    """budget_gain.png: every workload against ONE fixed layout. Per workload, a lane per limit B: hollow = the
    best of the 9 single layouts, filled = the best per type mix; the bar between them is what mixing adds. A last
    panel does the same for the fewest bytes at all. items = [(title, fixed_baseline(...))]."""
    name = layout_name(known)
    items = sorted(items, key=lambda t: t[0])           # by workload id (W01, W02, ...)
    n, lanes = len(items), sorted({b for _, f in items for b in f["ops"]})
    units = {f["unit"] for _, f in items}
    if len(units) != 1:
        raise SystemExit(f"budget_gain: the workloads' sweeps take B in different units ({sorted(units)}): one unit per figure")
    unit_cell = units.pop()
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(13.5, 0.74 * n + 2.0), sharey=True, width_ratios=(3.1, 1.25),
                                 gridspec_kw={"wspace": 0.04})
    step = 0.8 / len(lanes)
    for i, (_, f) in enumerate(items):
        for j, b in enumerate(lanes):
            if b not in f["ops"]:
                continue
            y = i + (j - (len(lanes) - 1) / 2) * step
            sgl, mix = f["ops"][b]
            color, marker = b_style(b)
            if sgl is not None and mix is not None:
                ax.plot([sgl, mix], [y, y], color=color, lw=2.6, alpha=0.45, solid_capstyle="butt", zorder=2)
            if sgl is not None:
                ax.plot([sgl], [y], marker, mfc="white", mec=color, mew=1.4, ms=6, zorder=3)
            if mix is not None:
                ax.plot([mix], [y], marker, color=color, mec="black", mew=0.4, ms=6.5, zorder=4)
            best, before = mix, sgl
            if best is not None and (before is None or best / before > 1.05):
                ax.annotate(gain_text(best) + ("" if before is not None else " (nothing else fits)"), (best, y),
                            xytext=(7, 0), textcoords="offset points", va="center", fontsize=7)
        sgl, mix = f["bytes"]
        bx.plot([sgl, mix], [i, i], color="0.35", lw=2.6, alpha=0.45, solid_capstyle="butt", zorder=2)
        bx.plot([sgl], [i], "o", mfc="white", mec="0.2", mew=1.4, ms=6, zorder=3)
        bx.plot([mix], [i], "o", color="0.2", ms=6.5, zorder=4)
        if mix / sgl > 1.05:
            bx.annotate(gain_text(mix), (mix, i), xytext=(6, 0), textcoords="offset points", va="center", fontsize=7)
    for a, title, xlabel in (
            (ax, "Operations, when the bytes are limited\n(each lane: the cheapest-in-operations layout that fits the byte limit)",
             f"improvement over {name}, in operations"),
            (bx, "Bytes, with no limit\n(the layout with the fewest bytes)", f"improvement over {name}, in bytes")):
        a.set_xscale("log")
        a.axvline(1, color="black", lw=0.9)
        a.grid(axis="x", alpha=0.3)
        for i in range(n - 1):
            a.axhline(i + 0.5, color="0.85", lw=0.6)
        a.set_title(title, fontsize=11)
        a.set_xlabel(xlabel, fontsize=10.5)
        lo, hi = a.get_xlim()
        a.set_xlim(min(lo, 0.8), hi * 2.2)                  # room for the labels right of the filled marks
        wide = math.log10(a.get_xlim()[1] / a.get_xlim()[0]) > (2.3 if a is ax else 1.2)   # fewer labels on a long axis
        a.xaxis.set_major_locator(LogLocator(base=10, subs=(1.0,) if wide else (1.0, 2.0, 5.0)))
        a.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
        a.xaxis.set_minor_formatter(NullFormatter())
        a.tick_params(axis="x", labelsize=9.5)
    ax.set_ylim(n - 0.5, -0.5)
    ax.set_yticks(range(n), [two_line(t[0]) for t in items], fontsize=8.5, linespacing=1.1)
    handles = [Line2D([], [], marker="o", mfc="white", mec="0.2", mew=1.4, ls="", ms=6, label="the best of the 9 single layouts"),
               Line2D([], [], marker="o", color="0.2", ls="", ms=6.5, label="the best per type mix (priced)"),
               Line2D([], [], color="0.35", lw=2.6, alpha=0.45, label="between them: what mixing adds")]
    handles += [Line2D([], [], marker=m, color=c, ls="", ms=6.5, mec="black", mew=0.4,
                       label=f"byte limit = {b:g}x the {cell_name(unit_cell)} cell's bytes") for b, (c, m) in ((b, b_style(b)) for b in lanes)]
    fh = fig.get_figheight()
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1 - 0.62 / fh), ncol=4, frameon=False, fontsize=8.5,
               columnspacing=1.3)
    fig.suptitle(f"{head}: every workload against one fixed layout, {name}\n"
                 "improvement over it: 5x = one fifth of its cost (1x = the same; left of 1x = worse). Hollow = the best of the 9 single "
                 "layouts, filled = the best mix", fontsize=11, y=1 - 0.05 / fh)
    caption(fig, f"Left: ops of {name} divided by the fewest ops within the limit (a lane per limit, top to "
            f"bottom B = {', '.join(f'{b:g}' for b in lanes)}); right: its bytes divided by the fewest bytes any option reaches. The label is the "
            "mix's value, where mixing adds over 5%. " + budgets_note(unit_cell, lanes) + " " + NOTE,
            y=0.55 / fh)
    fig.subplots_adjust(top=1 - 1.5 / fh, bottom=1.25 / fh)
    os.makedirs(out, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"budget_gain.{ext}"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {os.path.join(out, 'budget_gain')}.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    benchmarks.add_workload_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    benchmarks.add_root_arg(ap)
    ap.add_argument("--known-formats", default=None, metavar="SLUGS",
                    help="also draw <workload>/plots/budget-gain-vs-formats.png: the improvement over these named formats "
                         "(rows of the workload's named_formats.csv), e.g. dgraph,nebula")
    ap.add_argument("--formats-note", default="", help="a sentence for that figure's caption (which code state the formats are)")
    benchmarks.add_out_arg(ap)
    a = ap.parse_args()
    benchmarks.use_plots_out(a.out)
    apply_house_style()
    head = f"{axis_label(a.dataset, a.sf, a.param_set)}; {rule_label(a.criterion)}"
    fixed = []
    for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items():
        d = benchmarks.workload_dir(a.dataset, a.sf, name, w, a.root, criterion=a.criterion, param_set=a.param_set)
        path = os.path.join(d, "budget_sweep.csv")
        if a.known_formats and not os.path.exists(path) and os.path.exists(os.path.join(d, "options.csv")):
            # the named-formats figure reads options.csv and named_formats.csv only: drawn without a sweep
            by = {r["option"]: r for r in read_csv(os.path.join(d, NAMED_FORMATS))}
            panels = a.known_formats.split(",")
            plot_vs_formats(d, f"{display_title(w)} ({head})", read_csv(os.path.join(d, "options.csv")),
                            [by[f] for f in panels], [r for f, r in by.items() if f not in panels], a.formats_note)
            print(f"wrote {os.path.join(benchmarks.plots_dir(d), 'budget-gain-vs-formats')}.png (no budget_sweep.csv: budget-gain.png not drawn)")
            continue
        if not os.path.exists(path) or not os.path.exists(os.path.join(d, "options.csv")):
            print(f"  (no budget_sweep.csv or options.csv under {d}: skipped)")
            continue
        rows = budget_sweep.check_same_solve(d)     # refused when the sweep is from another solve than options.csv
        title = f"{display_title(w)} ({head})"
        opts = read_csv(os.path.join(d, "options.csv"))
        plot_workload(d, title, rows, opts)
        plot_workload(d, title, rows, opts, plain=True)
        fixed.append((display_title(w), fixed_baseline(opts, rows)))
        print(f"wrote {os.path.join(benchmarks.plots_dir(d), 'budget-gain')}{{,-plain}}.png")
        if a.known_formats:
            by = {r["option"]: r for r in read_csv(os.path.join(d, NAMED_FORMATS))}
            panels = a.known_formats.split(",")      # a panel each; every other priced row is drawn as an arm of its format
            plot_vs_formats(d, title, opts, [by[f] for f in panels], [r for f, r in by.items() if f not in panels], a.formats_note)
            print(f"wrote {os.path.join(benchmarks.plots_dir(d), 'budget-gain-vs-formats')}.png ({len(by)} named formats priced, "
                  f"{len(a.known_formats.split(','))} drawn)")
    if fixed and not a.workload:
        plot_across_fixed(fixed, benchmarks.plots_dir(a.root or benchmarks.out_root(a.dataset, a.sf, a.criterion, a.param_set)), head)


if __name__ == "__main__":
    main()
