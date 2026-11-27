#!/usr/bin/env python3
"""The per-workload figures, drawn from workload_story.py's CSVs (benchmarks.workload_dir: one folder per workload).
Three figures per workload:

  breakdown-parts.png, breakdown-ops-bytes.png
                 Why? Each query's cost (one run) and the workload total under the shown options, stacked by store
                 part / by KV op and byte lane.
  options.png    Is there a single best? The options map (total ops against total bytes: the 9 single layouts,
                 measured, and every distinct mix, priced) above the choice matrix (the layout each option gives
                 each part) and, under it on the same columns, which parts each query reads or writes.
                 --panel map|matrix draws the map alone / the matrix with the query grid (options-map.png /
                 options-matrix.png).

Shown options (breakdown): the named single layouts (compose.KNOWN), the best single in ops and in bytes, and
every distinct mix once (distinct_mixes). The workloads are drawn side by side in a fork pool (LAYOUT_CHOICE_JOBS at a
time; each writes its own folder); the printed lines come in workload order.

    python3 experiments/layout_choice/plot_workload_story.py --sf 3 [--workload w14] [--panel both|map|matrix]
"""
import argparse
import glob
import csv
import math
import multiprocessing
import os
import sys
import textwrap
from collections import defaultdict

import matplotlib
from kvevaluator.paths import ROOT
from kvevaluator.layouts import SETTINGS, cell_id
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter
from matplotlib.transforms import blended_transform_factory

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.formatting import number_to_human
from kvcost.plotting.style import BYTE_CHANNEL_COLOR, OP_COLOR, OP_LABEL, apply_house_style, ratio_label
from kvcost.plotting.formatting import decimal_bytes, short_bytes  # (decade ticks; labels)
from kvcost.layout_cost import cost_models as CM  # (the cost models, one definition)
from kvcost.plotting.style import caption, log_share_stack
from kvcost.layout_cost.labels import (BLEND_BYTES_COLOR, BLEND_OPS_COLOR, BUDGET_COLOR, DEL_COLOR, PUT_COLOR,
                                       RMW_READ_COLOR, WRITTEN_COLOR)
from kvcost.layout_cost import settings as cells  # (the cells' short names and the reference)
from kvcost.layout_search.search import options_b_unit  # (B's unit of an options.csv)
from kvcost.layout_cost.compose import KNOWN, MISS_LANES
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.labels import SUM_LABEL, axis_label, rule_label, sum_text
from kvcost.layout_cost.benchmarks import kind_of
from kvcost.layout_cost.labels import display_title
from kvevaluator.capture.step_record import HIT_BYTE_LANES  # (the lanes, one definition)

DATASET = "ldbc"     # the suite main() draws (benchmarks.py)
PARAM_SET = None   # its param set (--param-set; LinkBench's names it in the titles)


LAYOUTS = SETTINGS
SPLIT_CONDENSED = "props_split_adj_condensed"         # the breakdown ratios' other base (--ratio-to split-condensed)
RATIO_BASE = "props_condensed_adj_split"              # Co-Sp: the breakdown bars' ratio base (--ratio-to reference); not B's unit
B_UNIT_NAME = cells.name_of(cells.B_UNIT)            # B's unit (Sp-Co) as the captions name it
# the choice matrix's single-layout rows: all nine, in grid order (props, then adjacency)
REFERENCE_ROWS = tuple(cell_id(p, a) for p in SETTINGS
                       for a in SETTINGS)
# One colour per layout, the same for a part in the choice matrix and for the adjacency on the map
# (Dark2: not the format palette, so a layout is never read as a format)
LAYOUT_COLOR = {"split": "#1b9e77", "condensed": "#d95f02", "chunked": "#7570b3", "fixed": "#e6e6e6"}
PROPS_MARKER = {"split": "o", "condensed": "s", "chunked": "^"}     # the map: shape = props layout
MIX_SETS = ("per type", "per type and direction")    # the option sets the figures draw as mixes (options.csv also
                                                     # holds "per type, no grouping" rows)


def layout_style(lay):
    """(fill colour, text colour, hatch, cell label) of a layout"""
    return LAYOUT_COLOR[lay], "white", None, lay.capitalize()
# Store-part colours: one per part, fixed by the part's place in the schema (schema_parts), so a part keeps its colour
# in every figure of a suite. Vertex properties are hatched, edge lists solid: a colour only has to be told apart
# inside its family (LDBC: 7 vertex groups, 15 edge types). The index is grey.
PROPS_COLORS = ("#637939", "#b5cf6b", "#1c9099", "#fec44f", "#6baed6", "#014636", "#de77ae")
LIST_COLORS = ("#843c39", "#e7ba52", "#393b79", "#ce6dbd", "#8c6d31", "#d6616b", "#6b6ecf", "#17becf",
               "#f7b6d2", "#bcbd22", "#9edae5", "#c49c94", "#7b4173", "#fdd0a2", "#252525")
PROPS_HATCH = "///"


def part_styles(parts):
    """{part: (colour, hatch)} for a breakdown's store parts"""
    groups, etypes = schema_parts()
    if len(groups) > len(PROPS_COLORS) or len(etypes) > len(LIST_COLORS):
        raise SystemExit(f"part colours: {len(groups)} vertex groups / {len(etypes)} edge types, more than the palette holds")
    style = {"index": ("#bdbdbd", "")}
    style.update({f"{g} props": (PROPS_COLORS[i], PROPS_HATCH) for i, g in enumerate(groups)})
    style.update({f"{e} lists": (LIST_COLORS[i], "") for i, e in enumerate(etypes)})
    missing = [x for x in parts if x not in style]
    if missing:
        raise SystemExit(f"part colours: no schema part for {missing}")
    return style
READ_COLOR, WRITE_COLOR = "#4c8bc9", "#525252"      # not a layout colour (the query grid shows both)
OPS_AXIS = "total weighted ops (read + write; G:S:N:P:D = 1 : 1.1 : 0.01 : 1 : 1)"
BYTES_AXIS = "total bytes (read, misses included + written + read by writes)"
CAPTION = ("Costs are counted, not timed, and deterministic (no error bars). Ops weighted GET : SEEK : NEXT : PUT : "
           "DELETE = 1 : 1.1 : 0.01 : 1 : 1; write ops = PUTs + DELs + the reads a write does first (read-modify-write). "
           "Each query = mean over its params, times how often it runs. Per type = vertex props chosen per vertex type, "
           "adjacency per edge type (both directions share it); per type and direction = adjacency also per direction "
           "(priced, not built). Every mix is the exact answer to its question(s): fewest total ops, fewest total "
           f"bytes, fewest {SUM_LABEL}, or fewest ops within B x {B_UNIT_NAME}'s total bytes "
           "(the byte limit's unit, settings.B_UNIT).")


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def save(fig, d, name):
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(benchmarks.plots_dir(d), f"{name}.{ext}"), dpi=150, bbox_inches="tight")
    plt.close(fig)


def parse_assignment(s):
    """'knows lists=chunked; Message props=condensed' -> {part: layout}"""
    return dict(kv.split("=", 1) for kv in s.split("; ")) if s else {}


def cell_layouts(opt):
    """props_<p>_adj_<a> -> (p, a)"""
    p, a = opt[len("props_"):].split("_adj_")
    return p, a


PAIR_NOTE = "named properties-adjacency (Sp Split, Co Condensed, Ch Chunked; Sp-Co = Split properties, Condensed adjacency)"


def pair_name(opt):
    """'Co-Sp': a single layout as properties-adjacency, the short form every figure uses (python/kvcost/layout_cost/settings.py)"""
    return cells.name_of(opt)


# the reference layout says so in its label; a cell is otherwise named by its settings only (properties-adjacency)
NAMED = {cells.REF: "reference"}


def full_name(opt):
    """'Co-Sp': a single layout as properties-adjacency, spelled out (the breakdown figures)"""
    p, a = cell_layouts(opt)
    return f"{p.capitalize()}/{a.capitalize()}"


def single_name(opt):
    """'Co-Sp (reference)'"""
    return f"{pair_name(opt)} ({NAMED[opt]})" if opt in NAMED else pair_name(opt)


# ------------------------------------------------------------------------------------- the distinct mixes

def question_key(q):
    """the path from fewest ops to fewest bytes: fewest ops, then the budgets from the largest B down, then fewest
    bytes (a budget with nothing fits merges into the fewest-bytes mix, so it lands there)"""
    if q == "fewest ops":
        return (0, 0.0)
    if q == "fewest bytes":
        return (2, 0.0)
    return (1, -float(q[2:]))


def questions_label(qs, budgets):
    """['fewest bytes', 'B=0.5', 'B=1', 'B=2'] -> 'fewest bytes; B=0.5-2' (a run of consecutive budgets as a range)"""
    ends = [q for q in ("fewest ops", "fewest bytes") if q in qs]
    have = [b for b in budgets if f"B={b:g}" in qs]
    runs = []
    for b in have:
        if runs and budgets.index(b) == budgets.index(runs[-1][-1]) + 1:
            runs[-1].append(b)
        else:
            runs.append([b])
    return "; ".join(ends + [f"B={r[0]:g}" if len(r) == 1 else f"B={r[0]:g}-{r[-1]:g}" for r in runs])


def same_cost(a, b):
    return all(abs(float(a[c]) - float(b[c])) <= 1e-9 * max(1.0, abs(float(a[c]))) for c in ("total_ops", "total_bytes"))


def budgets_of(opts):
    return sorted({float(o["question"][2:]) for o in opts if o["question"].startswith("B=")})


def distinct_mixes(opts):
    """every distinct mix once, in path order, per type first: [{set, questions, option, row, assignment, label,
    end}]. Questions answered by one assignment merge into one mix. A per type and direction mix keeps only the
    questions where it beats the per type answer by more than 1% (beats_per_type_by_1pct: the rule the README table,
    the budget picks and R2 use); with none left it is dropped. A budget nothing fits is not an answer: its row is
    skipped (the budget line on the map says "(nothing fits)")."""
    budgets = budgets_of(opts)
    groups = {}                                        # (set, assignment) -> rows, in options.csv order
    for o in opts:
        if o["option_set"] not in MIX_SETS:
            continue
        if o["option_set"] == "per type and direction" and o["beats_per_type_by_1pct"] != "yes":
            continue
        if o["fits"] == "no":                          # nothing fits that budget: not an answer (its line says so)
            continue
        groups.setdefault((o["option_set"], o["assignment"]), []).append(o)
    mixes = []
    for (kind, assignment), rows in groups.items():
        rows = sorted(rows, key=lambda o: question_key(o["question"]))
        assert all(same_cost(rows[0], o) for o in rows), f"one assignment, two costs: {[o['option'] for o in rows]}"
        qs = [o["question"] for o in rows]
        mixes.append({"set": kind, "questions": qs, "option": rows[0]["option"], "row": rows[0],
                      "assignment": parse_assignment(assignment), "label": f"{SET_SHORT[kind]}: {questions_label(qs, budgets)}",
                      "end": any(q in ("fewest ops", "fewest bytes") for q in qs)})
    return sorted(mixes, key=lambda m: (m["set"] != "per type", question_key(m["questions"][0])))


def mix_marker(m):
    """(marker, edge colour, size): star = per type, diamond = per type and direction; pink = a budget pick (a mix
    that answers no end question)"""
    color = "black" if m["end"] else BUDGET_COLOR
    if m["set"] == "per type":
        return ("*", color, 300) if m["end"] else ("v", color, 130)
    return "D", color, 95


def draw_mix(ax, x, y, m, **kw):
    marker, color, s = mix_marker(m)
    ax.scatter([x], [y], marker=marker, s=s, facecolor="none", edgecolor=color, linewidth=1.8, zorder=5, **kw)


def draw_single(ax, x, y, opt, s=110, **kw):
    p, a = cell_layouts(opt)
    ax.scatter([x], [y], marker=PROPS_MARKER[p], s=s, color=LAYOUT_COLOR[a], edgecolor="black", linewidth=0.6,
               zorder=4, **kw)


def shown_options(opts, mixes):
    """the options the parts / breakdown figures show: [(option id, label)] -- the named single layouts, the best
    single in ops and in bytes, then every distinct mix once"""
    singles = [o for o in opts if o["option_set"] == "single"]
    show = [(o["option"], full_name(o["option"]) + (f" ({NAMED[o['option']]})" if o["option"] in NAMED else ""))
            for o in singles if o["option"] in KNOWN]
    for c in ("ops", "bytes"):
        b = min(singles, key=lambda o: float(o[f"total_{c}"]))["option"]
        if b not in dict(show):
            show.append((b, f"{full_name(b)} (best single in {c})"))
    return show + [(m["option"], m["label"]) for m in mixes]


SHOW_RULE = ("Options shown: four single layouts (Co-Sp, Sp-Co, Co-Co, Ch64k-Ch64k), the best "
             "single layout in total ops and in total bytes, and every distinct mix once (questions answered by the same "
             "assignment share one entry; a per type and direction mix only where it costs something else than the per "
             "type answer to the same question).")


# -------------------------------------------------------------------------------------------- options.png

def place_labels(ax, clusters, obstacles):
    """one text block per cluster of points, placed in pixel space: the first candidate spot (right, left, below,
    above, then further out) that stays inside the axes and overprints no marker and no earlier block; a block
    placed away from its points gets a thin leader line. clusters = [(x, y, text)] in data coordinates."""
    fig = ax.figure
    renderer = fig.canvas.get_renderer()
    inside = ax.get_window_extent(renderer)
    placed = list(obstacles)                           # pixel boxes (x0, y0, x1, y1)
    def overlap(b, o):
        return max(0.0, min(b[2], o[2]) - max(b[0], o[0])) * max(0.0, min(b[3], o[3]) - max(b[1], o[1]))
    spots = [(dx, dy, ha, va) for r in (1, 2, 3, 4, 6)
             for dx, dy, ha, va in ((9 * r, -3 * r, "left", "top"), (9 * r, 3 * r, "left", "bottom"),
                                    (-9 * r, -3 * r, "right", "top"), (-9 * r, 3 * r, "right", "bottom"),
                                    (0, -9 * r, "center", "top"), (0, 9 * r, "center", "bottom"))]
    for x, y, text in clusters:
        best = None
        for dx, dy, ha, va in spots:
            t = ax.annotate(text, (x, y), xytext=(dx, dy), textcoords="offset points", ha=ha, va=va,
                            fontsize=8 * OPT, linespacing=1.15, zorder=7)
            e = t.get_window_extent(renderer)
            box = (e.x0 - 2, e.y0 - 2, e.x1 + 2, e.y1 + 2)
            out = (max(0, inside.x0 - box[0]) + max(0, box[2] - inside.x1)
                   + max(0, inside.y0 - box[1]) + max(0, box[3] - inside.y1)) * (box[3] - box[1] + box[2] - box[0])
            cost = out + sum(overlap(box, o) for o in placed)
            if best is None or cost < best[0]:
                if best is not None:
                    best[1].remove()
                best = (cost, t, box, (dx, dy))
            else:
                t.remove()
            if cost == 0:
                break
        _, t, box, (dx, dy) = best
        placed.append(box)
        if abs(dx) > 9 or abs(dy) > 9:                 # away from its points: a leader line
            ax.annotate("", (x, y), xytext=(dx, dy), textcoords="offset points", zorder=6,
                        arrowprops=dict(arrowstyle="-", color="0.5", lw=0.6, shrinkA=0, shrinkB=5))


NEON = "#39ff14"                   # a layout chosen at another scale
OPT = 1.35                         # options.png: its type, against the sizes the layout was first tuned for
# how a figure names the two kinds of mix (the legend and the note spell them out once)
SET_SHORT = {"per type": "mix", "per type and direction": "mix (dir)"}
MIX_NOTE = "mix = a layout per vertex type and per edge type; mix (dir) = also per list direction"
FRONT_COLOR = "#880e4f"            # the single layouts' Pareto front: a dark pink beside the mixes' pink


def single_front(pts):
    """the points no other point beats in both ops and bytes, by ops ascending"""
    front = []
    for x, y in sorted(set(pts)):
        if not front or y < front[-1][1]:
            front.append((x, y))
    return front


def mix_line(sweep):
    """the best per type mix at each byte limit of the sweep (budget_sweep.csv), by bytes: [(ops, bytes)]"""
    return sorted({(float(r["ops"]), float(r["bytes"])) for r in sweep if r["option_set"] == "per type"}, key=lambda p: p[1])


def draw_options_map(ax, opts, mixes, sweep=()):
    """total ops vs total bytes, log-log: the singles filled, the mixes hollow, the budget lines, and (with the
    byte-limit sweep's rows) the line of the best per type mix at each byte limit; returns the legend handles and
    labels"""
    singles = [o for o in opts if o["option_set"] == "single"]
    budgets = budgets_of(opts)
    per_type = {o["question"]: o for o in opts if o["option_set"] == "per type"}
    unit = options_b_unit(o["question"] for o in opts)       # B's unit: Sp-Co, or Co-Sp in an options.csv in Co-Sp units
    ref_bytes = float(next(o for o in singles if o["option"] == unit)["total_bytes"])
    pt = lambda o: (float(o["total_ops"]), float(o["total_bytes"]))
    points = [(*pt(o), "single", o) for o in singles] + [(*pt(m["row"]), "mix", m) for m in mixes]
    ax.set_xscale("log")
    ax.set_yscale("log")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points] + [b * ref_bytes for b in budgets]
    def limits(lo, hi, below, above):                 # pad in log space: room for the label blocks
        span = max(math.log10(hi / lo), 0.3)
        return lo / 10 ** (below * span), hi * 10 ** (above * span)
    ax.set_xlim(*limits(min(xs), max(xs), 0.10, 0.45))
    ax.set_ylim(*limits(min(ys), max(ys), 0.10, 0.10))
    for x, y, kind, o in points:
        if kind == "single":
            draw_single(ax, x, y, o["option"])
        else:
            draw_mix(ax, x, y, o)
    front = single_front([(x, y) for x, y, kind, _ in points if kind == "single"])
    if len(front) > 1:
        # a staircase: between two front points no single layout exists, so the line holds the left one's bytes
        ax.step([x for x, _ in front], [y for _, y in front], where="post", color=FRONT_COLOR, lw=1.4, ls=":", zorder=2)
    line = mix_line(sweep) if sweep else []
    if len(line) > 1:
        ax.plot([x for x, _ in line], [y for _, y in line], color=BUDGET_COLOR, lw=1.2, alpha=0.8, zorder=3)
    right = blended_transform_factory(ax.transAxes, ax.transData)
    for b in budgets:
        y = b * ref_bytes
        ax.axhline(y, color="0.45", lw=0.9, ls="--", zorder=1)
        nothing = per_type[f"B={b:g}"]["fits"] == "no"
        # the closest option's miss, when nothing fits (a near miss can sit visually on the line)
        miss = min(py for _x, py, kind, _o in points if kind != "single") / y if nothing else None   # the points' y = total bytes
        tag = f" (nothing fits; closest misses by {100 * (miss - 1):.1f}%)" if nothing and miss is not None and miss < 1.05 else (" (nothing fits)" if nothing else "")
        ax.annotate(f"B={b:g}" + tag, (1, y), xycoords=right, xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8 * OPT, color="0.3", annotation_clip=False)
    for axis, h, (lo, hi) in ((ax.xaxis, number_to_human, ax.get_xlim()), (ax.yaxis, decimal_bytes, ax.get_ylim())):
        narrow = math.log10(hi / lo) < 1.5            # under ~1.5 decades 1-2-5 leaves too few labelled ticks
        axis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 3.0, 5.0, 7.0) if narrow else (1.0, 2.0, 5.0)))
        axis.set_major_formatter(FuncFormatter(lambda v, _, h=h: h(v)))
        axis.set_minor_formatter(NullFormatter())
    ax.set_xlabel(OPS_AXIS)
    ax.set_ylabel(BYTES_AXIS)
    ax.grid(alpha=0.3, which="major")
    # labels: points closer than ~a marker in pixels share one block (singles first, then the mixes in path order)
    fig = ax.figure
    fig.canvas.draw()
    to_px = ax.transData.transform
    clusters = []                                      # [[px, py, [lines], (x, y)]]
    for x, y, kind, o in points:
        px, py = to_px((x, y))
        line = single_name(o["option"]) if kind == "single" else o["label"]
        for c in clusters:
            if abs(c[0] - px) < 12 and abs(c[1] - py) < 12:
                c[2].append(line)
                break
        else:
            clusters.append([px, py, [line], (x, y)])
    obstacles = [(px - 8, py - 8, px + 8, py + 8) for px, py in (to_px((x, y)) for x, y, _, _ in points)]
    frame = ax.get_window_extent()
    obstacles += [(frame.x0, py - 2, frame.x1, py + 2)       # a label must not sit on a budget line
                  for py in (to_px((1, b * ref_bytes))[1] for b in budgets)]
    place_labels(ax, [(xy[0], xy[1], "\n".join(lines)) for _, _, lines, xy in clusters], obstacles)
    # legend: shapes (props), colours (adjacency), the mix markers, the budget line
    H, L = [], []
    for p in LAYOUTS:
        H.append(Line2D([], [], marker=PROPS_MARKER[p], color="0.75", markeredgecolor="black", ls="", ms=9))
        L.append(f"shape: props {p.capitalize()}")
    for a in LAYOUTS:
        H.append(Patch(facecolor=LAYOUT_COLOR[a], edgecolor="black", lw=0.6))
        L.append(f"colour: adjacency {a.capitalize()}")
    present = {(m["set"], m["end"]) for m in mixes}   # only the mix markers this map draws
    for key, marker, color, ms, lab in (
            (("per type", True), "*", "black", 15, "mix: fewest ops / fewest bytes"),
            (("per type", False), "v", BUDGET_COLOR, 10, "mix: fewest ops within byte limit B"),
            (("per type and direction", True), "D", "black", 9, "mix (dir): fewest ops / fewest bytes"),
            (("per type and direction", False), "D", BUDGET_COLOR, 9, "mix (dir): fewest ops within byte limit B")):
        if key not in present:
            continue
        H.append(Line2D([], [], marker=marker, color=color, mfc="none", mew=1.8, ls="", ms=ms))
        L.append(lab)
    H.append(Line2D([], [], color="0.45", lw=0.9, ls="--"))
    L.append(f"byte limit B: B x {pair_name(unit)}'s total bytes")
    if len(front) > 1:
        H.append(Line2D([], [], color=FRONT_COLOR, lw=1.4, ls=":"))
        L.append("Pareto front of the 9 single layouts (a mix below-left of it beats them all)")
    if len(line) > 1:
        H.append(Line2D([], [], color=BUDGET_COLOR, lw=1.2, alpha=0.8))
        L.append("the best mix at each byte limit")
    return H, L


def matrix_columns(opts, mixes):
    """the parts the workload touches, as the matrix's columns: props per vertex type, then lists per edge type --
    split into in-lists / out-lists when any shown mix is per type and direction"""
    touched = parse_assignment(next(o for o in opts if o["option_set"] == "single")["assignment"])
    props = sorted(p for p in touched if p.endswith(" props"))
    lists = sorted(p for p in touched if p.endswith(" lists"))
    by_dir = any(m["set"] == "per type and direction" for m in mixes)
    if by_dir:                                        # the directions the per type and direction solves priced
        seen = {p for o in opts if o["option_set"] == "per type and direction"
                for p in parse_assignment(o["assignment"]) if p.endswith("-lists")}
        lists = sorted(seen, key=lambda p: (p.rsplit(" ", 1)[0], p))
    return props, lists, by_dir


def layout_at(assign, col):
    """a part's layout in an assignment; a per type assignment gives both directions of an edge type its one layout"""
    if col in assign:
        return assign[col]
    etype, _ = col.rsplit(" ", 1)
    return assign[f"{etype} lists"]


def single_assignment(opt, cols):
    p, a = cell_layouts(opt)
    return {c: (p if c.endswith(" props") else a) for c in cols}


def closest_single(assign, cols):
    """(option id, number of differing cells): of the reference rows the matrix DISPLAYS (so a reader can check the
    outlines), the one the assignment differs from in the fewest shown cells; ties in REFERENCE_ROWS order"""
    order = list(REFERENCE_ROWS)
    diff = {s: sum(layout_at(assign, c) != v for c, v in single_assignment(s, cols).items()) for s in order}
    best = min(order, key=lambda s: (diff[s], order.index(s)))
    return best, diff[best]


def untouched_line(opts, cols, by_dir):
    """'not touched by this workload: ...' -- the schema's parts (per vertex type, per edge type, and per direction
    of a touched edge type when the matrix shows directions) that no query reads or writes; '' if none"""
    touched = parse_assignment(next(o for o in opts if o["option_set"] == "single")["assignment"])
    groups, etypes = schema_parts()
    props = [g for g in groups if f"{g} props" not in touched]
    lists = [e for e in etypes if f"{e} lists" not in touched]
    dirs = []
    if by_dir:
        dirs = [f"{e} {d}-lists" for e in etypes if f"{e} lists" in touched for d in ("in", "out")
                if f"{e} {d}-lists" not in cols]
    parts = ([", ".join(props) + " props"] if props else []) + ([", ".join(lists) + " lists"] if lists else []) + dirs
    if not parts:
        return ""
    return ("Not touched by this workload: " + "; ".join(parts) + " -- their layout doesn't change its cost; in a built "
            "store they take the default (the reference layout, Co-Sp).")


def schema_parts():
    """(vertex groups, edge types) of the suite's schema, sorted: every part a layout could be chosen for"""
    from kvevaluator.schema.vids import group_of
    if DATASET == "linkbench":
        from kvevaluator.datasets.linkbench.schema import get_schema
        schema = get_schema()
    else:
        from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA as schema
    return sorted({group_of(l) for l in schema.vertex_props}), sorted(schema.etypes)


def draw_choice_matrix(ax, opts, mixes):
    """rows = the reference single layouts, then the distinct mixes in path order; columns = the touched parts; cells
    coloured by layout, a mix's cells outlined where it differs from the closest reference row. Returns (column
    count, the untouched-parts line)."""
    props, lists, by_dir = matrix_columns(opts, mixes)
    cols = props + lists
    rows = [("single", s) for s in REFERENCE_ROWS] + [("mix", m) for m in mixes]
    for i, (kind, r) in enumerate(rows):
        assign = single_assignment(r, cols) if kind == "single" else r["assignment"]
        near = closest_single(assign, cols) if kind == "mix" else None
        near_assign = single_assignment(near[0], cols) if near else None
        for j, c in enumerate(cols):
            lay = layout_at(assign, c)
            color, _, hatch, _ = layout_style(lay)
            ax.add_patch(Rectangle((j - 0.5, i - 0.5), 1, 1, facecolor=color, edgecolor="white", lw=1.0, hatch=hatch))
            if near and lay != near_assign[c]:
                ax.add_patch(Rectangle((j - 0.4, i - 0.4), 0.8, 0.8, fill=False, edgecolor="black", lw=2.2, zorder=3))
        if kind == "single":
            draw_single(ax, -1, i, r, s=90, clip_on=False)
        else:
            draw_mix(ax, -1, i, r, clip_on=False)
    nref = len(REFERENCE_ROWS)
    ax.axhline(nref - 0.5, color="black", lw=2.5)
    npt = sum(m["set"] == "per type" for m in mixes)
    if 0 < npt < len(mixes):
        ax.axhline(nref + npt - 0.5, color="black", lw=1.2)
    if props and lists:
        ax.axvline(len(props) - 0.5, color="white", lw=5)
    ax.set_xlim(-1.6, len(cols) - 0.5)
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_yticks(range(len(rows)), [(f"{NAMED[r]} ({pair_name(r)})" if r in NAMED else pair_name(r)) if kind == "single" else r["label"]
                                     for kind, r in rows], fontsize=9 * OPT)
    ax.xaxis.tick_top()                               # the part names on top: the query grid sits right under the matrix
    ax.set_xticks(range(len(cols)), cols, rotation=55, ha="left", rotation_mode="anchor", fontsize=9 * OPT)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    return len(cols), untouched_line(opts, cols, by_dir)


def read_touch(d):
    """touch.csv's rows (workload_story.py; --touch-only makes it alone), or None with a note when it is missing"""
    path = os.path.join(d, "touch.csv")
    if not os.path.exists(path):
        print(f"  (no {path}: options.png drawn without the query grid; workload_story.py --touch-only writes it)")
        return None
    return read_csv(path)


def draw_query_grid(ax, w, cols, touch):
    """queries x the choice matrix's columns: which parts each query reads / writes, from touch.csv (a list part per
    direction). A matrix with per-type list columns folds the two directions (reads win). The index (not a layout
    choice, no matrix column) is drawn right of the last column."""
    queries = [(q, family, often) for family, q, often, _ in w["items"]]
    kind = defaultdict(dict)                          # query -> column (or "index") -> "reads" | "writes"
    for r in touch:
        part = r["part"]
        col = part if part in cols or part == "index" else (part.rsplit(" ", 1)[0] + " lists")
        if kind[r["query"]].get(col) != "reads":
            kind[r["query"]][col] = r["kind"]
    xi = len(cols) + 0.35                             # the index column
    for i, (q, family, often) in enumerate(queries):
        for c, x in [(c, j) for j, c in enumerate(cols)] + [("index", xi)]:
            k = kind[q].get(c)
            if k:
                ax.add_patch(Rectangle((x - 0.44, i - 0.38), 0.88, 0.76, clip_on=False,
                                       color=READ_COLOR if k == "reads" else WRITE_COLOR))
                ax.text(x, i, "R" if k == "reads" else "W", ha="center", va="center", color="white", fontsize=8 * OPT,
                        fontweight="bold")
    ax.text(xi, -0.55, "index", ha="center", va="bottom", fontsize=8.5 * OPT)
    ax.set_yticks(range(len(queries)), [f"{q}  ({kind_of(fam)}s, x{often:,})" for q, fam, often in queries], fontsize=9 * OPT)
    ax.set_xticks([])
    ax.set_xlim(-1.6, len(cols) - 0.5)
    ax.set_ylim(len(queries) - 0.5, -0.5)
    for s_ in ax.spines.values():
        s_.set_visible(False)
    ax.tick_params(length=0)
    for j in range(len(cols)):                        # light guides down the columns
        ax.axvline(j + 0.5, color="0.92", lw=0.6, zorder=0)


def read_sweep(d):
    """budget_sweep.csv's rows, or () where the sweep has not run for this workload"""
    path = os.path.join(d, "budget_sweep.csv")
    if not os.path.exists(path):
        return ()
    import budget_sweep
    return budget_sweep.check_same_solve(d)         # refused when the sweep is from another solve than options.csv


def plot_options(d, w, opts, sf, panel, touch=None, *, criterion):
    """options.png: the options map on top, the choice matrix below at full width, and under it (same columns) which
    parts each query reads or writes -- or one panel alone (--panel)"""
    mixes = distinct_mixes(opts)
    props, lists, by_dir = matrix_columns(opts, mixes)
    ncols, nrows = len(props) + len(lists), len(REFERENCE_ROWS) + len(mixes)
    nq = len(w["items"]) if (touch and panel in ("both", "matrix")) else 0
    # the layout in inches, top to bottom: map (axes + tick labels + legend + its caption), matrix (axes + column labels)
    cell = 0.46
    label_w = 0.075 * OPT * max([len(m["label"]) for m in mixes] + [44])      # the row labels, left of the matrix
    mat_left = label_w + 0.6
    mat_w = mat_left + cell * (ncols + 1.1) + 2.6                        # + the colour key at the right
    col_lab = 0.052 * OPT * max(len(c) for c in props + lists) + 0.2           # the rotated column labels
    left, map_h = 1.3, 5.6
    map_w = min(max(8.8, 0.55 * mat_w), 13.0) if panel != "map" else 8.8
    width = max(left + map_w + 1.9, mat_w if panel != "map" else 0)
    legend_rows = math.ceil((9 + len({(m["set"], m["end"]) for m in mixes})) / 3)   # draw_options_map's legend
    map_block = map_h + 1.15 + 0.19 * OPT * legend_rows + 0.3 if panel in ("both", "map") else 0   # + ticks, label, legend, caption
    key_rows = math.ceil(len(LAYOUTS) / 3)     # the matrix's colour key
    grid_h = (0.6 + cell * nq) if nq else 0           # the queries x parts block, right under the matrix
    mat_block = 0.5 + col_lab + cell * nrows + grid_h + 0.8 if panel in ("both", "matrix") else 0
    head_h = (0.9 if panel != "matrix" else 1.0) * OPT + 0.35   # the suptitle strip (+ the map's own title)
    height = head_h + map_block + mat_block
    fig = plt.figure(figsize=(width, height))
    rect = lambda x, top, w_, h_: (x / width, 1 - (top + h_) / height, w_ / width, h_ / height)
    top = head_h
    head = f"{display_title(w)} ({axis_label(DATASET, sf, PARAM_SET)})"
    if panel in ("both", "map"):
        ax = fig.add_axes(rect(left + (width - left - map_w - 1.9) / 2, top, map_w, map_h))
        sweep = read_sweep(d)
        H, L = draw_options_map(ax, opts, mixes, sweep)
        ax.set_title("every option's total ops against total bytes (lower-left is better)", fontsize=10 * OPT)
        leg = ax.legend(H, L, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3, frameon=False, fontsize=8.5 * OPT,
                        columnspacing=1.2)
        fig.canvas.draw()
        below = fig.transFigure.inverted().transform((0, leg.get_window_extent().y0))[1]
        fig.text(ax.get_position().x0 + ax.get_position().width / 2, below - 0.05 / height,
                 f"filled = a single layout (measured), {PAIR_NOTE}\nhollow = a mix (priced from the grid): {MIX_NOTE}", ha="center", va="top",
                 fontsize=8.5 * OPT)
        top += map_block
    if panel in ("both", "matrix"):
        mx = fig.add_axes(rect(mat_left, top + 0.5 + col_lab, cell * (ncols + 1.1), cell * nrows))
        _, untouched = draw_choice_matrix(mx, opts, mixes)
        mx.set_title("which layout each option gives each part: the single layouts for reference, then each "
                     "distinct mix\n(outlined = differs from the closest reference row above)",
                     fontsize=10 * OPT, loc="left", x=-1.6 / (ncols + 1.1), pad=col_lab * 72 + 4)
        key = [Patch(facecolor=LAYOUT_COLOR[l], edgecolor="white", label=l.capitalize()) for l in LAYOUTS]
        if nq:
            qx = fig.add_axes(rect(mat_left, top + 0.5 + col_lab + cell * nrows + 0.6, cell * (ncols + 1.1), cell * nq))
            draw_query_grid(qx, w, props + lists, touch)
            qx.set_title("which parts each query reads (R) or writes (W), in at least one single layout (same columns)",
                         fontsize=10 * OPT, loc="left", x=-1.6 / (ncols + 1.1), pad=16)
        # the colour key: right of the reference rows (the row-end text starts at the mixes, below them)
        key_leg = mx.legend(handles=key, loc="upper left", bbox_to_anchor=((ncols + 1.5) / (ncols + 1.1), 1.0), ncol=1,
                            frameon=False, fontsize=8.5 * OPT, title="cell colour = the part's layout", title_fontsize=8.5 * OPT)
        lines = [untouched] if untouched else []
        if lines:                                     # under the last block
            fig.canvas.draw()
            below = (qx if nq else mx).get_position().y0
            fig.text(mat_left / width, below - 0.1 / height,
                     textwrap.fill(lines[0], int((width - mat_left) * 72 / (8.5 * OPT * 0.55))), ha="left", va="top",
                     fontsize=8.5 * OPT)
    what = {"both": "the options, the layout each gives each part, and what each query touches", "map": "the options map",
            "matrix": "the layout each option gives each part"}[panel]
    fig.suptitle(f"{head}: {what}\n{rule_label(criterion)}", y=1 - 0.35 / height, fontsize=11 * OPT)
    save(fig, d, {"both": "options", "map": "options-map", "matrix": "options-matrix"}[panel])


def part_order(breakdown):
    """props parts, then list parts, then the index -- each alphabetically"""
    parts = {r["part"] for r in breakdown}
    return sorted(parts, key=lambda p: (p == "index", not p.endswith("props"), p))


# ------------------------------------------------------------------------------------------ breakdown.png


WEIGHT = {"G": 1.0, "S": CM.SEEK_PER_GET, "N": CM.NEXT_PER_GET, "P": 1.0, "D": 1.0}
WRITE_HATCH = "///"                                   # every write-side segment: PUT / DELETE, the writes' reads
BAND = 7                                              # query columns per band: a long workload wraps into bands


def op_segments(rows):
    """weighted KV ops by op, from breakdown rows: G / S / N of the read queries, the same three of the writes'
    own reads (hatched), PUT, DELETE (hatched)"""
    out = []
    for kind, hatch in (("read", ""), ("write", WRITE_HATCH)):
        for op in ("G", "S", "N"):
            out.append((f"{OP_LABEL[op]}" + (" (a write's own read)" if hatch else ""),
                        sum(WEIGHT[op] * float(r[op]) for r in rows if kind_of(r["family"]) == kind), OP_COLOR[op], hatch))
    for op, col in (("P", PUT_COLOR), ("D", DEL_COLOR)):
        out.append((OP_LABEL[op], sum(WEIGHT[op] * float(r[op]) for r in rows), col, WRITE_HATCH))
    return out


def byte_segments(rows):
    """bytes by lane: GET / SCAN / absence-probe keys of the read queries; the bytes the writes read first
    (hatched); the bytes written (key + value, hatched)"""
    rd = [r for r in rows if kind_of(r["family"]) != "write"]
    wr = [r for r in rows if kind_of(r["family"]) == "write"]
    f = lambda rs, cols: sum(float(r[c]) for r in rs for c in cols)
    miss = MISS_LANES
    return [("GET key + value", f(rd, ("get_key", "get_value")), BYTE_CHANNEL_COLOR["get_value_bytes"], ""),
            ("SCAN key + value", f(rd, ("scan_key", "scan_value")), BYTE_CHANNEL_COLOR["scan_value_bytes"], ""),
            ("absence-probe keys", f(rd, miss), BYTE_CHANNEL_COLOR["miss_key_bytes"], ""),
            ("read by a write first", f(wr, HIT_BYTE_LANES + miss),
             RMW_READ_COLOR, WRITE_HATCH),
            ("written: key + value", f(wr, ("write_key", "write_value")), WRITTEN_COLOR, WRITE_HATCH)]


def blend_segments(rows):
    """ops + bytes / 4,096 (the plan-pick rule, compose.py wops_bytes4k), split into its two terms: the weighted ops
    and the total bytes / 4,096 (bytes = read, misses included + read by a write first + written)"""
    ops = sum(v for _, v, _, _ in op_segments(rows))
    byt = sum(v for _, v, _, _ in byte_segments(rows))
    return [(f"weighted ops (its share of {SUM_LABEL})", ops, BLEND_OPS_COLOR, ""),
            ("total bytes / 4 KB (its share)", byt / CM.BYTES_PER_OP, BLEND_BYTES_COLOR, "")]


def plot_query_grid(d, w, opts, breakdown, parts, sf, stack, mixes, summary=False, with_carried=False, ratio_to=RATIO_BASE, *, criterion):
    """the per-query grid (as the kvstats grid_ic): rows = total weighted ops and total bytes (read + written +
    read by the writes), columns = the queries (cost of ONE run, each cell its own scale) + the workload total
    (x how often), bars = the shown options; more than BAND columns wrap into bands of two rows each.
    stack = "channels" (by op / byte lane) or "parts" (by store part)."""
    show = shown_options(opts, mixes)
    if summary:                                       # every single layout of the grid, then every distinct mix
        def grid_name(o):
            return (f"{NAMED[o]}:\n" if o in NAMED else "") + full_name(o)
        show = [(o, grid_name(o)) for o in REFERENCE_ROWS]
    big = 1.9 if summary else 1.0                     # the summary is read on a slide: larger type

    def name_of(o, lab):
        """a bar's name; in the summary a long one takes two lines, broken at the space nearest its middle"""
        lab = sum_text(lab)                        # the goal keys keep the old wording; the figure shows the shared label
        if not summary or len(lab) <= 16 or " " not in lab:
            return lab
        cut = min((i for i, ch in enumerate(lab) if ch == " "), key=lambda i: abs(i - len(lab) / 2))
        return lab[:cut] + "\n" + lab[cut + 1:].lstrip()
    extra, groups, neon, dashes = [], [], set(), []   # the summary: bar groups [(heading, first bar, one past the last)]
    if summary:
        # three groups, a solid vertical line between them: the single layouts, the per type mixes, the per type and
        # direction mixes. The group heading carries the kind of mix, so a bar's name is only its question. Inside a
        # mix group a dashed line parts the mixes that are best in ONE cost (ops, bytes, ops + bytes / 4,096:
        # the blend search) from the mixes that are the fewest ops within a byte limit.
        sets = ("per type", "per type and direction")
        bare = lambda m: m["label"][len(SET_SHORT[m["set"]]) + 2:]  # 'mix: B=3' -> 'B=3'
        best = {k: [(m["option"], bare(m)) for m in mixes if m["end"] and m["set"] == k] for k in sets}
        limit = {k: [(m["option"], "within " + bare(m)) for m in mixes if not m["end"] and m["set"] == k] for k in sets}
        if os.path.exists(os.path.join(d, "blend_breakdown.csv")):
            blend = {r["option_set"]: float(r["blend"]) for r in read_csv(os.path.join(d, "blend_optimum.csv")) if r["option_set"] != "single"}
            extra = read_csv(os.path.join(d, "blend_breakdown.csv"))
            best["per type"].append(("per_type_fewest_blend", "fewest ops + bytes / 4,096"))
            if blend["per type and direction"] < 0.99 * blend["per type"]:     # the 1% rule, as for the other direction mixes
                best["per type and direction"].append(("per_type_and_direction_fewest_blend", "fewest ops + bytes / 4,096"))
        # chosen at a smaller scale, deployed here, each beside its counterpart: its best single
        # layout is one of the 9 (that bar gets the frame and a note); its best per type mix per cost stands right
        # after this scale's own best per type mix for the same cost
        src = ""
        for path in sorted(glob.glob(os.path.join(d, "budget_carry_breakdown-from-*.csv"))) if with_carried else ():
            scale = os.path.basename(path)[len("budget_carry_breakdown-from-"):-len(".csv")]
            src = axis_label(DATASET, scale[2:], PARAM_SET) if scale.startswith("sf") else scale
            extra += read_csv(path)
            picks = [r for r in read_csv(path.replace("_breakdown", "")) if not r["from_cap_bytes"]]
            slug = lambda goal: goal.replace(" + bytes / 4,096", "_blend").replace(" ", "_")
            cells = {}                                # a single layout -> the goals it is the source scale's best for
            for r in picks:
                if r["option_set"] == "single":
                    cells.setdefault(r["assignment"], []).append(r["from_cap_kind"].replace("fewest ", ""))
            show = [(o, lab + (f"  [{src}'s best single: {'; '.join(cells[o])}]" if o in cells else "")) for o, lab in show]
            neon |= set(cells)
            own = {"fewest ops + bytes / 4,096": "per_type_fewest_blend"}            # goal -> this scale's own bar
            own.update({q: m["option"] for m in mixes if m["set"] == "per type" for q in m["questions"] if q in ("fewest ops", "fewest bytes")})
            for r in picks:
                if r["option_set"] != "per type":
                    continue
                bar = (f"{scale}_per_type_{slug(r['from_cap_kind'])}", f"{src}'s pick: {r['from_cap_kind']}")
                at = next((i for i, (o, _) in enumerate(best["per type"]) if o == own.get(r["from_cap_kind"])), len(best["per type"]) - 1)
                best["per type"].insert(at + 1, bar)
                neon.add(bar[0])
        for heading, bars, dash in (
                ("single layouts\n(the 9 of the grid)", show, None),
                ("mixed layout, per type" + (f"\n(neon frame = chosen at {src})" if src else ""),
                 best["per type"] + limit["per type"], len(best["per type"])),
                ("mixed layout,\nper type and direction", best["per type and direction"] + limit["per type and direction"],
                 len(best["per type and direction"]))):
            if bars:
                first = sum(g[2] - g[1] for g in groups)
                groups.append((heading, first, first + len(bars)))
                if dash and dash < len(bars):         # best in one cost | within a byte limit
                    dashes.append(first + dash)
        show = show + best["per type"] + limit["per type"] + best["per type and direction"] + limit["per type and direction"]
    rows_of = defaultdict(list)                       # (option, query) -> breakdown rows (one per part)
    for r in list(breakdown) + extra:
        rows_of[(r["option"], r["query"])].append(r)
    often = {q: f for _, q, f, _ in w["items"]}
    queries = [q for _, q, _, _ in w["items"]]
    columns = ["workload total"] if summary else queries + (["workload total"] if len(queries) > 1 else [])
    if stack == "channels":
        metrics = (("total weighted KV ops", number_to_human, op_segments),
                   ("total bytes", short_bytes, byte_segments),
                   (SUM_LABEL, number_to_human, blend_segments))
    else:
        style = part_styles(parts)
        def ops_by_part(rows):
            return [(p, sum(float(r["read_ops"]) + float(r["write_ops"]) + float(r["write_rmw_ops"])
                            for r in rows if r["part"] == p), *style[p]) for p in parts]
        def bytes_by_part(rows):
            return [(p, sum(float(r["read_bytes"]) + float(r["write_rmw_bytes"]) + float(r["write_bytes"])
                            for r in rows if r["part"] == p), *style[p]) for p in parts]
        metrics = (("total weighted KV ops", number_to_human, ops_by_part),
                   ("total bytes", short_bytes, bytes_by_part))
    nb = len(show)
    nband = math.ceil(len(columns) / BAND)
    ncol = min(len(columns), BAND)
    tick_h = big * 0.055 * max(len(line) for o, lab in show for line in name_of(o, lab).split("\n")) + 0.3                 # the rotated bar names under each band
    # placed in inches (tight_layout gives every row the tallest row's gap): per band a title, the ops row, a small
    # gap, the bytes row, then the rotated bar names
    cw, gap, left, head = (0.68 if summary else 0.22) * nb + 1.0, 0.6, (1.5 if summary else 1.1), (1.6 if summary else 0.9)
    heights = tuple(h * (1.25 if summary else 1) for h in (2.2, 2.4, 2.2)[:len(metrics)])
    tops = [0.6 + sum(heights[:i]) + 0.15 * i for i in range(len(heights))]
    nrow = len(metrics)
    band_h = tops[-1] + heights[-1] + tick_h + 0.1
    width, height = left + ncol * cw + (ncol - 1) * gap + 0.2, head + nband * band_h
    fig = plt.figure(figsize=(max(7.5, width), height))
    width = fig.get_figwidth()
    axes = [[None] * ncol for _ in range(nrow * nband)]
    for band in range(nband):
        for col in range(ncol):
            for row, (y, h) in enumerate(zip(tops, heights)):
                top = head + band * band_h + y
                axes[nrow * band + row][col] = fig.add_axes(((left + col * (cw + gap)) / width, 1 - (top + h) / height,
                                                          cw / width, h / height))
    labels = {}                                       # legend: segment label -> (color, hatch), in the stack's order
    for c, q in enumerate(columns + [None] * (nband * ncol - len(columns))):
        band, col = divmod(c, ncol)
        for row, (title, human, segs_of) in enumerate(metrics):
            ax = axes[nrow * band + row][col]
            if q is None:
                ax.set_axis_off()
                continue
            qs = queries if q == "workload total" else [q]
            stacks = []
            for o, _ in show:
                merged = None
                for x in qs:
                    k = often[x] if q == "workload total" else 1
                    segs = [(lab, v * k, colr, h) for lab, v, colr, h in segs_of(rows_of[(o, x)])]
                    merged = segs if merged is None else [(l, v + m[1], colr, h) for (l, v, colr, h), m in zip(segs, merged)]
                stacks.append(merged)
            tots = [sum(v for _, v, _, _ in st) for st in stacks]
            pos = [t for t in tots if t > 0]
            if not pos:
                ax.set_axis_off()
                continue
            floor = min(pos) / 4
            ax.set_yscale("log")
            ax.set_ylim(floor, max(pos) * 12)
            ref = tots[[o for o, _ in show].index(ratio_to)]
            for b, (st, tot, (o, _)) in enumerate(zip(stacks, tots, show)):
                log_share_stack(ax, b, 0.8, [(v, colr, h) for _, v, colr, h in st], floor)
                for lab, v, colr, h in st:
                    labels.setdefault(lab, [colr, h, False])[2] |= v > 0
                if tot <= 0:
                    ax.text(b, floor * 1.2, "0", ha="center", va="bottom", fontsize=7 * big)
                    continue
                if o in neon:                         # a layout chosen at another scale: a neon frame
                    ax.bar(b, tot - floor, 0.8, bottom=floor, facecolor="none", edgecolor=NEON, linewidth=3.0, zorder=5)
                lab = human(tot) if (o == ratio_to or ref <= 0) else ratio_label(tot / ref, number_to_human)
                ax.text(b, tot * 1.1, lab, ha="center", va="bottom", fontsize=7 * big, rotation=90,
                        fontweight="bold" if o == ratio_to else "normal")
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _, h=human: h(v)))
            ax.tick_params(axis="y", labelsize=8 * big)
            ax.grid(axis="y", alpha=0.3)
            ax.set_xlim(-0.6, nb - 0.4)
            ax.set_xticks(range(nb), [name_of(o, lab) for o, lab in show] if row == nrow - 1 else [], rotation=90, fontsize=7.5 * big)
            for b0 in dashes:
                ax.axvline(b0 - 0.5, color="0.4", lw=0.9, ls=(0, (4, 3)))
            for heading, b0, b1 in groups:
                if b0:
                    ax.axvline(b0 - 0.5, color="black", lw=1.0)
                if row == 0:
                    ax.text((b0 + b1 - 1) / 2, 1.02, heading, transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                            fontsize=14, fontweight="bold")
            if row == 0 and groups:
                pass                                  # the group headings stand where the column title would
            elif row == 0:
                kind = "writes" if any(kind_of(f) == "write" for f, x, _, _ in w["items"] if x == q) else "reads"
                ax.set_title(q if q == "workload total" else f"{q} ({kind})\none run; x{often[q]:,} in the workload",
                             fontsize=10, fontweight="bold")
            if col == 0:
                ax.set_ylabel(title, fontsize=10 * (1.5 if summary else 1))
    handles = [Patch(facecolor=colr, hatch=h, edgecolor="white", label=lab)       # only segments some bar has
               for lab, (colr, h, drawn) in labels.items() if drawn]
    nleg = min(len(handles), 5 if ncol > 3 else 3 if summary else 4)
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 0.0), ncol=nleg, frameon=False,
               **({"fontsize": 15} if summary else {}))
    what = "op and byte lane" if stack == "channels" else "store part"
    fig.suptitle(f"{display_title(w)} ({axis_label(DATASET, sf, PARAM_SET)}; {rule_label(criterion)})" + ("\n" if summary else ": ")
                 + "where the cost comes from, " + ("the whole workload" if summary else "query by query") + f", by {what}\n"
                 + ("bars: options" if summary else "columns: queries (each cell its own scale), bars: options"), y=1 - 0.2 / height, **({"fontsize": 16} if summary else {}))
    hatch_note = ("Hatched = write side: PUT / DELETE, and the reads a write does first (read-modify-write). "
                  if stack == "channels" else "Hatched = vertex properties, solid = edge lists. ")
    caption(fig, "Query cells: the cost of one run (mean over params); the workload total multiplies each query by "
            "how often it runs. " + hatch_note + "Log scale; each bar's height is split in proportion to each "
            "segment's share of its total. Bar label: " + (f"{full_name(ratio_to)}'s absolute value (bold), every other bar's ratio to it; a byte limit B is a multiple of "
            f"{B_UNIT_NAME}'s bytes. " if ratio_to == RATIO_BASE else
            f"{full_name(ratio_to)}'s absolute value (bold), every other bar's ratio to it. ") + "Total bytes = read (misses included) + written + read by writes. "
            + ("Third row: weighted ops + total bytes / 4 KB, the rule that picks each query's plan; the two colours are "
               "its two terms. " if len(metrics) > 2 else "")
            + ("In a mix group, left of the dashed line: the mix that is best in one cost; right of it: the fewest ops "
               f"within B x {B_UNIT_NAME}'s bytes. " if dashes else "")
            + ("Options shown: the 9 single layouts of the grid, named properties-adjacency (Sp Split, Co Condensed, Ch Chunked), then every distinct mix "
               "once (questions answered by the same assignment share one entry; a per type and direction mix only where "
               "it costs something else than the per type answer to the same question)." if summary else SHOW_RULE) + " " + CAPTION,
            y=-(0.25 + 0.32 * math.ceil(len(handles) / nleg)) / fig.get_figheight())   # below the legend, in inches
    save(fig, d, "breakdown-summary-carried" if with_carried else "breakdown-summary" if summary else "breakdown-ops-bytes" if stack == "channels" else "breakdown-parts")


def main():
    global DATASET, PARAM_SET
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True, help="the suite's axis value: LDBC's scale factor, LinkBench's scale tag")
    ap.add_argument("--dataset", default="ldbc", choices=benchmarks.DATASETS)
    ap.add_argument("--panel", default="both", choices=("both", "map", "matrix"),
                    help="both (default): every figure; map / matrix: only that panel of options.png "
                         "(options-map.png / options-matrix.png, e.g. for the paper)")
    ap.add_argument("--ratio-to", default="reference", choices=("reference", "split-condensed"),
                    help="the layout the breakdown figures' bar labels are ratios to: Co-Sp (default), or Sp-Co "
                         "(the other base). The byte limits are B x Sp-Co's bytes either way (settings.B_UNIT)")
    benchmarks.add_workload_arg(ap)
    benchmarks.add_root_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    benchmarks.add_out_arg(ap)
    a = ap.parse_args()
    benchmarks.use_plots_out(a.out)
    DATASET, PARAM_SET = a.dataset, a.param_set
    ratio_to = RATIO_BASE if a.ratio_to == "reference" else SPLIT_CONDENSED
    os.environ.setdefault("SOURCE_DATE_EPOCH", "0")  # PDFs: no creation timestamp, byte-stable re-renders
    apply_house_style(extralarge=1)
    _TASKS[:] = [(benchmarks.workload_dir(a.dataset, a.sf, name, w, a.root, criterion=a.criterion, param_set=a.param_set), w, a.sf, a.panel, ratio_to, a.criterion)
                 for name, w in benchmarks.select(a.dataset, a.sf, a.workload).items()]
    jobs = min(max(1, int(os.environ.get("LAYOUT_CHOICE_JOBS") or 1)), len(_TASKS))   # as cost_cache.jobs()
    if jobs > 1:
        with multiprocessing.get_context("fork").Pool(jobs) as pool:    # forked after the style is set: the workers share it
            done = pool.map(plot_workload, range(len(_TASKS)), chunksize=1)
    else:
        done = [plot_workload(i) for i in range(len(_TASKS))]
    for line in done:
        print(line)


_TASKS = []      # what a forked worker reads: per workload (its folder, the workload, sf, panel, ratio_to, the pick rule)


def plot_workload(i):
    """one workload's figures, into its own folder; returns the line main prints"""
    d, w, sf, panel, ratio_to, crit = _TASKS[i]
    opts = read_csv(os.path.join(d, "options.csv"))
    os.makedirs(benchmarks.plots_dir(d), exist_ok=True)
    if panel != "both":
        plot_options(d, w, opts, sf, panel, read_touch(d) if panel == "matrix" else None, criterion=crit)
        return f"wrote {benchmarks.plots_dir(d)}/options-{panel}.png"
    breakdown = read_csv(os.path.join(d, "breakdown.csv"))
    plot_options(d, w, opts, sf, panel, read_touch(d), criterion=crit)
    parts = part_order(breakdown)
    mixes = distinct_mixes(opts)
    plot_query_grid(d, w, opts, breakdown, parts, sf, "channels", mixes, ratio_to=ratio_to, criterion=crit)
    plot_query_grid(d, w, opts, breakdown, parts, sf, "channels", mixes, summary=True, ratio_to=ratio_to, criterion=crit)   # the workload total alone
    if glob.glob(os.path.join(d, "budget_carry_breakdown-from-*.csv")):                    # + the picks of a smaller scale
        plot_query_grid(d, w, opts, breakdown, parts, sf, "channels", mixes, summary=True, with_carried=True, ratio_to=ratio_to, criterion=crit)
    plot_query_grid(d, w, opts, breakdown, parts, sf, "parts", mixes, ratio_to=ratio_to, criterion=crit)
    return f"wrote {benchmarks.plots_dir(d)}/{{options,breakdown-ops-bytes,breakdown-summary,breakdown-parts}}.png"

if __name__ == "__main__":
    main()
