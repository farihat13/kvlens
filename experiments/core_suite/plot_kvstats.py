"""The run-kvstats figures: one set per (sf, workload), drawn from the harness's captured
CSVs and nothing else (analysis reads, never measures; --no-measure / --plots-only re-draw).

Inputs (measure/kvstats_layout: one file per STORE slug; a SERIES here is a slug): the counts and their per-step rows
from the workload's capture, output/captures/<dataset>/<scale>/<workload>/reported (grid_bodies / grid_writes --format;
kvstats_layout.load_counts and store_steps take the body the runners run: every op count and byte lane, the used lanes
-- the amplification source), and the latency under the workload's timings dir (kvstats_layout.timings_root):
  <slug>/warm-p0/steps.csv        cpplatency perf 0: per-step rows (kind kv|flow, wall_ns) + __plan__
  <slug>/cold-p2/steps.csv        cpplatency perf 2, direct I/O: same, cold cache
  <slug>/durable-p1/steps.csv     cppwlatency (writes): per-step apply wall + paired shadow wall
--show names the slugs to draw (default: every slug present); the figures dir follows the
SHOWN selection (kvstats_layout.plots_dir: plots/ for the two baselines dgraph + nebula, else
plots-<extra slugs>/), so one selection never overwrites another's figures.

Every figure is SMALL MULTIPLES -- one subplot per query, the formats as the bars inside --
with a LINEAR y-axis scaled to its own subplot (a stack must read additively; log would
break that, and the per-query panels keep one giant query from flattening the rest).

Figures -> the selection's plots dir (a figure whose CSV is absent is skipped with a note). Names
say stage + metric (the dir names the workload):
  grid_<tier>.png    the GRID: four metric rows x one column per query, formats as the bars in each
                     cell, every row an op-stack (weighted KV ops by op | read bytes by channel | warm
                     latency kv/flow | cold latency kv/flow), each cell its own scale. One file per
                     TIER (the registry's `figure` tag: ldbc ic | is; microbench reads | single-hop |
                     multi-hop), the tier's query groups as bands
  per-query_read-amplification[_<tier>].png   hit bytes read / an ideal store's used bytes, value and
                     key lanes, per query x format (reads only)
  per-query_cold-block-reads[_<tier>].png     cold-pass physical block reads per query x format
                     (both banded by query group: ldbc one file per tier, others one banded file)
  per-group_size-normalized_ratio-to-dg_dist.png   per query group the distribution of per-query
                     ratios to the baseline format (box + strip, geomean marked), one row per currency:
                     the size-NORMALIZED view (every query weighs the same)
  suite-totals[-<tier>].png          per query group (a row) and metric (a column) the group's total per
                     format = the sum of per-query means (latency: medians), bar labels = ratio to the baseline: the
                     size-WEIGHTED view (the largest query sets it, named in the caption); ldbc one file
                     per tier (ic | is), other workloads one banded file
  tradeoff-ops-bytes_size-weighted.png, _size-normalized.png   the ops-vs-bytes Pareto front (every workload;
                                       writes: weighted write ops vs bytes moved), a pair:
                     suite totals (the heaviest queries set it) and the geomean over queries (a typical
                     query); a front that differs between the two is the finding
  store-size.png     the store size on disk
  cost-latency-summary[-<tier>].png, cost-latency-perquery[-<tier>].png   does KV cost predict the
                     evaluator's latency, size-controlled (two angles, five currencies); banded like
                     suite-totals. writes: the same pair against the durable write wall, in currencies that
                     span the read-modify-write's read side, write side and both (P+D, S+G, write /
                     read / r+w bytes, an ops x bytes blend)
  io-validation_block-reads-vs-currency.png   does the counted currency track the physical block
                     reads (a proxy check, not cost-vs-latency) + the all-queries pair matrix
  writes/grid_<group>.png            the WRITES workload: one column per write, rows weighted ops
                     (P|D|S|G|N at 1:1:1.1:1:0.01) | bytes read beside written | write latency =
                     the apply wall as read-modify-write | WAL | memtable | other -- real units, every
                     bar labelled with its ratio to the reference slug (dgraph-c512k when drawn). The
                     formulas: measure/kvstats_derive.py, and only there.
  writes/write-amplification_<group>.png   (bytes read + written) / payload per write, log y

Currency: bytes are the C++ engine's realistic codec bytes (physical), not the neutral model.

    python3 experiments/core_suite/plot_kvstats.py --sf 0.1 --family interactive [--root <output tree>]
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from kvcost.plotting.style import (apply_house_style, byte_channel_color, byte_channel_label, op_color, op_label,
                                   step_kind_color, step_kind_label)
from kvcost.plotting import style as _style
from kvcost.plotting.style import DIVERGING_CMAP, box_strip, ratio_label, stacked_norm_bar, tradeoff_scatter
from kvcost.plotting.stats import POOLED, log_fit, rho_by_group, spearman
from kvevaluator.measure import kvstats_layout as L
from kvevaluator import stale
from kvevaluator.measure import kvstats_derive as D
from kvevaluator.measure.stores import store_dir
from kvevaluator.measure.capture_protocol import check_same
from kvevaluator.measure import store_config
from kvevaluator.output_tree import timing_in


# a SERIES in these figures is a store slug (dgraph, nebula-label_only, pertype_csr-c16k ...): the
# style canon resolves a slug itself -- colour from its format, hatch from its format plus the chunk
# overlay for a non-canonical arm (style.CHUNK_HATCH); the label is the short format + the slug's rest
def format_color(slug):
    return _style.format_color(slug)


def bar_kw(slug):
    return _style.bar_kw(slug)


def format_short(slug):
    f = L.format_of(slug)
    return _style.format_short(f) + slug[len(f):]
from kvcost.plotting.formatting import bytes_to_human, geomean, number_to_human, us_to_human
from kvevaluator.measure import workload_plans
from kvevaluator import weights

# the read stack: the four hit lanes + the absence-probe keys (every key an op landed on without
# asking for it); together they sum to a counts row's read_bytes
from kvevaluator.capture.step_record import HIT_BYTE_LANES, MISS_BYTE_LANES  # (the lanes, one definition)
READ_CHANNELS = (*(f"{lane}_bytes" for lane in HIT_BYTE_LANES), "miss_key_bytes")
WRITE_CHANNELS = ("write_key_bytes", "write_value_bytes")
NCOLS = 5


# ---------------------------------------------------------------- csv helpers
def _read(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return list(csv.DictReader(f))


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else None


def _order(queries, workload):
    """Suite order first, then anything extra the CSV holds."""
    suite = workload_plans.queries(workload)
    return [q for q in suite if q in queries] + sorted(q for q in queries if q not in suite)


def _wrap(fig, text, chars_per_inch=13):
    """Wrap each line of `text` to the figure's width (no suptitle ever runs off the canvas)."""
    import textwrap
    width = max(80, int(fig.get_figwidth() * chars_per_inch))
    return "\n".join(textwrap.fill(line, width) if len(line) > width else line for line in text.split("\n"))


def _suptitle(fig, text, **kw):
    """fig.suptitle with the width-aware wrap; the convention is question line + one caveat line,
    the method detail in _caption below the figure."""
    kw.setdefault("fontsize", 9)
    fig.suptitle(_wrap(fig, text, chars_per_inch=int(120 / kw["fontsize"])), **kw)


def _caption(fig, text, fontsize=8, y=-0.01):
    """The method detail, wrapped, under the figure (bbox_inches="tight" keeps it in the file).
    y = figure fraction of the caption's top; pass _below(fig, artist) to clear a legend that
    hangs under the axes."""
    fig.text(0.5, y, _wrap(fig, text, chars_per_inch=int(130 / fontsize)), ha="center", va="top", fontsize=fontsize)


def _below(fig, artist, gap=0.008):
    """The figure-fraction y just under an artist (a legend hung below the axes), so a caption
    placed there never runs into it."""
    fig.canvas.draw()
    box = artist.get_window_extent(fig.canvas.get_renderer()).transformed(fig.transFigure.inverted())
    return box.y0 - gap


def _save(fig, plots, name):
    os.makedirs(plots, exist_ok=True)
    out = os.path.join(plots, name)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {out}")


# ---------------------------------------------------------------- the small-multiples stack
def _grid(queries):
    n = len(queries)
    rows = max(1, math.ceil(n / NCOLS))
    fig, axes = plt.subplots(rows, NCOLS, figsize=(3.4 * NCOLS, 2.9 * rows), squeeze=False)
    flat = [ax for row in axes for ax in row]
    for ax in flat[n:]:
        ax.axis("off")
    return fig, dict(zip(queries, flat))


def _stack_bar(ax, x, segments, width=0.7, hatch_kw=None):
    """One stacked bar at x: segments = [(color, value, edge_kw), ...] bottom-up; None/0 skipped.
    Returns the bar's total height."""
    bottom = 0.0
    for color, value, edge in segments:
        if not value:
            continue
        kw = dict(hatch_kw or {})
        kw.update(edge or {})
        ax.bar(x, value, width, bottom=bottom, color=color, **kw)
        bottom += value
    return bottom


def _finish_panel(ax, q, formats, unit_fn):
    ax.set_title(q, pad=3)
    ax.set_xticks(range(len(formats)))
    ax.set_xticklabels([format_short(f) for f in formats], rotation=30, ha="right")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: unit_fn(v)))
    ax.grid(axis="y", alpha=0.3)
    ax.set_ylim(bottom=0)                 # linear, this panel's own top (see module docstring)
    ax.tick_params(axis="both", labelsize=8)


def _legend(fig, entries):
    """entries = [(label, color, edge_kw|None)] -> one legend under the grid."""
    handles = []
    for label, color, edge in entries:
        e = edge or {}
        handles.append(Patch(facecolor=color, edgecolor=e.get("edgecolor", "white"),
                             linewidth=e.get("linewidth", 1), label=label))
    fig.legend(handles=handles, loc="lower center", ncol=min(6, len(entries)),
               bbox_to_anchor=(0.5, -0.03))


# ---------------------------------------------------------------- counts + bytes (cpp.csv)
def _tiers(workload, present=None):
    """[(tag, [group, ...])] -- the workload's groups by the registry's `figure` tag, the split a
    DENSE per-query figure uses (ldbc: ic | is; microbench: reads | single-hop | multi-hop); a
    group without a tag lands in "" (the workload's one figure). `present` narrows the queries."""
    out = {}
    for g in workload_plans.groups(workload):
        qs = [q for q in g["queries"] if present is None or q in present]
        if qs:
            out.setdefault(g["figure"], []).append({**g, "queries": qs})
    return list(out.items())


def _agg_bands(workload):
    """[(tag, [group, ...])] for a SPARSE figure: ldbc keeps IC and IS as separate figures (the
    canon); any other workload bands all of its groups into one figure ("")."""
    if workload == "interactive":
        return _tiers(workload)
    return [("", workload_plans.groups(workload))]


def _tagged(name, tag):
    """grid_ic.png / grid.png -- the figure-name rule for a tag."""
    stem, ext = name.rsplit(".", 1)
    return f"{stem}_{tag}.{ext}" if tag else name


def _per_cell(rows):
    per = {}
    for r in rows:
        per.setdefault((r["query"], r["db"]), []).append(r)
    return per


def _med(per, q, fmt, fn, agg=_median):
    """One (query, format) value over its params: the median by default (the typical param, the
    per-query views), the mean where a figure SUMS queries into a total (a median is not additive)."""
    return agg([fn(r) for r in per.get((q, fmt), [])])


def kvops_summary(rows, workload, formats, w):
    """The overview's per-format suite medians of weighted ops (reads) / raw ops (writes); per query: the
    weighted-ops row of the per-query grid."""
    writing = workload == "writes"
    ops = ["S", "G", "N"] + (["P", "D"] if writing else [])
    if writing:
        return {f: {"ops_all": _median([sum(_f(r[o]) or 0 for o in ops) for r in rows if r["db"] == f])}
                for f in formats}
    return {f: {"wops": _median([weights.wops(_f(r["S"]) or 0, _f(r["G"]) or 0, _f(r["N"]) or 0, w)
                                  for r in rows if r["db"] == f])} for f in formats}


def _channels_of(r):
    """The read lanes (+ two write lanes) of one counts row (kvstats_layout.load_counts: the step record's lane names);
    the three miss-key lanes summed as the absence probes."""
    g = lambda k: _f(r.get(k)) or 0
    return {"get_key_bytes": g("get_key"),
            "get_value_bytes": g("get_value"),
            "scan_key_bytes": g("scan_key"),
            "scan_value_bytes": g("scan_value"),
            "miss_key_bytes": g("seek_miss_key") + g("next_miss_key") + g("get_miss_key"),
            "write_key_bytes": g("write_key"),
            "write_value_bytes": g("write_value")}


def kvbytes_summary(rows, workload, formats):
    """The overview's per-format suite medians of read (and write) bytes; per query: the read-bytes row of
    the per-query grid."""
    out = {f: {"read_bytes": _median([_f(r.get("read_bytes")) for r in rows if r["db"] == f])}
           for f in formats}
    if workload == "writes":
        for f in formats:
            out[f]["write_bytes"] = _median([(_f(r.get("write_key")) or 0) + (_f(r.get("write_value")) or 0)
                                             for r in rows if r["db"] == f])
    return out


# ---------------------------------------------------------------- latency (steps.csv)
def _timing_dir(root, regime, slug):
    return str(timing_in(root, slug, regime))


def _steps(root, regime, slug):
    return _read(os.path.join(_timing_dir(root, regime, slug), "steps.csv")) or None


# Every read-latency regime a figure can draw, in display order, each with the label that names
# its HONEST perf level (warm-p3 is the ~10%-taxed twin of the tax-free warm-p0; cold-p3 is
# page-warm on a high-RAM box, cold-p3-directio the true device cold; steady-p2 a bounded cache).
# The dirs are kvstats_layout.REGIMES; a figure draws only the regimes a capture actually holds
# (present_read_regimes), so adding a regime is one row here -- grid, cost-latency, suite-totals
# and the correlation side-car all read this one table.
READ_LATENCY = (
    # (dir, grid-ylabel / suite-column label [2 lines: name + honest perf tag], short cost-latency
    # panel label). The full caveat -- ~10% tax, page-warm vs device-cold, the 3 GB steady cache --
    # lives in the snapshot README and the figure caption, not on the axis.
    ("warm-p0",          "warm latency\n(perf 0)",        "WARM"),
    ("warm-p3",          "warm latency\n(perf 3, taxed)", "WARM p3"),
    ("cold-p2",          "cold latency\n(direct I/O)",    "COLD"),       #: perf 2, direct I/O (inputs/contract/store_config.json)
    ("cold-p3",          "cold latency\n(perf 3)",        "COLD"),
    ("cold-p3-directio", "cold direct-IO\n(device cold)", "COLD directIO"),
    ("steady-p2",        "steady cache\n(perf 2)",        "STEADY"),
)


def present_read_regimes(root, formats):
    """The READ_LATENCY entries at least one shown format captured -- so a counts-only capture draws
    no empty latency rows and a merged tree draws exactly the regimes it holds. cold-p2 and cold-p3 are one row
    (COLD): a refresh leaves the old cold-p3 beside the new cold-p2, so cold-p2 wins when a shown format has it."""
    present = [t for t in READ_LATENCY if any(_steps(root, t[0], f) for f in formats)]
    if any(t[0] == "cold-p2" for t in present):
        present = [t for t in present if t[0] != "cold-p3"]
    return present


def _first_regime(root, formats, prefix):
    """The dir of the first PRESENT read regime whose name starts with `prefix` ("warm" | "cold"),
    or None -- so the two-column side-cars (correlation.csv, the console summary) source their warm
    and cold columns from whatever warm*/cold* pass the capture holds (warm-p3 when warm-p0 is absent)."""
    return next((d for d, _l, _s in present_read_regimes(root, formats) if d.startswith(prefix)), None)


def _kind_walls(rows, q):
    """{param: {kind: sum of step walls}} for one query -- the kv-vs-flow split of the plan's
    P=1 latency (the steps' own walls; the __plan__ row adds only the interpreter's overhead).
    `kind` is the C++ executor's own classification of every step (kv = a store op, flow =
    an in-memory operator)."""
    by = {}
    for r in rows:
        if r["query"] != q or r["step"] == "__plan__":
            continue
        d = by.setdefault(r["param_idx"], {"kv": 0.0, "flow": 0.0})
        d[r["kind"] if r["kind"] in d else "flow"] += _f(r["wall_ns"]) or 0
    return by


def _latency_summary(root, regime, workload, formats):
    """{slug: suite median of the per-query kv+flow wall (us)} of one regime, None when the regime
    was not captured; per query: the warm / cold rows of the per-query grid."""
    src = {f: _steps(root, regime, f) for f in formats}
    if not any(src.values()):
        return None
    queries = _order({r["query"] for f in formats for r in (src[f] or []) if r["step"] == "__plan__"},
                     workload)
    tot = {f: [] for f in formats}
    for q in queries:
        for fmt in formats:
            if not src[fmt]:
                continue
            by = _kind_walls(src[fmt], q)
            med = {k: _median([d[k] for d in by.values()]) for k in ("kv", "flow")}
            h = sum((med[k] or 0) for k in ("kv", "flow")) / 1e3
            if h:
                tot[fmt].append(h)
    return {f: _median(tot[f]) for f in formats}


def latency_summary(root, workload, formats):
    warm_dir, cold_dir = _first_regime(root, formats, "warm"), _first_regime(root, formats, "cold")
    warm = _latency_summary(root, warm_dir, workload, formats) if warm_dir else None
    cold = _latency_summary(root, cold_dir, workload, formats) if cold_dir else None
    if warm is None and cold is None:
        print("  latency: no warm-*/ or cold-*/ steps.csv -- skipped")
        return {}
    return {f: {"warm_us": (warm or {}).get(f), "cold_us": (cold or {}).get(f)} for f in formats}


def _banded_single_metric(workload, formats, plots, sf, w, name, metric_row, queries, title, caption):
    """One single-metric per-query figure, banded: ldbc = one file per tier (ic | is), any other
    workload = one file with the tiers as its bands (the microbench's reads | single-hop |
    multi-hop). metric_row = one _combined_grid row tuple."""
    present = set(queries)
    tiers = _tiers(workload, present)
    if workload == "interactive":
        for tag, groups in tiers:
            _combined_grid([(g["title"], g["queries"]) for g in groups], [metric_row], formats, workload, sf,
                           plots, _tagged(name, tag), w, title=title, caption=caption)
    else:
        bands = [(" + ".join(g["title"] for g in groups) if len(groups) > 1 else groups[0]["title"],
                  [q for g in groups for q in g["queries"]]) for _tag, groups in tiers]
        _combined_grid(bands, [metric_row], formats, workload, sf, plots, name, w, title=title, caption=caption)


def fig_cold_reads(root, workload, formats, plots, sf, w):
    """per-query_cold-block-reads[_<tier>].png -- cold-pass physical block reads per query x format,
    banded by query group."""
    cold_dir = _first_regime(root, formats, "cold") or "cold-p2"
    src = {f: _steps(root, cold_dir, f) for f in formats}
    if not any(src.values()):
        return
    src = _drop_readahead_block_reads(root, cold_dir, src, "per-query_cold-block-reads")
    if not any(src.values()):
        return
    plan = {f: [r for r in (src[f] or []) if r["step"] == "__plan__"] for f in formats}
    queries = _order({r["query"] for f in formats for r in plan[f]}, workload)
    def series(q):
        return {f: [(format_color(f), _median([_f(r["block_read_count"]) for r in plan[f] if r["query"] == q]))]
                for f in formats}
    row = ("cold block reads", number_to_human, series, False,
           [(format_short(f), format_color(f), bar_kw(f)) for f in formats], None)
    _banded_single_metric(workload, formats, plots, sf, w, "per-query_cold-block-reads.png", row, queries,
                          title=f"which format touches the fewest cold blocks per query? sf{sf} {workload} -- physical block reads "
                                f"on a cold cache, median over params; each cell its own scale, log where the bars span > 100x",
                          caption="bars = formats (hatch = format); block reads come from RocksDB's PerfContext on the cold pass, "
                                  "index and filter blocks included")


WRITE_OP_COLOR = {"P": "#e377c2", "D": "#ff7f0e", "S": op_color("S"), "G": op_color("G"), "N": op_color("N")}
WRITE_BYTES = (("read_bytes", "bytes read (read-modify-write)", "#9ecae1"), ("written_bytes", "bytes written", "#1f77b4"))
# the apply wall decomposed (kvstats_derive.durable_cells): the read-modify-write (the shadow wall,
# the canon's rmw hue), the direct WAL / memtable timers, and the residual
WRITE_LATENCY = (("rmw_us", "read-modify-write", step_kind_color("rmw")), ("wal_us", "WAL", "#d62728"),
                 ("memtable_us", "memtable", "#17becf"), ("other_us", "other", "#7f7f7f"))
_EXTRALARGE = 3


# The bare `dgraph` store keeps each posting in ONE unbounded KV (chunk infinity), so a write rewrites
# the whole posting: its write bytes are that store's artifact, not the key layout's -- bounded at
# real dgraph's 512 KB split (dgraph-c512k) they matched dgneb's (retired; not checked on meta's All-Condensed
# corner, whose list encoding differs). So the
# writes figures normalize to the c512k slug when the selection has it, and say so when it does not.
WRITE_INF_NOTE = ("dgraph = the chunk-\u221e store (one unbounded KV per posting): its write bytes are that "
                  "store's whole-posting rewrite, not the layout's; at real dgraph's 512 KB split "
                  "(dgraph-c512k) they match dgneb's")


def write_ref(fmts):
    """The slug the writes bars are labelled relative to (the 1x): nebula when drawn -- the
    cheapest-write per-entry floor on BYTES (~130 B per edge; a ratio to it claims no floor on ops
    or latency), else dgraph-c512k (real dgraph's posting split), else the first slug."""
    for pick in (lambda f: L.format_of(f) == "nebula", lambda f: L.format_of(f) == "dgraph" and "-c512k" in f):
        f = next((f for f in fmts if pick(f)), None)
        if f:
            return f
    return fmts[0]


def write_inf_note(fmts):
    """The chunk-infinity caveat when bare dgraph is drawn without its c512k arm; when both are drawn,
    the one line that tells them apart (the bare bar is an idealization, not dgraph's cost); else ''."""
    bare = any(f == "dgraph" or f.startswith("dgraph-") and "-c" not in f for f in fmts)
    if not bare:
        return ""
    if any(L.format_of(f) == "dgraph" and "-c512k" in f for f in fmts):
        return "\ndg = the chunk-\u221e store (one unbounded KV per posting, an idealization); dg-c512k = real dgraph's 512 KB posting split"
    return f"\n{WRITE_INF_NOTE}"


def _band_columns(groups, cells, fmts):
    """[(group, [queries])] of the groups with captured queries, and the column list they occupy:
    one column per query, one blank gap column between groups (the read grid's banding)."""
    kept = [(g, [q for q in g["queries"] if any(q in cells[f] for f in fmts)]) for g in groups]
    kept = [(g, qs) for g, qs in kept if qs]
    cols = []                                        # (group or None for a gap, query, first-of-group?)
    for gi, (g, qs) in enumerate(kept):
        if gi:
            cols.append((None, None, False))
        cols += [(g, q, qi == 0) for qi, q in enumerate(qs)]
    return kept, cols


def fig_writes_grid(cells, durable, groups, formats, sf, tier, out):
    """The writes grid of one figure tier: the tier's groups as sub-bands (a gap column between,
    each under its own title), one column per write, three rows in real units (weighted ops | bytes
    read beside written | write latency = the apply wall, decomposed), every bar labelled with its
    ratio to write_ref(fmts). cells = kvstats_derive.writes_cells, durable = {slug:
    kvstats_derive.durable_cells}."""
    fmts = [f for f in formats if f in cells]
    kept, cols = _band_columns(groups, cells, fmts)
    if not kept:
        print(f"  {os.path.basename(out)}: no captured queries -- skipped")
        return
    # (label, segments, source, axis unit, log floor): the bytes and latency rows are LOG -- a chunk
    # store's 64K read-modify-write beside a posting store's 24 B append is 3-4 decades in one panel,
    # and a whole-vertex delete on a chunk store beside nebula's per-entry write is as many; ops stay
    # within ~15x of the reference and stay linear. A stack on log keeps its total honest (stack_on_log)
    # and distorts the segment proportions; the log floor is 1 B / 1 us
    rows = [("weighted ops", lambda c: tuple((c[k] * D.WRITE_PER_GET[k], WRITE_OP_COLOR[k]) for k in D.OPS), cells, weights.human, None),
            ("bytes read | written (log)", lambda c: tuple((c[k], colour) for k, _l, colour in WRITE_BYTES), cells, bytes_to_human, 1024)]
    if any(durable.get(f) for f in fmts):
        rows.append(("write latency (log)", lambda c: tuple((c[k], colour) for k, _l, colour in WRITE_LATENCY), durable, us_to_human, 10))
    n = len(cols)
    ref = write_ref(fmts)
    gaps = [j for j, (g, _q, _f) in enumerate(cols) if g is None]
    fig, axes = plt.subplots(len(rows), n, figsize=((0.55 * len(fmts) + 0.1) * (n - len(gaps)) + 0.6 * len(gaps) + 2.5,
                                                    3.6 * len(rows) + 1.4),
                             constrained_layout=True, squeeze=False,
                             gridspec_kw={"width_ratios": [0.25 if g is None else 1 for g, _q, _f in cols]})
    hatches = {f: _style.hatch(f) for f in fmts}
    for j in gaps:
        for r in range(len(rows)):
            axes[r, j].axis("off")
    for j, (g, q, first) in enumerate(cols):
        if g is None:
            continue
        xs = range(len(fmts))
        payload = next((cells[f][q]["payload_bytes"] for f in fmts if q in cells[f]), 0)
        axes[0, j].set_title(f"{q}\npayload {bytes_to_human(payload)}", fontsize=9 + _EXTRALARGE)
        if first:                                    # the class title over its first column (the read grid's rule)
            axes[0, j].annotate(f"{g['title']} (n={len([c for c in cols if c[0] is g])})", xy=(0, 1.3),
                                xycoords="axes fraction", fontsize=11 + _EXTRALARGE, fontweight="bold",
                                ha="left", va="bottom", annotation_clip=False)
        for r, (_label, segs_of, src, fmt_axis, log) in enumerate(rows):
            ax = axes[r, j]
            row_cells = [src.get(f, {}).get(q) for f in fmts]
            floor = None
            if log:
                ax.set_yscale("log")
                floor = 1.0                              # 1 B / 1 us: every bar starts at the same floor
            stacked_norm_bar(ax, xs, fmts, row_cells, segs_of, _EXTRALARGE, hatches=hatches, ref=ref,
                             human=number_to_human, absolute=True, log_floor=floor)
            ax.set_xticks(list(xs))
            ax.set_xticklabels([format_short(f) for f in fmts], rotation=60)
            ax.grid(axis="y", alpha=0.3)
            if log:
                ax.set_ylim(floor, ax.get_ylim()[1] * 4)
            else:
                ax.set_ylim(0, ax.get_ylim()[1] * 1.2)
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p, fn=fmt_axis: fn(v)))
            if log:                                      # bytes tick on 1 B / 1 KB / 1 MB, latency on decades
                ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=log))
                ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    for r, (label, *_r) in enumerate(rows):
        axes[r, 0].set_ylabel(label)
    title_groups = " | ".join(g["title"] for g, _qs in kept)
    handles = [Patch(facecolor=WRITE_OP_COLOR[k], label=op_label(k)) for k in D.OPS]
    handles += [Patch(facecolor=colour, label=l) for _k, l, colour in WRITE_BYTES]
    has_latency = len(rows) == 3
    if has_latency:
        handles += [Patch(facecolor=colour, label=f"latency: {l}") for _k, l, colour in WRITE_LATENCY]
    handles += [Patch(facecolor="white", edgecolor="grey", hatch=hatches[f], label=format_short(f)) for f in fmts]
    # hung under the figure's bottom edge (not layout-managed: an "outside" legend and a caption
    # fought for the same strip), the caption under it; bbox_inches="tight" keeps both in the file
    leg = fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0), ncol=5)
    what = "KV cost, bytes moved and write latency" if has_latency else "KV cost and bytes moved (no write latency in this capture)"
    _suptitle(fig, f"what does each write cost per format? writes: {title_groups} -- {what}, sf{sf}\n"
                 f"rows: weighted ops (G:S:N:P:D = 1:1.1:0.01:1:1) | bytes read by the read-modify-write beside bytes "
                 f"written, log scale, payload in the column title"
                 + (" | write latency = the whole op (apply wall) as read-modify-write | WAL | memtable | other, log scale" if has_latency else "")
                 + f"; every bar labelled with its ratio to {format_short(ref)} (1x)", fontsize=10)
    method = ("counts and bytes = means over the params (deterministic; the mean is the value); on the log rows the "
              "stack's top is the true total, the segments' proportions are not to scale")
    if has_latency:
        drawn = ", ".join(format_short(f) for f in fmts if durable.get(f))
        method += (f"; latency = medians. read-modify-write = the paired shadow wall (the op without the store write); "
                   f"WAL + memtable = the direct RocksDB timers, the durable part (WAL buffered, no fsync); other = the "
                   f"residual. Latency drawn for: {drawn}")
    method += (f". Ratios are to {format_short(ref)}: nebula is the floor on bytes (the per-entry write), not on ops or latency"
               if L.format_of(ref) == "nebula" else "")
    method += f"{write_inf_note(fmts)}"
    _caption(fig, method, fontsize=9, y=_below(fig, leg))
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {out}")


def fig_writes_wa(cells, groups, formats, sf, out):
    """(bytes read + bytes written) / payload per write of one figure tier, log y, the tier's groups
    as sub-bands (a gap between, each under its own title): the data movement per payload byte --
    both sides of the primitive; 1 = the payload alone."""
    fmts = [f for f in formats if f in cells]
    kept = [(g, [q for q in g["queries"] if any(cells[f].get(q, {}).get("wa") for f in fmts)]) for g in groups]
    kept = [(g, qs) for g, qs in kept if qs]
    if not kept:
        return
    qs, xq, bands = [], [], []                       # every query, its x slot, and (title, first slot, last slot)
    for g, gqs in kept:
        start = (xq[-1] + 2) if xq else 0            # one empty slot between groups
        qs += gqs
        xq += [start + i for i in range(len(gqs))]
        bands.append((g["title"], start, start + len(gqs) - 1))
    fig, ax = plt.subplots(figsize=(max(16, 0.9 * (xq[-1] + 1) + 4), 6.8), constrained_layout=True)
    width = 0.8 / len(fmts)
    ref = write_ref(fmts)
    for fi, f in enumerate(fmts):
        xs = [x + (fi - (len(fmts) - 1) / 2) * width for x in xq]
        ys = [cells[f].get(q, {}).get("wa") or float("nan") for q in qs]
        ax.bar(xs, ys, width * 0.95, color=format_color(f), label=format_short(f), **bar_kw(f))
        for x, y, q in zip(xs, ys, qs):                  # every bar: its ratio to the reference slug
            ref_wa = cells[ref].get(q, {}).get("wa")
            if y == y and ref_wa:
                ax.text(x, y, "1x" if f == ref else ratio_label(y / ref_wa, number_to_human), ha="center", va="bottom",
                        fontsize=2.5 + _EXTRALARGE, rotation=90, fontweight="bold" if f == ref else None)
    ax.set_yscale("log")
    ax.axhline(1.0, color="black", linestyle="--", linewidth=0.8)
    ax.set_xticks(xq)
    ax.set_xticklabels(qs, rotation=30, ha="right")
    ax.set_ylabel("write amplification: (bytes read + written) / payload (log)")
    ax.grid(axis="y", alpha=0.3)
    for x in xq[1:]:
        ax.axvline(x - 0.5, color="#bbb", linestyle=":", linewidth=0.7)
    for title, lo, hi in bands:                      # the class title over its slots; the gap slot separates
        ax.text((lo + hi) / 2, 1.01, title, transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                fontsize=11, fontweight="bold")
        if lo:
            ax.axvline(lo - 1, color="#888", linestyle="-", linewidth=0.8)
    fig.legend(loc="outside lower center", ncol=min(6, len(fmts)))
    title_groups = " | ".join(g["title"] for g, _qs in kept)
    _suptitle(fig, f"how much does each format write per logical payload byte? write amplification, {title_groups}, sf{sf}\n"
                 f"(bytes the read-modify-write reads + bytes written) / the write's payload bytes; 1 = the payload alone; "
                 f"bar labels = ratio to {format_short(ref)} (1x)"
                 + (", the floor on bytes (the per-entry write)" if L.format_of(ref) == "nebula" else "")
                 + f"{write_inf_note(fmts)}", fontsize=10)
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print(f"  -> {out}")


def fig_writes(rows, root_sf, formats, plots, sf):
    """Every writes figure: one grid + one wa figure per suite group; returns the overview summary."""
    cells = D.writes_cells(rows)
    durable = {f: D.durable_cells(root_sf, f) for f in formats}
    for tag, groups in _tiers("writes"):             # one grid + one wa figure per figure tier, its groups as sub-bands
        fig_writes_grid(cells, durable, groups, formats, sf, tag, os.path.join(plots, f"grid_{tag}.png"))
        fig_writes_wa(cells, groups, formats, sf, os.path.join(plots, f"write-amplification_{tag}.png"))
    out = {}
    for f in formats:
        # no read_bytes here: 12-14 of the 18 write ops read nothing on a baseline (the inserts
        # are appends), so a suite median is 0 and a sum is one whole-vertex delete -- the
        # per-op read is the grid's bytes row
        d = {"wops": _median([c["wops"] for c in cells.get(f, {}).values()]),
             "write_bytes": _median([c["written_bytes"] for c in cells.get(f, {}).values()]),
             "write_us": _median([c["write_us"] for c in durable.get(f, {}).values()]),
             "durable_us": _median([c["durable_us"] for c in durable.get(f, {}).values()])}
        out[f] = {k: v for k, v in d.items() if v is not None}
    return out


# ---------------------------------------------------------------- amplification (amp_raw.csv)
AMP_KEYS = ("used_value", "get_value", "scan_value", "used_key", "get_key", "scan_key",
            "used_write_value", "write_value", "used_write_key", "write_key",
            "seek_miss_key", "next_miss_key", "get_miss_key")
# the absence-probe lane (the metric's unified rule, experiments/kv_amp/README.md): the foreign
# keys the ops landed on without asking for them -- not-found (seek + get misses) and range-end
# (the NEXT that ended a scan), summed in the figures, apart in the CSV
MISS_KEY = "miss_key_bytes"
_miss = lambda c: sum(c[lane] for lane in MISS_BYTE_LANES)


def _amp_cells(rows):
    """{(query, param, format): channel sums over the plan's steps}."""
    cell = {}
    for r in rows:
        c = cell.setdefault((r["query"], r["pidx"], r["format"]), {k: 0.0 for k in AMP_KEYS})
        for k in AMP_KEYS:
            c[k] += _f(r[k]) or 0.0
    return cell


def _amed(cell, q, fmt, fn):
    return _median([fn(c) for (qq, _p, ff), c in cell.items() if qq == q and ff == fmt])


# the lanes: (name, used_fn, read_fn, miss_fn, used color, extra color) -- the extra segment takes
# the lane's read-channel hue, the used segment its darker "used" tone; the key lane carries a
# third segment, the absence-probe keys (miss_fn; None on the lanes that have none)
_LANE_READ_V = ("value", lambda c: c["used_value"], lambda c: c["get_value"] + c["scan_value"], None,
                "used_value_bytes", "scan_value_bytes")
_LANE_READ_K = ("key", lambda c: c["used_key"], lambda c: c["get_key"] + c["scan_key"], _miss,
                "used_key_bytes", "scan_key_bytes")
# no write lane: amplification.png is reads-only. The writes workload's used | extra split is
# uninformative on a linear axis (used << extra on every posting/chunk rewrite) -- fig_amp returns
# early there, and the write story is carried by writes-grid_<group>.png + writes_data-per-payload-byte_<group>.png.


def _amp_rows(sf, workload, formats, tree=None):
    """The per-step rows of the counts (the formats capture, the body the runners run: kvstats_layout.store_steps) per
    shown slug, in amp_raw's shape (format = the slug, pidx, the channel names without _bytes)."""
    out = []
    for slug in formats:
        for r in L.store_steps(sf, workload, slug, tree):
            out.append({**r, "format": slug, "pidx": r["param_idx"]})
    return out


def fig_amp(root, workload, formats, plots, sf, w, tree=None):
    rows = _amp_rows(sf, workload, formats, tree)
    if not rows:
        print("  amplification: no steps in the formats capture -- skipped")
        return {}
    cell = _amp_cells(rows)
    if workload == "writes":
        # amplification.png is skipped for the writes workload: its used | extra split is
        # uninformative here. `used` (the logical change of a write) is a tiny fraction of a
        # posting/chunk rewrite, so on a linear axis the used segment is an invisible sliver and
        # the small-byte formats (nebula) vanish under the big-byte ones. The write story is
        # carried by writes-grid_<group>.png (absolute bytes, log) and
        # writes_data-per-payload-byte_<group>.png. write_extra feeds the summary side-car.
        extra = lambda c: c["write_key"] + c["write_value"] - c["used_write_key"] - c["used_write_value"]
        return {f: {"write_extra": _median([extra(c) for (_q, _p, ff), c in cell.items() if ff == f])}
                for f in formats}
    queries = _order({r["query"] for r in rows}, workload)
    lanes = [_LANE_READ_V, _LANE_READ_K]
    bw = 0.7 / len(lanes)
    # a lane whose `used` is 0 on EVERY cell of a slug while the slug read bytes on it is
    # UNMEASURED, not "0 used" (pertype's value lane: its chunk cursor does not annotate used
    # yet -- the deferred capture item); drawn as the read total in gray + "n/a", never as
    # 100% extra. A real all-zero used lane over a whole suite does not exist.
    unmeasured = {(fmt, lane) for fmt in formats for (lane, used_fn, read_fn, *_r) in lanes
                  if any(read_fn(c) for (_q, _p, ff), c in cell.items() if ff == fmt)
                  and not any(used_fn(c) for (_q, _p, ff), c in cell.items() if ff == fmt)}
    if unmeasured:
        print("  amplification: used lane unmeasured (gray n/a): " + ", ".join(f"{f}/{l}" for f, l in sorted(unmeasured)))
    # (the absolute lane split -- value / key bytes stacked used | extra | absence-probe -- was
    # no amplification.png: the per-query grid's bytes row carries the channels,
    # this figure carries the ratio; the caption states what "used" means)
    # the RATIOS: hit read / used per lane -- granularity only, the absence-probe keys are not in
    # the numerator; None when used == 0 (an existence op has no denominator)
    ramp_v = lambda c: (c["get_value"] + c["scan_value"]) / c["used_value"] if c["used_value"] else None
    ramp_k = lambda c: (c["get_key"] + c["scan_key"]) / c["used_key"] if c["used_key"] else None
    lane_color = {"value": byte_channel_color("scan_value_bytes"), "key": byte_channel_color("scan_key_bytes")}
    def series(q):
        out = {}
        for f in formats:
            segs = []
            for lane, fn in (("value", ramp_v), ("key", ramp_k)):
                v = None if (f, lane) in unmeasured else _amed(cell, q, f, fn)
                segs.append((lane_color[lane], v))
            out[f] = segs
        return out
    row = ("read amp (read / used)", lambda v: f"{v:g}x", series, False,
           [("value lane", lane_color["value"], None), ("key lane", lane_color["key"], None)]
           + [(f"{format_short(f)}", "white", bar_kw(f) | {"edgecolor": "grey"}) for f in formats]
           + ([("n/a = used unmeasured for that lane", "#e6e6e6", None)] if unmeasured else []), None, 1.0)
    _banded_single_metric(workload, formats, plots, sf, w, "per-query_read-amplification.png", row, queries,
                          title=f"how much more than it uses does each format read? sf{sf} {workload} -- read amplification per query: "
                                f"hit bytes read / an ideal store's used bytes, value lane | key lane (median over params)\n"
                                f"used = the bytes a perfectly granular store would touch for the answer; everything above 1x is the "
                                f"layout's over-read (a whole posting or chunk for one value, a whole row for one property)",
                          caption="absence probes excluded; NOT cpp.csv's ra = read / answer; the absolute bytes per channel are the "
                                  "grid's bytes row; a blank lane = used unmeasured for that format")
    return {f: {"read_amp": _median([ramp_v(c) for (_q, _p, ff), c in cell.items() if ff == f]),
                "key_read_amp": _median([ramp_k(c) for (_q, _p, ff), c in cell.items() if ff == f]),
                "miss_key": _median([_miss(c) for (_q, _p, ff), c in cell.items() if ff == f])}
            for f in formats}


# ---------------------------------------------------------------- the five-row combined view
# a cell of the combined grid goes log only when its bars span more than this many decades
# (a linear axis would erase every bar but the tallest); stacks stay linear regardless, since
# a log axis misreads a stack's segments
LOG_SPAN_DECADES = 2
COMBINED_COLS = 19          # queries per band of the combined grid (ldbc's 19 fit in one)


def _cell_bars(ax, formats, series, stacked=True):
    """Draw one grid cell: series = {fmt: [(color, value), ...]} -- a stack per format (bottom-up)
    when `stacked`, else side-by-side bars (the read-amp lanes). Returns the per-format totals
    (the stack's height, or the tallest bar; None where the format has no data)."""
    totals = {}
    for fi, fmt in enumerate(formats):
        segs = series.get(fmt)
        if not segs or all(not v for _c, v in segs):
            totals[fmt] = None
            continue
        if stacked or len(segs) == 1:
            totals[fmt] = _stack_bar(ax, fi, [(c, v, None) for c, v in segs], width=0.7,
                                     hatch_kw=bar_kw(fmt))
        else:
            w = 0.7 / len(segs)
            for si, (c, v) in enumerate(segs):
                if v:
                    ax.bar(fi + (si - (len(segs) - 1) / 2) * w, v, w * 0.95, color=c, **bar_kw(fmt))
            totals[fmt] = max(v for _c, v in segs if v)
    return totals


def _scale_cell(ax, totals, stacked):
    """Linear, this cell's own top; log only for an unstacked cell whose bars span more than
    LOG_SPAN_DECADES, marked in the cell so a reader never mistakes the two."""
    pos = [t for t in totals.values() if t]
    if not stacked and len(pos) > 1 and max(pos) / min(pos) > 10 ** LOG_SPAN_DECADES:
        ax.set_yscale("log")
        ax.text(0.98, 0.96, "log", transform=ax.transAxes, ha="right", va="top", fontsize=7,
                color="dimgray")
    else:
        ax.set_ylim(bottom=0)


def fig_combined(rows, root, workload, formats, plots, sf, w):
    """The grid: four metric ROWS x one column PER QUERY; each cell holds the formats as bars,
    every row an op-stack (weighted ops by op, read bytes by channel, warm / cold latency by
    step kind), each cell scaled to its own data. (The value read-amp view is
    per-query_read-amplification.png, encoded by lane, not by op.)"""
    if workload == "writes" or not rows:
        return
    per = _per_cell(rows)
    queries = _order({r["query"] for r in rows}, workload)

    g = weights.per_get(w)                          # stacks and legend in GET units (1 : 1.1 : 0.01)

    def ops_series(q):
        return {f: [(op_color(op), (lambda v: v * g[op] if v is not None else None)(
            _med(per, q, f, lambda r, op=op: _f(r.get(op))))) for op in ("S", "G", "N")]
            for f in formats}

    def bytes_series(q):
        return {f: [(byte_channel_color(c), _med(per, q, f, lambda r, c=c: _channels_of(r)[c]))
                    for c in READ_CHANNELS] for f in formats}

    def latency_series(src):
        def series(q):
            out = {}
            for f in formats:
                if not src[f]:
                    continue
                by = _kind_walls(src[f], q)
                med = {k: _median([d[k] for d in by.values()]) for k in ("kv", "flow")}
                out[f] = [(step_kind_color(k), (med[k] or 0) / 1e3) for k in ("kv", "flow")]
            return out
        return series

    # (row label, unit formatter, series fn, stacked?, legend entries, per-cell annotate hook[, reference line])
    metric_rows = [
        ("weighted KV ops", weights.human, ops_series, True,
         [(f"{op_label(op)} x{g[op]:g}", op_color(op), None) for op in ("S", "G", "N")], None),
        ("read bytes by channel", bytes_to_human, bytes_series, True,
         [(byte_channel_label(c), byte_channel_color(c), None) for c in READ_CHANNELS], None),
    ]
    # one latency row per regime the capture holds (a counts-only capture draws none; the legend
    # rides the first latency row only). READ_LATENCY names the honest perf level of each.
    latency_legend = [(step_kind_label(k), step_kind_color(k), None) for k in ("kv", "flow")]
    for i, (regime, label, _short) in enumerate(present_read_regimes(root, formats)):
        src = {f: _steps(root, regime, f) for f in formats}
        metric_rows.append((label, us_to_human, latency_series(src), True,
                            latency_legend if i == 0 else [], None))
    # bands = the WORKLOAD's named query groups (workload_plans.groups: ldbc's IC / IS, the
    # microbench's seven access paths); a group names the figure its band belongs to (ldbc
    # keeps IC and IS in separate figures, the canon), else it is a band of the one figure.
    # Captured queries outside every group land in a trailing "other" band.
    present = set(queries)
    figures = {tag: [(g["title"], g["queries"]) for g in groups] for tag, groups in _tiers(workload, present)}
    grouped = {q for bands in figures.values() for _t, qs in bands for q in qs}
    rest = [q for q in queries if q not in grouped]
    if rest:
        figures.setdefault("", []).append(("other", rest))
    # the caption names the regimes the y-labels only tag (so the PNG explains itself without the README)
    regime_note = {"warm-p0": "warm-p0 = tax-free warm", "warm-p3": "warm-p3 = warm, ~10% timer tax",
                   "cold-p2": "cold-p2 = direct I/O (block cache cleared, every miss a device read)",
                   "cold-p3": "cold-p3 = page-warm (block cache cleared, refilled from OS page cache)",
                   "cold-p3-directio": "cold direct-IO = true device cold (direct IO)",
                   "steady-p2": "steady = bounded block cache"}
    lat = present_read_regimes(root, formats)
    caption = (f"weighted ops = {weights.label(w)}; linear stacks: a segment under ~1/500 of its cell's top is "
               f"sub-pixel (the size-normalized and read-amplification figures carry the small ones)")
    if lat:
        caption += " · latency: " + "; ".join(regime_note[d] for d, _l, _s in lat)
    for tag, bands in figures.items():
        _combined_grid(bands, metric_rows, formats, workload, sf, plots, _tagged("grid.png", tag), w, caption=caption)


def _pack_bands(groups, width):
    """Pack the groups, in order, into bands of at most `width` columns: consecutive groups share
    a band (one blank gap column between them) while they fit; a group wider than the band
    wraps into balanced pieces of its own title. Returns [[(title, queries, first column)]]."""
    pieces = []
    for title, qs in groups:
        nb = math.ceil(len(qs) / width)
        n = math.ceil(len(qs) / nb)                  # balanced pieces (40 -> 14/13/13, not 19/19/2)
        pieces += [(title, qs[i:i + n]) for i in range(0, len(qs), n)]
    bands, cur, col = [], [], 0
    for title, qs in pieces:
        need = len(qs) + (1 if cur else 0)
        if cur and col + need > width:
            bands.append(cur)
            cur, col = [], 0
        if cur:
            col += 1                                 # the gap column
        cur.append((title, qs, col))
        col += len(qs)
    if cur:
        bands.append(cur)
    return bands


def _combined_grid(groups, metric_rows, formats, workload, sf, plots, name, w, title=None, caption=None):
    """groups = [(band title, queries)]: the metric rows per band, bands one under another;
    small groups share a band side by side (a gap column between, each under its own title)."""
    nr = len(metric_rows)
    bands = _pack_bands(groups, COMBINED_COLS)
    nq = max(seg[2] + len(seg[1]) for band in bands for seg in band)
    # a FIXED grid (not constrained_layout: it sizes every column by its widest tick label
    # across all bands, which shifts the blank gap columns around); the band titles need
    # extra height above each band, hence the generous hspace
    # the margins are sized in INCHES, then turned into figure fractions: the top strip holds the
    # first band's title, the bottom strip the rotated slug labels (pt-label_only-c64k is 18
    # characters), whatever the row count; the legend and caption hang below the figure and
    # bbox_inches="tight" keeps them in the file
    R = nr * len(bands)
    top_in, axes_in = 0.75, 2.4 * R
    bottom_in = 0.25 + 0.06 * max(len(format_short(f)) for f in formats)
    height = top_in + axes_in + bottom_in
    fig, axes = plt.subplots(R, nq, figsize=(max(6, 1.45 * nq), height), squeeze=False)
    fig.subplots_adjust(left=0.035, right=0.995, top=1 - top_in / height, bottom=bottom_in / height,
                        wspace=0.65, hspace=0.8)
    ax_in = axes_in / (R + 0.8 * (R - 1))            # one axes' height under that hspace
    title_off = 1 + 0.42 / ax_in                     # the band title 0.42 in above its first axes
    for bi, band in enumerate(bands):                # blank the gap and spare cells
        filled = {seg[2] + i for seg in band for i in range(len(seg[1]))}
        for ri_ in range(nr):
            for ci, ax in enumerate(axes[bi * nr + ri_]):
                if ci not in filled:
                    ax.axis("off")
    # (band, group title, query, column, first-of-group?)
    cells = [(bi, btitle, q, c0 + qi, qi == 0) for bi, band in enumerate(bands)
             for btitle, qs, c0 in band for qi, q in enumerate(qs)]
    for (bi, btitle, q, ci, first), (ri, (label, unit_fn, series_fn, stacked, _leg, annotate_fn, *rest)) in (
            (c, m) for c in cells for m in enumerate(metric_rows)):
        ax = axes[bi * nr + ri][ci]
        totals = _cell_bars(ax, formats, series_fn(q), stacked)
        ref_line = rest[0] if rest else None             # a ratio row marks its 1x
        if ref_line is not None:
            ax.axhline(ref_line, color="black", linewidth=0.6, alpha=0.6)
        _scale_cell(ax, totals, stacked)
        if annotate_fn:                              # e.g. the ramp row marks used=0 (infinite over-read)
            annotate_fn(ax, q)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p, u=unit_fn: u(v)))
        ax.grid(axis="y", alpha=0.3)
        ax.tick_params(axis="both", labelsize=7)
        ax.set_xticks(range(len(formats)))
        # format ticks on EVERY row, not just the last: the columns are narrow (many formats),
        # so a reader should not have to trace rows down to the bottom to map a bar to its format
        ax.set_xticklabels([format_short(f) for f in formats], rotation=90)
        if ri == 0:
            ax.set_title(q, pad=3, fontsize=8)
            if first:                                # the group's title over its first column
                ax.annotate(btitle, xy=(0, title_off), xycoords="axes fraction", fontsize=11,
                            fontweight="bold", ha="left", va="bottom", annotation_clip=False)
        if ci == 0:                                  # 8 pt fits the row height; 2-line labels stay compact
            ax.set_ylabel(label, fontsize=8)
    entries = [e for row in metric_rows for e in row[4]]
    # hung under the figure's bottom edge, so it never covers the bottom row's format labels
    leg = fig.legend(handles=[Patch(facecolor=c, edgecolor=(e or {}).get("edgecolor", "white"),
                                    linewidth=(e or {}).get("linewidth", 1), label=l)
                              for l, c, e in entries],
                     loc="upper center", bbox_to_anchor=(0.5, 0), ncol=min(7, len(entries)))
    # va="bottom" at y=1.0: the title lives above the axes region, so it cannot overlap a band
    # title on a narrow (IS) canvas; bbox_inches="tight" keeps it in the file
    _suptitle(fig, title or (f"how do the formats compare on every metric, query by query? sf{sf} {workload} -- rows: metrics, "
                             f"columns: queries, bars: formats (hatch = format, a chunk or schema arm adds an overlay; median over params; each cell its own scale)\n"
                             f"evaluator latencies: every format on ONE shared RocksDB, latency = access pattern + our operator code, "
                             f"not an engine"), y=1.0, va="bottom", fontsize=9)
    _caption(fig, caption or (f"weighted ops = {weights.label(w)}; linear stacks: a segment under ~1/500 of its cell's top is "
                              f"sub-pixel (the size-normalized and read-amplification figures carry the small ones)"),
             y=_below(fig, leg))                     # under the legend, however many rows it took
    _save(fig, plots, name)                          # bbox_inches="tight" keeps the legend in


# ---------------------------------------------------------------- correlation (ops, bytes ~ latency)
def _plan_walls(rows, regime=None):
    """{(query, param): plan wall us} from a latency steps.csv (the __plan__ rollup row); `regime`
    picks one when the file holds several (the durable capture: headline + breakdown in one file;
    unfiltered, the last row per key would win)."""
    return {(r["query"], r["param_idx"]): (_f(r["wall_ns"]) or 0) / 1e3
            for r in rows if r["step"] == "__plan__" and (regime is None or r.get("regime") == regime)}


def _drop_readahead_block_reads(root, regime, by_format, what):
    """by_format without the formats whose capture was timed with readahead on (their block_read_count is not the
    misses: measure/store_config.READAHEAD_NOTE); each one dropped is named"""
    out = {}
    for f, rows in by_format.items():
        if rows and not store_config.block_reads_are_misses(_timing_dir(root, regime, f)):
            print(f"  {what}: {f} left out: {store_config.READAHEAD_NOTE}")
            rows = None
        out[f] = rows
    return out


def _plan_blocks(rows):
    """{(query, param): physical block reads} from a cold steps.csv (the __plan__ row's
    PerfContext block_read_count; 0 on a warm pass, where every block is cached)."""
    return {(r["query"], r["param_idx"]): _f(r.get("block_read_count")) or 0
            for r in rows if r["step"] == "__plan__"}


def correlation_table(rows, root, workload, formats, w):
    """One row per (slug, query): the medians over params of weighted ops, read bytes, warm and
    cold plan wall (us) -- the figure's own CSV side-car (correlation.csv), plus the rank
    correlations per slug and pooled. Pairs a query's median latency with its median ops/bytes;
    a query missing a regime is left blank there and out of that regime's correlation."""
    per = _per_cell(rows)
    warm_dir = _first_regime(root, formats, "warm") or "warm-p0"
    cold_dir = _first_regime(root, formats, "cold") or "cold-p2"
    walls = {f: {"warm": _plan_walls(_steps(root, warm_dir, f) or []),
                 "cold": _plan_walls(_steps(root, cold_dir, f) or [])} for f in formats}
    blocks = {f: _plan_blocks(rows or []) for f, rows in _drop_readahead_block_reads(
        root, cold_dir, {f: _steps(root, cold_dir, f) for f in formats}, "correlation.csv cold_block_reads").items()}
    suite = set(workload_plans.queries(workload))     # the suite only: a twin plan (ic5_listjoin) would
    table = []                                      # double-weight its base query
    for f in formats:
        for q in _order({q for (q, d) in per if d == f and q in suite}, workload):
            m = lambda fn: _med(per, q, f, fn)
            ops = m(lambda r: weights.wops(_f(r["S"]) or 0, _f(r["G"]) or 0, _f(r["N"]) or 0, w))
            by = m(lambda r: _f(r.get("read_bytes")))
            warm = _median([v for (qq, _p), v in walls[f]["warm"].items() if qq == q])
            cold = _median([v for (qq, _p), v in walls[f]["cold"].items() if qq == q])
            table.append({"slug": f, "query": q, "group": next((g["key"] for g in workload_plans.groups(workload)
                                                                 if q in g["queries"]), ""),
                          "wops": ops, "read_bytes": by, "warm_us": warm, "cold_us": cold,
                          "warm_ns_per_op": (warm * 1e3 / ops) if (warm and ops) else None,
                          # the decomposition a pair matrix reads
                          "S": m(lambda r: _f(r["S"])), "G": m(lambda r: _f(r["G"])), "N": m(lambda r: _f(r["N"])),
                          "value_bytes": m(lambda r: _channels_of(r)["get_value_bytes"] + _channels_of(r)["scan_value_bytes"]),
                          "key_bytes": m(lambda r: _channels_of(r)["get_key_bytes"] + _channels_of(r)["scan_key_bytes"]
                                         + _channels_of(r)["miss_key_bytes"]),
                          "cold_block_reads": _median([v for (qq, _p), v in blocks[f].items() if qq == q])})
    stats = []
    # per-slug only (cost~latency ACROSS a format's queries); NO pooled row -- both are dominated by
    # query size (a big query is big on cost AND latency), so they score ~0.95 for every currency and
    # cannot discriminate formats. The honest format-ranking view is cost-latency-summary-<group>.png.
    for name, sel in [(f, [t for t in table if t["slug"] == f]) for f in formats]:
        row = {"slug": name, "n": len(sel)}
        for x in ("wops", "read_bytes"):
            for y in ("warm_us", "cold_us"):
                pts = [(t[x], t[y]) for t in sel if t[x] and t[y]]
                row[f"rho_{x}~{y}"] = spearman([a for a, _ in pts], [b for _, b in pts]) if len(pts) >= 3 else None
        row["median_warm_ns_per_op"] = _median([t["warm_ns_per_op"] for t in sel if t["warm_ns_per_op"]])
        stats.append(row)
    return table, stats


def _write_side_car(plots, name, rows):
    os.makedirs(plots, exist_ok=True)
    cols = list(rows[0]) if rows else []
    with open(os.path.join(plots, name), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for r in rows:
            wr.writerow({c: ("" if r[c] is None else (f"{r[c]:.4f}" if isinstance(r[c], float) else r[c])) for c in cols})


# the pair matrix: predictors x targets, Spearman over per-query medians (
# prints and writes it; the combined correlation figure draws it)
# the BLENDS of the two currencies (ops = weighted ops, bytes = total read bytes, each normalised
# by the MEDIAN over the ANCHOR format's queries -- the first present format in canonical order,
# dgraph on every canonical figure -- so a format's blend cell is a property of that format, not
# of what else shares the figure; a max over the whole table moved dgraph's cells 3.8x when the
# byte-heavy pertype_csr-c4k joined. The search's L8 criterion normalises within a query's
# candidates -- a fixed scope, unaffected): the fixed weights, and the shape-only combinations
# (geomean is scale-invariant: the anchor-free cross-check)
BLENDS = [("blend_70_30", "blend 0.7 ops+0.3 bytes"), ("blend_50_50", "blend 0.5/0.5"),
          ("blend_30_70", "blend 0.3 ops+0.7 bytes"), ("blend_max", "max(ops, bytes)"),
          ("blend_min", "min(ops, bytes)"), ("blend_geomean", "geomean(ops, bytes)")]
PREDICTORS = [("wops", "weighted ops"), ("S", "SEEK"), ("G", "GET"), ("N", "NEXT"), ("SG", "SEEK+GET"),
              ("blockproxy", "(S+G)+B/4K"),
              ("read_bytes", "total bytes"), ("value_bytes", "value bytes"), ("key_bytes", "key bytes")] + BLENDS
TARGETS = [("warm_us", "warm"), ("cold_us", "cold"), ("cold_block_reads", "cold blocks")]


def blend_anchor(table):
    """The format whose queries set the blend's scales: the first present in canonical order."""
    present = {t["slug"] for t in table}
    return next((s for s in L.sort_slugs(present)), None)


def enrich(table):
    """S+G (the random-access ops) and the blends (BLENDS) of ops and bytes, each normalised by
    the median over the anchor format's queries (blend_anchor); returns the table."""
    anchor = blend_anchor(table)
    ao = [t["wops"] for t in table if t["slug"] == anchor and t["wops"]]
    ab = [t["read_bytes"] for t in table if t["slug"] == anchor and t["read_bytes"]]
    mo = statistics.median(ao) if ao else 1
    mb = statistics.median(ab) if ab else 1
    for t in table:
        t["SG"] = (t["S"] or 0) + (t["G"] or 0)
        # cold-latency proxy: distinct random reads (S+G) + the sequential transfer as blocks
        # (bytes / 4KB). A run-free, countable stand-in for cold_block_reads (hence cold latency);
        # beats S+G or bytes alone because it carries both the block-count and the transfer signal.
        t["blockproxy"] = t["SG"] + (t["read_bytes"] or 0) / 4096.0
        o, b = (t["wops"] or 0) / mo, (t["read_bytes"] or 0) / mb
        t["blend_70_30"] = 0.7 * o + 0.3 * b
        t["blend_50_50"] = 0.5 * o + 0.5 * b
        t["blend_30_70"] = 0.3 * o + 0.7 * b
        t["blend_max"] = max(o, b)
        t["blend_min"] = min(o, b)
        t["blend_geomean"] = math.sqrt(o * b)
        t["blend"] = t["blend_70_30"]                     # the emitted criterion's (L8)
    return table


def rho_of(sel, x, y):
    """Spearman over the cells where both values exist and are non-zero. A predictor that is
    zero on most of the series (dgraph's NEXT: its adjacency is one blob, no scan) is blank --
    a correlation over the few non-zero leftovers is noise that reads as a signal."""
    pts = [(t[x], t[y]) for t in sel if t.get(x) not in (None, 0) and t.get(y) not in (None, 0)]
    have_y = [t for t in sel if t.get(y) not in (None, 0)]
    if len(pts) < 3 or len(pts) < 0.5 * len(have_y):
        return None, len(pts)
    return spearman([a for a, _ in pts], [b for _, b in pts]), len(pts)


def pair_matrix(table, workload, formats):
    """[{group, series, target, predictor, rho, n}] over every predictor x target, per format +
    pooled, per query group (+ all)."""
    out = []
    groups = [(g["key"], g["queries"]) for g in workload_plans.groups(workload)] + [("all", None)]
    for gkey, gqs in groups:
        gsel = [t for t in table if gqs is None or t["query"] in gqs]
        for name, sel in [(f, [t for t in gsel if t["slug"] == f]) for f in formats] + [("pooled", gsel)]:
            for tkey, _tl in TARGETS:
                for pkey, _pl in PREDICTORS:
                    r, n = rho_of(sel, pkey, tkey)
                    out.append({"group": gkey, "series": name, "target": tkey, "predictor": pkey, "rho": r, "n": n})
    return out


# every rho in the IO-validation scatters is ACROSS a format's queries (and pooled): a big query
# is big in every currency, so these say "how big were the queries", not which currency ranks
# the formats -- that is cost-latency-summary-*.png. Said on every panel.
IO_VALIDATION_PNG = "io-validation_block-reads-vs-currency.png"
SIZE_CONFOUND_NOTE = "rho across queries = size-confounded;\nformat ranking: cost-latency-summary-*.png"


def _scatter(ax, pts, xcol, ycol, formats, xlabel, ylabel, xfmt, yfmt, markers, title):
    rhos = rho_by_group(pts, "slug", xcol, ycol, pooled=True, min_n=3) if pts else {}
    for f in formats:
        mine = [t for t in pts if t["slug"] == f]
        if not mine:
            continue
        xs, ys = [t[xcol] for t in mine], [t[ycol] for t in mine]
        r = rhos[f][0]
        for t in mine:
            ax.scatter([t[xcol]], [t[ycol]], s=26, color=format_color(f), marker=markers.get(t["group"], "o"),
                       alpha=0.85, edgecolor="white", linewidth=0.5)
        ax.scatter([], [], color=format_color(f), label=f"{format_short(f)}  rho={r:.2f}" if r is not None else format_short(f))
        fit = log_fit(xs, ys)
        if fit:
            lo, hi = min(xs), max(xs)
            ax.plot([lo, hi], [10 ** fit[1] * lo ** fit[0], 10 ** fit[1] * hi ** fit[0]],
                    color=format_color(f), linewidth=1, alpha=0.7)
    pooled = rhos[POOLED][0] if rhos else None
    ax.set_xscale("log"); ax.set_yscale("log")
    ax.xaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, numticks=7))
    ax.xaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: xfmt(v)))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: yfmt(v)))
    ax.tick_params(labelsize=7)
    ax.set_xlabel(xlabel, fontsize=8); ax.set_ylabel(ylabel, fontsize=8)
    ax.grid(alpha=0.3, which="both")
    ax.set_title(title, fontsize=9)
    ax.legend(title=(f"pooled rho={pooled:.2f}; " if pooled is not None else "") + SIZE_CONFOUND_NOTE,
              fontsize=7, title_fontsize=6.5, loc="lower right")


def _heatmap(ax, matrix, gk, formats, title, ylabels=True, colorbar=None):
    """One pair-matrix heatmap: PREDICTORS x (TARGETS x formats + pooled) for query group `gk`.
    A cell rho_of blanked (a mostly-zero currency, fewer than 3 pairs) is light-gray "n/a" --
    not applicable, not a hole. Returns the image (for a colorbar)."""
    series = list(formats) + ["pooled"]
    nf = len(series)
    cols = [(tk, s) for tk, _tl in TARGETS for s in series]
    m = {(r["predictor"], r["target"], r["series"]): r["rho"] for r in matrix if r["group"] == gk}
    grid = [[m.get((pk, tk, s)) for (tk, s) in cols] for pk, _pl in PREDICTORS]
    data = [[v if v is not None else float("nan") for v in row] for row in grid]
    cmap = matplotlib.cm.get_cmap("viridis").copy()
    cmap.set_bad("#e6e6e6")
    im = ax.imshow(data, cmap=cmap, vmin=0.4, vmax=1.0, aspect="auto")
    for ri, row in enumerate(grid):
        for cj, v in enumerate(row):
            if v is None:
                ax.text(cj, ri, "n/a", ha="center", va="center", fontsize=6, color="#8a8a8a")
            else:
                ax.text(cj, ri, f"{v:.2f}", ha="center", va="center", fontsize=6.5,
                        color="white" if v < 0.8 else "black")
    ax.set_xticks(range(len(cols)))
    ax.set_xticklabels([format_short(s) if s != "pooled" else "pooled" for _tk, s in cols], rotation=90, fontsize=7)
    for ti, (_tk, tl) in enumerate(TARGETS):                  # the target group labels above
        ax.text(ti * nf + (nf - 1) / 2, -0.75, tl, ha="center", va="bottom", fontsize=8, fontweight="bold")
        if ti:
            ax.axvline(ti * nf - 0.5, color="white", linewidth=2)
    ax.set_yticks(range(len(PREDICTORS)))
    ax.set_yticklabels([pl for _pk, pl in PREDICTORS] if ylabels else [], fontsize=7.5)
    ax.axhline(len(PREDICTORS) - len(BLENDS) - 0.5, color="white", linewidth=2)   # the blends block
    ax.set_title(title, fontsize=9, pad=30)
    if colorbar is not None:
        colorbar.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    return im


def heatmap_width(formats):
    """Inches for one heatmap: 0.5 per column (3 targets x formats + pooled) plus the label margin."""
    return 2.2 + 0.5 * 3 * (len(formats) + 1)




def fig_combined_correlation(table, workload, formats, plots, sf, w):
    """io-validation_block-reads-vs-currency.png -- the two views the size-controlled figures do not carry:
      row 1  the IO validation: cold block reads ~ SEEK+GET (the random-access ops) | ~ total bytes
             -- does our currency predict the PHYSICAL reads (the thesis check; still across queries,
             so size-confounded like any pooled rho, said on the panel)
      row 2  the pair matrix as a heatmap: predictors x (target, format) over ALL queries -- the
             format asymmetry of the ops (NEXT is n/a for a posting store, GET alone uninformative)
    The ranking questions live in cost-latency-*.png (no latency ~ currency scatters, blend
    scatters or pooled footer: each would show the size confound).
    The per-pair numbers are pairs.csv beside the figure (analysis reproduces from the CSV)."""
    table = enrich(table)
    pts = [t for t in table if t["wops"] and t["read_bytes"]]
    if not pts:
        return None
    matrix = pair_matrix(table, workload, formats)
    # no latency / cold-block capture for these slugs (a counts-only capture): every scatter would be
    # empty and an empty figure reads as "no effect" -- skip it, and drop a stale one, loudly
    if not any(t["warm_us"] or t["cold_us"] or t["cold_block_reads"] for t in pts):
        stale = os.path.join(plots, IO_VALIDATION_PNG)
        if os.path.exists(stale):
            os.remove(stale)
        print(f"  {IO_VALIDATION_PNG}: no latency or cold-block capture for these slugs -- skipped "
              "(a blank figure would read as 'no effect'); the pair matrix is still pairs.csv")
        return matrix
    groups = [g for g in workload_plans.groups(workload)]
    gkeys = [g["key"] for g in groups] if len(groups) > 1 else []
    marks = ["o", "^", "s", "D", "v", "P", "X", "*"]
    markers = {k: marks[i % len(marks)] for i, k in enumerate(gkeys)}
    width = max(12, heatmap_width(formats))            # the heatmap sets the width past 5 formats
    fig = plt.figure(figsize=(width, 5.2 + 1.2 + 0.5 * len(PREDICTORS)), constrained_layout=True)
    gs = fig.add_gridspec(2, 2, height_ratios=[4.2, 1.2 + 0.5 * len(PREDICTORS)])
    # row 1: the IO validation
    io = [t for t in pts if t["cold_block_reads"]]
    _scatter(fig.add_subplot(gs[0, 0]), io, "SG", "cold_block_reads", formats, "SEEK + GET (random-access ops)",
             "cold block reads", number_to_human, number_to_human, markers, "physical reads ~ SEEK+GET")
    _scatter(fig.add_subplot(gs[0, 1]), io, "read_bytes", "cold_block_reads", formats, "read bytes (realistic codec)",
             "cold block reads", bytes_to_human, number_to_human, markers, "physical reads ~ read bytes")
    # row 2: the all-queries pair matrix
    ax = fig.add_subplot(gs[1, :])
    _heatmap(ax, matrix, "all", formats, "Spearman rho -- all queries (per format, and pooled): the ops' format asymmetry",
             colorbar=fig)
    mk = "  ".join(f"{markers[k]} = {next(g['title'] for g in groups if g['key'] == k)}" for k in gkeys) if gkeys else ""
    _suptitle(fig, f"does the counted KV currency (S+G, bytes) track the PHYSICAL block reads RocksDB does? sf{sf} {workload} "
                   f"-- a proxy check, NOT cost-vs-latency (that is cost-latency-*.png)\n"
                   f"a rho across queries is size-confounded (a big query is big in every currency): read the panels as 'the proxy "
                   f"holds' or not, never as a format ranking", fontsize=10)
    _caption(fig, f"one point per (query, format), per-query medians over params; colour = format{('; ' + mk) if mk else ''}; "
                  f"NEXT is n/a where a posting store scans nothing")
    _save(fig, plots, IO_VALIDATION_PNG)
    return matrix


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    ap.add_argument("--family", dest="workload", choices=list(workload_plans.WORKLOADS), default="interactive")
    ap.add_argument("--root", default=None, help="the output tree (default: the output dir)")
    ap.add_argument("--show", default=None,
                    help="comma list of store SLUGS to draw, in that order (default: every slug present, "
                         "canonical order); the figures dir follows the selection (kvstats_layout.plots_dir)")
    ap.add_argument("--refresh", action="store_true",
                    help="re-render EVERY existing selection under the workload (each plots*/ dir's "
                         "selection.json) whose slugs are all present -- after a capture, so no "
                         "selection's figures go stale; --show adds this run's selection first")
    ap.add_argument("--weights", default=None,
                    help="G:S:N for the weighted-ops stack (default 1:1.1:0.01, python/kvevaluator/weights.py)")
    ap.add_argument("--ratio-baseline", default=None,
                    help="the FORMAT (not slug) ratio-dist and suite-totals normalize each query to (=1); the "
                         "schema-matched slug in the selection is used (e.g. dgraph-label_only). Default dgraph; "
                         "writes default nebula (the floor on bytes)")
    ap.add_argument("--allow-mixed-protocol", action="store_true",
                    help="a no-op (the default): captures timed under different request protocols "
                         "(measure/capture_protocol) are drawn, each capture's protocol written to the plots dir's "
                         "capture_protocol.json, and the run marked protocol_mixed")
    a = ap.parse_args(argv)
    a.ratio_baseline = a.ratio_baseline or ("nebula" if a.workload == "writes" else "dgraph")
    apply_house_style(1)
    w = weights.parse(a.weights)
    a.timings = str(L.timings_root(a.sf, a.workload, a.root))
    present = L.present(a.sf, a.workload, a.root)
    formats = [f for f in a.show.split(",") if f] if a.show else list(present)
    if not formats:
        raise SystemExit(f"nothing captured under {a.timings} (no counts/steps/latency of any slug)")
    for f in formats:
        L.check_slug(f)
    selections = [formats]
    if a.refresh:
        for _d, shown in L.selections(a.sf, a.workload, a.root):
            if all(s in present for s in shown) and sorted(shown) != sorted(formats):
                selections.append(shown)
    for shown in selections:
        render(a, w, shown, present)
    return 0


# --------------------------------------------------------- per-query ratio distribution (overview sibling)
# the count rows; the reads figures append the present read-latency labels (present_read_regimes)
RATIO_ROWS = ("weighted ops", "read bytes")
# the writes rows of the same two figures mirror the writes GRID (kvstats_derive: P|D|S|G|N at
# 1:1:1.1:1:0.01, bytes written, the apply wall); the read/write decomposition lives only in
# the cost-latency figures
WR_RATIO_ROWS = ("weighted write ops", "write bytes", "durable latency")
# metric label -> (regime dir, the `regime` column value to keep). The read metrics ARE the
# READ_LATENCY labels (a figure draws only the present ones, present_read_regimes); durable is the
# writes wall. One table, so adding a read regime needs no edit here.
LATENCY_REGIME = {lbl: (d, None) for d, lbl, _s in READ_LATENCY}
LATENCY_REGIME["durable latency"] = ("durable-p1", "headline")


def _group_by_query(per_run):
    """{(query, param): v} -> {query: [v, ...]}"""
    by = {}
    for (q, _p), v in per_run.items():
        by.setdefault(q, []).append(v)
    return by


def _ratio_series(rows, root, metric, formats, w, agg=_median):
    """{slug: {query: per-params value}} for one metric row: the median over params by default
    (ratio-dist, the typical query); `agg=_mean` for a figure that SUMS the queries into a total
    (suite_totals: a sum of medians is not the expected total, a sum of means is). Counts rows come
    from cpp.csv and take `agg`; the latency rows (the __plan__ wall of each regime's steps.csv) are
    measured and heavy-tailed, so they stay the MEDIAN over params in every figure (honest-plotting rule C)."""
    if metric == "cold block reads":
        cold = _first_regime(root, formats, "cold") or "cold-p2"
        return {f: {q: _median(v) for q, v in _group_by_query(_plan_blocks(_steps(root, cold, f) or [])).items()}
                for f in formats}
    counts_fn = {"weighted ops": lambda r: weights.wops(_f(r["S"]) or 0, _f(r["G"]) or 0, _f(r["N"]) or 0, w),
                 "read bytes": lambda r: _f(r.get("read_bytes")) or 0,
                 "weighted write ops": lambda r: sum((_f(r[k]) or 0) * D.WRITE_PER_GET[k] for k in D.OPS),
                 "write bytes": lambda r: (_f(r["write_key"]) or 0) + (_f(r["write_value"]) or 0)}
    if metric in counts_fn:
        per = _per_cell(rows)
        fn = counts_fn[metric]
        out = {f: {} for f in formats}
        for (q, db) in per:
            if db in out:
                out[db][q] = _med(per, q, db, fn, agg)
        return out
    regime, keep = LATENCY_REGIME[metric]
    out = {}
    for f in formats:
        by = {}
        for (q, _p), v in _plan_walls(_steps(root, regime, f) or [], regime=keep).items():
            by.setdefault(q, []).append(v)
        out[f] = {q: _median(v) for q, v in by.items()}
    return out


def ratio_dist_name(base):
    """per-group_size-normalized_ratio-to-<format>_dist.png -- the baseline's short FORMAT name (dg,
    neb), never the arm, so a label_only selection keeps the canonical name."""
    return f"per-group_size-normalized_ratio-to-{_style.format_short(L.format_of(base))}_dist.png"


def fig_ratio_dist(rows, root, workload, formats, plots, sf, w, baseline="dgraph", metrics=None, caveat=""):
    """Per-query cost RATIO to the baseline format, as a DISTRIBUTION -- one column per query
    group (IC | IS, workload_plans.groups), one row per currency (ops / bytes / warm / cold).
    Each cell: a box + jittered strip of the group's per-query ratios per non-baseline format,
    with the GEOMEAN (the headline) marked as a diamond; the baseline sits at 1 (log y). The
    geomean is the single number; the spread beside it stops one giant query passing for the
    typical case. Sibling of suite-totals; baseline = the selection's slug whose FORMAT is
    `baseline` (so a label_only selection normalizes to dgraph-label_only, not dgraph). metrics = the
    rows (RATIO_ROWS for reads, WR_RATIO_ROWS for writes); caveat = one clause under the title."""
    if metrics is None:                              # reads: count rows + the present latency labels
        metrics = tuple(RATIO_ROWS) + tuple(lbl for _d, lbl, _s in present_read_regimes(root, formats))
    base = next((f for f in formats if L.format_of(f) == baseline), None)
    others = [f for f in formats if f != base]
    grps = workload_plans.groups(workload)
    if base is None or not others or not grps:
        print(f"  ratio-dist: no {baseline} baseline in the selection -- skipped")
        return
    series = {m: _ratio_series(rows, root, m, formats, w) for m in metrics}
    if not any(series[m].get(base) for m in metrics):
        print(f"  ratio-dist: no data for the {baseline} baseline -- skipped")
        return
    fig, axes = plt.subplots(len(grps), len(metrics),
                             figsize=(3.9 * len(metrics), 3.3 * len(grps)), squeeze=False)
    for gi, g in enumerate(grps):
        for mi, m in enumerate(metrics):
            s = series[m]
            ax = axes[gi][mi]
            gqs = [q for q in g["queries"]
                   if s.get(base, {}).get(q, 0) > 0 and all(s.get(f, {}).get(q, 0) > 0 for f in others)]
            for i, f in enumerate(others):
                ratios = [s[f][q] / s[base][q] for q in gqs]
                if not ratios:
                    continue
                box_strip(ax, i + 1, ratios, format_color(f), show_mean=False)
                gm = geomean(ratios)
                if gm:
                    ax.plot(i + 1, gm, "D", ms=5, color="black", zorder=6)
                    ax.annotate(ratio_label(gm, number_to_human), (i + 1, gm), textcoords="offset points",
                                xytext=(6, 2), fontsize=10, fontweight="bold", va="bottom", ha="left", rotation=30)
                    ext = max(range(len(ratios)), key=lambda k: max(ratios[k], 1 / ratios[k]))
                    ax.annotate(gqs[ext], (i + 1, ratios[ext]), textcoords="offset points",
                                xytext=(0, 5 if ratios[ext] >= gm else -9), fontsize=6, ha="center", color="#666")
            ax.axhline(1.0, color="black", ls="--", lw=0.9, alpha=0.7)
            ax.set_yscale("log")
            ax.grid(axis="y", alpha=0.25)
            if gi == 0:
                ax.set_title(m, fontsize=10)
            if mi == 0:
                ax.set_ylabel(f"{g['key'].upper()}: ratio to {format_short(base)} (log, n={len(g['queries'])})", fontsize=9)
            ax.set_xticks(range(1, len(others) + 1))
            ax.set_xticklabels([format_short(f) for f in others] if gi == len(grps) - 1 else [],
                               rotation=20, ha="right", fontsize=8)
    _suptitle(fig, f"how much does each format cost relative to {format_short(base)}, query by query? sf{sf} {workload}\n"
                 f"size-NORMALIZED: every query weighs the same (its ratio to {format_short(base)}); box+strip = the per-query spread, "
                 f"diamond = geometric mean; the size-WEIGHTED total is suite-totals" + (f"\n{caveat}" if caveat else ""), y=1.0)
    fig.tight_layout()
    _save(fig, plots, ratio_dist_name(base))


# ------------------------------------------------ cost vs latency, size-controlled (two angles)
# Correlate cost with latency only where query size is held: within one (slug, query) across its
# params (does a costlier param run slower?), and within one (query, param) across the slugs (does
# the lower-cost format run faster?). A rho pooled across queries measures query size instead
# , so every rho here is per unit and summarized across the group's
# queries as a distribution. Latency = the __plan__ row's wall, as in every other kvstats figure.
CL_CURRENCIES = (("wops", "wops"), ("S+G", "S+G"), ("total_bytes", "bytes"),
                 ("geomean(ops*bytes)", "geo(o\u00d7b)"), ("(S+G)+bytes/4096", "(S+G)+B/4K"))
CL_ANGLES = (("param", "vary PARAM, fix (query, format)\n-> does cost track latency?"),
             ("format", "vary FORMAT, fix (query, param)\n-> does the lower-cost format have lower latency?"))
# The default warm/cold pair, kept as a stable export for external readers that price their own
# units against the two canonical regimes (dated captures read it: cold-p3 stays, it is what those captures hold). The
# no figure reads it -- cost_latency_units derives the PRESENT read regimes (present_read_regimes).
CL_REGIMES = (("warm_us", "warm-p0", "WARM"), ("cold_us", "cold-p3", "COLD"))


def cl_spec(w):
    """The read side's spec of the family: the currencies, the headline question, the latency note
    under it, and the per-query figure's lead. The caller injects "regimes" (the captured read
    regimes, from cost_latency_units) since which regimes a capture holds is data, not a constant."""
    return {"currencies": CL_CURRENCIES, "question": "does KV cost predict latency?",
            "latency_note": f"the plan's P=1 wall on the evaluator (every format on one RocksDB); weighted ops = {weights.label(w)}",
            "perquery_lead": "per-query cost-vs-latency rho (median over the varying axis)"}


# The WRITES mirror of the family: does write cost predict DURABLE write latency? The durable wall
# is ~97% the format's READ-MODIFY-WRITE CPU (measure/cppwlatency: decode + merge + re-encode of the
# posting / chunk / index), not the engine write, so the currencies span the read side (S+G = the
# fetch, read bytes = the decode volume), the write side (P+D, write bytes = the re-encode volume),
# and both (r+w bytes, the ops x bytes blend). The latency is the apply wall (WAL + memtable,
# buffered, no fsync), one durable-p1 file with two regimes: headline (perf 1, the number) and
# breakdown (perf 3, the timer-taxed pass that fills the WAL / memtable split -- the same cache
# state instrumented, a robustness line in the caption, never a figure row).
WR_CURRENCIES = (("P+D", "P+D"), ("S+G", "S+G"), ("wops", "wops"), ("write_bytes", "wbytes"),
                 ("read_bytes", "rbytes"), ("rmw_bytes", "r+w bytes"), ("geomean(ops*rmw_bytes)", "geo(ops\u00d7b)"))
# (col, the `regime` column's value in durable-p1/<slug>/steps.csv, label)
WR_REGIMES = (("durable_us", "headline", "DURABLE"), ("durable_brk_us", "breakdown", "DURABLE p3"))   # p3: side-car + caption only
WR_SPEC = {"currencies": WR_CURRENCIES, "regimes": WR_REGIMES,
           "question": "does write cost predict durable write latency?",
           "latency_note": ("latency = the perf-1 apply wall on the evaluator (WAL + memtable, no fsync; one measurement per "
                            "cell; ~97% of it the read-modify-write CPU)"),
           "caption": ("currencies: P+D = write ops, S+G = the read-modify-write's fetch ops, wops = the fetch weighted as the "
                       "reads are (110 S + 100 G + N), wbytes = bytes re-encoded, rbytes = bytes decoded (get + scan), r+w = both, "
                       "geo = sqrt((S+G+P+D) x r+w bytes); 'constant' = the currency never varies over that axis (a fixed-shape "
                       "write touches the same keys for every param)"),
           "perquery_lead": "per-query write-cost-vs-durable-latency rho (median over the varying axis)"}


def cost_latency_units(rows, root, formats, w):
    """(units, regimes): one row per (slug, query, param) priced in every CL_CURRENCIES currency, with
    its plan wall (us) per CAPTURED read-latency regime (READ_LATENCY, present-aware -- warm-p0 or its
    taxed warm-p3 twin, cold-p3, the true device-cold directIO, the bounded-cache steady pass); only
    the units every captured regime has. Each regime's column is its dir. rows = kvstats_layout.load_counts."""
    regimes = [(reg, reg, short) for reg, _lbl, short in present_read_regimes(root, formats)]  # (col, dir, label)
    walls = {f: {reg: _plan_walls(_steps(root, reg, f) or []) for _c, reg, _l in regimes} for f in formats}
    units = []
    for r in rows:
        f, k = r["db"], (r["query"], r["param_idx"])
        lat = {col: walls[f][reg].get(k) for col, reg, _l in regimes}
        if not regimes or not all(lat.values()):
            continue
        S, G, N = (_f(r["S"]) or 0), (_f(r["G"]) or 0), (_f(r["N"]) or 0)
        B = _f(r.get("read_bytes")) or 0
        wops = weights.wops(S, G, N, w)
        units.append({"slug": f, "query": r["query"], "param_idx": r["param_idx"],
                      "wops": wops, "S+G": S + G, "total_bytes": B,
                      "geomean(ops*bytes)": math.sqrt(wops * B) if B > 0 else 0.0,
                      "(S+G)+bytes/4096": S + G + B / 4096.0, **lat})
    return units, regimes


def _regime_agreement(units, groups, currencies, col_a, col_b):
    """{angle: the largest |median rho(col_a) - median rho(col_b)| over the angle's (group, currency)
    panels} -- how far the perf-3 pass moves the writes summary's medians from the perf-1 pass, per
    axis (a param-axis rho over n=4 params moves in steps of 0.2, the format axis over k=9 is finer)."""
    worst = {}
    for angle, _q in CL_ANGLES:
        worst[angle] = 0.0
        for g in groups:
            for cur, _s in currencies:
                meds = []
                for col in (col_a, col_b):
                    by = cost_latency_rhos(units, angle, cur, col)
                    vals = [r for q in g["queries"] for r in by.get(q, [])]
                    meds.append(statistics.median(vals) if vals else None)
                if None not in meds:
                    worst[angle] = max(worst[angle], abs(meds[0] - meds[1]))
    return worst


def _leading_kind(units, groups, currencies, col, tol=0.02):
    """{group title: which KIND of currency predicts best on the format axis} for one latency column:
    "bytes" when a byte currency (or the ops x bytes blend) is within `tol` of the top median rho,
    else the ops currencies that lead (e.g. "ops (S+G/wops)"). Kind, not the exact currency: the
    byte currencies tie within a few hundredths and a tie-break is not a ranking change."""
    lead = {}
    for g in groups:
        meds = {}
        for cur, short in currencies:
            vals = [r for q in g["queries"] for r in cost_latency_rhos(units, "format", cur, col).get(q, [])]
            if vals:
                meds[(cur, short)] = statistics.median(vals)
        if not meds:
            continue
        top = max(meds.values())
        leaders = [(cur, short) for (cur, short), m in meds.items() if m >= top - tol]
        ops = [short for cur, short in leaders if "bytes" not in cur]
        if any("bytes" in cur for cur, _s in leaders):
            lead[g["title"]] = "bytes" + (f" (tied with {'/'.join(ops)})" if ops else "")
        else:
            lead[g["title"]] = "ops (" + "/".join(ops) + ")"
    return lead


def cost_latency_units_writes(rows, root, formats, w):
    """(units, regimes): one row per (slug, query, param) priced in every WR_CURRENCIES currency, with
    its durable write wall (us) per captured WR_REGIMES regime; only the units every captured regime
    has. The counts row's ops and bytes are the same execution's as the durable pass's (checked: they
    agree), so the currencies come from the counts as on the read side: the row's read_bytes is the
    read-modify-write's READ volume (every read lane; kvstats_derive.writes_cells reads it the same way),
    write_key / write_value its write volume."""
    walls = {f: {col: _plan_walls(_steps(root, "durable-p1", f) or [], regime=reg) for col, reg, _l in WR_REGIMES}
             for f in formats}
    regimes = tuple(t for t in WR_REGIMES if any(walls[f][t[0]] for f in formats))
    units = []
    for r in rows:
        f, k = r["db"], (r["query"], r["param_idx"])
        lat = {col: walls[f][col].get(k) for col, _reg, _l in regimes}
        if not regimes or not all(lat.values()):
            continue
        S, G, N = (_f(r["S"]) or 0), (_f(r["G"]) or 0), (_f(r["N"]) or 0)
        pd = (_f(r["P"]) or 0) + (_f(r["D"]) or 0)
        wb = (_f(r["write_key"]) or 0) + (_f(r["write_value"]) or 0)
        rb = _f(r.get("read_bytes")) or 0
        units.append({"slug": f, "query": r["query"], "param_idx": r["param_idx"],
                      "P+D": pd, "S+G": S + G, "wops": weights.wops(S, G, N, w),
                      "write_bytes": wb, "read_bytes": rb, "rmw_bytes": rb + wb,
                      "geomean(ops*rmw_bytes)": math.sqrt((pd + S + G) * (rb + wb)) if rb + wb > 0 else 0.0, **lat})
    return units, regimes


def cost_latency_rhos(units, angle, currency, regime_col):
    """{query: [rho per unit]} for one angle: 'param' units are (slug, query) correlated across their
    params, 'format' units are (query, param) correlated across the slugs. A unit with a constant
    currency or latency (a point lookup whose params change nothing) has no rho and is left out."""
    key = (lambda u: (u["slug"], u["query"])) if angle == "param" else (lambda u: (u["query"], u["param_idx"]))
    # min_n=1: the unit sizes (params, formats) are small by design and stated on the figure
    per_unit = rho_by_group(units, key, currency, regime_col, min_n=1)
    by = {}
    for g, (rho, _n) in per_unit.items():
        if rho is not None:
            by.setdefault(g[1] if angle == "param" else g[0], []).append(rho)
    return by


def _cl_sizes(units, queries):
    """(k, n): the formats a format-axis unit ranks, and the typical (median) param count a
    param-axis unit spans -- the figure states both, since a rho over k or n points is quantized."""
    sel = [u for u in units if u["query"] in queries]
    per_unit = {}
    for u in sel:
        per_unit.setdefault((u["slug"], u["query"]), set()).add(u["param_idx"])
    n = int(statistics.median(len(v) for v in per_unit.values())) if per_unit else 0
    return len({u["slug"] for u in sel}), n


def _cl_panel_summary(ax, units, queries, col, angle, currencies, annotate=True):
    """One summary panel: per currency a box + strip of the per-unit rho over `queries`."""
    ax.axhline(0, color="0.4", lw=1, ls="--")
    drawn, empty = False, []
    for ci, (cur, short) in enumerate(currencies):
        by = cost_latency_rhos(units, angle, cur, col)
        vals = [r for q in queries for r in by.get(q, [])]
        if not vals:
            empty.append(ci + 1)
            continue
        drawn = True
        box_strip(ax, ci + 1, vals, "#4c78a8", show_mean=False, whis=(0, 100), ms=3.5)
        if annotate:
            ax.text(ci + 1, 1.32, f"{statistics.median(vals):+.2f}\n{sum(v > 0 for v in vals) / len(vals):.0%}>0",
                    ha="center", va="center", fontsize=7)
    if not drawn:                                    # a unit whose cost never varies (a point lookup) has no rho
        ax.text(0.5, 0.5, "no variation\n(rho undefined)", transform=ax.transAxes, ha="center", va="center",
                fontsize=8, color="0.4")
    else:                                            # one currency that never varies over this axis (a fixed-shape write)
        for x in empty:
            ax.text(x, 0, "constant\n(no rho)", ha="center", va="center", fontsize=3.5 + _EXTRALARGE, color="0.45")
    ax.set_xticks(range(1, len(currencies) + 1))
    ax.set_xticklabels([short for _c, short in currencies], fontsize=7, rotation=30, ha="right")
    ax.set_ylim(-1.25, 1.55)
    ax.grid(axis="y", ls=":", alpha=0.4)


def fig_cost_latency_summary(units, groups, formats, sf, workload, out, spec):
    """cost-latency-summary[-<tier>].png -- per currency a box + strip of every per-unit rho over a
    group's queries, annotated with the median (how strongly cost tracks latency) and the share of
    units with rho > 0 (how often it points the right way). One group: rows = the regimes (warm |
    cold; writes: the durable passes), cols = the two angles. Several groups (a banded workload):
    rows = (regime, angle), cols = the groups. spec = cl_spec(w) or WR_SPEC; its optional "caption"
    (the writes' currency glossary + robustness line) hangs under the figure."""
    currencies, regimes, question, latency_note = (spec[k] for k in ("currencies", "regimes", "question", "latency_note"))
    all_q = [q for g in groups for q in g["queries"]]
    k, n = _cl_sizes(units, all_q)
    panels = [(rl, col, angle, q) for col, _reg, rl in regimes for angle, q in CL_ANGLES]
    if len(groups) == 1:
        fig, axes = plt.subplots(len(regimes), len(CL_ANGLES), figsize=(6.4 * len(CL_ANGLES), 4.0 * len(regimes)),
                                 squeeze=False, constrained_layout=True)
        for ri, (col, _reg, rlabel) in enumerate(regimes):
            for ai, (angle, aq) in enumerate(CL_ANGLES):
                ax = axes[ri][ai]
                _cl_panel_summary(ax, units, groups[0]["queries"], col, angle, currencies)
                ax.set_title(f"{rlabel}: {aq}", fontsize=9.5)
                if ai == 0:
                    ax.set_ylabel("Spearman rho (cost vs latency)")
        title_groups = groups[0]["title"]
    else:
        fig, axes = plt.subplots(len(groups), len(panels), figsize=(4.4 * len(panels) + 0.8, 3.0 * len(groups) + 0.8),
                                 squeeze=False, constrained_layout=True)
        for gi, g in enumerate(groups):
            for pi, (rlabel, col, angle, aq) in enumerate(panels):
                ax = axes[gi][pi]
                _cl_panel_summary(ax, units, g["queries"], col, angle, currencies)
                if gi == 0:
                    ax.set_title(f"{rlabel}: {aq}", fontsize=9)
                if pi == 0:
                    ax.set_ylabel(f"{g['title']} (n={len(g['queries'])})\nrho", fontsize=9)
        title_groups = "every query group as a row"
    _suptitle(fig, f"{question} {title_groups}, sf{sf} {workload} -- size-controlled: one rho per unit "
                   f"(a param-axis unit spans n={n} params, a format-axis unit ranks k={k} formats)\n"
                   f"median = how strongly, '% > 0' = how often (different things); {latency_note}; formats: "
                   f"{', '.join(format_short(f) for f in formats)}")
    if spec.get("caption"):
        _caption(fig, spec["caption"], fontsize=8.5)
    _save(fig, os.path.dirname(out), os.path.basename(out))


def fig_cost_latency_perquery(units, groups, formats, sf, workload, out, spec):
    """cost-latency-perquery[-<tier>].png -- heatmap query x currency of the median per-unit rho,
    one panel per (regime, angle); several groups are banded down the rows with a separator.
    BLUE = cost predicts latency, RED = anti-predicts, blank = nothing varied."""
    currencies, regimes, lead = spec["currencies"], spec["regimes"], spec["perquery_lead"]
    queries = [q for g in groups for q in g["queries"]]
    bounds = []                                          # the row index where each group starts
    i = 0
    for g in groups:
        bounds.append((i, g["title"]))
        i += len(g["queries"])
    fig, axes = plt.subplots(len(regimes), len(CL_ANGLES),
                             figsize=(6.6 * len(CL_ANGLES), max(3.4, 0.26 * len(queries) + 2.2) * len(regimes)),
                             squeeze=False, constrained_layout=True)
    im = None
    for ri, (col, _reg, rlabel) in enumerate(regimes):
        for ai, (angle, aq) in enumerate(CL_ANGLES):
            ax = axes[ri][ai]
            mat = []
            for q in queries:
                row = []
                for cur, _s in currencies:
                    vals = cost_latency_rhos(units, angle, cur, col).get(q, [])
                    row.append(statistics.median(vals) if vals else float("nan"))
                mat.append(row)
            im = ax.imshow(mat, cmap=DIVERGING_CMAP, vmin=-1, vmax=1, aspect="auto")
            for i, q in enumerate(queries):
                for j in range(len(currencies)):
                    v = mat[i][j]
                    if v == v:
                        ax.text(j, i, f"{v:+.1f}", ha="center", va="center", fontsize=6.5,
                                color="white" if abs(v) > 0.6 else "black")
            for start, title in bounds[1:]:
                ax.axhline(start - 0.5, color="white", lw=2.5)
            if len(groups) > 1 and ai == len(CL_ANGLES) - 1:
                for start, title in bounds:
                    ax.text(len(currencies) - 0.4, start - 0.3, title, fontsize=7, ha="left", va="top",
                            color="0.25", clip_on=False)
            ax.set_xticks(range(len(currencies)))
            ax.set_xticklabels([short for _c, short in currencies], fontsize=8, rotation=30, ha="right")
            ax.set_yticks(range(len(queries)))
            ax.set_yticklabels(queries, fontsize=7)
            ax.set_title(f"{rlabel}: {aq}", fontsize=9.5)
    if im is not None:
        fig.colorbar(im, ax=axes, fraction=0.03, label="median rho (cost vs latency)")
    _suptitle(fig, f"{lead}, sf{sf} {workload}\n"
                   f"BLUE = cost predicts latency, RED = anti-predicts, blank = nothing varied; evaluator latencies")
    _save(fig, os.path.dirname(out), os.path.basename(out))


def fig_cost_latency(rows, root, workload, formats, plots, sf, w):
    """Both cost-vs-latency figures, banded (ldbc per tier, else one per type), plus the per-unit
    table (cost-latency-units.csv); nothing when no unit has every regime captured. The writes
    workload prices its units in the write currencies against the durable write wall (WR_*); every
    other workload in the read currencies against the warm and cold walls (CL_*)."""
    if workload == "writes":
        units, regimes = cost_latency_units_writes(rows, root, formats, w)
        # the figure draws the perf-1 pass only; the perf-3 pass (the same regime instrumented) is a
        # robustness line in the caption, computed here, and the durable_brk_us column of the side-car
        spec = {**WR_SPEC, "regimes": regimes[:1]}
        if len(regimes) > 1:
            # the robustness line: the pooled (all writes) medians first -- the finding's stability -- then the
            # per-class panels, whose few units make a median jump a rank step; then the ranking itself
            groups = workload_plans.groups(workload)
            pooled = [{"title": "all", "queries": [q for g in groups for q in g["queries"]]}]
            p1, p3 = regimes[0][0], regimes[1][0]
            dp = _regime_agreement(units, pooled, spec["currencies"], p1, p3)
            dc = _regime_agreement(units, groups, spec["currencies"], p1, p3)
            lead1, lead3 = (_leading_kind(units, groups, spec["currencies"], c) for c in (p1, p3))
            if lead1 == lead3:
                lead = ("which kind of currency predicts best per class is the same under both passes: "
                        + ", ".join(f"{g} = {c}" for g, c in lead1.items()))
            else:
                lead = ("the best-predicting kind changes under perf 3 for: "
                        + ", ".join(f"{g} ({lead1.get(g)} -> {lead3.get(g)})" for g in lead1 if lead1.get(g) != lead3.get(g)))
            # k formats per unit: under 5 a rho is one of a few values (k=3: -1, -0.5, 0.5, 1) and a
            # panel median jumps a whole step between passes -- say so instead of calling it robust
            k = len({u["slug"] for u in units})
            quant = (f" k={k}: a per-unit rho takes one of a few values, so read the boxes as vote counts ('% > 0'), not "
                     f"as a spread, and a panel median can jump a whole step between passes." if k < 5 else "")
            spec["caption"] += (f". The perf-3 timer tax ({p3} in cost-latency-units.csv, the same pass instrumented) moves a "
                                f"median over all writes by at most {dp['format']:.2f} (format axis) / {dp['param']:.2f} (param "
                                f"axis), a per-class panel's by up to {dc['format']:.2f} / {dc['param']:.2f} (a 3-op class has few "
                                f"units; one rank step over n=4 params is 0.2).{quant} {lead[0].upper() + lead[1:]}")
    else:
        units, regimes = cost_latency_units(rows, root, formats, w)
        spec = {**cl_spec(w), "regimes": regimes}
    if not units:
        print(f"  cost-latency: no unit with every regime captured ({', '.join(r[1] for r in spec['regimes'])}) -- skipped")
        return
    _write_side_car(plots, "cost-latency-units.csv", units)
    have = {u["query"] for u in units}
    for tag, groups in _agg_bands(workload):
        groups = [{**g, "queries": [q for q in g["queries"] if q in have]} for g in groups]
        groups = [g for g in groups if g["queries"]]
        if not groups:
            continue
        suffix = f"-{tag}" if tag else ""
        fig_cost_latency_summary(units, groups, formats, sf, workload, os.path.join(plots, f"cost-latency-summary{suffix}.png"), spec)
        fig_cost_latency_perquery(units, groups, formats, sf, workload, os.path.join(plots, f"cost-latency-perquery{suffix}.png"), spec)


# ------------------------------------------------ suite totals, the two-currency trade-off, store size
# the count columns; the reads figure inserts a column per PRESENT read-latency regime between
# them and cold block reads (read_suite_rows), so a merged tree shows warm / cold / directIO / steady
SUITE_METRICS = (("weighted ops", weights.human), ("read bytes", bytes_to_human),
                 ("cold block reads", number_to_human))


def _log_axis(ax, human, axis="y"):
    """A log axis labelled in human units on 1/2/5 decades and nothing on the minor ticks -- a
    narrow range otherwise shows raw '4 x 10^7' minors and no major at all."""
    a = ax.yaxis if axis == "y" else ax.xaxis
    # the 1/2/5 labels crowd past ~2 decades (a trade-off panel spanning 500 to 20M x-labelled
    # every 1/2/5 step was unreadable): decades only there, 1/2/5 on a narrow range
    lo, hi = a.get_view_interval()
    span = math.log10(hi / lo) if lo > 0 and hi > lo else 1.0
    subs = (1.0,) if span > 2.2 else (1.0, 2.0, 5.0)
    a.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=subs, numticks=12))
    a.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: human(v)))
    a.set_minor_formatter(matplotlib.ticker.NullFormatter())


def format_name(slug):
    """The full name for an axis with room (suite totals, store size, the trade-off): the canon's
    label for the format plus the slug's arm (`Dgraph`, `NebulaGraph`, `pertype_csr c4k`)."""
    base = L.format_of(slug)
    rest = slug[len(base):].strip("-").replace("-", " ")
    return (_style.db_label(base) + (f" {rest}" if rest else "")).strip()


def suite_totals(rows, root, formats, w, queries, metrics=SUITE_METRICS):
    """{metric: {slug: (total, n)}} -- the SUM over `queries` of the per-query MEANS over params (the
    expected, additive total; a sum of per-query medians is not -- on the small IS queries it ran 25%
    above the mean-based total): the size-weighted 'total work under this suite' reading,
    never a 'typical query'. Latency rows sum per-query MEDIANS (measured, heavy-tailed: honest-plotting
    rule C). A slug missing a query is summed over what it has (n says how many). The derived
    'warm ns per op' row exists only when its two inputs are among `metrics`."""
    out = {}
    for metric, _fmt in metrics:
        if metric == "warm ns per op":               # derived below, not a measured series
            continue
        per = _ratio_series(rows, root, metric, formats, w, agg=_mean)
        out[metric] = {}
        for f in formats:
            vals = [per.get(f, {}).get(q) for q in queries]
            vals = [v for v in vals if v]
            if vals:
                out[metric][f] = (sum(vals), len(vals))
    # warm ns per weighted op = the ratio of the two totals (a ratio of sums, as per-group_means' was);
    # keyed on whichever warm regime the capture holds (warm-p0 or the taxed warm-p3)
    warm_key = next((m for m, _f in metrics if m.startswith("warm latency")), None)
    if warm_key and warm_key in out and "weighted ops" in out:
        out["warm ns per op"] = {f: (out[warm_key][f][0] * 1e3 / out["weighted ops"][f][0], out[warm_key][f][1])
                                 for f in formats if f in out.get(warm_key, {}) and f in out.get("weighted ops", {})
                                 and out["weighted ops"][f][0]}
    return out


SUITE_ROWS = SUITE_METRICS + (("warm ns per op", lambda v: f"{v:.0f}"),)
WR_SUITE_ROWS = (("weighted write ops", weights.human), ("write bytes", bytes_to_human), ("durable latency", us_to_human))


def read_suite_rows(root, formats):
    """The reads suite-totals columns: the count rows, then a column per PRESENT read-latency regime
    (READ_LATENCY order), cold block reads, and the derived warm-ns-per-op. Present-aware, so a
    counts-only capture drops the latency columns and a merged tree shows every regime it holds."""
    lat = tuple((lbl, us_to_human) for _d, lbl, _s in present_read_regimes(root, formats))
    return ((("weighted ops", weights.human), ("read bytes", bytes_to_human)) + lat
            + (("cold block reads", number_to_human), ("warm ns per op", lambda v: f"{v:.0f}")))


def fig_suite_totals(rows, root, workload, formats, plots, sf, w, baseline="dgraph", metrics=None,
                     size_metric="weighted ops", caption=None):
    """suite-totals[-<tier>].png -- per query group (a row) and metric (a column) the group's TOTAL
    per format: the sum over the group's queries of each query's mean over params (latency: median), every bar
    labelled with its ratio to the baseline format (the merged per-group_means: a ratio of sums).
    Size-WEIGHTED by construction -- the largest query sets it (named per row); the equal-weight
    view is per-group_size-normalized_ratio-to-<base>_dist. metrics = the columns (SUITE_ROWS for
    reads, WR_SUITE_ROWS for writes); size_metric names the largest query per group; caption = the
    method line (None: the reads' one)."""
    if metrics is None:                              # reads: count rows + present latency + block reads
        metrics = read_suite_rows(root, formats)
    base = next((f for f in formats if L.format_of(f) == baseline), None)
    dist_name = ratio_dist_name(base).rsplit(".", 1)[0] if base else "per-group_size-normalized_ratio-to-dg_dist"
    for tag, groups in _agg_bands(workload):
        tots = [(g, suite_totals(rows, root, formats, w, g["queries"], metrics=metrics)) for g in groups]
        tots = [(g, t) for g, t in tots if any(t.get(m) for m, _f in metrics)]
        if not tots:
            continue
        cols = [(m, fmt) for m, fmt in metrics if any(t.get(m) for _g, t in tots)]
        fig, axes = plt.subplots(len(tots), len(cols), figsize=(2.6 * len(cols) + 1, 2.9 * len(tots) + 0.6),
                                 constrained_layout=True, squeeze=False)
        dominant = []
        for gi, (g, t) in enumerate(tots):
            per = _ratio_series(rows, root, size_metric, formats, w, agg=_mean)
            dom = max(g["queries"], key=lambda q: max((per.get(f, {}).get(q) or 0) for f in formats), default=None)
            if dom and t.get(size_metric):
                share = max((per.get(f, {}).get(dom) or 0) / t[size_metric][f][0] for f in formats if f in t[size_metric])
                dominant.append(f"{g['title']}: {dom} = {share:.0%} of the largest format's {size_metric}")
            for ci, (metric, fmt) in enumerate(cols):
                ax = axes[gi][ci]
                vals = t.get(metric, {})
                fmts = [f for f in formats if f in vals]
                base_v = vals[base][0] if base in vals else None
                for fi, f in enumerate(fmts):
                    v, n = vals[f]
                    ax.bar(fi, v, 0.7, color=format_color(f), **bar_kw(f))
                    lab = ("1x" if f == base else ratio_label(v / base_v, number_to_human)) if base_v else fmt(v)
                    ax.text(fi, v, lab, ha="center", va="bottom", fontsize=7)
                ax.set_yscale("log")
                _log_axis(ax, fmt)
                lo = min(vals[f][0] for f in fmts)
                ax.set_ylim(bottom=lo / 4, top=max(vals[f][0] for f in fmts) * 2.5)   # every bar has a visible foot
                ax.set_xticks(range(len(fmts)))
                ax.set_xticklabels([format_name(f) for f in fmts] if gi == len(tots) - 1 else [], rotation=45, ha="right", fontsize=7)
                ax.tick_params(axis="y", labelsize=7)
                ax.grid(axis="y", alpha=0.3)
                if gi == 0:
                    ax.set_title(metric, fontsize=9)
                if ci == 0:
                    ax.set_ylabel(f"{g['title']} (n={len(g['queries'])})\nsuite total (log)", fontsize=8)
        _suptitle(fig, f"which format does the least work over the whole suite, per query group? sf{sf} {workload} -- "
                       f"size-WEIGHTED totals: sum over the group's queries of each query's mean over params (the expected, additive total); bar label = ratio to "
                       f"{format_short(base) if base else 'the first format'}\n"
                       f"the largest query sets a total; the size-NORMALIZED view (equal weight per query) is "
                       f"{dist_name}")
        if caption is None:
            caption = (f"ops, bytes and block reads are deterministic sums of per-query means; warm/cold = per-query MEDIANS then summed "
                       f"(measured, heavy-tailed); warm ns per op = "
                       f"warm total / weighted-ops total (weighted ops = {weights.label(w)})")
        _caption(fig, f"{caption}. Largest query per group -- " + "; ".join(dominant))
        _save(fig, plots, _tagged("suite-totals.png", tag).replace("suite-totals_", "suite-totals-"))


def fig_tradeoff(rows, root, workload, formats, plots, sf, w, agg="sum"):
    """tradeoff-ops-bytes_size-weighted.png / _size-normalized.png -- one panel per query group:
    each format as a point on the (weighted ops, read bytes) plane, log-log, the Pareto front
    drawn; a hollow point is dominated (another format does no worse on both currencies). Two
    aggregations, a pair: agg="sum" = the suite totals (size-WEIGHTED: the heaviest queries set
    the point -- which format for the whole workload's total cost); agg="geomean" = the geometric
    mean over the group's queries of the per-query medians (size-NORMALIZED: equal weight per
    query in log space -- which format on a typical query). A front that differs between the two
    is the finding: is the trade-off ranking size-robust? (honest-plotting pitfall E)"""
    groups = workload_plans.groups(workload)
    ncol = min(4, len(groups)); nrow = math.ceil(len(groups) / ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(5.2 * ncol, 4.8 * nrow), squeeze=False, constrained_layout=True)
    flat = [ax for row in axes for ax in row]
    drawn = False
    writing = workload == "writes"
    # writes: the same plane in the write currencies -- weighted write ops (P/D beside the
    # read-modify-write's S/G/N) vs bytes MOVED (the read-modify-write's read + the written)
    wcells = D.writes_cells(rows) if writing else {}
    for ax, g in zip(flat, groups):
        if writing:
            pts = []
            for f in formats:
                qs = [q for q in g["queries"] if q in wcells.get(f, {})]
                if not qs:
                    continue
                o = [wcells[f][q]["wops"] for q in qs]
                b = [wcells[f][q]["read_bytes"] + wcells[f][q]["written_bytes"] for q in qs]
                o, b = (sum(o), sum(b)) if agg == "sum" else (geomean(o), geomean(b))
                if o and b:
                    pts.append((o, b, f))
        elif agg == "sum":
            tot = suite_totals(rows, root, formats, w, g["queries"])
            pts = [(tot["weighted ops"][f][0], tot["read bytes"][f][0], f) for f in formats
                   if f in tot.get("weighted ops", {}) and f in tot.get("read bytes", {})]
        else:
            ops = _ratio_series(rows, root, "weighted ops", formats, w)
            byt = _ratio_series(rows, root, "read bytes", formats, w)
            pts = []
            for f in formats:
                o = geomean([ops.get(f, {}).get(q) or 0 for q in g["queries"]])      # positives only (log space)
                b = geomean([byt.get(f, {}).get(q) or 0 for q in g["queries"]])
                if o and b:
                    pts.append((o, b, f))
        if len(pts) < 2:
            ax.axis("off")
            continue
        drawn = True
        ax.set_xscale("log"); ax.set_yscale("log")    # before the scatter: it places labels in pixel space
        front = tradeoff_scatter(ax, pts, format_color, format_name)
        _log_axis(ax, weights.human, "x"); _log_axis(ax, bytes_to_human, "y")
        ax.tick_params(labelsize=8)
        how = ", suite total" if agg == "sum" else ", geomean over queries"
        ax.set_xlabel(("weighted write ops" if writing else "weighted KV ops") + how + " (log)", fontsize=9)
        ax.set_ylabel(("bytes moved" if writing else "read bytes") + how + " (log)", fontsize=9)
        ax.set_title(f"{g['title']}: on the front = {', '.join(format_short(f) for f in front)}", fontsize=9)
        ax.grid(alpha=0.3, which="both")
    for ax in flat[len(groups):]:
        ax.axis("off")
    if not drawn:
        plt.close(fig)
        return
    # writes: counts and bytes are deterministic, the per-write value is the mean over params
    stat = "mean" if writing else "median"
    wlabel = ("G:S:N:P:D = " + ":".join(f"{D.WRITE_PER_GET[k]:g}" for k in ("G", "S", "N", "P", "D"))
              + "; bytes moved = the read-modify-write's read bytes + the written key and value bytes") if writing else weights.label(w)
    plane = "write ops vs bytes moved" if writing else "ops vs bytes"
    if agg == "sum":
        _suptitle(fig, f"{plane}, size-WEIGHTED: which formats are worth having for the whole workload's TOTAL cost? "
                       f"sf{sf} {workload} -- one point per format = the suite totals, lower-left is better\n"
                       f"filled = on the Pareto front (no format beats it on both currencies), hollow = dominated")
        _caption(fig, f"suite totals = the sum over the group's queries of each query's {stat} over params -- set by the "
                      f"heaviest queries; weighted ops = {wlabel}. Typical-query view = the _size-normalized twin.")
        name = "tradeoff-ops-bytes_size-weighted.png"
    else:
        _suptitle(fig, f"{plane}, size-NORMALIZED: which formats are worth having on a TYPICAL query? sf{sf} {workload} "
                       f"-- one point per format = the geometric mean over the group's queries of the per-query {stat}s, "
                       f"lower-left is better\nfilled = on the Pareto front, hollow = dominated; a front that differs from the "
                       f"size-weighted twin is the finding: is the trade-off ranking size-robust?")
        _caption(fig, f"geomean = equal weight per query in log space (an arithmetic mean is still size-dominated); non-positive "
                      f"values are left out; weighted ops = {wlabel}. Total-cost view = the _size-weighted twin.")
        name = "tradeoff-ops-bytes_size-normalized.png"
    _save(fig, plots, name)


def store_bytes(sf, slug, run_records, sizes_csv=None):
    """(bytes, source) of a slug's store, most PORTABLE source first: the committed store-size.csv
    (travels with the capture -- redraws on a fresh machine with no store ingested), then the newest
    run record that carries store_bytes (runs/ is not committed), then the store on disk right now."""
    row = (sizes_csv or {}).get(slug)
    if row and row.get("total_bytes"):
        return int(row["total_bytes"]), f"csv {row.get('recorded', '')}".strip()
    for rec in run_records:                          # newest first
        b = (rec.get("store_bytes") or {}).get(slug)
        if b:
            return int(b), f"run {rec.get('run')}"
    d = store_dir("ldbc", sf, slug)
    if os.path.isdir(d):
        total = 0
        for dirpath, _dirs, files in os.walk(d):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        return total, "on disk at plot time"
    return None, None


def fig_store_size(sf, workload, formats, plots, tree=None):
    """store-size.png -- the absolute size of every drawn store (the pertype study's store-size
    pattern): bytes from the capture's run record when it recorded them, else the store as it is
    on disk now (said on the bar); log y."""
    recs = []
    d = L.timings_root(sf, workload, tree) / "runs"
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d), reverse=True):
            if fn.endswith(".json"):
                try:
                    recs.append(json.load(open(os.path.join(d, fn))))
                except (OSError, ValueError):
                    pass
    sizes_csv = L.load_store_sizes(sf, workload)     # the committed, machine-portable source
    sizes = {f: store_bytes(sf, f, recs, sizes_csv) for f in formats}
    sizes = {f: v for f, v in sizes.items() if v[0]}
    if not sizes:
        print("  store-size: no store-size.csv, no run record, and no store on disk -- skipped "
              "(run run/kvstats.sh on the machine that has the stores)")
        return
    fig, ax = plt.subplots(figsize=(1.1 * len(sizes) + 3, 4.6), constrained_layout=True)
    fmts = [f for f in formats if f in sizes]
    for fi, f in enumerate(fmts):
        b, src = sizes[f]
        ax.bar(fi, b, 0.7, color=format_color(f), **bar_kw(f))
        ax.text(fi, b, f" {bytes_to_human(b)}\n{src}", ha="center", va="bottom", fontsize=7)
    # linear unless the stores span > 100x (a chunk store beside an unbounded one can): the bar
    # length is then the size, which a log axis would hide
    big = max(b for b, _s in sizes.values()) / max(1, min(b for b, _s in sizes.values()))
    if big > 100:
        ax.set_yscale("log")
        _log_axis(ax, bytes_to_human)
    else:
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _p: bytes_to_human(v)))
    ax.set_ylim(top=ax.get_ylim()[1] * (4 if big > 100 else 1.18))
    ax.set_xticks(range(len(fmts)))
    ax.set_xticklabels([format_name(f) for f in fmts], rotation=45, ha="right")
    ax.set_ylabel("store size on disk (RocksDB files" + (", log)" if big > 100 else ")"))
    ax.grid(axis="y", alpha=0.3)
    ax.set_title(f"how big is each store? sf{sf} -- the ingested RocksDB store per format (bytes = "
                 f"store-size.csv, else the run record, else the store on disk now; source on each bar)", fontsize=9)
    _save(fig, plots, "store-size.png")


# figure names this script no longer writes: removed from a plots dir on render
RETIRED_FIGURES = ("kvops.png", "kvbytes.png", "latency.png", "latency_cold.png", "amplification.png",
                   "blends-ic.png", "blends-is.png", "correlation-perrun.png", "overview.png",
                   "amplification_ratio.png", "combined-ic.png", "combined-is.png", "combined.png",
                   "overview-groups.png", "overview-groups-ratio-dist.png", "combined-correlation.png",
                   "cold_reads.png", "writes-primitives.png", "writes-ldbc.png",
                   "writes_wa-primitives.png", "writes_wa-ldbc.png",
                   # other names of the same figures
                   "per-group_means.png", "per-query_ratio-to-baseline_dist.png", "per-query-grid_ic.png",
                   "per-query-grid_is.png", "per-query-grid.png", "writes-grid_primitives.png", "writes-grid_ldbc.png",
                   "writes_data-per-payload-byte_primitives.png", "writes_data-per-payload-byte_ldbc.png",
                   "suite-totals-point.png", "suite-totals-enumerate.png", "suite-totals-property.png",
                   "suite-totals-topk.png", "suite-totals-hop1.png", "suite-totals-nhop.png", "suite-totals-composed.png",
                   "cost-latency-summary-point.png", "cost-latency-summary-enumerate.png", "cost-latency-summary-property.png",
                   "cost-latency-summary-topk.png", "cost-latency-summary-hop1.png", "cost-latency-summary-nhop.png",
                   "cost-latency-summary-composed.png", "cost-latency-perquery-point.png", "cost-latency-perquery-enumerate.png",
                   "cost-latency-perquery-property.png", "cost-latency-perquery-topk.png", "cost-latency-perquery-hop1.png",
                   "cost-latency-perquery-nhop.png", "cost-latency-perquery-composed.png", "tradeoff-ops-bytes.png")


def latency_captures(root, formats):
    """every latency capture dir of the shown slugs under a capture root: the read regimes and the durable writes"""
    return [_timing_dir(root, d, f) for d in [d for d, _l, _s in READ_LATENCY] + ["durable-p1"] for f in formats
            if os.path.exists(os.path.join(_timing_dir(root, d, f), "steps.csv"))]


def render(a, w, formats, present):
    """One selection's figures: every query the shown slugs ever measured (the union over the
    accumulated counts/steps/latency files), into the selection's plots dir."""
    plots = L.plots_dir(a.sf, a.workload, formats, a.root)
    # the latency captures drawn side by side should have timed their requests the same way (measure/capture_protocol):
    # when they differ the figures are drawn, each capture's protocol written beside them, and the run marked
    # (kvevaluator.stale: protocol_mixed; --allow-mixed-protocol is a no-op).
    # One check over every regime dir on the requests and the timer: dirs of one regime (warm-p0 and warm-p3) are
    # compared in the same figures, as two series named by their perf level. Then each regime dir (one series) on its own,
    # the perf timers included: a series never mixes a wall capture with a breakdown one
    caps = latency_captures(a.timings, formats)
    labels = check_same(caps, os.path.relpath(a.timings), allow_mixed=True, with_perf=False)
    for d in sorted({os.path.basename(c) for c in caps}):
        labels.update(check_same([c for c in caps if os.path.basename(c) == d],
                                 f"{os.path.relpath(a.timings)} {d}", allow_mixed=True))
    L.write_selection(plots, formats)
    if labels:
        with open(os.path.join(plots, "capture_protocol.json"), "w") as f:
            json.dump({os.path.relpath(c, a.timings): v for c, v in sorted(labels.items())}, f, indent=1, sort_keys=True)
        stale.note("protocol_mixed", f"{os.path.relpath(plots)}: captures timed under different request protocols, "
                   f"drawn labelled (each capture's protocol: capture_protocol.json beside the figures)")
    elif os.path.exists(os.path.join(plots, "capture_protocol.json")):
        os.remove(os.path.join(plots, "capture_protocol.json"))   # an earlier mixed render's labels; this set is one protocol
    print(f"[plot_kvstats] slugs {formats} -> {os.path.relpath(plots)}")
    summary = {f: {} for f in formats}
    rows = L.load_counts(a.sf, a.workload, formats, a.root)
    for name in RETIRED_FIGURES:                     # a re-render leaves no retired or old-name file behind
        if os.path.exists(os.path.join(plots, name)):
            os.remove(os.path.join(plots, name))
            print(f"  removed retired {name}")
    if a.workload == "writes":
        if rows:
            for f, d in fig_writes(rows, a.sf, formats, plots, a.sf).items():
                summary[f].update(d)
            fig_tradeoff(rows, a.timings, a.workload, formats, plots, a.sf, w, agg="sum")
            fig_tradeoff(rows, a.timings, a.workload, formats, plots, a.sf, w, agg="geomean")
            fig_cost_latency(rows, a.timings, a.workload, formats, plots, a.sf, w)
            wr_caveat = (f"ratio to {_style.format_short('nebula')}: nebula is the floor on bytes (the per-entry write), "
                         f"not on ops or latency")
            fig_suite_totals(rows, a.timings, a.workload, formats, plots, a.sf, w, baseline=a.ratio_baseline, metrics=WR_SUITE_ROWS,
                             size_metric="weighted write ops",
                             caption=f"weighted write ops = P:D:S:G:N at 1:1:1.1:1:0.01 (the writes grid's row) and write bytes "
                                     f"are deterministic sums of per-write means over the params; durable latency = per-write MEDIANS of "
                                     f"the perf-1 apply wall, then summed; {wr_caveat}")
            fig_ratio_dist(rows, a.timings, a.workload, formats, plots, a.sf, w, baseline=a.ratio_baseline, metrics=WR_RATIO_ROWS,
                           caveat=wr_caveat)
        else:
            print("  writes: no counts/<slug>.csv -- skipped")
    elif rows:
        for f, d in kvops_summary(rows, a.workload, formats, w).items():
            summary[f].update(d)
        for f, d in kvbytes_summary(rows, a.workload, formats).items():
            summary[f].update(d)
    else:
        print("  counts/bytes: no counts/<slug>.csv -- skipped")
    if a.workload != "writes":
        for f, d in latency_summary(a.timings, a.workload, formats).items():
            summary[f].update(d)
    fig_cold_reads(a.timings, a.workload, formats, plots, a.sf, w)
    for f, d in fig_amp(a.timings, a.workload, formats, plots, a.sf, w, tree=a.root).items():
        summary[f].update(d)
    if a.workload != "writes":
        fig_ratio_dist(rows, a.timings, a.workload, formats, plots, a.sf, w, baseline=a.ratio_baseline)
        if rows:
            fig_suite_totals(rows, a.timings, a.workload, formats, plots, a.sf, w, baseline=a.ratio_baseline)
            fig_tradeoff(rows, a.timings, a.workload, formats, plots, a.sf, w, agg="sum")
            fig_tradeoff(rows, a.timings, a.workload, formats, plots, a.sf, w, agg="geomean")
    fig_store_size(a.sf, a.workload, formats, plots, tree=a.root)
    stats = None
    if rows and a.workload != "writes":
        table, stats = correlation_table(rows, a.timings, a.workload, formats, w)
        _write_side_car(plots, "correlation.csv", table)
        _write_side_car(plots, "correlation-summary.csv", stats)
        matrix = fig_combined_correlation(table, a.workload, formats, plots, a.sf, w)
        if matrix:
            _write_side_car(plots, "pairs.csv", matrix)
            # no correlation-<group>.png (per-slug pair matrices): size-confounded,
            # they score ~0.95 for every currency. cost-latency-summary-<group>.png is the format-ranking view.
            fig_cost_latency(rows, a.timings, a.workload, formats, plots, a.sf, w)
    fig_combined(rows, a.timings, a.workload, formats, plots, a.sf, w)


if __name__ == "__main__":
    sys.exit(main())
