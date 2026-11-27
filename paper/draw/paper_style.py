# released from gs-paper at 4b5c722 (evidence/paper_style.py); edit it there, then run evidence/release_draw.py
"""The style the paper's draw scripts share: the house rcParams block, and the cell encoding (a uniform layout's
colour, hatch and marker). The studies' own figures in graph-storage-artifact use kvcost.plotting.style, which holds
the same values; the artifact's tests/gates/check_paper_style.py checks they stay equal.

The cell encoding: colour = the adjacency layout (Split purple, Condensed teal, Chunked red, its sizes as shades);
bars: hatch = the properties layout, drawn white (Split none, Condensed //, Chunked x, denser = smaller chunk);
points: marker = the properties option. A Chunked part with no size is 64 KB.

The database grid (db_reads_grid, complex-kv-reads): one bar per database, hatched by database; or stacked by KV operation.

Print size (print_style): a figure is drawn at about the width it prints at, and its text is sized to print at PRINT_PT
once LaTeX scales it to the column (or the page, for figure*). A figure drawn three times too wide prints at a third
of its font size.
"""
import math
import re

import matplotlib
import matplotlib.pyplot as plt

matplotlib.rcParams["pdf.fonttype"] = 42   # TrueType, not Type 3, in every PDF (submission checkers flag Type 3)

CELL_SHORT = {"split": "Sp", "condensed": "Co", "chunked": "Ch"}
DEFAULT_CHUNK_KB = 64
ADJ_COLOR = {"split": "#756bb1", "condensed": "#35978f"}
CHUNK_SHADE = {1: "#fc9272", 4: "#fb6a4a", 16: "#ef3b2c", 64: "#cb181d", 128: "#a50f15", 256: "#67000d"}
PROPS_HATCH = {"split": "", "condensed": "//"}
CHUNK_HATCH_DENSITY = {1: ("x" * 9, 0.35), 4: ("x" * 6, 0.5), 16: ("x" * 4, 0.7), 64: ("x" * 3, 0.9),
                       128: ("x" * 2, 1.3), 256: ("x", 1.9)}    # (hatch, hatch line width) per Chunked size
PROPS_MARKER = {"split": "o", "condensed": "s", 1: "^", 4: ">", 16: "v", 64: "<", 128: "D", 256: "p"}

# props_<layout>[_c<N>k]_adj_<layout>[_c<N>k]
_CELL = re.compile(r"props_(split|condensed|chunked)(?:_c(\d+)k)?_adj_(split|condensed|chunked)(?:_c(\d+)k)?")


def apply_house_style(extralarge: int = 3) -> None:
    """The house rcParams block; ``extralarge`` is the one density knob (1 dense, 3 default, 8 sparse)."""
    matplotlib.rcParams.update({
        "font.size": 13 + extralarge, "axes.titlesize": 14 + extralarge,
        "axes.labelsize": 13 + extralarge, "xtick.labelsize": 12 + extralarge,
        "ytick.labelsize": 11 + extralarge, "legend.fontsize": 11 + extralarge,
    })


# The paper's widths (acmart sigconf): \columnwidth 241.14 pt, \textwidth 506.295 pt, in inches (72.27 pt each)
COLUMN_IN = 241.14 / 72.27
TEXT_IN = 506.295 / 72.27
PRINT_PT = 7.0                                    # the printed size of a figure's text; the body is 9 pt
SMALL = 0.9                                       # tick labels, legend and bar values: 0.9 of PRINT_PT


def print_style(drawn_in: float, print_in: float = COLUMN_IN, print_pt: float = PRINT_PT) -> float:
    """Set the rcParams text sizes so a figure drawn drawn_in wide prints at print_pt when scaled to print_in.
    Returns the drawn size of print_pt (for a text's own fontsize=, times SMALL for the small ones)."""
    size = print_pt * drawn_in / print_in
    matplotlib.rcParams.update({
        "font.size": size, "axes.titlesize": size, "axes.labelsize": size,
        "xtick.labelsize": size * SMALL, "ytick.labelsize": size * SMALL, "legend.fontsize": size * SMALL,
    })
    return size


def cell_parts(cell: str):
    """((props layout, KB), (adjacency layout, KB)) of a cell id; a Chunked part's KB defaults to 64, others None"""
    m = _CELL.fullmatch(cell)
    if not m:
        raise ValueError(f"not a cell: {cell}")
    p, pk, a, ak = m.group(1), m.group(2), m.group(3), m.group(4)
    for layout, k in ((p, pk), (a, ak)):
        if k is not None and layout != "chunked":
            raise ValueError(f"not a cell: {cell} ({layout} takes no chunk size)")
    parts = ((p, int(pk or DEFAULT_CHUNK_KB) if p == "chunked" else None),
             (a, int(ak or DEFAULT_CHUNK_KB) if a == "chunked" else None))
    # one id per layout: an id cell_id would write otherwise (an old shape, a 64 KB size written out) is refused
    settings = [layout if kb in (None, DEFAULT_CHUNK_KB) else f"chunked_c{kb}k" for layout, kb in parts]
    if cell_id(*settings) != cell:
        raise ValueError(f"not a cell: {cell} (its id is {cell_id(*settings)})")
    return parts


def cell_id(props_setting: str, adj_setting: str) -> str:
    """a uniform layout's id from its two settings (split, condensed, chunked = 64 KB, chunked_c<K>k), the artifact's
    rule (kvevaluator.layouts.cell_id): when both sides are Chunked and either is not 64 KB, both carry their size"""
    def parts(setting):
        m = re.fullmatch(r"(split|condensed|chunked)(?:_c(\d+)k)?", setting)
        if not m or (m.group(2) and m.group(1) != "chunked"):
            raise ValueError(f"not a setting: {setting}")
        kb = int(m.group(2)) if m.group(2) else None
        return m.group(1), None if kb == DEFAULT_CHUNK_KB else kb
    (p, pk), (a, ak) = parts(props_setting), parts(adj_setting)
    if p == a == "chunked" and (pk or ak):
        return f"props_chunked_c{pk or DEFAULT_CHUNK_KB}k_adj_chunked_c{ak or DEFAULT_CHUNK_KB}k"
    side = lambda layout, kb: f"chunked_c{kb}k" if kb else layout   # noqa: E731
    return f"props_{side(p, pk)}_adj_{side(a, ak)}"


def side_name(layout: str, kb=None) -> str:
    """a side's shown name, the artifact's rule (kvevaluator.layouts.side_name): Sp, Co, and a Chunked side always
    with its chunk size (Ch4k; 64 KB, which a cell id leaves unwritten, is Ch64k)"""
    return CELL_SHORT[layout] + (f"{kb or DEFAULT_CHUNK_KB}k" if layout == "chunked" else "")


def cell_name(cell: str) -> str:
    """a uniform layout's shown name, properties-adjacency: 'Co-Ch64k', 'Ch1k-Ch16k'"""
    return "-".join(side_name(layout, kb) for layout, kb in cell_parts(cell))


def cell_settings(cell: str):
    """(props setting, adjacency setting) of a cell id, the inverse of cell_id: a 64 KB chunk is `chunked`"""
    name = lambda layout, kb: layout if kb in (None, DEFAULT_CHUNK_KB) else f"chunked_c{kb}k"   # noqa: E731
    return tuple(name(layout, kb) for layout, kb in cell_parts(cell))


def cell_style(cell: str) -> dict:
    """A cell's encoding: colour (the adjacency), hatch and hatch_linewidth (the properties, for bars), marker (the
    properties option, for points)."""
    (p, pk), (a, ak) = cell_parts(cell)
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


# --- the engines' layouts marked among the cells -------------------------------------------------------------------
# Dgraph's layout is Sp-Co, NebulaGraph's Co-Sp (ArangoDB's too). A figure of the cells marks them with the engine's
# short name (DB_SHORT) and its colour: blue and gold, which colour-blind readers tell apart and no cell uses.
ENGINE_OF_CELL = {"props_split_adj_condensed": "dgraph", "props_condensed_adj_split": "nebula"}
ENGINE_COLOR = {"dgraph": "#f2a900", "nebula": "#0098f8"}
ENGINE_NAME_PT = 5.6                                   # the bold short name
ENGINE_NAME_RADIUS = 3.0                               # points around the name where its leader line starts


ENGINE_TEXT_COLOR = {"dgraph": "#c48a00", "nebula": "#0077c8"}   # a deeper shade of each, readable as text on white; figures/tikz-style.tex: nebcol is this blue, dgcol a gold between the two (#d99f00)


def engine_name_kw(engine: str) -> dict:
    """text keywords for an engine's short name: the figures' regular text, in the engine's text colour"""
    return dict(s=DB_SHORT[engine], fontsize=ENGINE_NAME_PT, color=ENGINE_TEXT_COLOR[engine], ha="center",
                va="center")


def mark_engine_point(ax, x, y, cell: str, dx: float, dy: float, leader: bool = False) -> None:
    """the engine's short name dx, dy points from an engine cell's point (x, y), with a leader line in the engine's
    colour when asked; nothing for another cell"""
    if cell not in ENGINE_OF_CELL:
        return
    engine = ENGINE_OF_CELL[cell]
    arrow = dict(arrowstyle="-", lw=0.5, color=ENGINE_COLOR[engine], patchA=None, shrinkA=ENGINE_NAME_RADIUS,
                 shrinkB=2.5) if leader else None
    kw = engine_name_kw(engine)
    ax.annotate(kw.pop("s"), (x, y), xytext=(dx, dy), textcoords="offset points", zorder=9, annotation_clip=False,
                arrowprops=arrow, **kw)


def mark_engine_ticks(fig, ax, cells) -> None:
    """the engine's short name past the end of an engine cell's rotated x tick label; call after the ticks are set"""
    renderer = fig.canvas.get_renderer()
    for c, tick in zip(cells, ax.get_xticklabels()):
        if c in ENGINE_OF_CELL:
            box = tick.get_window_extent(renderer).transformed(fig.transFigure.inverted())
            kw = engine_name_kw(ENGINE_OF_CELL[c])
            fig.text((box.x0 + box.x1) / 2, box.y0 - 0.006, kw.pop("s"), **{**kw, "va": "top", "rotation": 90})


def outline_engine_bar(ax, x, height, cell: str, width: float, bottom: float = 0.0) -> None:
    """an outline in the engine's colour around an engine cell's whole bar; nothing for another cell"""
    if cell in ENGINE_OF_CELL:
        ax.bar(x, height, bottom=bottom, width=width, fill=False, lw=0.9,
               edgecolor=ENGINE_COLOR[ENGINE_OF_CELL[cell]], zorder=4.1)


def engine_legend():
    """(handles, labels) with no symbol: "DG = Dgraph", "NG = NebulaGraph"; colour_engine_legend colours them"""
    from matplotlib.lines import Line2D
    engines = ("dgraph", "nebula")
    return ([Line2D([], [], ls="", marker="") for _ in engines],
            [f"{DB_SHORT[e]} = {DB_LABEL[e]}" for e in engines])


def colour_engine_legend(legend) -> None:
    """the engine entries of a legend (engine_legend's labels, among any others) in the engines' text colours"""
    for text in legend.get_texts():
        for engine in ("dgraph", "nebula"):
            if text.get_text() == f"{DB_SHORT[engine]} = {DB_LABEL[engine]}":
                text.set_color(ENGINE_TEXT_COLOR[engine])


def tint(color, f: float):
    """color mixed with white; f = the white's share"""
    from matplotlib.colors import to_rgb
    return tuple(c + (1 - c) * f for c in to_rgb(color))


def two_tones(cell: str):
    """(base, light) for a two-part bar: the cell's adjacency colour and a lighter tint of it (stronger for a light
    shade, Ch1k's)"""
    from matplotlib.colors import to_rgb
    base = cell_style(cell)["color"]
    return base, tint(base, 0.75 if sum(to_rgb(base)) / 3 > 0.55 else 0.62)


def swatch(colors):
    """a legend handle: the colours side by side (with handler_map={tuple: HandlerTuple(ndivide=None, pad=0)})"""
    from matplotlib.patches import Patch
    return tuple(Patch(fc=c) for c in colors)


# --- the database grid ---------------------------------------------------------------------------------------------
# the live databases, in the paper's order (DB_LABEL's order is the order a figure draws them in)
# (engine configurations of the complex-kv-reads figure: dgraph-no-index, Dgraph with its equality filters not through
# their index, drawn as Dgraph's bar; tugraph-detached, TuGraph with detach_property on)
DB_HATCH = {"dgraph": "", "dgraph-no-index": "", "nebula": "//", "arangodb": "xx", "tugraph": "..",
            "tugraph-detached": "oo"}
DB_LABEL = {"dgraph": "Dgraph", "dgraph-no-index": "Dgraph (no index)", "nebula": "NebulaGraph", "arangodb": "ArangoDB",
            "tugraph": "TuGraph", "tugraph-detached": "TuGraph (detached)"}
DB_SHORT = {"dgraph": "DG", "dgraph-no-index": "DG", "nebula": "NG", "arangodb": "AR", "tugraph": "TG",
            "tugraph-detached": "TG-d"}   # narrow panels
# the KV operations: a GET teal, a scan's SEEK steel blue and its NEXTs light steel (Figure 2's colours);
# a write's PUT and DELETE take SEEK's and NEXT's. GET's teal is near Condensed's (ADJ_COLOR): a figure that draws
# both the layouts and the operations needs other colours for one of them.
OP_COLOR = {"S": "#4f6d9a", "G": "#2a9d8f", "N": "#b9c8de", "G_notfound": "#9fd3cc",
            "P": "#4f6d9a", "D": "#b9c8de"}
OP_LABEL = {"S": "SEEK", "G": "GET", "N": "NEXT", "P": "PUT", "D": "DELETE"}


def db_label(name: str) -> str:
    return DB_LABEL.get(name, name)


def op_color(op: str) -> str:
    return OP_COLOR[op]


def op_label(op: str) -> str:
    return OP_LABEL.get(op, op)


def db_bar_kw(db: str) -> dict:
    """a database's bar: hatched with a white edge when it has a hatch (its colour comes from the colour cycle)"""
    h = DB_HATCH[db]
    return {"hatch": h, "edgecolor": "white"} if h else {}


def bytes_to_human(num_bytes) -> str:
    """bytes, binary units (e.g. 1.5 MB)"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} PB"


def number_to_human(num) -> str:
    """a count (e.g. 1.5M)"""
    value = float(num)
    if abs(value) < 1000:
        return f"{value:.0f}"
    for suffix in ("", "K", "M", "B", "T"):
        if abs(value) < 1000:
            return f"{value:.1f}{suffix}"
        value /= 1000.0
    return f"{value:.1f}P"


OPS = ("G", "S", "N")                              # stack order, bottom up
BYTES = (("G", "GET bytes"), ("N", "scan bytes"))


def db_reads_grid(data, titles, dbs, by_op=False, normalize=False):
    """A 2 x len(titles) grid: top row KV operations, bottom row bytes read, one bar per database.

    data[db][query] = ({"G", "S", "N": count}, {"G", "N": bytes}); titles = {query: panel title}, in panel order.
    by_op stacks each bar by operation (bytes: GET, scan) instead of colouring it by database. normalize draws every
    panel as a share of its largest bar (= 100%), one shared axis per row; the bars keep their absolute labels.
    Returns the figure."""
    apply_house_style(extralarge=1)
    fig, axes = plt.subplots(2, len(titles), figsize=(3.4 * len(titles) + (1.7 if by_op else 0), 4.4),
                             constrained_layout=True, squeeze=False, sharey="row" if normalize else False)
    for j, q in enumerate(titles):
        for row, kinds in enumerate((OPS, [o for o, _ in BYTES])):
            ax = axes[row][j]
            totals = [sum(data[db][q][row][o] for o in kinds) for db in dbs]
            # bytes: one unit per panel, so the ticks are plain numbers
            div, unit = (1, "") if row == 0 else next((d, u) for d, u in ((2**20, "MB"), (2**10, "KB"), (1, "B"))
                                                      if max(totals) >= d)
            if normalize:
                div = max(totals) / 100
            for i, db in enumerate(dbs):
                vals = data[db][q][row]
                if by_op:
                    bottom = 0
                    for o in kinds:
                        ax.bar(i, vals[o] / div, 0.62, bottom=bottom, color=op_color(o), edgecolor="white", linewidth=0.6)
                        bottom += vals[o] / div
                else:
                    bottom = totals[i] / div
                    ax.bar(i, bottom, 0.62, **db_bar_kw(db))
                label = (number_to_human(totals[i]) if row == 0 else
                         f"{totals[i]:.0f} B" if totals[i] < 1024 else bytes_to_human(totals[i]))
                ax.text(i, bottom, label, ha="center", va="bottom")
            ax.set_ylim(0, max(totals) / div * 1.18)
            ax.grid(axis="y", color="0.85", linewidth=0.8)
            ax.set_axisbelow(True)
            ax.set_xticks(range(len(dbs)))
            ax.set_xticklabels([db_label(d) for d in dbs] if row == 1 else [""] * len(dbs), rotation=20, ha="right")
            if normalize:
                ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(decimals=0))
            elif row == 0:
                ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: number_to_human(v) if v >= 10000 else f"{v:g}"))
            if j == 0:                 # no unit on the bytes axis: each bar carries its value with the unit
                ax.set_ylabel((("KV operations" if by_op else "KV pairs read") if row == 0 else "bytes read")
                              + ("\n(% of the most)" if normalize else ""))
            if row == 0:
                ax.set_title(f"Q{j + 1}: {titles[q]}")     # numbered, so the text can say "on Q2"
    if by_op:
        for row, (keys, labels) in enumerate(((OPS, [op_label(o) for o in OPS]), zip(*BYTES))):
            handles = [plt.Rectangle((0, 0), 1, 1, color=op_color(o)) for o in keys]
            axes[row][-1].legend(handles[::-1], list(labels)[::-1], loc="upper left", bbox_to_anchor=(1.02, 1.0), frameon=False)
    fig.align_ylabels(axes[:, 0])
    return fig


COLUMN_TIMES_SIZE = (5.0, 3.75)                     # drawn inches; printed one column wide (COLUMN_IN)


def two_digits(v):
    """a value at two significant digits, for a label: 7.08 -> 7.1, 321 -> 320, 1545296 -> 1500000"""
    if v == 0:
        return 0.0
    import math
    return round(v, 1 - math.floor(math.log10(abs(v))))


def ops_label(v):
    """an operation count in a short label: exact under 1,000 (445), else two digits (7K, 200K, 1.5M)"""
    if v < 1e3:
        return f"{v:.0f}"
    r = two_digits(v)
    if r >= 1e6:
        return f"{r / 1e6:.1f}M"                    # two digits: 1.0M, 1.9M
    return f"{r / 1e3:.1f}K" if r < 1e4 else f"{r / 1e3:.0f}K"


def ms_label_2(v):
    """a time in a short label at two digits: 4.1ms, 93ms, 320ms, 700ms, 5.1s, 12s"""
    r = two_digits(v)
    return f"{r:g}ms" if r < 1000 else f"{r / 1000:g}s"


def bytes_label_2(v):
    """bytes in a short label at two digits, KB and MB of 1024: 37KB, 5.7MB, 84MB"""
    for div, unit in ((2**20, "MB"), (2**10, "KB")):
        if v >= div:
            return f"{two_digits(v / div):g}{unit}"
    return f"{two_digits(v):g}B"


# Figure 2's colours by kind: a GET (teal) and a scan, a SEEK and its NEXTs (steel blue), as operations and as bytes;
# the time in the KV store (dark grey) and the rest (light grey)
READ_KIND_COLOR = {"get": OP_COLOR["G"], "scan": OP_COLOR["S"], "kv": "#4a4a4a", "rest": "#cfcfcf"}


def db_reads_column_times(data, times, titles, dbs):
    """Three rows by query, sized for one column: the KV operations and the bytes read, GETs (teal) under scans (a
    SEEK and its NEXTs; steel blue); then each live engine's time (the
    median of its runs), the time in the KV store (dark grey) under the rest (light grey). Each panel is linear, scaled
    to its tallest bar, with no y axis: the value above each bar at two digits (operations under 1,000 exact).
    data[db][query] = ({"G", "S", "N": count}, {"G", "N": bytes}); times[(query, db, "live")] = {"median_ms", "kv_ms",
    ...}; titles = {query: title}, in panel order."""
    size = print_style(COLUMN_TIMES_SIZE[0])
    small = size * SMALL
    fig, axes = plt.subplots(3, len(titles), figsize=COLUMN_TIMES_SIZE, constrained_layout=True, squeeze=False)
    for j, q in enumerate(titles):
        for row in range(3):
            if row == 0:                            # operations: GET, then the scans (a SEEK and its NEXTs)
                segs = [[("get", data[db][q][0]["G"]), ("scan", data[db][q][0]["S"] + data[db][q][0]["N"])]
                        for db in dbs]
                label = ops_label
            elif row == 1:                          # bytes: GET bytes, then scan bytes
                segs = [[("get", data[db][q][1]["G"]), ("scan", data[db][q][1]["N"])] for db in dbs]
                label = bytes_label_2
            else:                                   # time: in the KV store, then the rest
                segs = []
                for db in dbs:
                    t = times[(q, db, "live")]
                    kv = min(t["kv_ms"], t["median_ms"])
                    segs.append([("kv", kv), ("rest", t["median_ms"] - kv)])
                label = ms_label_2
            ax = axes[row][j]
            totals = [sum(v for _, v in sg) for sg in segs]
            top = max(totals)
            for i, sg in enumerate(segs):
                bottom = 0
                for kind, v in sg:
                    ax.bar(i, v, 0.7, bottom=bottom, color=READ_KIND_COLOR[kind], edgecolor="white", linewidth=0.4)
                    bottom += v
                ax.text(i, totals[i] + 0.02 * top, label(totals[i]), ha="center", va="bottom", fontsize=small)
            ax.set_ylim(0, top * 1.24)              # room for the value above the tallest bar
            ax.set_yticks([])
            ax.set_xlim(-0.6, len(dbs) - 0.4)
            ax.set_xticks(range(len(dbs)))
            ax.set_xticklabels([DB_SHORT.get(d, d) for d in dbs] if row == 2 else [""] * len(dbs))
            ax.tick_params(axis="x", length=0 if row < 2 else 2, pad=2)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            if row == 0:
                ax.set_title(f"Q{j + 1}", pad=2)
    for row, ylabel in enumerate(("KV operations", "Bytes read", "Latency")):
        axes[row][0].set_ylabel(ylabel)
    keys = [("get", "GET"), ("scan", "Scan"), ("kv", "In KV store"), ("rest", "Outside KV store")]
    fig.legend([plt.Rectangle((0, 0), 1, 1, color=READ_KIND_COLOR[k]) for k, _ in keys], [t for _, t in keys],
               loc="outside upper center", ncol=len(keys), frameon=False, handlelength=1.2, columnspacing=1.4)
    fig.align_ylabels(axes[:, 0])
    return fig


REGIME_LATENCY_SIZE = (5.0, 2.3)                    # drawn inches; printed one column wide (COLUMN_IN)
REGIME_COLOR = ("#8a8a8a", "#7fa8cf")               # the figure's regime; the other beside it
REGIME_LABEL_DECADES = 0.8                         # log-axis room above the tallest bar for its upright value


def db_reads_latency_regimes(times, titles, dbs, regimes, regime_label):
    """One row by query: each live engine's time (the median of its runs) at two cache regimes side by side, the
    figure's (grey) and another (blue), on a log axis: the times differ by orders of magnitude, so a bar's height is
    read off the axis, and its value is printed above it at two digits. Each panel's axis starts at the whole power
    of ten at or below half its smallest bar (so the smallest bar is at least a third of a decade tall), and every
    panel spans the same number of decades: the widest panel's, with REGIME_LABEL_DECADES above its tallest bar for
    the upright value. The bars are the two times alone: the disk wait of a run is in the times table (io_wait_ms),
    summed over the server's threads, so it is not a part of the bar.
    times[(regime, query, db, "live")] = {"median_ms", ...}; titles = {query: title}, in panel order;
    regime_label = {regime: its legend text}."""
    size = print_style(REGIME_LATENCY_SIZE[0])
    small = size * SMALL
    fig, axes = plt.subplots(1, len(titles), figsize=REGIME_LATENCY_SIZE, constrained_layout=True, squeeze=False)
    width = 0.38
    values = {q: [times[(regime, q, db, "live")]["median_ms"] for db in dbs for regime in regimes] for q in titles}
    low = {q: 10 ** math.floor(math.log10(min(v) / 2)) for q, v in values.items()}
    decades = math.ceil(max(math.log10(max(v) / low[q]) for q, v in values.items()) + REGIME_LABEL_DECADES)
    for j, q in enumerate(titles):
        ax = axes[0][j]
        for i, db in enumerate(dbs):
            for k, regime in enumerate(regimes):
                t = times[(regime, q, db, "live")]
                x = i + (k - 0.5) * width
                ax.bar(x, t["median_ms"], width, color=REGIME_COLOR[k], edgecolor="white", linewidth=0.4)
                ax.text(x, t["median_ms"] * 1.15, ms_label_2(t["median_ms"]), ha="center", va="bottom",
                        fontsize=small, rotation=90)
        ax.set_yscale("log")
        ax.set_ylim(low[q], low[q] * 10 ** decades)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(ms_tick))
        ax.minorticks_off()
        ax.tick_params(axis="y", labelsize=small, length=2, pad=1)
        ax.set_xlim(-0.6, len(dbs) - 0.4)
        ax.set_xticks(range(len(dbs)))
        ax.set_xticklabels([DB_SHORT.get(d, d) for d in dbs])
        ax.tick_params(axis="x", length=2, pad=2)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.set_title(f"Q{j + 1}", pad=2)
    axes[0][0].set_ylabel("Latency")
    fig.legend([plt.Rectangle((0, 0), 1, 1, color=c) for c in REGIME_COLOR], [regime_label[r] for r in regimes],
               loc="outside upper center", ncol=2, frameon=False, handlelength=1.2, columnspacing=1.4)
    return fig


def short_bytes(v):
    """bytes in a short label (KB and MB of 1024): 512 B, 3.7 KB, 26 KB, 5.4 MB, 72 MB"""
    for div, unit in ((2**20, "MB"), (2**10, "KB")):
        if v >= div:
            return f"{v / div:.1f} {unit}" if v / div < 10 else f"{v / div:.0f} {unit}"
    return f"{v:.0f} B"


# --- the database grid with times (complex-kv-reads' overview) --------------------------------------------------------
TIMES_SIZE = (5.0, 5.6)                            # drawn inches; printed one column wide (COLUMN_IN)
TIME_COLOR = {"kv": "#3a3a3a", "rest": "#cfcfcf", "engine": "#2b5d8a", "cheapest": "#a9c8e3"}


def ms_label(v):
    """a time in a short label: 0.96ms, 93ms, 699ms, 3.6s"""
    return f"{v:.2g}ms" if v < 10 else f"{v:.0f}ms" if v < 1000 else f"{v / 1000:.2g}s"


def count_label(v):
    """a count in a short label, three digits: 63, 7.08K, 356K, 1.55M"""
    for div, unit in ((1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"{v / div:.3g}{unit}"
    return f"{v:.0f}"


def ms_tick(v, _=None):
    return f"{v:g} ms" if v < 1000 else f"{v / 1000:g} s"


def _stacked_counts(ax, values, colors, label, fontsize):
    """one count panel on a linear y: per database one bar stacked bottom-up in colors' order, its total above.
    values[i] = the segments of database i, in colors' order."""
    top = max(sum(v) for v in values) * 1.25
    for x, segs in enumerate(values):
        bottom = 0
        for v, c in zip(segs, colors):
            ax.bar(x, v, 0.66, bottom=bottom, color=op_color(c), edgecolor="white", linewidth=0.4)
            bottom += v
        ax.text(x, bottom, label(bottom), ha="center", va="bottom", fontsize=fontsize)
    ax.set_ylim(0, top)
    ax.yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3))


def db_reads_times(data, times, titles, dbs, with_cheapest=False):
    """Four rows by query, sized for one column: KV operations (GET, SEEK, NEXT stacked), bytes read (GET bytes and
    scan bytes stacked), the live engines' time (the time in the KV store stacked under the rest, its
    share above), and our evaluator's time running the engine's plan (with_cheapest: also our cheapest plan, on a log
    y). Times are medians with min-max whiskers. data[db][query] = ({"G", "S", "N": count}, {"G", "N": bytes});
    times[(query, db, arm)] = {"median_ms", "min_ms", "max_ms", "kv_ms", "kv_pooled"}, arm live, engine or cheapest;
    titles = {query: title}, in panel order. Returns the figure."""
    size = print_style(TIMES_SIZE[0])
    small = size * SMALL
    fig, axes = plt.subplots(4, len(titles), figsize=TIMES_SIZE, constrained_layout=True, squeeze=False)
    arms = ("engine", "cheapest") if with_cheapest else ("engine",)
    for j, q in enumerate(titles):
        _stacked_counts(axes[0][j], [[data[db][q][0][o] for o in OPS] for db in dbs], OPS, count_label, small)
        axes[0][j].yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: count_label(v)))
        _stacked_counts(axes[1][j], [[data[db][q][1][o] for o in ("G", "N")] for db in dbs], ("G", "N"),
                        lambda v: short_bytes(v).replace(" ", ""), small)
        top_b = axes[1][j].get_ylim()[1]            # round byte ticks in the unit the labels use (KB or MB of 1024)
        unit = 2**20 if top_b >= 2**20 else 2**10
        axes[1][j].yaxis.set_major_locator(matplotlib.ticker.FixedLocator(
            [t * unit for t in matplotlib.ticker.MaxNLocator(3).tick_values(0, top_b / unit) if 0 <= t * unit <= top_b]))
        axes[1][j].yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: short_bytes(v) if v else "0"))
        top = 0
        ax = axes[2][j]                             # live: time in the KV store, then the rest of the query
        for x, db in enumerate(dbs):
            t = times[(q, db, "live")]
            med, lo, hi = t["median_ms"], t["min_ms"], t["max_ms"]
            kv = min(t["kv_ms"], med)
            ax.bar(x, kv, 0.66, color=TIME_COLOR["kv"], edgecolor="white", linewidth=0.4)
            ax.bar(x, med - kv, 0.66, bottom=kv, color=TIME_COLOR["rest"], edgecolor="white", linewidth=0.4)
            ax.plot([x, x], [lo, hi], color="black", linewidth=0.6)
            ax.text(x, hi, f"{ms_label(med)}\n{100 * kv / med:.0f}%{'*' if t['kv_pooled'] else ''}", ha="center",
                    va="bottom", fontsize=small, linespacing=0.9)
            top = max(top, hi)
        ax = axes[3][j]                             # the evaluator: the engine's plan (and our cheapest plan)
        width = 0.66 / len(arms)
        for x, db in enumerate(dbs):
            for k, arm in enumerate(arms):
                t = times[(q, db, arm)]
                xx = x + (k - (len(arms) - 1) / 2) * width
                ax.bar(xx, t["median_ms"], width * 0.95, color=TIME_COLOR[arm], edgecolor="white", linewidth=0.4)
                ax.plot([xx, xx], [t["min_ms"], t["max_ms"]], color="black", linewidth=0.6)
                if with_cheapest:                   # two bars per database: the value up the bar's axis
                    ax.text(xx, t["max_ms"] * 1.3, ms_label(t["median_ms"]), ha="center", va="bottom",
                            fontsize=small, rotation=90)
                else:
                    ax.text(xx, t["max_ms"], ms_label(t["median_ms"]), ha="center", va="bottom", fontsize=small)
                    top = max(top, t["max_ms"])
        # the live row and (engine's plan only) the evaluator row share one linear y, so the two times compare
        for r in (2, 3) if not with_cheapest else (2,):
            axes[r][j].set_ylim(0, top * 1.45)      # room for the two-line label above the tallest bar
            axes[r][j].yaxis.set_major_locator(matplotlib.ticker.MaxNLocator(3))
            axes[r][j].yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(ms_tick))
        if with_cheapest:                           # our cheapest plan can be 1,000x faster: a log y
            lo = min(times[(q, db, a)]["min_ms"] for db in dbs for a in arms)
            hi = max(times[(q, db, a)]["max_ms"] for db in dbs for a in arms)
            ax.set_yscale("log")
            ax.set_ylim(10 ** math.floor(math.log10(lo)), hi * 30)
            ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, numticks=4))
            ax.yaxis.set_minor_locator(matplotlib.ticker.NullLocator())
            ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(ms_tick))
        for i in range(4):
            a = axes[i][j]
            a.grid(axis="y", color="0.85", linewidth=0.5)
            a.set_axisbelow(True)
            a.set_xticks(range(len(dbs)))
            a.set_xticklabels([DB_SHORT.get(d, d) for d in dbs] if i in (1, 3) else [""] * len(dbs))
            a.set_xlim(-0.55, len(dbs) - 0.45)
            a.tick_params(length=2, pad=2)
        axes[0][j].set_title(f"Q{j + 1}", pad=2)
    for i, ylabel in enumerate(("KV operations", "bytes read", "live engine\ntime",
                                "our evaluator\ntime" if with_cheapest else "engine's plan\non our store")):
        axes[i][0].set_ylabel(ylabel)
    keys = [op_color(o) for o in OPS] + [TIME_COLOR[k] for k in ("kv", "rest") + arms]
    labels = ["GET / GET bytes", "SEEK", "NEXT / scan bytes", "live: in the KV store", "live: the rest",
              "evaluator: the engine's plan", "evaluator: our cheapest plan"][:5 + len(arms)]
    fig.legend([plt.Rectangle((0, 0), 1, 1, color=c) for c in keys], labels, loc="outside upper center", ncol=3,
               frameon=False, handlelength=1.2, columnspacing=1.2)
    fig.align_ylabels(axes[:, 0])
    return fig
