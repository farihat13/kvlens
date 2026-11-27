"""The measured cost of every query of a workload on the uniform layouts, one strip of layouts per query.

Three panels stacked (wops, bytes, wops_bytes4k = wops + bytes / 4096), one column per query, banded by the workload's
own GROUPS (Interactive: IC | IS; LinkBench: reads | writes); each point one layout (colour: adjacency; marker:
properties), a black tick at the median layout. y is the measured cost of one run (one request), or with --over-best
each layout's cost over that query's cheapest (1x = the best; the spread printed above each strip). Above the top
panel, per query: its share of the workload's runs (its weight over all weights) and of its cost (weight x cost over
the layout's weighted total, the median over the layouts, wops_bytes4k; kvcost.layout_cost.query_weighting.shares).

Inputs (the file's header says which):
  - a layout_choice by_query.csv (the measured columns: measured_wops, measured_bytes; the 49 layouts at SF3 / SF30;
    --same-measured checks another tier's file holds the same measured columns), per run = the mean over params 1-5;
  - experiments/mix_benefit/per_query_disagreement.py's costs.csv with --costs-family (LinkBench: the nine grid cells, per
    request = the mean over the request log's requests of that type; a write with its own reads).
Run from the artifact's root:

    R=<a predict-results snapshot>/tier1-flat/reports
    uv run --locked python experiments/layout_choice/plot_measured_cost_by_query.py --family interactive --scale SF3 \\
        --mix I02 $R/full-q19/layout_choice/by_query.csv --same-measured $R/flat-q19/layout_choice/by_query.csv \\
        --out <dir>/measured_strip_ic_is.png
    uv run --locked python experiments/layout_choice/plot_measured_cost_by_query.py --family linkbench \\
        --scale "LinkBench n1m" --mix "the request log" --costs-family LinkBench \\
        <a mix-benefit snapshot>/sf3/per-query-disagreement/costs.csv \\
        --over-best --out <dir>/measured_strip_over_best.png
Each writes the one file --out names, and prints the five heaviest and five lightest queries by median wops_bytes4k.
"""
import argparse
import csv
import itertools
import math
import os
import re
import statistics
import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_cost.cost_models import wops_bytes4k
from kvcost.plotting.formatting import decimal_bytes, params_phrase, percent
from kvcost.plotting.strip import layout_legend_handles, separators, strip_point, strip_x
from kvcost.plotting.style import apply_house_style, cell_short, log_axis
from kvevaluator.measure.workload_plans import groups as workload_groups

# the panels, top to bottom: (name, the value from a run's wops and bytes)
MODELS = [("wops", lambda w, b: w), ("bytes", lambda w, b: b), ("wops_bytes4k", wops_bytes4k)]
MEASURED_COLS = ("measured_body", "measured_wops", "measured_bytes")
# what one row of each input is, and its models (the caption says both)
SOURCES = {
    "by_query": {"unit": "run", "per": "Per run: the mean over params 1-5, each layout's own plan",
                 "formula": {"wops": "G + 1.1 S + 0.01 N", "bytes": "key and value bytes read",
                             "wops_bytes4k": "wops + bytes read / 4096"}},
    "costs": {"unit": "request", "per": "Per request: the mean over the log's requests of that type; reads at the "
                                        "planner's body, a write with its own reads",
              "formula": {"wops": "G + 1.1 S + 0.01 N + PUTs + DELs", "bytes": "key and value bytes read + bytes written",
                          "wops_bytes4k": "wops + bytes / 4096"}},
}


def short_count(v):
    """a count's tick label: 2, 50, 1.5k, 20M"""
    for unit, d in (("M", 1e6), ("k", 1e3)):
        if v >= d:
            return f"{v / d:g}{unit}"
    return f"{v:g}"


def by_query_rows(path, others):
    """a layout_choice by_query.csv's measured rows; refuses a file in `others` whose measured columns differ"""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    mine = {(r["layout"], r["query"]): tuple(r[c] for c in MEASURED_COLS) for r in rows}
    for other in others:
        with open(other) as f:
            theirs = {(r["layout"], r["query"]): tuple(r[c] for c in MEASURED_COLS) for r in csv.DictReader(f)}
        differ = sorted(k for k in mine if theirs.get(k) != mine[k]) + sorted(set(theirs) - set(mine))
        if differ:
            raise SystemExit(f"{other}: measured columns differ from {path} on {len(differ)} rows (e.g. {differ[0]})")
        print(f"plot_measured_cost_by_query: {other}: measured columns equal on {len(mine)} (layout, query) rows")
    return [{"query": r["query"], "cell": r["cell"], "layout": r["layout"], "weight": r["weight"],
             "wops": r["measured_wops"], "bytes": r["measured_bytes"]} for r in rows]


def costs_rows(path, family):
    """per_query_disagreement's costs.csv, one family's rows; its wops_bytes4k column must match the formula"""
    with open(path) as f:
        rows = [r for r in csv.DictReader(f) if r["family"] == family]
    if not rows:
        raise SystemExit(f"{path}: no rows of family {family!r}")
    for r in rows:
        if not math.isclose(wops_bytes4k(float(r["wops"]), float(r["bytes"])), float(r["wops_bytes4k"]), rel_tol=1e-5):
            raise SystemExit(f"{path}: {r['query']} {r['cell']}: wops_bytes4k {r['wops_bytes4k']} is not wops + bytes / 4096")
    return [{"query": r["query"], "cell": r["cell"], "layout": cell_short(r["cell"]), "weight": r["weight"],
             "wops": r["wops"], "bytes": r["bytes"]} for r in rows]


def load(path, others, family):
    """(rows, the source's kind): the header picks the reader"""
    with open(path) as f:
        header = next(csv.reader(f))
    if "measured_wops" in header:
        if family:
            raise SystemExit("--costs-family is for a costs.csv; a by_query.csv holds one workload")
        return by_query_rows(path, others), "by_query"
    if "family" in header:
        if not family or others:
            raise SystemExit("a costs.csv needs --costs-family and takes no --same-measured")
        return costs_rows(path, family), "costs"
    raise SystemExit(f"{path}: neither a layout_choice by_query.csv nor a per_query_disagreement costs.csv")


def tick_label(q):
    """ic3 -> IC3; a longer name (getlinklist_link1) as the capture names it"""
    return q.upper() if re.fullmatch(r"[a-z]+\d+", q) else q


def plot_measured_strip(path, others, family, workload, over_best, scale, mix, out, chunk=None, params="1-5"):
    """the measured cost per run of every query of the workload, one strip of the layouts per query, three model panels"""
    rows, kind = load(path, others, family)
    src = dict(SOURCES[kind])
    if kind == "by_query" and params != "1-5":      # JOB: one param per query; LinkBench: its requests in the log
        src["per"] = src["per"].replace("the mean over params 1-5", params_phrase(params))
    present = {r["query"] for r in rows}
    bands, missing = [], []
    for g in workload_groups(workload):         # the workload's own groups, in its order
        qs = [q for q in g["queries"] if q in present]
        missing += [q for q in g["queries"] if q not in present]
        if qs:
            bands.append((g["title"], qs))
    queries = [q for _, qs in bands for q in qs]
    stray = sorted(present - set(queries))
    if stray:
        raise SystemExit(f"{path}: {stray} are in no group of {workload}")
    if missing:
        print(f"plot_measured_cost_by_query: {workload}'s {missing} have no rows (not drawn)")
    by_q = {q: sorted((r for r in rows if r["query"] == q), key=lambda r: r["layout"]) for q in queries}
    layouts = {q: [r["layout"] for r in by_q[q]] for q in queries}
    n_layouts = len(layouts[queries[0]])
    if any(v != layouts[queries[0]] for v in layouts.values()) or n_layouts != len(set(layouts[queries[0]])):
        raise SystemExit(f"{path}: the queries are not costed on the same layouts, once each")
    weight = {q: float(by_q[q][0]["weight"]) for q in queries}
    if any(float(r["weight"]) != weight[q] for q in queries for r in by_q[q]):
        raise SystemExit(f"{path}: a query's weight differs between layouts")
    runs, cost_share = QW.shares({q: {r["layout"]: wops_bytes4k(float(r["wops"]), float(r["bytes"])) for r in by_q[q]}
                                  for q in queries}, weight)
    left_out = 0
    for q in queries:
        ok = [r for r in by_q[q] if r["wops"] != "" and r["bytes"] != "" and float(r["wops"]) > 0 and float(r["bytes"]) > 0]
        left_out += len(by_q[q]) - len(ok)
        by_q[q] = ok
    unit = src["unit"]
    medians = {}

    def draw(bands, queries, out):
        """one figure of these bands (the shares above it stay the whole workload's); returns the points drawn"""
        # x: each band's queries, a gap between bands
        gap = 0.8
        xpos = {}
        for b, (_, qs) in enumerate(bands):
            for q in qs:
                xpos[q] = len(xpos) + gap * b
        labels = [tick_label(q) for q in queries]
        rotate = max(len(t) for t in labels) > 5

        panel_in = 2.7
        spread_in = 0.22 if over_best else 0.08        # room for the spread above each strip
        # a band's title wraps to the band's width (about 5 characters of 10 pt per column, the gap between bands included)
        titles = [textwrap.fill(t, max(8, int((len(qs) + gap) * 5))) for t, qs in bands]
        lines = max(t.count("\n") + 1 for t in titles)
        shares_in, band_title_in = 0.36, 0.25 + 0.17 * (lines - 1)    # the two share rows above the top panel, the band titles
        gap_in = spread_in + 0.15
        top_in = spread_in + shares_in + band_title_in + 0.45
        bottom_in = 0.5 + (0.075 * max(len(t) for t in labels) if rotate else 0)
        height = 3 * panel_in + 2 * gap_in + top_in + bottom_in
        fig, axes = plt.subplots(3, 1, sharex=True, figsize=(1.4 + 0.55 * len(queries), height),
                                 gridspec_kw={"hspace": gap_in / panel_in, "top": 1 - top_in / height,
                                              "bottom": bottom_in / height})
        drawn = 0
        for ax, (model, value) in zip(axes, MODELS):
            values = []
            for q in queries:
                cost = [value(float(r["wops"]), float(r["bytes"])) for r in by_q[q]]
                base = min(cost) if over_best else 1.0
                ys = [c / base for c in cost]
                n = len(ys)
                for j, (r, y) in enumerate(zip(by_q[q], ys)):
                    strip_point(ax, strip_x(xpos[q], j, n, 0.6), y, r["cell"])
                med = statistics.median(ys)
                ax.plot([xpos[q] - 0.38, xpos[q] + 0.38], [med, med], color="black", lw=1.6, zorder=4)
                medians[(model, q)] = statistics.median(cost)
                if over_best:
                    spread = max(ys)       # whole numbers from 1000x: '2938x', never '2.94e+03x'
                    ax.annotate(f"{spread:.3g}x" if spread < 1000 else f"{spread:.0f}x", (xpos[q], 1),
                                xycoords=ax.get_xaxis_transform(), xytext=(0, 3), textcoords="offset points",
                                ha="center", va="bottom", fontsize=7.5)
                values += ys
                drawn += n
            ax.set_yscale("log")
            lo, hi = min(values) / 1.5, max(values) * 1.5
            if over_best:
                lo = 0.85
            ax.set_ylim(lo, hi)
            human = (lambda v: f"{v:g}x") if over_best else (decimal_bytes if model == "bytes" else short_count)
            log_axis(ax.yaxis, human, lo, hi)
            if math.log10(hi / lo) > 3:      # a wide panel: one label per decade
                ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0,)))
            if over_best:
                ax.axhline(1.0, ls="--", lw=0.9, color="grey", zorder=2)
            band_of = {q: b for b, (_, qs) in enumerate(bands) for q in qs}
            mids = [((xpos[q] + xpos[nxt]) / 2, band_of[q] == band_of[nxt]) for q, nxt in itertools.pairwise(queries)]
            separators(ax, [x for x, same in mids if same], lw=0.6, color="0.8")             # between queries
            separators(ax, [x for x, same in mids if not same], lw=1.4, color="0.35")        # between bands
            column = "query" if src["unit"] == "run" else "type"
            what = f"{model}\nover the {column}'s best" if over_best else f"{model}\nper {src['unit']}"
            ax.set_ylabel(what, fontsize=11)
            ax.tick_params(axis="y", labelsize=10)
            ax.grid(True, axis="y", which="major", alpha=0.3)
        axes[-1].set_xlim(-0.6, max(xpos.values()) + 0.6)
        axes[-1].set_xticks([xpos[q] for q in queries])
        axes[-1].set_xticklabels(labels, rotation=35 if rotate else 0, ha="right" if rotate else "center",
                                 rotation_mode="anchor")
        axes[-1].tick_params(axis="x", length=0, labelsize=10 if not rotate else 9)
        # above the top panel: the run-share and cost-share rows, then the band titles
        lift = 3 + spread_in * 72
        for k, (name, share) in enumerate((("runs", runs), ("cost", cost_share))):
            y_pt = lift + (1 - k) * 12
            for q in queries:
                axes[0].annotate(percent(share[q]), (xpos[q], 1), xycoords=axes[0].get_xaxis_transform(),
                                 xytext=(0, y_pt), textcoords="offset points", ha="center", va="bottom", fontsize=7.5)
            axes[0].annotate(name, (0, 1), xycoords="axes fraction", xytext=(-4, y_pt), textcoords="offset points",
                             ha="right", va="bottom", fontsize=7.5)
        for title, (_, qs) in zip(titles, bands):
            axes[0].annotate(title, ((xpos[qs[0]] + xpos[qs[-1]]) / 2, 1), xycoords=axes[0].get_xaxis_transform(),
                             xytext=(0, lift + (shares_in + 0.06) * 72), textcoords="offset points",
                             ha="center", va="bottom", fontsize=10)
        cells = {r["cell"] for q in queries for r in by_q[q]}
        handles, ncol = layout_legend_handles(cells)
        # the legend hangs just under the drawn tick labels (rotated ones reach further down)
        fig.canvas.draw()
        ticks_y0 = min(t.get_window_extent().y0 for t in axes[-1].get_xticklabels())
        below = axes[-1].transAxes.inverted().transform((0, ticks_y0))[1] - 0.04
        leg = axes[-1].legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, below), ncol=ncol, frameon=False,
                              handletextpad=0.3, columnspacing=1.2, fontsize=9)
        fig.canvas.draw()
        caption_y = leg.get_window_extent().y0 / fig.bbox.height - 0.01
        unit = src["unit"]
        title = f"{scale}: " + (f"measured cost of each layout over the {'query' if unit == 'run' else 'type'}'s cheapest layout"
                                if over_best else f"measured cost per {unit} of each {'query' if unit == 'run' else 'request type'}, "
                                                  f"{n_layouts} layouts")
        fig.suptitle(title, fontsize=12, y=1 - 0.12 / height)
        formulas = "; ".join(f"{m} = {src['formula'][m]}" for m, _ in MODELS)
        y_text = (f"each layout's cost over the cheapest of the {n_layouts} for that column and model (1x = the best; the "
                  "number above a strip is its worst over its best)" if over_best else f"the cost of one {unit}")
        fig.text(0.5, caption_y, f"Each point: one layout (colour: its adjacency layout; marker: its properties layout); "
                 f"y: {y_text}. {src['per']}; not weighted by how often it runs. Above the top panel: runs = its share of "
                 f"{mix}'s {unit}s (its weight over all weights); cost = its share of {mix}'s cost (weight x cost per {unit} "
                 f"over the total, wops_bytes4k, the median over the {n_layouts} layouts). Deterministic counts (no error "
                 f"bars). Black tick: the median layout. {formulas}.", ha="center", va="top", fontsize=8, wrap=True)
        fig.savefig(out, dpi=200, bbox_inches="tight")
        return drawn

    parts = [(bands, queries, out)]
    if chunk:      # one figure per run of consecutive queries, for a workload of one group too many to draw at once
        if len(bands) != 1:
            raise SystemExit(f"--chunk splits a workload of one group; {workload} has {len(bands)}: its groups band it")
        title, qs = bands[0]
        size = math.ceil(len(qs) / chunk)
        stem, ext = os.path.splitext(out)
        parts = [([(f"{title}, {tick_label(qs[k])} to {tick_label(qs[min(k + size, len(qs)) - 1])} (part {i + 1} of {chunk})",
                    qs[k:k + size])], qs[k:k + size], f"{stem}_part{i + 1}{ext}") for i, k in enumerate(range(0, len(qs), size))]
    drawn = sum(draw(b, qs, o) for b, qs, o in parts)
    out = ", ".join(o for _, _, o in parts)
    print(f"plot_measured_cost_by_query: measured strip {len(queries)} queries x {n_layouts} layouts x {len(MODELS)} "
          f"models = {drawn} points drawn, {left_out} left out (an empty or zero value); "
          f"{'over the best' if over_best else 'per ' + unit} -> {out}")
    ranked = sorted(queries, key=lambda q: medians[("wops_bytes4k", q)], reverse=True)
    fmt = lambda q: f"{tick_label(q)} {medians[('wops_bytes4k', q)]:.4g}"
    print(f"  median wops_bytes4k per {unit}: heaviest {', '.join(fmt(q) for q in ranked[:5])}; "
          f"lightest {', '.join(fmt(q) for q in ranked[-5:])}")
    print(f"  share of {mix}'s {unit}s: " + ", ".join(f"{tick_label(q)} {percent(runs[q])}" for q in
                                                   sorted(queries, key=lambda q: -runs[q])))
    print(f"  share of {mix}'s cost (median over the layouts): " + ", ".join(
        f"{tick_label(q)} {100 * cost_share[q]:.1f}%" for q in sorted(queries, key=lambda q: -cost_share[q])) +
        f"; sum of the {len(queries)} medians {100 * sum(cost_share.values()):.1f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("costs", help="a layout_choice by_query.csv, or per_query_disagreement's costs.csv with --costs-family")
    ap.add_argument("--family", dest="workload", required=True, help="the family (a query set) whose GROUPS band the queries")
    ap.add_argument("--costs-family", dest="family", help="a costs.csv: the family drawn (its 'family' column, e.g. LinkBench)")
    ap.add_argument("--over-best", action="store_true", help="each layout over that query's cheapest layout (1x = the best)")
    ap.add_argument("--same-measured", action="append", default=[], metavar="BY_QUERY_CSV",
                    help="another tier's by_query.csv whose measured columns must equal (repeatable)")
    ap.add_argument("--scale", required=True, help="the data's scale, the title's first words: SF3, SF30, 'LinkBench n1m'")
    ap.add_argument("--mix", required=True, help="whose weights the shares use, for the caption: I02, 'the request log'")
    ap.add_argument("--out", required=True, help="the figure to write (its extension picks the format: .png, .pdf)")
    ap.add_argument("--params", default="1-5", help="a by_query.csv: the params each query's cost is the mean over, for "
                    "the caption (default 1-5; JOB: 1; or the words themselves, LinkBench: 'the mean over its requests in the log')")
    ap.add_argument("--chunk", type=int, default=None, metavar="N", help="a workload of one group (JOB): N figures of "
                    "consecutive queries, <out stem>_part<i><ext>; the shares stay the whole workload's")
    a = ap.parse_args()
    apply_house_style(extralarge=3)
    plot_measured_strip(a.costs, a.same_measured, a.family, a.workload, a.over_best, a.scale, a.mix, a.out, a.chunk, a.params)


if __name__ == "__main__":
    main()
