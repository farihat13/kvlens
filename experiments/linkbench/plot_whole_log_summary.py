#!/usr/bin/env python3
"""LinkBench's whole request log on the nine cells in counts (three rows) and in steady time (the last row), drawn
like I01's breakdown-summary-latency.png (the same rows, stacks, colours, labels; the reference layout, python/kvcost/layout_cost/settings.py).
Reads whole_log_counts.csv and whole_log_time.csv (whole_log_counts_and_time.py); the time row is the steady whole-log
run's 68,994 reads, each request's median run.

    python3 experiments/linkbench/plot_whole_log_summary.py --data DIR --out DIR [--extended] [--linear]
        -> <out>/linkbench-breakdown-summary[-extended][-linear].png"""
import csv, os, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter
HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.plotting.formatting import number_to_human, short_bytes
from kvcost.plotting.style import apply_house_style, caption, log_share_stack, op_color
from kvcost.layout_cost import settings as cells  # (the reference)
from kvcost.layout_cost import cost_models as CM  # (the cost models, one definition)
from kvcost.layout_cost.labels import BLEND_BYTES_COLOR, BLEND_OPS_COLOR, PUT_COLOR, WRITTEN_COLOR
from kvcost.layout_cost.settings import name_of as pair_name  # ('Co-Sp': a cell's short name)
from kvcost.layout_cost.compose import CELLS

def _arg(flag):
    i = sys.argv.index(flag)
    v = sys.argv[i + 1]
    del sys.argv[i:i + 2]
    return v


DATA, OUT = _arg("--data"), _arg("--out")
REF = cells.REF                 # every ratio is to the reference layout (python/kvcost/layout_cost/settings.py)
GET_BYTES, SCAN_BYTES, MISS_BYTES = "#5b7c5a", "#c5b0d5", "#bfa575"      # the byte lanes' colors
# the columns: the whole log, then its four request groups
GROUPS = (("all requests", None), ("node reads", ("getnode",)),
          ("link reads", ("getlinklist_link1", "getlinklist_link2", "countlink_link1", "countlink_link2", "getlink_link1", "getlink_link2")),
          ("node updates", ("addnode", "updatenode", "deletenode")),
          ("link updates", ("addlink_link1", "addlink_link2", "updatelink_link1", "updatelink_link2", "deletelink_link1", "deletelink_link2")))
COUNTS = list(csv.DictReader(open(os.path.join(DATA, "whole_log_counts.csv"))))
TIMES = list(csv.DictReader(open(os.path.join(DATA, "whole_log_time.csv"))))
logn = {r["request"]: float(r["requests"]) for r in COUNTS}
LINEAR = "--linear" in sys.argv            # linear axes in units of each panel's reference bar; adds -linear to the name
EXTENDED = "--extended" in sys.argv        # one column per request group; writes linkbench-breakdown-summary-extended.png
assert set(logn) == {q for _, g in GROUPS[1:] for q in g}, "a request type is in no group"
if not EXTENDED:
    GROUPS = GROUPS[:1]


def load(group):
    """per cell: the summed lanes of the group's requests; the reads' steady time (wall, storage steps, inside RocksDB; s); requests"""
    tot, time, n = {c: {} for c in CELLS}, {c: [0.0, 0.0, 0.0] for c in CELLS}, 0
    for r in COUNTS:
        if group is None or r["request"] in group:
            for k, v in r.items():
                if k not in ("cell", "request"):
                    tot[r["cell"]][k] = tot[r["cell"]].get(k, 0.0) + float(v)
    for r in TIMES:      # a request type's time = the sum over its requests (the whole log is timed: the weight below is 1)
        if group is None or r["request"] in group:
            w = logn[r["request"]] / float(r["requests"]) / 1e9
            n += 1
            for i, k in enumerate(("wall_ns", "kv_ns", "rocksdb_ns")):
                time[r["cell"]][i] += w * float(r[k])
    return tot, time, n > 0, sum(v for q, v in logn.items() if group is None or q in group)

ROCKS_COLOR, OPS_COLOR, OUTSIDE_COLOR = "#d95f02", "#7b9fc7", "#bdbdbd"


def segments(t):
    """the same three stacks as I01's summary: the reads a write does first are inside GET / SEEK / NEXT and the read bytes"""
    ops = [(t["G"] + t["wG"], op_color("G"), ""), (CM.SEEK_PER_GET * (t["S"] + t["wS"]), op_color("S"), ""), (CM.NEXT_PER_GET * (t["N"] + t["wN"]), op_color("N"), ""),
           (t["P"] + t["D"], PUT_COLOR, "///")]
    byt = [(t["get"], GET_BYTES, ""), (t["scan"] + t["wread"], SCAN_BYTES, ""), (t["miss"], MISS_BYTES, ""), (t["written"], WRITTEN_COLOR, "///")]
    o, b = sum(v for v, *_ in ops), sum(v for v, *_ in byt)
    return ops, byt, [(o, BLEND_OPS_COLOR, ""), (b / CM.BYTES_PER_OP, BLEND_BYTES_COLOR, "")]

apply_house_style()
ref = CELLS.index(REF)
names = [pair_name(c) for c in CELLS]
REF_NAME = pair_name(REF)
data = [load(g) for _, g in GROUPS]
fig, axes = plt.subplots(4, len(GROUPS), figsize=((0.55 if EXTENDED else 0.82) * len(CELLS) * len(GROUPS) + (2.2 if EXTENDED else 3.4), 15.6), sharex=True, squeeze=False,
                         gridspec_kw={"hspace": 0.07, "wspace": 0.2 if LINEAR else 0.1, "height_ratios": (1, 1, 1, 1.15)})
for _k in range(0 if LINEAR else 3):                 # the count rows share an axis across the columns; the time row has one per column
    for _ax in axes[_k][1:]:
        _ax.sharey(axes[_k][0])
        _ax.tick_params(labelleft=False)
rows = (("total weighted KV ops", 0, lambda v: number_to_human(v)), ("total bytes", 1, short_bytes), ("ops + bytes / 4,096", 2, lambda v: number_to_human(v)))
for title, k, human in rows:
    allsegs = [[segments(tot[c])[k] for c in CELLS] for tot, *_ in data]
    alltt = [[sum(v for v, *_ in s) for s in segs] for segs in allsegs]
    floor, top = min(min(t) for t in alltt) / 4, max(max(t) for t in alltt)
    for j, (segs, tt) in enumerate(zip(allsegs, alltt)):
        ax = axes[k][j]
        if not LINEAR:
            ax.set_yscale("log")
        for x, s in enumerate(segs):
            if LINEAR:
                bottom = 0.0
                for v, color, hatch in s:
                    ax.bar(x, v / tt[ref], 0.8, bottom=bottom, color=color, hatch=hatch, edgecolor="white", linewidth=0.4)
                    bottom += v / tt[ref]
            else:
                log_share_stack(ax, x, 0.8, s, floor)
            ax.text(x, tt[x] / tt[ref] + max(tt) / tt[ref] * 0.02 if LINEAR else tt[x] * 1.12, human(tt[ref]) if x == ref else (f"{tt[x] / tt[ref]:.0f}x" if tt[x] / tt[ref] >= 99.5 else f"{tt[x] / tt[ref]:.2g}x"), rotation=90, ha="center", va="bottom", fontsize=9.5,
                    fontweight="bold" if x == ref else None)
        if LINEAR:
            ax.set_ylim(0, max(tt) / tt[ref] * 1.3)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
        else:
            ax.set_ylim(floor, top * 12)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _, h=human: h(v)))
        ax.grid(axis="y", alpha=0.3)
    axes[k][0].set_ylabel(title + (f"\n({REF_NAME} = 1)" if LINEAR else ""), fontsize=13)
for j, (_, time, timed, nreq) in enumerate(data):
    ax = axes[3][j]
    unit = time[REF][0] if LINEAR and timed else 1.0          # linear: the reference cell = 1
    time = {c: [v / unit for v in t] for c, t in time.items()}
    top = max([time[c][0] for c in CELLS if timed] or [1.0])
    if LINEAR:
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:g}x"))
    for x, c in enumerate(CELLS if timed else ()):
        wall, kv, rocks = time[c]
        ax.bar(x, wall - kv, 0.8, color=OUTSIDE_COLOR, edgecolor="white", linewidth=0.4)
        ax.bar(x, kv - rocks, 0.8, bottom=wall - kv, color=OPS_COLOR, edgecolor="white", linewidth=0.4)
        ax.bar(x, rocks, 0.8, bottom=wall - rocks, color=ROCKS_COLOR, edgecolor="white", linewidth=0.4)
        ax.text(x, wall + top * 0.02, f"{wall * unit:.2f} s" if x == ref else f"{wall / time[REF][0]:.2f}x", rotation=90, ha="center", va="bottom", fontsize=9.5,
                fontweight="bold" if x == ref else None)
    if not timed:
        ax.text(0.5, 0.5, "updates are not timed", transform=ax.transAxes, ha="center", va="center", fontsize=11, color="#666666")
        ax.set_yticks([])
    ax.set_ylim(0, top * 1.33)
    ax.grid(axis="y", alpha=0.3)
    ax.set_xticks(range(len(CELLS)), names, rotation=90, fontsize=10)
    ax.set_xlim(-0.6, len(CELLS) - 0.4)
    axes[0][j].set_title(f"{GROUPS[j][0]}\n({nreq:,.0f} requests)", fontsize=12, fontweight="bold")
axes[3][0].set_ylabel("steady time, the reads\n" + (f"({REF_NAME} = 1)" if LINEAR else "(s per pass of the log's reads)"), fontsize=13)
handles = [Patch(color=op_color("G"), label="GET"), Patch(color=op_color("S"), label="SEEK"), Patch(color=op_color("N"), label="NEXT"),
           Patch(facecolor=PUT_COLOR, hatch="///", edgecolor="white", label="PUT + DELETE"),
           Patch(color=GET_BYTES, label="GET key + value"), Patch(color=SCAN_BYTES, label="SCAN key + value"), Patch(color=MISS_BYTES, label="absence-probe keys"),
           Patch(facecolor=WRITTEN_COLOR, hatch="///", edgecolor="white", label="written: key + value"),
           Patch(color=BLEND_OPS_COLOR, label="weighted ops (its share of ops + bytes / 4,096)"), Patch(color=BLEND_BYTES_COLOR, label="total bytes / 4,096 (its share)"),
           Patch(color=ROCKS_COLOR, label="time inside RocksDB"),
           Patch(color=OPS_COLOR, label="storage operators, outside RocksDB"),
           Patch(color=OUTSIDE_COLOR, label="the steps that do not read the store")]
fh = fig.get_figheight()
fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, (1.0 if EXTENDED else 1.15) / fh), ncol=5 if EXTENDED else 2, frameon=False, fontsize=10)
fig.suptitle("L01 (rw): LinkBench's default request mix (LinkBench n1m, the whole log: 100,000 requests)\nthe cost of every option in counts (three rows) and in measured time (last row)", fontsize=14, y=1 - 0.25 / fh)
CAP = (("Columns: the whole log, then its four request groups; a count row shares one axis across the columns, so a group's bar is its part of the whole; the time row has one axis per column. " if EXTENDED else "") + "Bar label: the reference layout " + pair_name(REF) + "'s absolute value (bold), every other bar's ratio to it. A layout is named properties-adjacency (Sp Split, Co Condensed, Ch Chunked); Chunked = 64 KB chunks. "
        "Log scale, each bar split in proportion to its segments' shares; ops weighted GET : SEEK : NEXT : PUT : DELETE = "
        "1 : 1.1 : 0.01 : 1 : 1, the reads a write does first included; total bytes = read (misses included) + read by writes + written; counts are deterministic. "
        "Each of the log's 100,000 requests is priced once (68,994 reads, 31,006 writes). No mixes and no named formats are in this capture. Time row: linear scale, "
        "steady, 102 MB cache (direct I/O), the 68,994 reads only, every one timed: a request's time is its median run over the three measured passes, "
        "summed over the log's reads; inside RocksDB = its seek, get and next timers. Measured on the build of the n1m re-measure. "
       "Data: " + (DATA[DATA.index("output/"):] if "output/" in DATA else DATA) + ".")
if LINEAR:
    CAP = CAP.replace("; a count row shares one axis across the columns, so a group's bar is its part of the whole; the time row has one axis per column", "").replace(
        "Log scale, each bar split in proportion to its segments' shares;", "Every axis is linear, in units of its own panel's reference bar (1x), so a bar's height is its label; a count bar is stacked by its segments;")
caption(fig, CAP, y=0.9 / fh)
fig.subplots_adjust(top=1 - 1.25 / fh, bottom=(3.8 if EXTENDED else 5.2) / fh, left=0.06 if EXTENDED else 0.13, right=0.99)
os.makedirs(OUT, exist_ok=True)
fig.savefig(os.path.join(OUT, "linkbench-breakdown-summary" + ("-extended" if EXTENDED else "") + ("-linear" if LINEAR else "") + ".png"), dpi=130)
print("wrote", "extended" if EXTENDED else "summary")
