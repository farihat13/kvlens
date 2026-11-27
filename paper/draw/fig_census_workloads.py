#!/usr/bin/env python3
# released from gs-paper at 4b5c722 (evidence/census-workloads/fig_census_workloads.py); edit it there, then run evidence/release_draw.py
"""Paper figures: the elements each workload's plans touch, by part (adjlist, vertex props, index; read and written).

  figures/evaluation/census-workloads-touched.pdf   (half a column, beside the layout grid in fig:layout-grid: each
                                                     workload's KV ops on Sp-Sp by kind, from census-workloads-ops.csv; LDBC
                                                     Interactive, BI, JOB and LinkBench at SF3, each at its own weighting)
  figures/evaluation/census-workloads-all.pdf       (one column: every workload, Interactive and BI at SF3, a
                                                     bar per weighting)
  figures/appendix/census-workloads-groups.pdf    (one column: LDBC Interactive at SF3, a bar per query
                                                     group, complex reads, short reads, updates, every query once)
  figures/evaluation/census-workloads.pdf           (one column, LDBC only: (a) the elements, every query once and every
                                                     query equal; (b) the KV items Co-Co and Ch64k-Ch64k read for them)
  figures/appendix/census-workloads-wops-bytes.pdf  (one column, LDBC Interactive at SF3, LDBC's frequencies: (b) in wops
                                                     and bytes, side by side, for Sp-Sp, Co-Co, Sp-Ch256k, Ch256k-Ch256k)

An element: an adjacency entry a plan reads or writes, a vertex property value, an index entry. Split stores one KV item
per element, so the elements are Sp-Sp's KV items. A KV item: an op that lands on an item, (S - S_miss) + (G - G_miss) +
(N - N_miss) read, P + D written. Weightings: every query once (each query's elements summed; LinkBench: each operation
once, per request); the workload's own frequencies (Interactive: LDBC's; LinkBench: its 100,000-request log); equal
share (each query weighted 1 / its combined cost on Sp-Co at SF3; BI's and JOB's own). A
part's share = sum over the queries of weight x elements, over the total. Counts are deterministic: no error bars.

Reads only its tables: census-workloads.csv (LDBC SF3, per query: the elements and each layout's KV items, by segment)
and census-workloads-all.csv (every workload, per query: the elements by segment and the weights), census-workloads-ops.csv
(the SF3 workloads, per query: the KV ops (S + G + N, misses included; P + D written) of each kind on Sp-Sp and on Co-Co:
adjlist, adjlist whose step asks for edge props, edge props by a single-edge probe, vertex props, index, written), and
census-workloads-wops-bytes.csv (LDBC Interactive at SF3, per query (its frequency), part and setting: one run's
wops and bytes); the README says
where each comes from.

    python fig_census_workloads.py --data <dir of the tables> --out <dir>
"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FixedLocator, PercentFormatter

from common import args, read_table, use_paper_font
from paper_style import COLUMN_IN

PT = 8.5                    # the base text size; the smallest text in the figure is PT - 1.5
PT_BAR = 8                  # the shares on and beside the bars (bold on a bar: thin white text fades on the colours)
BLUE, ORANGE, GREEN = "#2171b5", "#e6550d", "#31a354"
# (key, legend label, colour, hatch), drawn left to right on every row; a kind too small to see is left out of the legend
SEGS = (("adj_r", "adjlist read", BLUE, None), ("adj_w", "adjlist written", "#9ecae1", "////"),
        ("v_r", "vertex props read", ORANGE, None), ("v_w", "vertex props written", "#fdbe85", "////"),
        ("ix_r", "index read", GREEN, None), ("ix_w", "index written", "#a1d99b", "////"))
DARK = {"adj_r", "v_r", "ix_r"}
CELLS = (("props_condensed_adj_condensed", "Co-Co"), ("props_chunked_adj_chunked", "Ch64k-Ch64k"))
PANELS = (("ldbc", "(a) LDBC Interactive"), ("bi", "(b) LDBC BI"))


def zero():
    return dict.fromkeys((k for k, *_ in SEGS), 0.0)


def load(data):
    """(elements, {layout name: KV items}), each {(family, query, body): by segment}, in the census' query order"""
    by = {}
    for r in sorted(read_table(data, "census-workloads"), key=lambda r: int(r["order"])):
        by.setdefault(r["kind"], {})[(r["family"], r["query"], r["body"])] = {k: float(r[k]) for k in zero()}
    return by["elements"], {name: by[cell] for cell, name in CELLS}


def shares(rows, el, fam):
    """every query once: the rows' counts summed over the family's queries, over the elements' sum"""
    keys = [k for k in el if k[0] == fam]
    n = sum(sum(el[k].values()) for k in keys)
    return {s: sum(rows[k][s] for k in keys) / n for s in zero()}


def main():
    data, out_dir = args(__doc__)
    tables = data                               # the dir of both tables (`data` is reused by panel (b)'s loop)
    el, lays = load(data)
    use_paper_font()
    plt.rcParams.update({"font.size": PT, "hatch.linewidth": 0.4, "hatch.color": "white"})
    fig, (ax, bx) = plt.subplots(1, 2, figsize=(3.33, 1.8), gridspec_kw={"wspace": 0.38, "width_ratios": (1, 1)})
    drawn = set()
    H = 0.78                                    # bar height (rows 1 apart)

    def stack(a, y, sh, cut=0.12, size=PT_BAR):
        left = 0.0
        for k, _, col, hatch in SEGS:
            if sh[k] > 0:
                a.barh(y, sh[k], left=left, height=H, color=col, edgecolor="white", lw=0.25, hatch=hatch)
                if sh[k] >= cut:
                    a.text(left + sh[k] / 2, y, f"{sh[k]:.0%}", ha="center", va="center", fontsize=size,
                           fontweight="bold", color="white" if k in DARK else "0.15")
                if sh[k] >= 0.0005:
                    drawn.add(k)
            left += sh[k]
        return left

    def style(a, title, xmax):
        a.set_title(title, fontsize=PT - 0.5, pad=1.5, loc="left")
        a.set_xlim(0, xmax)
        a.xaxis.set_major_locator(FixedLocator((0, 0.5, 1)))
        a.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        a.tick_params(labelsize=PT - 1.5, pad=1, length=2, width=0.5)
        a.tick_params(axis="y", length=0)
        for side in a.spines.values():
            side.set_linewidth(0.5)
        a.invert_yaxis()

    # (a) what each workload touches: every query once (counts summed) and every query equal (each query's shares averaged)
    bars = (("ldbc", "once", "LDBC\nInteractive\nonce"), ("ldbc", "equal", "LDBC\nInteractive\nequal"),
            ("bi", "once", "LDBC BI\nonce"), ("bi", "equal", "LDBC BI\nequal"))
    for y, (fam, how, _) in enumerate(bars):
        keys = [k for k in el if k[0] == fam]
        sh = shares(el, el, fam) if how == "once" else \
            {s: sum(el[k][s] / sum(el[k].values()) for k in keys) / len(keys) for s in zero()}
        stack(ax, y, sh)
        print(f"(a) {fam} {how}: " + " ".join(f"{k} {sh[k]:.4f}" for k in sh if sh[k]))
    ax.set_yticks(range(len(bars)), [b[2] for b in bars], fontsize=PT - 1.5, linespacing=0.95)
    ax.set_ylim(len(bars) - 0.5, -0.5)
    style(ax, "(a) elements touched", 1.0)
    ax.invert_yaxis()
    # (b) every query once: per workload, the elements (= Sp-Sp's KV items) and the KV items Co-Co and Ch64k-Ch64k read
    rows = (("Sp-Sp", el),) + tuple((name, lays[name]) for _, name in CELLS)   # Sp-Sp's items = the elements
    ys, labels = [], []
    for g, fam in enumerate(("ldbc", "bi")):
        for i, (name, data) in enumerate(rows):
            y = g * (len(rows) + 0.5) + i
            tot = stack(bx, y, shares(data, el, fam), cut=0.15)
            if i:
                bx.text(tot + 0.03, y, f"{tot:.0%}", va="center", fontsize=PT_BAR)
            ys.append(y)
            labels.append(name)
            print(f"(b) {fam} {name}: {tot:.4f}")
    for g, name in enumerate(("LDBC Interactive", "LDBC BI")):     # the group, in the empty space of its Ch64k-Ch64k row
        bx.text(0.97, g * (len(rows) + 0.5) + 2, name, ha="right", va="center", fontsize=PT - 1, fontweight="bold",
                color="0.25")
    bx.set_yticks(ys, labels, fontsize=PT - 1.5)
    bx.set_ylim(ys[-1] + 0.5, ys[0] - 0.5)
    bx.axhline(len(rows) - 0.25, color="0.6", lw=0.4)
    style(bx, "(b) KV pairs, once", 1.0)
    bx.invert_yaxis()
    # the legend: one column per part (adjacency, vertex props, index), filled top-down; blanks keep the columns aligned
    cols = (("adj_r", "adj_w"), ("v_r", "v_w"), ("ix_r", "ix_w"))
    seg = {k: (lab, c, h) for k, lab, c, h in SEGS}
    handles = []
    for col in cols:
        shown = [k for k in col if k in drawn]
        handles += [Patch(fc=seg[k][1], hatch=seg[k][2], ec="white", lw=0, label=seg[k][0]) for k in shown]
        handles += [Patch(fc="none", ec="none", label="")] * (2 - len(shown))
    fig.legend(handles=handles, loc="upper center", ncol=3, frameon=False, fontsize=PT - 1.5, handlelength=1.0,
               columnspacing=0.9, handletextpad=0.3, labelspacing=0.1, borderaxespad=0.0, bbox_to_anchor=(0.53, 1.005))
    # elements = Sp-Sp's KV items (Split stores one item per element); the caption says both, and what the x axis is
    fig.subplots_adjust(left=0.137, right=0.965, top=0.755, bottom=0.09)
    OUT = out_dir / "figures/evaluation/census-workloads.pdf"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT)
    fig.savefig(OUT.with_suffix(".png"), dpi=300)
    print(f"wrote {OUT}")

    draw_touched(tables, out_dir)
    draw_all(tables, out_dir)
    draw_groups(tables, out_dir)
    draw_wops_bytes(tables, out_dir)


ALL = "census-workloads-all"
OWN = (("ldbc", "Interactive", "frequency"), ("bi", "BI", "equal_share"), ("job", "JOB", "equal_share"),
       ("linkbench", "LinkBench", "frequency"))      # the touched panel: each workload at its own weighting, SF3
WEIGHT = {"once": lambda r: 1 / float(r["requests"]),       # a row sums its requests; once = per request
          "frequency": lambda r: float(r["frequency"]),     # the workload's own frequencies
          "equal_share": lambda r: float(r["equal_share"])}  # 1 / the query's combined cost on Sp-Co at SF3


def weighted(rows, how):
    """{segment: share} of rows at weighting `how`: sum of weight x elements per segment, over the total"""
    t = {k: sum(WEIGHT[how](r) * float(r[k]) for r in rows) for k, *_ in SEGS}
    n = sum(t.values())
    return {k: v / n for k, v in t.items()}


def bar(a, y, sh, cut, size, height):
    """one stacked bar of shares, labelled where a segment is at least `cut`"""
    left = 0.0
    for k, _, col, hatch in SEGS:
        if sh[k] > 0:
            a.barh(y, sh[k], left=left, height=height, color=col, edgecolor="white", lw=0.25, hatch=hatch)
            if sh[k] >= cut:
                a.text(left + sh[k] / 2, y, f"{sh[k]:.0%}", ha="center", va="center", fontsize=size,
                       fontweight="bold", color="white" if k in DARK else "0.15")
        left += sh[k]


def axis(a, size):
    a.set_xlim(0, 1)
    a.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    a.tick_params(labelsize=size, pad=1, length=2, width=0.5)
    a.tick_params(axis="y", length=0)
    for side in a.spines.values():
        side.set_linewidth(0.5)


OPS = "census-workloads-ops"
# the touched panel's kinds, left to right: (column on Sp-Sp, legend label, colour, hatch)
TOUCHED = (("sp_adj", "adjlist", BLUE, None), ("sp_adj_ep", "+ edge props", "#6baed6", None),
           ("sp_ep", "edge props alone", "#756bb1", None), ("sp_props", "vertex props", ORANGE, None),
           ("sp_index", "index", GREEN, None), ("sp_written", "written", "0.75", "////"))
TOUCHED_DARK = {"sp_adj", "sp_ep", "sp_props", "sp_index"}


def touched_shares(rows, how):
    """{kind: share} of a workload's KV ops on Sp-Sp at weighting `how`"""
    t = {k: sum(WEIGHT[how](r) * float(r[k]) for r in rows) for k, *_ in TOUCHED}
    n = sum(t.values())
    return {k: v / n for k, v in t.items()}


def draw_touched(data, out_dir):
    """half a column at print size (7 pt), beside the layout grid: each workload's KV ops on Sp-Sp by kind, at its own
    weighting, SF3"""
    table = read_table(data, OPS)
    plt.rcParams.update({"font.size": 7, "hatch.linewidth": 0.4, "hatch.color": "white"})
    h = 0.55 + 0.30 * len(OWN)
    fig, ax = plt.subplots(figsize=(1.62, h))
    for y, (fam, name, how) in enumerate(OWN):
        sh = touched_shares([r for r in table if r["family"] == fam], "frequency" if fam == "linkbench" else how)
        left = 0.0
        for k, _, col, hatch in TOUCHED:
            if sh[k] > 0:
                ax.barh(y, sh[k], left=left, height=0.78, color=col, edgecolor="white", lw=0.25, hatch=hatch)
                if sh[k] >= 0.12:
                    ax.text(left + sh[k] / 2, y, f"{sh[k]:.0%}", ha="center", va="center", fontsize=7,
                            fontweight="bold", color="white" if k in TOUCHED_DARK else "0.15")
            left += sh[k]
        print(f"touched {name} ({how}): " + " ".join(f"{k} {v:.4f}" for k, v in sh.items() if v))
    ax.set_yticks(range(len(OWN)), [name for _, name, _ in OWN], fontsize=7)
    ax.set_ylim(len(OWN) - 0.5, -0.5)
    ax.xaxis.set_major_locator(FixedLocator((0, 0.5, 1)))
    axis(ax, 7)
    ax.set_xlabel("share of KV ops on Sp-Sp", fontsize=7, labelpad=1)
    swatches = [Patch(fc=col, hatch=hatch, ec="white" if hatch else "none", lw=0, label=label)
                for _, label, col, hatch in TOUCHED]
    fig.legend(handles=swatches, loc="upper center", ncol=2, frameon=False, fontsize=7, handlelength=1.0,
               columnspacing=0.5, handletextpad=0.3, labelspacing=0.15, borderaxespad=0.0, bbox_to_anchor=(0.47, 1.0))
    fig.subplots_adjust(left=0.30, right=0.92, top=1 - 0.40 / h, bottom=0.33 / h)
    out = out_dir / "figures/evaluation/census-workloads-touched.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


# the all-workloads figure: (family, name, queries, bars)
WORKLOADS = (("ldbc", "LDBC Interactive, SF3", 27, ("once", "frequency", "equal_share")),
             ("bi", "LDBC BI, SF3", 17, ("once", "equal_share")),
             ("job", "JOB (IMDb)", 113, ("once", "equal_share")),
             ("linkbench", "LinkBench", 10, ("once", "frequency")))
BAR_LABEL = {("linkbench", "once"): "every operation once", ("linkbench", "frequency"): "request log",
             ("ldbc", "frequency"): "LDBC's frequencies"}


def draw_all(data, out_dir):
    """one column: every workload, a bar per weighting"""
    table = read_table(data, ALL)
    plt.rcParams.update({"font.size": 8, "hatch.linewidth": 0.4, "hatch.color": "white"})
    fig, ax = plt.subplots(figsize=(3.33, 4.1))
    ys, labels, y = [], [], 0.0
    for fam, name, nq, bars in WORKLOADS:
        rows = [r for r in table if r["family"] == fam]
        if len(rows) != nq:
            raise SystemExit(f"{ALL}.csv: {fam} has {len(rows)} queries, want {nq}")
        for how in bars:
            sh = weighted(rows, how)
            bar(ax, y, sh, 0.07, 7, 0.8)
            ys.append(y)
            labels.append(BAR_LABEL.get((fam, how)) or {"once": "every query once", "equal_share": "equal share"}[how])
            print(f"{name} {how}: " + " ".join(f"{k} {v:.4f}" for k, v in sh.items() if v))
            y += 1
        ax.text(-0.02, y - len(bars) - 0.62, name, transform=ax.get_yaxis_transform(), ha="right", va="center",
                fontsize=8, fontweight="bold")
        y += 0.55
    ax.set_yticks(ys, labels, fontsize=7)
    ax.set_ylim(ys[-1] + 0.5, -0.9)
    ax.xaxis.set_major_locator(FixedLocator((0, 0.25, 0.5, 0.75, 1)))
    axis(ax, 7)
    ax.set_xlabel("share of the elements the plans touch", fontsize=8, labelpad=1)
    handles = []
    for col in (("adj_r", "adj_w"), ("v_r", "v_w"), ("ix_r", "ix_w")):           # one legend column per part
        handles += [Patch(fc=c, hatch=h, ec="white", lw=0, label=lab) for k, lab, c, h in SEGS if k in col]
    fig.legend(handles=handles, loc="upper center", ncol=3, frameon=False, fontsize=7, handlelength=1.0,
               columnspacing=0.9, handletextpad=0.3, labelspacing=0.1, borderaxespad=0.0, bbox_to_anchor=(0.6, 1.0))
    fig.subplots_adjust(left=0.43, right=0.97, top=0.87, bottom=0.13)
    out = out_dir / "figures/evaluation/census-workloads-all.pdf"
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


GROUPS = (("ic", "complex reads", 12), ("is", "short reads", 7), ("iu", "updates", 8))
GROUP_SCALES = (("ldbc", "LDBC Interactive, SF3"),)


def draw_groups(data, out_dir):
    """one column: LDBC Interactive at SF3, a bar per query group, every query once"""
    table = read_table(data, ALL)
    plt.rcParams.update({"font.size": 8, "hatch.linewidth": 0.4, "hatch.color": "white"})
    fig, ax = plt.subplots(figsize=(3.33, 2.2))
    ys, labels, y = [], [], 0.0
    for fam, name in GROUP_SCALES:
        for prefix, gname, nq in GROUPS:
            rows = [r for r in table if r["family"] == fam and r["query"].startswith(prefix)]
            if len(rows) != nq:
                raise SystemExit(f"{ALL}.csv: {fam} {gname} has {len(rows)} queries, want {nq}")
            sh = weighted(rows, "once")
            bar(ax, y, sh, 0.07, 7, 0.8)
            ys.append(y)
            labels.append(f"{gname} ({nq}, once)")
            print(f"{name} {gname}: " + " ".join(f"{k} {v:.4f}" for k, v in sh.items() if v))
            y += 1
        ax.text(-0.02, y - len(GROUPS) - 0.62, name, transform=ax.get_yaxis_transform(), ha="right", va="center",
                fontsize=8, fontweight="bold")
        y += 0.55
    ax.set_yticks(ys, labels, fontsize=7)
    ax.set_ylim(ys[-1] + 0.5, -0.9)
    ax.xaxis.set_major_locator(FixedLocator((0, 0.25, 0.5, 0.75, 1)))
    axis(ax, 7)
    ax.set_xlabel("share of the elements touched", fontsize=8, labelpad=1)
    handles = []
    for col in (("adj_r", "adj_w"), ("v_r", "v_w"), ("ix_r", "ix_w")):           # one legend column per part
        handles += [Patch(fc=c, hatch=h, ec="white", lw=0, label=lab) for k, lab, c, h in SEGS if k in col]
    fig.legend(handles=handles, loc="upper center", ncol=3, frameon=False, fontsize=7, handlelength=1.0,
               columnspacing=0.9, handletextpad=0.3, labelspacing=0.1, borderaxespad=0.0, bbox_to_anchor=(0.6, 1.0))
    fig.subplots_adjust(left=0.43, right=0.955, top=0.84, bottom=0.15)
    out = out_dir / "figures/appendix/census-workloads-groups.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out)
    fig.savefig(out.with_suffix(".png"), dpi=300)
    plt.close(fig)
    print(f"wrote {out}")


# (properties setting, adjacency setting, label): a layout's cost is each part's at its own setting (the index's is the
# same at every setting)
WB_LAYOUTS = (("split", "split", "Sp-Sp"), ("chunked_c256k", "split", "Ch256k-Sp"),     # adjacency-major
              ("condensed", "condensed", "Co-Co"),
              ("split", "chunked_c256k", "Sp-Ch256k"), ("chunked_c256k", "chunked_c256k", "Ch256k-Ch256k"))
WB_PARTS = (("adj", "adjlist", BLUE), ("props", "props", ORANGE), ("index", "index", GREEN))


def draw_wops_bytes(data, out_dir):
    """one column: LDBC Interactive at SF3 at LDBC's frequencies, the wops (left) and bytes (right) of four layouts
    by part, as shares of Sp-Sp's"""
    cost, total = {}, {}
    for r in read_table(data, "census-workloads-wops-bytes"):
        t = total if r["part"] == "total" else cost
        key = r["setting"] if r["part"] == "total" else (r["setting"], r["part"])
        v = t.setdefault(key, [0.0, 0.0])
        f = float(r["frequency"])                       # LDBC's frequency of the query (W16's)
        v[0] += f * float(r["wops"])
        v[1] += f * float(r["bytes"])
    for s in ("split", "condensed", "chunked", "chunked_c256k"):   # the parts sum to the one-setting layout's total
        for m in (0, 1):
            parts = sum(cost[(s, k)][m] for k, _, _ in WB_PARTS)
            if abs(parts / total[s][m] - 1) > 1e-9:
                raise SystemExit(f"census-workloads-wops-bytes: {s}'s parts {parts} are not its total {total[s][m]}")

    def part(props, adj, k, m):
        return cost[({"adj": adj, "props": props, "index": adj}[k], k)][m]

    use_paper_font()
    plt.rcParams.update({"font.size": 7})
    tops = [max(sum(part(ps, adj, k, m) for k, _, _ in WB_PARTS) / total["split"][m] for ps, adj, _ in WB_LAYOUTS)
            for m in (0, 1)]
    xmaxes = [max(1.0, t * 1.17) for t in tops]          # room for the total's label right of the longest bar
    fig, axes = plt.subplots(1, 2, figsize=(COLUMN_IN, 0.66 + 0.13 * (len(WB_LAYOUTS) - 4)), sharey=True,
                             gridspec_kw={"wspace": 0.05, "width_ratios": (1.0, 1.25)})
    fig.subplots_adjust(left=0.176, right=0.985, bottom=0.2, top=0.995)
    for ax, (m, name), xmax in zip(axes, ((0, "$\\mathit{wops}$"), (1, "bytes")), xmaxes):
        ref = total["split"][m]
        for y, (ps, adj, _) in enumerate(WB_LAYOUTS):
            left = 0.0
            for k, _, col in WB_PARTS:
                sh = part(ps, adj, k, m) / ref
                ax.barh(y, sh, left=left, height=0.82, color=col, edgecolor="white", lw=0.25)
                if sh >= 0.2 * xmax:
                    ax.text(left + sh / 2, y, f"{sh:.0%}", ha="center", va="center", fontsize=6.5,
                            fontweight="bold", color="white")
                left += sh
            if y:
                ax.text(left + 0.02 * xmax, y, f"{left:.0%}" if left >= 0.1 else f"{left:.1%}", va="center",
                        fontsize=6.5)
            print(f"wops-bytes {name} {WB_LAYOUTS[y][2]}: {left:.4f} of Sp-Sp's")
        # the panel's name in its own empty space: wops beside its shortest bar (Ch256k-Ch256k), bytes beside Sp-Sp's
        ax.text(xmax * 0.97, len(WB_LAYOUTS) - 1 if m == 0 else 0, name, ha="right", va="center", fontsize=7.5, fontweight="bold",
                color="0.25")
        ax.set_xlim(0, xmax)
        ax.xaxis.set_major_locator(FixedLocator((0, 0.5, 1) if m == 0 else [t for t in ((1, 2, 3) if xmax < 4 else (1, 3, 5)) if t < xmax]))
        # (no 0% on the right: it would touch the left's 100%)
        ax.xaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
        ax.tick_params(labelsize=6.5, pad=1, length=2, width=0.5)
        ax.tick_params(axis="y", length=0, labelsize=6.5, pad=1)
        for side in ax.spines.values():
            side.set_linewidth(0.5)
    axes[0].set_yticks(range(len(WB_LAYOUTS)), [lab for _, _, lab in WB_LAYOUTS])
    axes[0].set_ylim(len(WB_LAYOUTS) - 0.5, -0.5)
    # the legend inside the bytes panel, right of Co-Co's short bar
    co = [lab for _, _, lab in WB_LAYOUTS].index("Co-Co")
    axes[1].legend(handles=[Patch(fc=c, ec="none", label=lab) for _, lab, c in WB_PARTS], ncol=3, loc="center right",
                   bbox_to_anchor=(1.0, 1.0 - (co + 0.5) / len(WB_LAYOUTS)), frameon=False, fontsize=6.5, handlelength=0.9,
                   handletextpad=0.3, columnspacing=0.7, borderaxespad=0.2)
    out = out_dir / "figures" / "appendix" / "census-workloads-wops-bytes.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", pad_inches=0.02)
    fig.savefig(out.with_suffix(".png"), dpi=300, bbox_inches="tight", pad_inches=0.02)
    plt.close(fig)


if __name__ == "__main__":
    main()
