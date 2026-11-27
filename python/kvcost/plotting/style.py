"""The house figure style: canonical palettes, the rcParams setter, and the shared drawing primitives.

Call sites go through the accessors (``db_color`` / ``format_color`` / ``op_color`` / ``hatch`` / ``db_label`` /
``db_short`` / ``op_label``), not the raw dicts: a name outside the canon gets a deterministic fallback plus a one-time
warning, so a new format plots at once instead of raising KeyError. Keys are the repo's cross-language identifiers
("dgraph", "S", "scan_key_bytes"); the color and label layer sits on top, Python-side only.

What stays per script, on purpose: figure layout (panel grids, figsize, normalization, legends, captions), CSV
loading, and the headless pattern (``matplotlib.use("Agg")`` before pyplot; ``fig.savefig``, never ``plt.show``).
"""
from __future__ import annotations

import hashlib
import math
import re
import sys
import textwrap

import matplotlib
from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

matplotlib.rcParams["pdf.fonttype"] = 42   # TrueType, not Type 3, in every PDF (submission checkers flag Type 3)

# --------------------------------------------------------------------------------------
# Canonical palettes (the backing data; call sites use the accessors below).
# Hex literals on purpose: the value IS the spec.
# --------------------------------------------------------------------------------------

# DB-comparison scheme (DB is the color axis).
DB_COLOR = {"dgraph": "#1f77b4", "nebula": "#ff7f0e", "arangodb": "#2ca02c"}
# Hatch keeps DBs apart in B&W prints; draw hatched bars with edgecolor="white" (bar_kw).
DB_HATCH = {"dgraph": "", "nebula": "//", "arangodb": "xx"}
DB_LABEL = {"dgraph": "Dgraph", "nebula": "NebulaGraph", "arangodb": "ArangoDB"}
DB_SHORT = {"dgraph": "dg", "nebula": "neb", "arangodb": "ara"}

# Research-format extension. NB pertype_csr's purple DELIBERATELY reuses the SEEK op
# color: a figure colors EITHER by op OR by format, never both, so the two meanings never
# share a color axis. Documented reuse — do not "fix" by reassigning (that would silently
# change every existing figure).
# `all_condensed`: the configurable format's All-Condensed corner (cpp/kvevaluator/tests/fixtures/meta_corner_dgneb.json), the colour of
# Condensed adjacency in the layout-choice and mix-benefit figures. `dgneb` is the slug of the retired format that held
# that layout, kept with the same entries so the kvstats captures that name it still render.
FORMAT_COLOR = {**DB_COLOR, "pertype_csr": "#9467bd", "all_condensed": "#8c564b",
                "dgneb": "#8c564b"}
FORMAT_HATCH = {**DB_HATCH, "pertype_csr": "\\\\", "all_condensed": "++", "dgneb": "++"}
FORMAT_SHORT = {**DB_SHORT, "pertype_csr": "pt", "all_condensed": "allC", "dgneb": "dgnb"}
# Marker per format, for line and scatter plots: a SHAPE per format, so two points that overlap
# (All-Condensed on top of Dgraph) or a B&W print still tell formats apart. The three databases
# keep the classic trio (circle, square, triangle); the new formats get shapes unlike any of them
# (All-Condensed a star, pertype_csr a diamond). A chunk arm takes its format's marker (arms differ
# by line style / label, as for colour).
FORMAT_MARKER = {"dgraph": "o", "nebula": "s", "arangodb": "^",
                 "all_condensed": "*", "dgneb": "*", "pertype_csr": "D"}
# matplotlib draws shapes with unequal ink at one size (a star looks far smaller than a circle);
# multiply a marker's SIZE (markersize) by this, or its AREA (scatter s) by its square, to even them.
MARKER_SIZE_SCALE = {"*": 1.5, "D": 0.85, "P": 1.1, "X": 1.1}

# A store SLUG -- <format>[-<schema>][-<layout>][-c<N>k][-<codec>], the C++ bridge's naming rule
# (pertype_csr-c4k, dgraph-c512k, dgraph-label_only) -- takes its FORMAT's colour and hatch, and its
# CHUNK ARM adds a hatch overlay so two arms of one format are told apart beside each other and in
# B&W (dgraph and dgraph-c512k were both plain blue). The format's canonical chunk -- the store its
# bare name means -- keeps the plain hatch: pertype's canonical store is the 64K chunk (and the retired dgneb's
# default was 64K), while dgraph's bare store is unbounded, so its c64k and c512k arms both carry an
# overlay. Overlay characters are disjoint from every base hatch and from the fallback cycle.
CANONICAL_CHUNK = {"pertype_csr": "c64k", "dgneb": "c64k"}
CHUNK_HATCH = {"c4k": "||", "c16k": "|", "c64k": "-", "c512k": "--"}
_CHUNK_HATCH_OTHER = "|-"                    # a chunk tag outside the map (still an overlay)
# A SCHEMA ARM (dgraph-label_only, nebula-nocontent: the same format under a different schema)
# adds its own overlay for the same reason -- beside its canonical store it was the same bar.
SCHEMA_HATCH = "*"

# KV-op scheme (color = operation, hatch = DB), reads AND writes: GET teal, SEEK steel blue,
# NEXT light steel (the paper's colours; gs-paper's paper_style.py must match).
# PUT deliberately shares SEEK's colour and DELETE shares NEXT's (the write analogue of the
# read family); read ops and write ops never appear on one color axis (separate figures/rows).
OP_COLOR = {"S": "#4f6d9a", "G": "#2a9d8f", "N": "#b9c8de", "G_notfound": "#9fd3cc",
            "P": "#4f6d9a", "D": "#b9c8de"}
OP_LABEL = {"S": "SEEK", "G": "GET", "N": "NEXT", "P": "PUT", "D": "DELETE"}

# Diverging map for a signed statistic (a rank correlation): BLUE = positive (cost predicts),
# RED = negative (anti-predicts), white = 0; always drawn with vmin=-1, vmax=1 so cells compare
# across figures.
DIVERGING_CMAP = "RdBu"
# Sequential map for a one-sided ratio (a cost over the best, 1x and up): pale = at the best, dark = far from it;
# drawn on a log scale (LogNorm) when the ratios span decades.
SEQUENTIAL_CMAP = "YlOrRd"

# Byte-CHANNEL scheme (color = the KVCost byte lane; hatch = DB), for a bar stacked into what
# the bytes were: each lane takes its op's hue -- GET lanes the GET teal, SCAN lanes the
# NEXT light steel (a scan = SEEK + NEXTs), write lanes the PUT steel blue -- with the KEY lane
# the darker tone of the VALUE lane. Documented reuse of the op hues: a figure colors by channel
# OR by op, never both on one axis.
BYTE_CHANNEL_COLOR = {"get_key_bytes": "#1d6f65", "get_value_bytes": "#2a9d8f",
                      "scan_key_bytes": "#7f97bb", "scan_value_bytes": "#b9c8de",
                      "write_key_bytes": "#34496b", "write_value_bytes": "#4f6d9a",
                      "used_key_bytes": "#1d6f65", "used_value_bytes": "#2a9d8f",
                      "miss_key_bytes": "#c2a878"}   # absence-probe keys: read that found nothing
BYTE_CHANNEL_LABEL = {"get_key_bytes": "GET key", "get_value_bytes": "GET value",
                      "scan_key_bytes": "SCAN key", "scan_value_bytes": "SCAN value",
                      "write_key_bytes": "PUT key", "write_value_bytes": "PUT value",
                      "used_key_bytes": "used key", "used_value_bytes": "used value",
                      "miss_key_bytes": "absence-probe"}

# Step-KIND scheme (color = what a plan step's time is): kv = the store's own work (the
# read_*/write ops -- the I/O side), flow = the in-memory operators between them (join,
# filter, top_k, dedup, ...); for a write plan: write = the engine write itself (apply minus
# shadow), rmw = its read-modify-write rest. kv/rmw take the GET teal, write the PUT steel blue
# (the op hues again -- kinds and ops never share an axis); flow a neutral light gray outside
# every other palette and the fallback cycle.
STEP_KIND_COLOR = {"kv": "#2a9d8f", "flow": "#c9c9c9", "write": "#4f6d9a", "rmw": "#2a9d8f"}
STEP_KIND_LABEL = {"kv": "kv-store ops", "flow": "flow ops (in-memory)",
                   "write": "engine write", "rmw": "read-modify-write"}

# The uniform-layout CELLS (props_<p>[_c<N>k]_adj_<a>[_c<N>k]: a properties layout and an adjacency layout, each
# Split, Condensed or Chunked at a chunk size). The rules:
# - Order: adjacency outer, properties inner, chunk sizes ascending (Sp-Sp, Co-Sp, Ch64k-Sp, Sp-Co, ...); tables and CSV
#   rows too. A matrix keeps adjacency as the columns.
# - Colour = the adjacency layout: Split purple, Condensed teal, Chunked red, its sizes as shades (light = small).
# - Bars: hatch = the properties layout, drawn white: Split none, Condensed //, Chunked x, its size as the hatch's
#   density, denser = smaller chunk; the line weight goes the other way, so the six sizes stay apart at paper size.
# - Points: marker = the properties option, one shape for each of the eight.
# A Chunked part with no size is 64 KB (the nine-cell grid's chunk), and is drawn as 64 KB.
CELL_LAYOUTS = ("split", "condensed", "chunked")
CELL_SHORT = {"split": "Sp", "condensed": "Co", "chunked": "Ch"}
CHUNK_SIZES_KB = (1, 4, 16, 64, 128, 256)
DEFAULT_CHUNK_KB = 64
ADJ_COLOR = {"split": "#756bb1", "condensed": "#35978f"}
CHUNK_SHADE = {1: "#fc9272", 4: "#fb6a4a", 16: "#ef3b2c", 64: "#cb181d", 128: "#a50f15", 256: "#67000d"}
PROPS_HATCH = {"split": "", "condensed": "//"}
CHUNK_HATCH_DENSITY = {1: ("x" * 9, 0.35), 4: ("x" * 6, 0.5), 16: ("x" * 4, 0.7), 64: ("x" * 3, 0.9),
                       128: ("x" * 2, 1.3), 256: ("x", 1.9)}    # (hatch, hatch line width) per Chunked size
PROPS_MARKER = {"split": "o", "condensed": "s", 1: "^", 4: ">", 16: "v", 64: "<", 128: "D", 256: "p"}
# A figure that draws the cell scheme AND splits a bar by KV op or byte lane (the op colours above clash: SEEK's purple is
# Split's) takes these: greys, golds and browns, apart from the cell palette and the three database colours.
OP_COLOR_BESIDE_CELLS = {"G": "#4d4d4d", "S": "#bf812d", "N": "#e3cf9a", "P": "#8c510a", "D": "#bdbdbd"}
BYTE_CHANNEL_COLOR_BESIDE_CELLS = {"get_key": "#252525", "get_value": "#7a7a7a", "scan_key": "#a6761d",
                                   "scan_value": "#dfc27d", "miss_key": "#c7c7c7", "write_key": "#543005",
                                   "write_value": "#8c510a"}

# The fallback cycles for names outside the canon (deterministic by name hash).
# Deliberately DISJOINT from every canonical color and hatch above, so a fallback can
# never impersonate a canonical entry in the same figure.
_FALLBACK_COLORS = ("#17becf", "#bcbd22", "#7f7f7f",                # no #8c564b: all_condensed owns it
                    "#aec7e8", "#ffbb78", "#98df8a", "#ff9896", "#c49c94")
_FALLBACK_MARKERS = ("v", "<", ">", "h", "p", "X")                  # none of FORMAT_MARKER's shapes
_FALLBACK_HATCHES = ("oo", "OO", "//.", "xx.", "\\\\.")             # no "++": all_condensed owns it; no "*": the schema overlay


# --------------------------------------------------------------------------------------
# Accessors — graceful on unknown names (deterministic fallback + one-time warning)
# --------------------------------------------------------------------------------------

_warned: set = set()


def _stable_idx(name: str, n: int) -> int:
    return int(hashlib.md5(name.encode()).hexdigest(), 16) % n


def slug_parts(name: str, table: dict = FORMAT_COLOR):
    """(format, chunk tag) of a store slug: the longest key of `table` the slug starts with (a bare
    key is its own format), and its `-c<N>k` tag if any. A name no key prefixes is its own format
    with no chunk (so the fallback path is unchanged for unknown names)."""
    m = re.search(r"-(c\d+k)(?:-|$)", name)
    chunk = m.group(1) if m else None
    for key in sorted(table, key=len, reverse=True):
        if name == key or name.startswith(key + "-"):
            return key, chunk
    # unknown format: the name minus its chunk tag, so its arms share one fallback (+ the overlay)
    return (name.replace(f"-{chunk}", "", 1) if chunk else name), chunk


def slug_schema(name: str, table: dict = FORMAT_COLOR):
    """The schema tag of a store slug (`label_only` of dgraph-label_only), or None for the
    canonical schema. The slug grammar is <format>[-<schema>][-c<N>k][-<codec>]: the schema is
    the first token after the format unless it is the chunk tag or a codec."""
    base, _chunk = slug_parts(name, table)
    rest = name[len(base) + 1:] if name.startswith(base + "-") else ""
    tok = rest.split("-", 1)[0]
    if not tok or re.fullmatch(r"c\d+k", tok) or tok.startswith("zstd"):
        return None
    return tok


def _lookup(table: dict, name: str, kind: str, cycle: tuple):
    """`table[name]`, or the value of the format a slug `name` starts with; else a deterministic
    fallback from `cycle` (hashed on the name minus any chunk tag) with a one-time warning."""
    if name in table:
        return table[name]
    base, _chunk = slug_parts(name, table)
    if base in table:                        # a slug of a canonical format: its format's value
        return table[base]
    if (kind, name) not in _warned:
        _warned.add((kind, name))
        print(f"[style] no canonical {kind} for {name!r} -- using a stable fallback; "
              f"add one to python/kvcost/plotting/style.py to promote it", file=sys.stderr)
    return cycle[_stable_idx(base, len(cycle))]


def db_color(name: str) -> str:
    return _lookup(DB_COLOR, name, "db color",
                   _FALLBACK_COLORS)


def format_color(name: str) -> str:
    return _lookup(FORMAT_COLOR, name, "format color",
                   _FALLBACK_COLORS)


def marker_scale(marker: str) -> float:
    """Size multiplier that evens a marker's visual weight (MARKER_SIZE_SCALE; 1 by default)."""
    return MARKER_SIZE_SCALE.get(marker, 1.0)


def op_color(op: str) -> str:
    return _lookup(OP_COLOR, op, "op color",
                   _FALLBACK_COLORS)


def hatch(name: str, hatches: dict = FORMAT_HATCH) -> str:
    """The format's hatch, plus the chunk overlay when `name` is a non-canonical chunk arm and the
    schema overlay when it is a schema arm."""
    h = _lookup(hatches, name, "hatch", _FALLBACK_HATCHES)
    base, chunk = slug_parts(name, hatches)
    if chunk is not None and chunk != CANONICAL_CHUNK.get(base):
        h += CHUNK_HATCH.get(chunk, _CHUNK_HATCH_OTHER)
    if slug_schema(name, hatches) is not None:
        h += SCHEMA_HATCH
    return h


def db_label(name: str) -> str:
    return DB_LABEL.get(name, name)          # unknown -> the identifier itself, no warning


def db_short(name: str) -> str:
    return DB_SHORT.get(name, name)


def format_short(name: str) -> str:
    return FORMAT_SHORT.get(name, name)


def op_label(op: str) -> str:
    return OP_LABEL.get(op, op)


def byte_channel_color(channel: str) -> str:
    return _lookup(BYTE_CHANNEL_COLOR, channel, "byte-channel color",
                   _FALLBACK_COLORS)


def byte_channel_label(channel: str) -> str:
    return BYTE_CHANNEL_LABEL.get(channel, channel)


def step_kind_color(kind: str) -> str:
    return _lookup(STEP_KIND_COLOR, kind, "step-kind color",
                   _FALLBACK_COLORS)


def step_kind_label(kind: str) -> str:
    return STEP_KIND_LABEL.get(kind, kind)


# --------------------------------------------------------------------------------------
# rcParams — the one setter
# --------------------------------------------------------------------------------------

def apply_house_style(extralarge: int = 3) -> None:
    """The rcParams block. ``extralarge`` is the only density knob (1 for dense per-query grids, 3 default, 8 for sparse
    paired-bar figures); the base sizes are canon and must not vary per script. Never pass ``fontsize=`` in plot calls (it
    overrides this and the knob stops working); a deliberately tiny label ties itself to the knob, e.g.
    ``fontsize=6.5 + extralarge``."""
    matplotlib.rcParams.update({
        "font.size": 13 + extralarge, "axes.titlesize": 14 + extralarge,
        "axes.labelsize": 13 + extralarge, "xtick.labelsize": 12 + extralarge,
        "ytick.labelsize": 11 + extralarge, "legend.fontsize": 11 + extralarge,
    })


# --------------------------------------------------------------------------------------
# Small geometry / naming helpers
# --------------------------------------------------------------------------------------

def bar_kw(name: str, hatches: dict = FORMAT_HATCH) -> dict:
    """Bar style for a DB/format: hatched + white edge when the map hatches it, plain
    otherwise (a name outside the map gets the deterministic fallback hatch)."""
    h = hatch(name, hatches)
    return {"hatch": h, "edgecolor": "white"} if h else {}


def _cell_options(cell: str):
    """((props layout, KB), (adjacency layout, KB)) of a cell id; a Chunked part's KB defaults to 64, others None"""
    from kvcost.plotting.cell_ids import cell_parts
    p, pk, a, ak = cell_parts(cell)
    return ((p, (pk or DEFAULT_CHUNK_KB) if p == "chunked" else None),
            (a, (ak or DEFAULT_CHUNK_KB) if a == "chunked" else None))


def _option_rank(option) -> tuple:
    layout, kb = option
    return (CELL_LAYOUTS.index(layout), kb or 0)


def cell_order(cells) -> list:
    """The cells sorted adjacency outer, properties inner, chunk sizes ascending. ValueError on a name not a cell."""
    def key(cell):
        props, adj = _cell_options(cell)
        return (_option_rank(adj), _option_rank(props))
    return sorted(cells, key=key)


def cell_short(cell: str) -> str:
    """'Ch4k-Sp' of props_chunked_c4k_adj_split: properties-adjacency, a Chunked part with its size (64 KB when unsized)."""
    return "-".join(CELL_SHORT[layout] + (f"{kb}k" if kb else "") for layout, kb in _cell_options(cell))


def cell_style(cell: str) -> dict:
    """A cell's encoding: colour (the adjacency), hatch and hatch_linewidth (the properties, for bars), marker (the
    properties option, for points)."""
    (p, pk), (a, ak) = _cell_options(cell)
    hatch_, hatch_lw = CHUNK_HATCH_DENSITY[pk] if p == "chunked" else (PROPS_HATCH[p], 0.9)
    return {"color": CHUNK_SHADE[ak] if a == "chunked" else ADJ_COLOR[a], "hatch": hatch_,
            "hatch_linewidth": hatch_lw, "marker": PROPS_MARKER[pk if p == "chunked" else p]}


def cell_bar_kw(cell: str) -> dict:
    """ax.bar keywords for a cell: its colour, its properties hatch drawn white, no bar outline."""
    s = cell_style(cell)
    return {"color": s["color"], "hatch": s["hatch"], "hatch_linewidth": s["hatch_linewidth"], "edgecolor": "white",
            "linewidth": 0}


def cell_scatter_kw(cell: str) -> dict:
    """ax.scatter keywords for a cell: its colour and its properties marker, a thin black outline."""
    s = cell_style(cell)
    return {"color": s["color"], "marker": s["marker"], "edgecolors": "black", "linewidths": 0.5}


def bar_offsets(n: int, bar_w: float) -> list:
    """Centered x-offsets for n side-by-side bars: n=1->[0], n=2->[-w/2,+w/2], n=3->[-w,0,+w]."""
    start = -(n - 1) / 2.0
    return [(start + i) * bar_w for i in range(n)]


# --------------------------------------------------------------------------------------
# Drawing primitives
# --------------------------------------------------------------------------------------

def stack_on_log(ax, x, width, segments, floor, barkw=None) -> float:
    """One stacked bar on a log axis whose every segment edge sits at its TRUE cumulative value: the top
    at the total, each boundary at the running sum, readable against the axis. Only the first
    segment is lifted to ``floor`` (a log axis can't start a bar at 0; bottom=floor for every
    segment would push the top to floor + sum, past its label). On a log axis a segment's drawn
    length is not its share: lower segments look larger. Use it when the boundaries' values are
    read; use log_share_stack when the shares are. ``segments`` = [(value, color), ...] bottom-up;
    returns the true stack top (label it with that)."""
    barkw = barkw or {}
    cum = 0.0
    for val, color in segments:
        val = 0.0 if val is None or val != val else float(val)      # NaN -> 0
        cumtop = cum + val
        base = max(cum, floor)
        height = max(cumtop - base, 0.0)
        if val > 0:
            ax.bar(x, height, width, bottom=base, color=color, **barkw)
        cum = cumtop
    return cum


def ratio_label(r, human) -> str:
    """The canon's ratio-to-ref label: ``0x`` / ``1/42Kx`` / ``2.3x`` / ``900Kx``."""
    if r == 0:
        return "0x"
    if r < 0.01:
        return f"1/{human(1 / r)}x"                  # e.g. 1/42Kx: 42,000 times less
    if r < 100:
        return f"{r:.2g}x"
    return f"{human(r)}x"


def stacked_norm_bar(ax, xs, names, cells, segs_of, extralarge, unit="", ref=None,
                     bold=None, hatches: dict = FORMAT_HATCH, human=None, bar_w=0.8,
                     absolute=False, log_floor=None):
    """One stacked bar per name, every bar divided by the REF cell's total (ref = 1; linear axis, so no log floor). The
    label on the ref bar is its absolute total; on the others the ratio to the ref (``0x`` / ``1/42Kx`` / ``2.3x`` /
    ``900Kx``); ``bold`` names the emphasized bar (default: the ref). ``cells``: one data cell per name (None = missing,
    skipped); ``segs_of(cell)`` returns [(value, color), ...]. Draws the ref line at 1.
    ``absolute=True``: the bars keep their real values (the axis reads in the unit) and every bar is labelled with its
    ratio to the ref (the ref itself ``1x``); no ref line.
    ``log_floor`` (absolute mode): the axis is log; the stack is drawn with stack_on_log's rule."""
    if human is None:
        # NB not byte-identical to the closures' _human at >=1e12 or for negatives (the lib
        # adds T/P buckets and keeps the sign) -- the lib is the more correct one; no plot
        # label in the extracted figures reaches either regime.
        from kvcost.plotting.formatting import number_to_human as human
    ref = names[0] if ref is None else ref
    bold = ref if bold is None else bold
    if ref not in names:
        raise ValueError(f"stacked_norm_bar: ref {ref!r} is not one of names {list(names)}")
    ref_cell = cells[list(names).index(ref)]
    ref_total = sum(v for v, _ in segs_of(ref_cell)) if ref_cell else None
    if not ref_total:
        ref_total = None
    for x, name, c in zip(xs, names, cells):
        if c is None:
            continue
        segs = segs_of(c)
        total = sum(v for v, _ in segs)
        kw = dict(hatch=hatch(name, hatches), edgecolor="white", linewidth=0.5)
        if absolute and log_floor:
            cum = stack_on_log(ax, x, bar_w, [(v, color) for v, color in segs], log_floor, kw)
        else:
            cum = 0.0
            for v, color in segs:
                y = v if absolute else (v / ref_total if ref_total else v)
                ax.bar(x, y, bar_w, bottom=cum, color=color, **kw)
                cum += y
        if absolute and ref_total:
            label = "1x" if name == ref else ratio_label(total / ref_total, human)
        elif name == ref or ref_total is None:
            label = human(total) + unit
        else:
            label = ratio_label(total / ref_total, human)
        ax.text(x, max(cum, log_floor or 0), label, ha="center", va="bottom", fontsize=6.5 + extralarge,
                fontweight="bold" if name == bold else "normal")
    if ref_total and not absolute:
        ax.axhline(1.0, color="black", linestyle="--", linewidth=0.7)


def pareto_front(points, lower_is_better=(True, True)):
    """The non-dominated subset of `points` -- [(x, y, payload), ...] -- for a two-currency
    trade-off (ops vs bytes: nobody is best in both, honest-plotting pitfall E). A point is on
    the front when no other point is at least as good on both axes and strictly better on one.
    Returned in x order, so it draws as a staircase. `lower_is_better` per axis."""
    def better(a, b, lower):
        return a < b if lower else a > b
    def at_least(a, b, lower):
        return a <= b if lower else a >= b
    front = []
    for p in points:
        dominated = any(q is not p and at_least(q[0], p[0], lower_is_better[0]) and at_least(q[1], p[1], lower_is_better[1])
                        and (better(q[0], p[0], lower_is_better[0]) or better(q[1], p[1], lower_is_better[1]))
                        for q in points)
        if not dominated:
            front.append(p)
    return sorted(front, key=lambda p: (p[0], p[1]))


def tradeoff_scatter(ax, points, color_of, label_of, marker="o", hatch_of=None, ms=90, front_kw=None,
                     fontsize=8, marker_of=None, fill_front=True):
    """One point per arm on a two-currency plane with the Pareto front drawn as a staircase:
    `points` = [(x, y, name)]; `color_of(name)`, `label_of(name)`. Points off the front are
    hollow. Returns the front (names). Set the axis scales BEFORE calling: the labels are placed
    in pixel space (a label that would overprint an earlier one -- the c4k and c64k chunk arms of
    one format often land together -- slides down until it is clear). `marker_of(name)` gives each
    arm its own marker, so two points that overlap stay distinguishable; hollow (dominated) points
    are transparent and drawn above the filled ones, and a filled point's fill is half-transparent
    under a solid outline, so an overlap never hides either point. fill_front=False draws EVERY
    point hollow (two near-coincident points then both show their outline) and marks the front by
    the dashed staircase plus a bold label instead of a fill."""
    from matplotlib.colors import to_rgba
    front = pareto_front(points)
    on = {p[2] for p in front}
    for x, y, name in points:
        filled = name in on and fill_front
        m = marker_of(name) if marker_of else marker
        ax.scatter([x], [y], s=ms * marker_scale(m) ** 2, marker=m,
                   facecolor=to_rgba(color_of(name), 0.5) if filled else "none",
                   edgecolor=color_of(name), linewidth=1.6, zorder=3 if filled else 4)
    ax.autoscale_view()
    px_per_pt = ax.figure.dpi / 72
    row = 1.25 * fontsize * px_per_pt                # a label's height in pixels
    boxes = []                                       # placed labels, (x0, y0, x1, y1) in pixels
    for x, y, name in sorted(points, key=lambda p: -p[1]):
        text = label_of(name)
        cx, cy = ax.transData.transform((x, y))
        w = 0.62 * fontsize * px_per_pt * len(text) + 4 * px_per_pt     # a DejaVu char is ~0.6 em; a little air
        dx, dy = 6 * px_per_pt, 4 * px_per_pt
        for _try in range(12):
            box = (cx + dx, cy + dy, cx + dx + w, cy + dy + row)
            if not any(b[0] < box[2] and box[0] < b[2] and b[1] < box[3] and box[1] < b[3] for b in boxes):
                break
            dy -= row
        boxes.append(box)
        ax.annotate(text, (x, y), textcoords="offset points", xytext=(dx / px_per_pt, dy / px_per_pt),
                    fontsize=fontsize,
                    fontweight="bold" if (name in on and not fill_front) else "normal")
    if len(front) > 1:
        ax.plot([p[0] for p in front], [p[1] for p in front], drawstyle="steps-post", color="0.35",
                lw=1.2, ls="--", zorder=2, **(front_kw or {}))
    return [p[2] for p in front]


def box_strip(ax, position, values, color, width=0.5, whis=(5, 95), show_mean=True,
              jitter=0.36, alpha=0.35, ms=4.5) -> None:
    """One box (25-75% quartiles, colored to match; solid black median; dotted mean line
    when show_mean) with the per-item points jittered on top. Jitter is DETERMINISTIC
    (index-based, no RNG) so reruns are pixel-stable. Writing the mean VALUE as text next
    to the box stays with the caller (placement/color are per-figure)."""
    props = dict(medianprops={"color": "black", "lw": 2},
                 boxprops={"color": color}, whiskerprops={"color": color},
                 capprops={"color": color})
    if show_mean:
        props.update(showmeans=True, meanline=True,
                     meanprops={"color": "black", "ls": (0, (1, 1)), "lw": 1.4})
    ax.boxplot([values], positions=[position], widths=width, whis=whis,
               showfliers=False, **props)
    for i, v in enumerate(values):
        jx = position + (i / max(1, len(values) - 1) - 0.5) * jitter
        ax.plot(jx, v, "o", ms=ms, color=color, alpha=alpha, zorder=3)


# ---- small figure helpers: every study may draw with them

def caption(fig, text, y=-0.01, fontsize=8.5):
    """a caption under the axes, wrapped to the figure's width by hand (text wrap=True is unreliable under
    bbox_inches='tight'). The defaults are the layout-pricing figures' (layout_choice, mix_benefit, linkbench)"""
    width = int(fig.get_figwidth() * 72 / (fontsize * 0.55))     # ~0.55 em per character
    fig.text(0.5, y, textwrap.fill(text, width), ha="center", va="top", fontsize=fontsize)


def log_axis(axis, human, lo, hi):
    """a log axis's ticks: majors at 1, 2, 5 per decade (1, 2, 3, 5, 7 when lo..hi spans under 1.5 decades), labelled by
    `human`; no minor labels"""
    narrow = math.log10(hi / lo) < 1.5
    axis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 3.0, 5.0, 7.0) if narrow else (1.0, 2.0, 5.0)))
    axis.set_major_formatter(FuncFormatter(lambda v, _: human(v)))
    axis.set_minor_formatter(NullFormatter())


def log_share_stack(ax, x, width, segs, floor):
    """one bar from `floor` to its total on a log axis, its drawn length split among the segments in proportion
    to their SHARE of the total: the top is at the true total, the inner boundaries are not values on the axis.
    Use it when the shares are read (with stack_on_log's true boundaries, the segment holding 79% of a bar can
    look like a thin top band); use stack_on_log when the boundaries' values are. segs = [(value, color, hatch)]"""
    total = sum(v for v, *_ in segs)
    if total <= floor:
        return
    cum = 0.0
    for v, color, hatch in segs:
        if v <= 0:
            continue
        lo = floor * (total / floor) ** (cum / total)
        cum += v
        hi = floor * (total / floor) ** (cum / total)
        ax.bar(x, hi - lo, width, bottom=lo, color=color, hatch=hatch, edgecolor="white", linewidth=0.4)
