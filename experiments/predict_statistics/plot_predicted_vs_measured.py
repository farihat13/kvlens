"""Predicted against measured cost of the 49 uniform layouts: I02's total (--totals), or each IC or IS query (--by-query).

Each point is one layout (colour: adjacency; marker: properties). Two figures:

- **--totals** (one panel): I02's cost, each side over its own best (x: measured over the measured best; y: predicted
  over predict's cheapest), in wops_bytes4k; one line above the panel names predict's pick against the measured
  best, from the data.
- **--totals-grid --series "tier 1=totals.csv" ...** (2x2): the four tiers' --totals panels on one scale; or 2 or 4
  series of any names (two workloads: "I01, tier 1=..." "B01, tier 1=..."), each panel titled by its name, on its own
  scale.
- On both totals views, opt-in: **--mark-pick** draws an open ring around the y side's pick (its cheapest, y = 1), no
  legend entry; **--y-label TEXT** replaces the y axis's words ("predicted cost over predict's cheapest").
- On --totals, opt-in, for a panel whose y side is not predict (two measured scales): **--pick-words W** and
  **--best-words W** replace "predict's pick" and "the measured best" in the line above the panel; **--x-label TEXT**
  replaces the x axis's words.
- **--by-query-ratio by_query.csv --group ic|is** (a grid, one panel per query): I02's totals view per query, each
  layout's measured over the query's measured best (x) and predicted over its predicted cheapest (y), wops_bytes4k,
  one log range on every panel and both axes, y = x; the title names rho over the 49, the query's share of I02's runs
  (its weight over all weights) and of I02's cost (weight x measured cost over the total, the median over the layouts).
- **--by-query --group ic|is|bi** (a grid): one row per query, one column per cost model: wops (G + 1.1 S + 0.01 N),
  bytes (key and value bytes read) and wops_bytes4k (wops + bytes / 4096). Absolute values per run (the mean over
  params 1-5), measured on x and predicted on y, both log; the dashed line is predicted = measured. Each panel's title
  gives Spearman's rho over its layouts (1 = the same order) and the median predicted / measured. Each side runs its
  own plan per (layout, query), as by_query.csv records it; --same-body costs predict on the measured side's plan
  instead.
- **--strip --group ic|is|bi** (three panels stacked: wops, bytes, wops_bytes4k; --model draws one): every query of the
  group side by side; in each query's column one strip per --series (a tier and its by_query.csv), the 49 layouts'
  predicted / measured per run (log, each panel its own range, 1x dashed); above each column Spearman's rho over the
  49 layouts, one per tier.

Inputs: the layout_choice dir of kvcost predict-accuracy Sp-Sp Sp-Co ... Ch256k-Ch256k (summary/ldbc/sf<SF>/predict-statistics/<workload>/<set>/)
or of a predict-results snapshot
(totals.csv, by_query.csv). Run from the artifact's root:

    R=<a predict-results snapshot>/tier1-flat/reports/full-q19/layout_choice
    uv run --locked python experiments/predict_statistics/plot_predicted_vs_measured.py --sf 3 --totals $R/totals.csv --out <dir>/w17_tier2.png
    uv run --locked python experiments/predict_statistics/plot_predicted_vs_measured.py --sf 3 --by-query $R/by_query.csv --group ic --out <dir>/ic_tier2.png
    uv run --locked python experiments/predict_statistics/plot_predicted_vs_measured.py --sf 3 --by-query $R/by_query.csv --group is --out <dir>/is_tier2.png
    uv run --locked python experiments/predict_statistics/plot_predicted_vs_measured.py --sf 3 --strip --group ic \
        --series "tier 1=<flat-q19 layout_choice>/by_query.csv" --series "tier 2=$R/by_query.csv" --out <dir>/strip_ic_tier1_tier2.png
Each writes the one file --out names. JOB (113 queries, one param, no anchor tier) takes --scale for the titles,
--params 1, --chunk 4 on --by-query and --strip (four figures, <out stem>_part<i>), and --totals-grid with tier 1 and
tier 2 alone:

    uv run --locked python experiments/predict_statistics/plot_predicted_vs_measured.py --scale "imdb full" --params 1 \
        --strip --group job --chunk 4 --series "tier 1=<t1>/by_query.csv" --series "tier 2=<t2>/by_query.csv" \
        --note "JOB's plans bind no anchor vertex, so + anchor equals each tier." --out <dir>/strip_job_tier1_tier2.png

LinkBench's request types share no prefix: --family linkbench names its GROUPS, and --group is one of their keys,
wide (its reads) or writes:

    uv run --locked python experiments/predict_statistics/plot_predicted_vs_measured.py --scale "LinkBench n1m" \
        --params "the mean over its requests in the log" --strip --family linkbench --group wide --series "tier 2=<t2>/by_query.csv" --out <dir>/strip_lb_reads.png
"""
import argparse
import csv
import math
import re
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from kvcost.layout_cost import query_weighting as QW
from kvcost.layout_cost.cost_models import wops_bytes4k
from kvcost.plotting.formatting import decimal_bytes, params_phrase, percent
from kvcost.plotting.stats import spearman
from kvcost.plotting.strip import layout_legend_handles, separators, strip_point, strip_x
from kvcost.plotting.style import apply_house_style, cell_scatter_kw, log_axis
from kvevaluator.measure.workload_plans import groups as workload_groups

CURRENCY = "wops_bytes4k"   # the model plans are picked by: G + 1.1 S + 0.01 N + bytes read / 4096
SAME_RATIO_AT = 2.2         # where the y = x line's label sits, just under the line (the points lie above it)
# the by-query columns: (title, the value from a row's wops and bytes)
MODELS = [("wops", lambda w, b: w), ("bytes", lambda w, b: b), ("wops_bytes4k", wops_bytes4k)]
MODEL_FORMULA = {"wops": "G + 1.1 S + 0.01 N", "bytes": "key and value bytes read",
                 "wops_bytes4k": "wops + bytes read / 4096"}
# the --strip series: the four tiers (docs/terminology.md) and the short label under each strip
TIER_SHORT = {"tier 1": "t1", "tier 1 + anchor": "t1+a", "tier 2": "t2", "tier 2 + anchor": "t2+a"}
TWO_TIERS = ("tier 1", "tier 2")   # --totals-grid of a workload whose anchor sets equal its tiers (JOB)
# set once by main: the queries one --chunk part draws (None: all), its title words, the params' words, a caption note
KEEP, PART, PARAMS, NOTE = [None], [""], ["the mean over params 1-5"], [""]
GROUP = [None]      # --family: (its group's title, its queries in GROUPS order); None: --group is a name prefix
MARK_PICK, Y_LABEL = [False], [None]   # the totals views: --mark-pick, --y-label (None: the fixed words)
PICK_WORDS, BEST_WORDS, X_LABEL = ["predict's pick"], ["the measured best"], [None]   # --totals: the words of each side
# around a s=34 marker; unclipped: at y = 1 on a wide log range the ring crosses the axes' lower edge
PICK_RING = dict(s=210, facecolors="none", edgecolors="#222222", linewidths=1.3, zorder=4, clip_on=False)


def short_count(v):
    """a count's tick label: 2, 50, 1.5k, 20M"""
    for unit, d in (("M", 1e6), ("k", 1e3)):
        if v >= d:
            return f"{v / d:g}{unit}"
    return f"{v:g}"


def ratio_axes(ax, hi=None):
    """log axes ticked as ratios (1x .. 7x; past 10x, `hi` the axes' top: 1x, 3x, 10x, 30x ... up to it; past 300x,
    one tick per decade, 1x, 10x, 100x ..., so the labels do not run together)"""
    ax.set_xscale("log")
    ax.set_yscale("log")
    ticks = [1, 1.5, 2, 3, 5, 7]
    if hi is not None and hi > 300:
        ticks = [t for t in (1, 10, 100, 1000, 10000, 100000) if t <= hi]
    elif hi is not None and hi > 10:
        ticks = [t for t in (1, 3, 10, 30, 100, 300, 1000, 3000, 10000) if t <= hi]
    for axis in (ax.xaxis, ax.yaxis):
        axis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
        axis.set_major_formatter(matplotlib.ticker.FixedFormatter([f"{t:g}x" for t in ticks]))
        axis.set_minor_formatter(matplotlib.ticker.NullFormatter())


def totals_data(path):
    """I02's total per layout, each side over its own best: (rows, x, y, pick, best)"""
    with open(path) as f:
        rows = sorted((r for r in csv.DictReader(f) if r["currency"] == CURRENCY), key=lambda r: r["layout"])
    if len(rows) != 49:
        raise SystemExit(f"{path}: {len(rows)} layouts under {CURRENCY}, want 49")
    m0 = min(float(r["measured"]) for r in rows)
    p0 = min(float(r["predicted"]) for r in rows)
    x = {r["layout"]: float(r["measured"]) / m0 for r in rows}
    y = {r["layout"]: float(r["predicted"]) / p0 for r in rows}
    pick = min(rows, key=lambda r: (float(r["predicted"]), r["layout"]))["layout"]
    best = min(rows, key=lambda r: (float(r["measured"]), r["layout"]))["layout"]
    return rows, x, y, pick, best


def totals_limits(datas, pad=1.1):
    """one range for both axes (a square panel, y = x corner to corner): 0.92 to 8x, widened to `pad` times the largest
    point when one of any of the data falls outside it: (xlim, ylim)"""
    hi = max(8.0, max(max(max(d[1].values()), max(d[2].values())) for d in datas) * pad)
    return (0.92, hi), (0.92, hi)


def pick_line(data):
    """the panel's one line: the y side's pick against the x side's best (--pick-words, --best-words), from the data"""
    _, x, _, pick, best = data
    if pick == best:
        return f"{PICK_WORDS[0]} = {BEST_WORDS[0]}: {best}"
    return f"{PICK_WORDS[0]}: {pick}, {x[pick]:.3f}x {BEST_WORDS[0]} ({best})"


def draw_totals(ax, data, xlim, ylim, title, line_size=9.5):
    """one panel: the 49 layouts and y = x; the title, and under it the pick line (pick_line)"""
    rows, x, y, pick, _ = data
    for r in rows:
        ax.scatter(x[r["layout"]], y[r["layout"]], s=34, zorder=3, **cell_scatter_kw(r["cell"]))
    if MARK_PICK[0]:    # the y side's pick: an open ring around its marker, no legend entry
        ax.scatter(x[pick], y[pick], **PICK_RING)
    ax.set_title(title, fontsize=12, pad=20)
    ax.text(0.5, 1.015, pick_line(data), transform=ax.transAxes, ha="center", va="bottom", fontsize=line_size)
    ratio_axes(ax, max(xlim[1], ylim[1]))
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_box_aspect(1)
    # y = x: a layout as far from predict's cheapest as from the measured best; behind the points, across the limits
    lo, hi = 0.92, max(ax.get_xlim()[1], ax.get_ylim()[1])
    ax.plot([lo, hi], [lo, hi], ls="--", lw=0.8, color="grey", zorder=2)
    ax.text(SAME_RATIO_AT, SAME_RATIO_AT / 1.06, "same ratio", color="grey", fontsize=8, rotation=45,
            rotation_mode="anchor", transform_rotates_text=True, ha="center", va="top")
    ax.grid(True, which="major", alpha=0.3)


def totals_line(data):
    """the sanity line of one panel"""
    rows, x, _, pick, best = data
    return f"{len(rows)} layouts; pick {pick} at {x[pick]:.3f}x of the measured best ({best})"


def totals_legend(fig, rows, y, fontsize):
    """the layouts' legend (colour: adjacency, marker: properties), its top at figure fraction y"""
    handles, ncol = layout_legend_handles({r["cell"] for r in rows})
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, y), ncol=ncol, frameon=False,
               fontsize=fontsize, handletextpad=0.2, columnspacing=0.8)


def plot_totals(path, out, sf, label=None):
    """one panel: I02's total per layout, each side over its own best"""
    data = totals_data(path)
    fig, ax = plt.subplots(figsize=(4.6, 4.2))
    draw_totals(ax, data, *totals_limits([data]), sf + (f", {label}" if label else ""))   # the grid's sizes
    ax.tick_params(labelsize=11)
    ax.set_xlabel(X_LABEL[0] or "measured cost over the measured best", fontsize=12)
    ax.set_ylabel(Y_LABEL[0] or "predicted cost over\npredict's cheapest", fontsize=12)
    fig.tight_layout()
    totals_legend(fig, data[0], ax.get_position().y0 - 0.62 / fig.get_figheight(), 8)
    save(fig, out)
    print(f"plot_predicted_vs_measured: {totals_line(data)} -> {out}")


def plot_totals_grid(series, out, sf, mix="I02"):
    """the four tiers' --totals panels in a 2x2 grid (tier 1 | tier 1 + anchor, tier 2 | tier 2 + anchor), one scale; or
    tier 1 | tier 2 in a row (a workload whose anchor sets equal its tiers: JOB); or any 2 or 4 named series (e.g.
    "I01, tier 1" and "B01, tier 1": two workloads), in the order given, each panel titled by its name"""
    given = dict(series)
    names = [t for t, _ in series]
    tiers = tuple(TIER_SHORT) if sorted(given) == sorted(TIER_SHORT) else TWO_TIERS
    named = sorted(given) != sorted(tiers)       # not the tiers of one workload: the panels are titled by the series
    if named:
        tiers = tuple(names)
    if len(given) != len(series) or len(series) not in (2, 4):
        known = f"{', '.join(repr(t) for t in TIER_SHORT)}, or {' and '.join(repr(t) for t in TWO_TIERS)}"
        raise SystemExit(f"--totals-grid: 2 or 4 --series with distinct names (the tiers {known}, or any names); "
                         f"got {names}")
    datas = {t: totals_data(given[t]) for t in tiers}
    rows = len(tiers) // 2
    # the tiers of one workload share one scale (1.1x over the top point: an I02 view whose top point is ~9x stays under
    # the 10x switch to sparse ticks); named series (two workloads, 8x beside 4000x) get one scale each, 1.15x over
    # their own top point so its marker is inside the frame
    limits = {t: totals_limits([datas[t]], pad=1.15) for t in tiers} if named else \
        dict.fromkeys(tiers, totals_limits(list(datas.values()), pad=1.1))
    # laid out in inches (tight_layout cannot place square panels); wide columns: a pick line is wider than its panel
    width, height = 11, (10.5 if rows == 2 else 5.9) + (0.35 if NOTE[0] else 0)
    left_in, right_in, top_in, hgap_in, wgap_in = 0.9, 0.3, 1.0, 0.85, 1.0
    legend_in, label_in, ticks_in = 0.6, 0.45, 0.35        # under the panels: the legend, the x label, the tick labels
    bottom_in = legend_in + label_in + ticks_in + (0.35 if NOTE[0] else 0)
    fig, axes = plt.subplots(rows, 2, figsize=(width, height), sharex=not named, sharey=not named, squeeze=False)
    fig.subplots_adjust(left=left_in / width, right=1 - right_in / width, top=1 - top_in / height,
                        bottom=bottom_in / height, wspace=wgap_in / ((width - left_in - right_in - wgap_in) / 2),
                        hspace=hgap_in / ((height - top_in - bottom_in - hgap_in) / 2) if rows == 2 else 0)
    for ax, tier in zip(axes.flat, tiers):          # tier 1, tier 1 + anchor, tier 2, ...; named series as given
        draw_totals(ax, datas[tier], *limits[tier], tier, line_size=9)
        ax.tick_params(labelsize=10)
        print(f"plot_predicted_vs_measured: {tier}: {totals_line(datas[tier])}")
    fig.supylabel(Y_LABEL[0] or "predicted cost over predict's cheapest", fontsize=13, x=0.15 / width, ha="left")
    whose = "each workload's" if named else f"{mix}'s"
    fig.suptitle(f"{sf}: {whose} total per layout, each side over its own best", fontsize=13)
    note_in = 0.35 if NOTE[0] else 0
    fig.supxlabel("measured cost over the measured best (wops_bytes4k)" if named else
                  f"measured cost over the measured best ({mix}, wops_bytes4k)", fontsize=13,
                  y=(legend_in + note_in) / height, va="bottom")
    totals_legend(fig, [r for d in datas.values() for r in d[0]], (legend_in + note_in) / height, 10)
    if NOTE[0]:
        fig.text(0.5, 0.1 / height, NOTE[0].strip(), ha="center", va="bottom", fontsize=10)
    save(fig, out)
    print(f"plot_predicted_vs_measured: {len(tiers)} panels x 49 layouts -> {out}")


def in_group(q, group):
    """is query `q` one of --group's: a query of the --family's group of that key, else a name with that prefix"""
    return q in GROUP[0][1] if GROUP[0] else re.fullmatch(rf"{group}\d+[a-z]?", q) is not None


def group_label(group):
    """the group's name in a title: its GROUPS title (--family), else the prefix in capitals"""
    return GROUP[0][0] if GROUP[0] else group.upper()


def query_label(q):
    """a query's name in a label: as written for a --family group (LinkBench's getlinklist_link1), else in capitals"""
    return q if GROUP[0] else q.upper()


def query_order(q):
    """ic2 before ic10; job1a before job1b before job2a; a --family group's queries in its GROUPS order"""
    if GROUP[0]:
        return (GROUP[0][1].index(q), 0, "")
    m = re.match(r"([a-z]+)(\d+)([a-z]?)$", q)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (q, 0, "")


def plot_by_query(path, group, same_body, out, sf, label=None):
    """a grid: one row per query of the group, one column per cost model, 49 layouts per panel"""
    pw, pb = ("predicted_wops_at_measured_body", "predicted_bytes_at_measured_body") if same_body else \
        ("predicted_wops", "predicted_bytes")
    rows = [r for r in csv.DictReader(open(path)) if in_group(r["query"], group)
            and (KEEP[0] is None or r["query"] in KEEP[0])]
    queries = sorted({r["query"] for r in rows}, key=query_order)
    if not queries:
        raise SystemExit(f"{path}: no {group} query")
    fig, axes = plt.subplots(len(queries), len(MODELS), figsize=(3.0 * len(MODELS), 2.5 * len(queries)), squeeze=False)
    left_out = 0
    for i, q in enumerate(queries):
        mine = sorted((r for r in rows if r["query"] == q), key=lambda r: r["layout"])
        if len(mine) != 49:
            raise SystemExit(f"{path}: {q} has {len(mine)} layouts, want 49")
        ok = [r for r in mine if r[pw] != "" and r["measured_wops"] != ""]
        left_out += len(mine) - len(ok)
        for j, (name, value) in enumerate(MODELS):
            ax = axes[i][j]
            xs = [value(float(r["measured_wops"]), float(r["measured_bytes"])) for r in ok]
            ys = [value(float(r[pw]), float(r[pb])) for r in ok]
            for r, xv, yv in zip(ok, xs, ys):
                ax.scatter(xv, yv, s=18, zorder=3, **cell_scatter_kw(r["cell"]))
            pos = [v for v in xs + ys if v > 0]
            if pos:
                lo, hi = min(pos) / 1.3, max(pos) * 1.3
                ax.plot([lo, hi], [lo, hi], ls="--", lw=0.8, color="grey", zorder=2)
            ax.set_xscale("log")
            ax.set_yscale("log")
            if pos:
                ax.set_xlim(lo, hi)
                ax.set_ylim(lo, hi)
                human = decimal_bytes if name == "bytes" else short_count
                for axis in (ax.xaxis, ax.yaxis):
                    log_axis(axis, human, lo, hi)
                    # fewer ticks where the labels would run together: a wide panel, or bytes (long labels)
                    decades = math.log10(hi / lo)
                    if decades > 3 or (name == "bytes" and decades > 2):
                        axis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0,)))
                    elif name == "bytes" and decades > 1:
                        axis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0, 3.0)))
                    elif name == "bytes":
                        axis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
            rho = spearman(xs, ys)
            ratios = [yv / xv for xv, yv in zip(xs, ys) if xv > 0]
            med = statistics.median(ratios) if ratios else None
            # the panel's numbers go in its title, never inside the axes, where they could cover points
            stats = (f"rho {rho:.2f}" if rho is not None else "rho -") + \
                (f" · pred/meas {med:.2f}x" if med is not None else "")
            ax.tick_params(labelsize=7)
            ax.grid(True, which="major", alpha=0.3)
            ax.set_title(f"{name}\n{stats}" if i == 0 else stats, fontsize=8.5)
            if j == 0:
                ax.set_ylabel(f"{query_label(q)}\npredicted", fontsize=9)
            if i == len(queries) - 1:
                ax.set_xlabel("measured", fontsize=9)
    body = "predict on the measured side's plan" if same_body else "each side its own plan"
    what = f"{sf}, {group_label(group)}{PART[0]}" + (f", {label}" if label else "")
    fig.suptitle(f"{what}: predicted vs measured per run, 49 layouts ({body})" + (f"\n{NOTE[0].strip()}" if NOTE[0] else ""),
                 fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.985))
    save(fig, out)
    print(f"plot_predicted_vs_measured: {group} {len(queries)} queries x {len(MODELS)} models, 49 layouts each; "
          f"{left_out} (layout, query) pairs left out (no predicted or measured value); {body} -> {out}")


def ratio_ticks(lo, hi):
    """ratio ticks inside lo..hi: 1, 1.5, 2, 3, 5, 7 per decade on a narrow range, 1, 2, 5 on a wide one"""
    subs = (1, 1.5, 2, 3, 5, 7) if hi / lo < 30 else (1, 2, 5)
    return [d * m for d in (10 ** k for k in range(-1, 6)) for m in subs if lo <= d * m <= hi]


def plot_by_query_ratio(path, group, out, sf, label=None):
    """per query of the group, one panel: each layout's measured over the query's measured best (x) and predicted over
    its predicted cheapest (y), wops_bytes4k, one log range on every panel and both axes, y = x"""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if "" in (r["measured_wops"], r["measured_bytes"], r["predicted_wops"], r["predicted_bytes"]):
            raise SystemExit(f"{path}: {r['layout']} {r['query']} has no measured or predicted value (the shares need all)")
    weight = {r["query"]: float(r["weight"]) for r in rows}
    per_query = {}
    for r in rows:
        per_query.setdefault(r["query"], {})[r["layout"]] = wops_bytes4k(float(r["measured_wops"]),
                                                                         float(r["measured_bytes"]))
    runs, shares = QW.shares(per_query, weight)      # its share of I02's runs, and of I02's cost (median over layouts)
    queries = sorted({r["query"] for r in rows if in_group(r["query"], group)}, key=query_order)
    if not queries:
        raise SystemExit(f"{path}: no {group} query")
    panels, left_out = {}, 0
    for q in queries:
        mine = sorted((r for r in rows if r["query"] == q), key=lambda r: r["layout"])
        if len(mine) != 49:
            raise SystemExit(f"{path}: {q} has {len(mine)} layouts, want 49")
        m = [wops_bytes4k(float(r["measured_wops"]), float(r["measured_bytes"])) for r in mine]
        p = [wops_bytes4k(float(r["predicted_wops"]), float(r["predicted_bytes"])) for r in mine]
        keep = [i for i in range(len(mine)) if m[i] > 0 and p[i] > 0]
        left_out += len(mine) - len(keep)
        m0, p0 = min(m[i] for i in keep), min(p[i] for i in keep)
        panels[q] = ([mine[i]["cell"] for i in keep], [m[i] / m0 for i in keep], [p[i] / p0 for i in keep],
                     spearman([m[i] for i in keep], [p[i] for i in keep]))
    hi = max(max(max(xs), max(ys)) for _, xs, ys, _ in panels.values()) * 1.15
    lo = 0.9
    ticks = ratio_ticks(lo, hi)
    ncol = 4
    nrow = math.ceil(len(queries) / ncol)
    # laid out in inches, so the panels stay square and no band opens under them: the bottom strip holds the caption,
    # the legend (two rows), the shared x label and the bottom tick labels; a row gap holds tick labels and a title
    caption_in, legend_in, label_in, ticks_in = 0.45, 0.5, 0.35, 0.3
    left_in, right_in, wgap_in, top_in, hgap_in = 0.8, 0.15, 0.35, 0.85, 0.75
    width = 3.0 * ncol
    side = (width - left_in - right_in - (ncol - 1) * wgap_in) / ncol
    bottom_in = caption_in + legend_in + label_in + ticks_in
    height = top_in + nrow * side + (nrow - 1) * hgap_in + bottom_in
    fig, axes = plt.subplots(nrow, ncol, figsize=(width, height), sharex=True, sharey=True, squeeze=False,
                             gridspec_kw={"left": left_in / width, "right": 1 - right_in / width,
                                          "top": 1 - top_in / height, "bottom": bottom_in / height,
                                          "wspace": wgap_in / side, "hspace": hgap_in / side})
    for ax, q in zip(axes.flat, queries):
        cells, xs, ys, rho = panels[q]
        for cell, xv, yv in zip(cells, xs, ys):
            ax.scatter(xv, yv, s=18, zorder=3, **cell_scatter_kw(cell))
        ax.plot([lo, hi], [lo, hi], ls="--", lw=0.8, color="grey", zorder=2)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
        ax.set_box_aspect(1)
        for axis in (ax.xaxis, ax.yaxis):
            axis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))
            axis.set_major_formatter(matplotlib.ticker.FixedFormatter([f"{t:g}x" for t in ticks]))
            axis.set_minor_formatter(matplotlib.ticker.NullFormatter())
        ax.tick_params(labelsize=9)
        ax.grid(True, which="major", alpha=0.3)
        rho_text = f"rho {rho:.2f}" if rho is not None else "rho -"
        ax.set_title(f"{q.upper()} · {rho_text}\n{percent(runs[q])} of runs, {percent(shares[q])} of I02's cost",
                     fontsize=10)
    for k in range(len(queries), nrow * ncol):
        axes.flat[k].set_visible(False)
        axes.flat[k - ncol].xaxis.set_tick_params(labelbottom=True)    # the panel above an empty one keeps its ticks
    what = f"{sf}, {group_label(group)}{PART[0]}" + (f", {label}" if label else "")
    fig.suptitle(f"{what}: each layout's cost over its query's best, predicted against measured (wops_bytes4k)",
                 fontsize=12, y=1 - 0.1 / height, va="top")
    fig.supxlabel(f"measured cost over the query's measured best (per run, {PARAMS[0]})", fontsize=11,
                  y=(caption_in + legend_in) / height, va="bottom")
    fig.text(0.5, 0.05 / height, "Runs: the query's share of I02's runs (its weight over the 19 queries' weights). "
             "I02's cost: weight x measured cost per run over I02's total, the median over the 49 layouts "
             "(wops_bytes4k = wops + bytes read / 4096). rho: Spearman over the 49 layouts, predicted vs measured.",
             ha="center", va="bottom", fontsize=8.5, wrap=True)
    fig.supylabel("predicted cost over the query's predicted cheapest", fontsize=11, x=0.1 / width, ha="left")
    handles, ncol_leg = layout_legend_handles({c for cells, *_ in panels.values() for c in cells})
    fig.legend(handles=handles, loc="lower center", ncol=ncol_leg, frameon=False, fontsize=9, handletextpad=0.3,
               columnspacing=1.2, bbox_to_anchor=(0.5, caption_in / height))
    save(fig, out)
    print(f"plot_predicted_vs_measured: ratio {group} {len(queries)} queries x 49 layouts, {left_out} left out "
          f"(a zero cost); one range 0.9x-{hi:.3g}x -> {out}")
    print("  share of I02's runs: " + ", ".join(f"{q.upper()} {percent(v)}" for q, v in
                                                sorted(runs.items(), key=lambda kv: -kv[1])))
    print("  share of I02's cost (median over the layouts): " + ", ".join(
        f"{q.upper()} {100 * v:.1f}%" for q, v in sorted(shares.items(), key=lambda kv: -kv[1])) +
        f"; sum of the {len(shares)} medians {100 * sum(shares.values()):.1f}%")


def strip_points(path, group, model):
    """{query: [(layout, measured, predicted, cell)]} of one by_query.csv, in `model`, and the pairs left out (an empty or
    zero value)"""
    value = dict(MODELS)[model]
    out, left_out = {}, 0
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if not in_group(r["query"], group) or (KEEP[0] is not None and r["query"] not in KEEP[0]):
            continue
        cols = ("measured_wops", "measured_bytes", "predicted_wops", "predicted_bytes")
        if any(r[c] == "" for c in cols):
            left_out += 1
            continue
        m = value(float(r["measured_wops"]), float(r["measured_bytes"]))
        p = value(float(r["predicted_wops"]), float(r["predicted_bytes"]))
        if m <= 0 or p <= 0:
            left_out += 1
            continue
        out.setdefault(r["query"], []).append((r["layout"], m, p, r["cell"]))
    return out, left_out


RHO_LINE_PT = 11      # the rho rows above a strip panel: one per tier, this many points apart


def load_strip_series(series, group, model):
    """[(tier, {query: [(layout, measured, predicted, cell)]}, left out)] in `model`, and the queries; refuses a tier name
    outside the four, a query missing from a series, or a query without 49 layouts"""
    loaded = []
    for label, path in series:
        if label not in TIER_SHORT:
            raise SystemExit(f"--series {label!r}: name it {', '.join(repr(t) for t in TIER_SHORT)}")
        pts, left = strip_points(path, group, model)
        loaded.append((label, pts, left))
    queries = sorted(loaded[0][1], key=query_order)
    if not queries:
        raise SystemExit(f"{series[0][1]}: no {group} query")
    for label, pts, _ in loaded:
        if sorted(pts, key=query_order) != queries:
            raise SystemExit(f"--series {label!r}: queries {sorted(pts, key=query_order)}, want {queries}")
        for q in queries:
            if len(pts[q]) != 49:
                raise SystemExit(f"--series {label!r}: {q} has {len(pts[q])} layouts, want 49")
    return loaded, queries


def strip_panel(ax, loaded, queries, model, bottom):
    """one model's panel: per query a column, per tier a strip of the 49 layouts' predicted / measured (colour: the
    adjacency, marker: the properties), the rho rows above it, and (bottom) each strip's tier under it; returns the
    points drawn"""
    n = len(loaded)
    slot = 0.8 / n                       # each tier's share of a query's column
    ratios = []
    for k, (label, pts, _) in enumerate(loaded):
        centre = (k - (n - 1) / 2) * slot
        for i, q in enumerate(queries):
            rows = sorted(pts[q])        # by layout name, so the jitter is the same on every run
            for j, (_, m, p, cell) in enumerate(rows):
                x = strip_x(i + centre, j, len(rows), slot, 0.75)
                strip_point(ax, x, p / m, cell)
                ratios.append(p / m)
            rho = spearman([m for _, m, _, _ in rows], [p for _, _, p, _ in rows])
            ax.annotate(f"{rho:.2f}" if rho is not None else "-", (i + centre, 1), xycoords=ax.get_xaxis_transform(),
                        xytext=(0, 3 + RHO_LINE_PT * (n - 1 - k)), textcoords="offset points", ha="center",
                        va="bottom", fontsize=7.5)
            if bottom:
                ax.annotate(TIER_SHORT[label], (i + centre, 0), xycoords=ax.get_xaxis_transform(), xytext=(0, -3),
                            textcoords="offset points", ha="center", va="top", fontsize=7.5, color="0.3")
            if k < n - 1:                # the light line between this tier's strip and the next one
                separators(ax, [i + centre + slot / 2], lw=0.5, color="0.88")
        # the rho row's tier, left of the panel
        ax.annotate(f"rho {TIER_SHORT[label]}", (0, 1), xycoords="axes fraction",
                    xytext=(-4, 3 + RHO_LINE_PT * (n - 1 - k)), textcoords="offset points", ha="right", va="bottom",
                    fontsize=7.5)
    ax.axhline(1.0, ls="--", lw=0.9, color="grey", zorder=2)
    separators(ax, [i + 0.5 for i in range(len(queries) - 1)], lw=0.9, color="0.55")
    ax.set_yscale("log")
    lo, hi = min(ratios) / 1.4, max(ratios) * 1.4
    ax.set_ylim(lo, hi)
    log_axis(ax.yaxis, lambda v: f"{v:g}x", lo, hi)
    if math.log10(hi / lo) > 5:      # a very wide panel (JOB: ~8 decades; LDBC stays under 5): one label per decade, 1/N below 1x
        ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0,)))
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:g}x" if v >= 1 else f"1/{1 / v:,.0f}x"))
    ax.set_xlim(-0.5, len(queries) - 0.5)
    ax.set_xticks(range(len(queries)))
    if GROUP[0]:    # long names (LinkBench's getlinklist_link1): one line per part, so neighbours do not overlap
        ax.set_xticklabels([query_label(q).replace("_", "\n") for q in queries])
    else:
        ax.set_xticklabels([q.upper() for q in queries])
    ax.tick_params(axis="x", length=0, labelsize=11, pad=14)    # the pad leaves room for the tier labels
    ax.tick_params(axis="y", labelsize=10)
    ax.set_ylabel(f"{model}\npredicted / measured", fontsize=11)
    ax.grid(True, axis="y", which="major", alpha=0.3)
    return len(ratios)


def plot_strip(series, group, models, out, sf):
    """one panel per model, stacked; in each, one column per query of the group and one strip per tier"""
    per_model = [(m, *load_strip_series(series, group, m)) for m in models]
    queries = per_model[0][2]
    n = len(series)
    panel_in, band_in = 3.0, (3 + RHO_LINE_PT * n + 6) / 72     # a panel's height; the rho rows' band above it
    width = 1.2 + len(queries) * (0.35 + 0.28 * n)
    # sized in inches: each axes panel_in tall; the gap between two panels holds the lower one's rho rows and the upper
    # one's lowest tick label; above the top panel, its rho rows and the title
    gap_in, top_in, bottom_in = band_in + 0.15, band_in + 0.45, 0.6
    height = len(models) * panel_in + (len(models) - 1) * gap_in + top_in + bottom_in
    fig, axes = plt.subplots(len(models), 1, sharex=True, squeeze=False, figsize=(width, height),
                             gridspec_kw={"hspace": gap_in / panel_in, "top": 1 - top_in / height,
                                          "bottom": bottom_in / height})
    axes = [row[0] for row in axes]
    drawn = 0
    for ax, (model, loaded, _) in zip(axes, per_model):
        drawn += strip_panel(ax, loaded, queries, model, ax is axes[-1])
    for ax in axes[:-1]:
        ax.tick_params(axis="x", labelbottom=False)
    cells = {c for _, loaded, _ in per_model for _, pts, _ in loaded for rows in pts.values() for *_, c in rows}
    handles, ncol = layout_legend_handles(cells)
    leg = axes[-1].legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.17 * 3.0 / panel_in),
                          ncol=ncol, frameon=False, handletextpad=0.3, columnspacing=1.2, fontsize=9)
    # the caption goes under the legend's drawn extent, so the two never overlap
    fig.canvas.draw()
    caption_y = leg.get_window_extent().y0 / fig.bbox.height - 0.01
    axes[0].set_title(f"{sf}, {group_label(group)}{PART[0]}: predicted over measured cost, 49 layouts per query and tier", fontsize=11,
                      pad=band_in * 72 + 2)
    formulas = "; ".join(f"{m} = {MODEL_FORMULA[m]}" for m in models)
    key = ", ".join(f"{TIER_SHORT[t]} = {t}" for t, _ in series)
    fig.text(0.5, caption_y, "Each point: one layout (colour: its adjacency layout; marker: its properties layout); "
             f"predicted and measured cost per run, {PARAMS[0]}, each side its own plan. Each query's "
             f"column holds one strip per tier: {key}. {formulas}. Above each strip: Spearman's rho over the 49 "
             "layouts, predicted vs measured." + NOTE[0], ha="center", va="top", fontsize=8, wrap=True)
    save(fig, out)
    left = sum(l for _, loaded, _ in per_model for _, _, l in loaded)
    print(f"plot_predicted_vs_measured: strip {group} {len(queries)} queries x {n} tiers x 49 layouts x {len(models)} "
          f"models = {drawn} points drawn, {left} left out (an empty or zero value); {', '.join(models)} -> {out}")


def save(fig, out):
    fig.savefig(out, dpi=200, bbox_inches="tight")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--totals", help="a layout_choice totals.csv: I02's total per layout")
    src.add_argument("--by-query", help="a layout_choice by_query.csv: each query per layout")
    src.add_argument("--by-query-ratio", metavar="BY_QUERY_CSV",
                     help="per query of --group, each layout over the query's best, predicted against measured")
    src.add_argument("--totals-grid", action="store_true",
                     help="the four tiers' I02 totals in a 2x2 grid, one --series 'TIER=totals.csv' per tier; or 2 or "
                     "4 --series of any names, one panel each, titled by the name, each on its own scale")
    src.add_argument("--strip", action="store_true", help="every query of --group in one figure, one strip per --series")
    ap.add_argument("--group", help="--by-query, --strip: the queries drawn, by their names' prefix (ic, is, bi for LDBC BI; "
                    "job for JOB; --by-query-ratio: ic or is), or with --family a key of its GROUPS (linkbench: wide, "
                    "writes); a group with no query in the input is refused")
    ap.add_argument("--family", dest="workload", help="--by-query, --strip: a registered workload whose GROUPS name the queries (--group is "
                    "then a group key, its title the figure's)")
    ap.add_argument("--same-body", action="store_true", help="--by-query: cost predict on the measured side's plan")
    ap.add_argument("--series", action="append", default=[], metavar="TIER=BY_QUERY_CSV",
                    help="--strip (a by_query.csv) or --totals-grid (a totals.csv), repeated, in drawing order: a tier ('tier 1', 'tier 1 + anchor', 'tier 2', "
                         "'tier 2 + anchor') and its by_query.csv")
    ap.add_argument("--model", choices=[name for name, _ in MODELS],
                    help="--strip: draw this one model (default: one panel per model, wops, bytes, wops_bytes4k)")
    ap.add_argument("--label", help="a title naming the statistics: 'tier 1', 'tier 1 + anchor', 'tier 2' or 'tier 2 + anchor'")
    ap.add_argument("--mix", default="I02", help="--totals-grid: the workload the totals.csv files cost, for the titles (default I02; B01: LDBC BI)")
    ap.add_argument("--sf", help="the scale factor the inputs are at, for the titles: 3, 30 (SF3, SF30)")
    ap.add_argument("--scale", help="the titles' scale words in place of SF<--sf>: 'imdb full'")
    ap.add_argument("--params", default="1-5", help="the params each query's cost is the mean over, for the captions "
                    "(default 1-5; JOB: 1; or the words themselves, LinkBench: 'the mean over its requests in the log')")
    ap.add_argument("--chunk", type=int, metavar="N", help="--by-query, --strip: N figures of consecutive queries of "
                    "--group, <out stem>_part<i><ext> (JOB's 113)")
    ap.add_argument("--note", help="a sentence added to the caption (or under the title where a figure has none); refused with --totals")
    ap.add_argument("--mark-pick", action="store_true", help="--totals, --totals-grid: an open ring around the y side's "
                    "pick (its cheapest layout), no legend entry")
    ap.add_argument("--y-label", help="--totals, --totals-grid: the y axis's words (default \"predicted cost over "
                    "predict's cheapest\")")
    ap.add_argument("--pick-words", help="--totals: the y side's pick in the line above the panel (default \"predict's "
                    "pick\"; e.g. \"SF3's best\")")
    ap.add_argument("--best-words", help="--totals: the x side's best in that line (default \"the measured best\"; e.g. "
                    "\"SF30's best\")")
    ap.add_argument("--x-label", help="--totals: the x axis's words (default \"measured cost over the measured best\")")
    ap.add_argument("--out", required=True, help="the figure to write (its extension picks the format: .png, .pdf)")
    a = ap.parse_args()
    if bool(a.sf) == bool(a.scale):
        ap.error("give --sf or --scale")
    a.sf = a.scale or f"SF{a.sf}"
    PARAMS[0] = params_phrase(a.params)
    NOTE[0] = f" {a.note.strip()}" if a.note else ""
    if a.note and a.totals:
        ap.error("--note: the --totals panel has no caption line to draw it in; put the sentence in the figure's caption")
    if (a.mark_pick or a.y_label) and not (a.totals or a.totals_grid):
        ap.error("--mark-pick and --y-label go with --totals or --totals-grid")
    MARK_PICK[0], Y_LABEL[0] = a.mark_pick, a.y_label
    if (a.pick_words or a.best_words or a.x_label) and not a.totals:
        ap.error("--pick-words, --best-words and --x-label go with --totals")
    PICK_WORDS[0] = a.pick_words or PICK_WORDS[0]
    BEST_WORDS[0] = a.best_words or BEST_WORDS[0]
    X_LABEL[0] = a.x_label
    if a.chunk and not (a.by_query or a.strip):
        ap.error("--chunk goes with --by-query or --strip")
    apply_house_style(extralarge=3)
    if a.workload:
        if a.by_query_ratio:
            raise SystemExit("--by-query-ratio takes --group ic|is, not --family")
        gs = {g["key"]: g for g in workload_groups(a.workload)}
        if a.group not in gs:
            raise SystemExit(f"--group: one of {sorted(gs)} for --family {a.workload}")
        GROUP[0] = (gs[a.group]["title"], gs[a.group]["queries"])
    if a.chunk:
        return chunked(a)
    draw(a)


def chunked(a):
    """--chunk N: the group's queries (from the first input), in order, split into N runs; one figure per run"""
    import os
    first = a.by_query or a.series[0].split("=", 1)[1]
    with open(first) as f:
        qs = sorted({r["query"] for r in csv.DictReader(f) if in_group(r["query"], a.group)},
                    key=query_order)
    if not qs:
        raise SystemExit(f"{first}: no {a.group} query")
    size = math.ceil(len(qs) / a.chunk)
    stem, ext = os.path.splitext(a.out)
    out = a.out
    for i, k in enumerate(range(0, len(qs), size)):
        part = qs[k:k + size]
        first, last = (q if GROUP[0] else q[len(a.group):].upper() for q in (part[0], part[-1]))
        KEEP[0], PART[0] = set(part), f" {first} to {last} (part {i + 1} of {a.chunk})"
        a.out = f"{stem}_part{i + 1}{ext}"
        draw(a)
    a.out = out


def draw(a):
    """the one figure the flags name"""
    if a.totals:
        plot_totals(a.totals, a.out, a.sf, a.label)
    elif a.by_query_ratio:
        if a.group not in ("ic", "is"):     # its titles and shares are I02's
            raise SystemExit("--by-query-ratio needs --group ic|is")
        plot_by_query_ratio(a.by_query_ratio, a.group, a.out, a.sf, a.label)
    elif a.totals_grid:
        series = [s.split("=", 1) for s in a.series]
        if any(len(s) != 2 for s in series):
            raise SystemExit("--series takes TIER=TOTALS_CSV")
        plot_totals_grid([(t.strip(), p) for t, p in series], a.out, a.sf, a.mix)
    elif a.strip:
        if not a.group or not a.series:
            raise SystemExit("--strip needs --group (a prefix, or a --family group) and one --series per tier")
        if a.same_body or a.label:
            raise SystemExit("--strip takes neither --same-body nor --label (the legend names the tiers)")
        series = [s.split("=", 1) for s in a.series]
        if any(len(s) != 2 for s in series):
            raise SystemExit("--series takes TIER=BY_QUERY_CSV")
        models = [a.model] if a.model else [name for name, _ in MODELS]
        plot_strip([(t.strip(), p) for t, p in series], a.group, models, a.out, a.sf)
    else:
        if not a.group:
            raise SystemExit("--by-query needs --group (a prefix, or a --family group)")
        plot_by_query(a.by_query, a.group, a.same_body, a.out, a.sf, a.label)


if __name__ == "__main__":
    main()
