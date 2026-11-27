#!/usr/bin/env python3
"""Three store-level views of the I01 mix-latency run, per regime, from the scorer's files only (w16_latency_score.py score
--all-arms + engine): every timed store as a bar, cells named by their settings (Sp-Co = Split props, Condensed
adjacency), the named formats as formats, the mixes by their byte limit. Linear y, each panel its own scale (the stores
span well under 10x within a panel: a 1.2x difference is the size of the result).

    per_query   19 panels (IC and IS rows), per store the median over params of the query's plan wall
    per_group   IC / IS / IU panels, per store the class total (seconds per 100,000 updates, the scorer's statistic) with the
                counts' weighted ops of the class as a tick on a right axis (the ratio to the reference cell)
    by_mix      per byte limit B the mix, its best single within B and the reference, with the scorer's word; the named
                formats beside their cells at the right, with the engine word

    python3 experiments/layout_choice/plot_w16_stores.py --sf 3 --root <latency root> [--regime steady-307mb-p3] [--note "..."]
Output: <root>/analysis/plots/w16_<regime>_{per_query,per_group,by_mix}.{png,pdf}

--order formats-first (the latency-levels set): the named formats, then the nine
cells with the properties setting as the major key, then the mixed layouts by B, a line between the kinds; per_query and
per_group only, the workload named in full, --note as a caption below. --skip a,b leaves stores out (said in the output).
"""
import argparse
import csv
import os
import re
import sys
import textwrap

import matplotlib
from kvevaluator.paths import ROOT, figure_dir
from kvevaluator.layouts import SETTINGS, cell_id, cell_parts, is_cell
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.plotting.style import apply_house_style, format_color, hatch as format_hatch
from kvcost.plotting.formatting import number_to_human
from kvcost.layout_cost import settings as cells  # (the cells' short names)

# the options map's colours (plot_workload_story.LAYOUT_COLOR): a cell is coloured by its ADJACENCY setting, its props
# setting is the hatch; a mix and a named format have their own colours, so a store is never read as another kind
LAYOUT_COLOR = {"split": "#1b9e77", "condensed": "#d95f02", "chunked": "#7570b3"}
PROPS_HATCH = {"split": "//", "condensed": "", "chunked": "xx"}
MIX_COLOR = "#17becf"
REF = cells.REF                 # every ratio is to the reference layout (python/kvcost/layout_cost/settings.py)
RN = cells.name_of(REF)       # its name in the figure's text
GRID_ORDER = [cell_id(p, a) for a in SETTINGS for p in SETTINGS]
GROUP_TITLE = {"IC": "IC (complex reads)", "IS": "IS (short reads)", "IU": "IU (updates, apply wall)"}
REGIME_TITLE = {"steady": "steady (bounded cache)", "warm": "warm", "cold": "cold"}


def read_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def stem_of(arm):
    return arm[5:] if arm.startswith("meta-") else arm


FORMATS = ("dgraph", "nebula", "dgraph-c512k", "pertype_csr-c64k")   # the named formats' arms (a chunk arm: the format's colour, the chunk hatch)
FORMATS_FIRST = False          # --order formats-first
WORKLOAD = {False: "LDBC Interactive, LDBC frequencies, reads only (I02)", True: "LDBC Interactive, LDBC frequencies (I01)"}   # by: updates timed


def kind_of(stem):
    return "cell" if is_cell(stem) else "format" if stem in FORMATS else "mix"


FORMAT_EDGE = {"edgecolor": "black", "linewidth": 1.6}      # a named format: its palette colour (NebulaGraph's orange is near
                                                             # Condensed's) with a black edge, so it is never read as a cell


def style_of(stem):
    """(colour, hatch, bar kwargs) of a store by its kind"""
    k = kind_of(stem)
    if k == "cell":
        p, pk, a, ak = cell_parts(stem)         # the layouts with their sizes (kvevaluator.layouts, the one parser)
        chunk = pk or ak
        return LAYOUT_COLOR[a], PROPS_HATCH[p], ({"edgecolor": "black", "linewidth": 1.2} if chunk else {"edgecolor": "white"})   # an added chunk-size variant: a black edge
    if k == "format":
        base = stem.split("-")[0]
        return format_color(base), (format_hatch(stem) if "-" in stem and not FORMATS_FIRST else ""), dict(FORMAT_EDGE)
    return MIX_COLOR, "", {"edgecolor": "white"}


def store_order(stems):
    """the nine cells in grid order, then the mixes by B, then the formats"""
    cells = [s for s in GRID_ORDER if s in stems] + sorted(s for s in stems if kind_of(s) == "cell" and s not in GRID_ORDER)   # the added cell variants after the nine
    def b_of(s):                            # w16_b05 -> 0.5, w16_b2_picks_20261012 -> 2 (a re-picked mix after the first pick of its B)
        m = re.search(r"_b(\d+)", s)
        return (float("0.5" if m.group(1) == "05" else m.group(1)) if m else 9, s)
    mixes = sorted((s for s in stems if kind_of(s) == "mix"), key=b_of)
    fmts = [s for s in FORMATS if s in stems]
    if FORMATS_FIRST:                       # the order of latency-levels-summary.png: the properties setting is the major key
        props_major = [cell_id(p, a) for p in SETTINGS for a in SETTINGS]
        cells = [s for s in props_major if s in stems] + [s for s in cells if s not in props_major]
        return [s for s in ("dgraph", "dgraph-c512k", "nebula", "pertype_csr-c64k") if s in stems] + cells + mixes
    return cells + mixes + fmts


def partitions(ax, stems):
    """a thin vertical line where the store kind changes (named formats | cells | mixes)"""
    for i in range(1, len(stems)):
        if kind_of(stems[i]) != kind_of(stems[i - 1]):
            ax.axvline(i - 0.5, color="0.55", lw=0.7, ls=(0, (4, 3)), zorder=0)


def plain_label(label):
    """the scorer's label in the figures' words: 'I01 mix B=1 (re-picked ...)' -> 'mixed layout, within B=1, re-picked 10-12'"""
    label = cells.short_names(label)
    if label == "Dgraph format":                       # beside its 512 KB split arm, as the latency-levels figures name it
        return "Dgraph format (unsplit lists)"
    m = re.match(r"I01 mix B=([\d.]+)", label)
    return f"mixed layout, within B={m.group(1)}" + (", re-picked 10-12" if "re-picked" in label else "") if m else label


def drop_skipped(d, skip):
    left = sorted(s for s in d if s in skip)
    if left:
        print(f"{len(d) - len(left)} stores drawn; in the scorer's files and not drawn (--skip): {left}")
    return {s: v for s, v in d.items() if s not in skip}


def legend_handles():
    hs = [plt.Rectangle((0, 0), 1, 1, facecolor=LAYOUT_COLOR[a], edgecolor="white", label=f"{a.capitalize()} adjacency") for a in SETTINGS]
    hs += [plt.Rectangle((0, 0), 1, 1, facecolor="0.85", hatch=PROPS_HATCH[p], edgecolor="0.3", label=f"{p.capitalize()} props") for p in ("split", "chunked")]
    if FORMATS_FIRST:
        hs.append(plt.Rectangle((0, 0), 1, 1, facecolor=MIX_COLOR, edgecolor="white", label="a built mixed layout (per type, by its byte limit B)"))
        return hs + [plt.Rectangle((0, 0), 1, 1, facecolor=format_color(f.split("-")[0]), label=f"{n} (its own store; black edge)", **FORMAT_EDGE)
                     for f, n in (("dgraph-c512k", "Dgraph format (512 KB split)"), ("nebula", "NebulaGraph format"),
                                  ("pertype_csr-c64k", "Per-type CSR (64 KB chunks)"))]
    hs.append(plt.Rectangle((0, 0), 1, 1, facecolor=MIX_COLOR, edgecolor="white", label="an I01 mix (by its byte limit)"))
    hs += [plt.Rectangle((0, 0), 1, 1, facecolor=format_color(f.split("-")[0]), hatch=(format_hatch(f) if "-" in f else ""),
                         label=f"{n} (its own store; black edge)", **FORMAT_EDGE)
           for f, n in (("dgraph", "Dgraph format"), ("dgraph-c512k", "Dgraph format, 512 KB posting split"), ("nebula", "NebulaGraph format"))]
    return hs


def per_query(regime, A, out, sf, note, xl, skip=()):
    rows = [r for r in read_csv(os.path.join(A, "w16_per_store_query.csv")) if stem_of(r["arm"]) not in skip]
    labels = {stem_of(r["arm"]): (plain_label(r["label"]) if FORMATS_FIRST else r["label"]) for r in rows}
    stems = store_order(set(labels))
    queries = sorted({r["query"] for r in rows if r["class"] in ("IC", "IS")}, key=lambda q: (q[:2], int(q[2:])))
    med = {(stem_of(r["arm"]), r["query"]): float(r["median_ns"]) for r in rows}
    ncol = 5
    nrow = -(-len(queries) // ncol)
    fig, axes = plt.subplots(nrow, ncol, figsize=(3.3 * ncol, 2.9 * nrow + 1.4), constrained_layout=True)
    axes = axes.ravel()
    for ax in axes[len(queries):]:
        ax.set_visible(False)
    for ax, q in zip(axes, queries):
        for i, st in enumerate(stems):
            v = med.get((st, q))
            if v is None or v != v:
                continue
            c, h, kw = style_of(st)
            ax.bar(i, v / 1e6, 0.8, color=c, hatch=h, **kw)
        ax.set_title(q.upper(), fontsize=8 + xl)
        ax.set_xticks(range(len(stems)), [labels[s] for s in stems], rotation=60, ha="right", fontsize=5 + xl)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v / 1000:.3g} s" if v >= 1000 else f"{v:.3g} ms" if v >= 1 else f"{v * 1000:.0f} us"))
        ax.tick_params(axis="y", labelsize=6 + xl)
        ax.set_ylim(0, ax.get_ylim()[1] * 1.05)
        if FORMATS_FIRST:
            partitions(ax, stems)
    if FORMATS_FIRST:
        fig.legend(handles=legend_handles(), loc="lower center", ncol=4, fontsize=7 + xl, bbox_to_anchor=(0.5, -0.085))
        fig.suptitle(f"The queries of {WORKLOAD[False]} at SF{sf}, {regime.split('-')[0]}: per query and store, the time of one run", fontsize=9 + xl)
        fig.text(0.5, -0.09, textwrap.fill(f"One run of each query (no frequencies applied). {note}", 240), ha="center", va="top", fontsize=6.5 + xl)
        return _save(fig, out, f"w16_{regime}_per_query")
    fig.legend(handles=legend_handles(), loc="lower center", ncol=5, fontsize=7 + xl, bbox_to_anchor=(0.5, -0.04))
    fig.suptitle(f"I01 at SF{sf}, {REGIME_TITLE[regime.split('-')[0]]} ({regime}): per query, the median over params 1-5 of the plan wall, every timed store"
                 + (f"\n{note}" if note else ""), fontsize=9 + xl)
    _save(fig, out, f"w16_{regime}_per_query")


def per_group(regime, A, out, sf, note, xl, skip=()):
    lat = drop_skipped({stem_of(r["arm"]): r for r in read_csv(os.path.join(A, "w16_latency.csv"))}, skip)
    stems = store_order(set(lat))
    ref_ops = {cls: float(lat[REF][f"counts_ops_median{'' if cls == 'I01' else '_' + cls}"]) for cls in ("IC", "IS", "I01")} if REF in lat else {}
    groups = ("IC", "IS", "IU")
    with_iu = any(float(r["IU_median"] or 0) for r in lat.values())
    if FORMATS_FIRST:
        groups = groups if with_iu else ("IC", "IS")            # no empty panel: the caption says what was not timed
        for r in lat.values():
            r["label"] = plain_label(r["label"])
    else:
        stems.sort(key=lambda s: -float(lat[s]["W17_median"]))      # ordered by the I02 total, the slowest first
    fig, axes = plt.subplots(1, len(groups), figsize=((6.0 if FORMATS_FIRST else 5.2) * len(groups), 5.4), constrained_layout=True)
    for ax, cls in zip(axes, groups):
        vals = [float(lat[s][f"{cls}_median"]) / 1e9 for s in stems]
        if cls == "IU" and all(v == 0 for v in vals):
            ax.text(0.5, 0.5, "IU not measured yet\n(the writes stage)", ha="center", va="center", transform=ax.transAxes, fontsize=9 + xl, color="0.4")
            ax.set_title(GROUP_TITLE[cls], fontsize=9 + xl); ax.set_xticks([]); ax.set_yticks([])
            continue
        for i, (s, v) in enumerate(zip(stems, vals)):
            c, h, kw = style_of(s)
            ax.bar(i, v, 0.8, color=c, hatch=h, **kw)
        ax.set_xticks(range(len(stems)), [lat[s]["label"] for s in stems], rotation=60, ha="right", fontsize=6 + xl)
        ax.set_ylabel(f"{cls} total, seconds per 100,000 updates", fontsize=8 + xl)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: number_to_human(v) + " s"))
        ax.set_ylim(0, max(vals) * 1.12)
        ax.set_title(GROUP_TITLE[cls], fontsize=9 + xl)
        if FORMATS_FIRST:
            partitions(ax, stems)
        if cls in ref_ops and ref_ops[cls]:                     # the counts' tick: the class's weighted ops over the reference cell's
            a2 = ax.twinx()
            key = f"counts_ops_median_{cls}"
            ratios = [float(lat[s][key]) / ref_ops[cls] if lat[s].get(key) else float("nan") for s in stems]
            a2.plot(range(len(stems)), ratios, "k_", ms=14, mew=1.6, label="counts: weighted ops / the reference cell's")
            a2.set_ylim(0, max(r for r in ratios if r == r) * 1.12)
            a2.set_ylabel("counts: weighted ops / " + RN + "'s (tick)", fontsize=7 + xl)
            a2.tick_params(axis="y", labelsize=6 + xl)
    hs = legend_handles() + [matplotlib.lines.Line2D([], [], color="k", marker="_", ms=14, mew=1.6, ls="", label="counts: weighted ops / the reference cell's (right axis)")]
    if FORMATS_FIRST:
        fig.legend(handles=hs, loc="lower center", ncol=3, fontsize=7 + xl, bbox_to_anchor=(0.5, -0.2))
        fig.suptitle(f"{WORKLOAD[with_iu]} at SF{sf}, {regime.split('-')[0]}: time per query group and store", fontsize=9 + xl)
        fig.text(0.5, -0.215, "A group's total = the sum over its queries of (runs per 100,000 updates x the query's time); the tick = the same sum of weighted "
                 f"ops (G + 1.1 S + 0.01 N, median over params).\n" + textwrap.fill(note, 175), ha="center", va="top", fontsize=6.5 + xl)
        return _save(fig, out, f"w16_{regime}_per_group")
    fig.legend(handles=hs, loc="lower center", ncol=5, fontsize=7 + xl, bbox_to_anchor=(0.5, -0.17))
    fig.suptitle(f"I01 at SF{sf}, {REGIME_TITLE[regime.split('-')[0]]} ({regime}): per query group, every timed store "
                 "(the class total = sum over its queries of the I01 count x the median over params of the plan wall; stores ordered by their I02 total)"
                 + (f"\n{note}" if note else ""), fontsize=9 + xl, wrap=True)
    _save(fig, out, f"w16_{regime}_per_group")


def by_mix(regime, A, out, sf, note, xl, picks):
    lat = {stem_of(r["arm"]): r for r in read_csv(os.path.join(A, "w16_latency.csv"))}
    words = {(w["B"], w["other_role"], w["class"]): w for w in read_csv(os.path.join(A, "w16_words.csv"))}
    eng = read_csv(os.path.join(A, "engine_arms.csv")) if os.path.exists(os.path.join(A, "engine_arms.csv")) else []
    no_iu = all(float(r["IU_median"] or 0) == 0 for r in lat.values())
    cls = "I02" if no_iu else "I01"
    Bs = list(picks["picks"])
    fig, ax = plt.subplots(figsize=(2.3 * len(Bs) + 3.2 * max(1, len(eng) // 4), 5.6), constrained_layout=True)
    x, ticks, labs = 0, [], []
    for B in Bs:
        rec = picks["picks"][B]
        trio = (("mix", rec["spec"][:-5]), ("best single within B", rec["best_single_within_B"]["cell"]),
                ("reference", picks["reference"]))     # B's unit as the picks file was picked (Co-Sp in a picks file in Co-Sp units)
        for j, (role, st) in enumerate(trio):
            if st in lat:
                c, h, kw = style_of(st)
                ax.bar(x + j, float(lat[st][f"{cls}_median"]) / 1e9, 0.8, color=c, hatch=h, **kw)
                labs.append(lat[st]["label"]); ticks.append(x + j)
        w = words.get((B, "best_single", cls))
        word = w["word"] if w else "no word"
        ax.annotate(f"B = {B}\n{word.replace('no measurable difference', 'no measurable diff.')}", (x + 1, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(0, -2), textcoords="offset points", ha="center", va="top", fontsize=6 + xl, color="0.25")
        x += 4
    for r in [r for r in eng if r.get("class") == "I02"]:   # the whole I02 (the same class as the groups), the engine word beside it
        if not r.get("cell_ns") or not r.get("format_ns"):
            continue
        for j, (st, v) in enumerate(((r["cell"], float(r["cell_ns"])), (r["format"], float(r["format_ns"])))):
            c, h, kw = style_of(st)
            ax.bar(x + j, v / 1e9, 0.8, color=c, hatch=h, **kw)
            labs.append(lat[st]["label"] if st in lat else (r["cell_label"] if j == 0 else r["format_label"])); ticks.append(x + j)
        ax.annotate(f"{r['format_label']}\n{r['word'].replace('no measurable difference', 'no measurable diff.')}", (x + 0.5, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(0, -2), textcoords="offset points", ha="center", va="top", fontsize=6 + xl, color="0.25")
        x += 3
    ax.set_xticks(ticks, labs, rotation=60, ha="right", fontsize=6 + xl)
    ax.set_ylabel(f"{cls} total, seconds per 100,000 updates", fontsize=8 + xl)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: number_to_human(v) + " s"))
    ax.set_ylim(0, ax.get_ylim()[1] * 1.18)
    fig.legend(handles=legend_handles(), loc="lower center", ncol=5, fontsize=7 + xl, bbox_to_anchor=(0.5, -0.16))
    fig.suptitle(f"I01 at SF{sf}, {REGIME_TITLE[regime.split('-')[0]]} ({regime}): per byte limit the picked mix, its best single and the reference, "
                 "with the scorer's word; the engine formats beside their cells" + (" (reads only: IU not measured yet)" if no_iu else "")
                 + (f"\n{note}" if note else ""), fontsize=9 + xl, wrap=True)
    _save(fig, out, f"w16_{regime}_by_mix")


def _save(fig, out, name):
    os.makedirs(out, exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(out, f"{name}.{ext}"), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"-> {out}/{name}.png")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--root", required=True, help="the latency root: <root>/analysis/<regime>/ (the scorer's files)")
    ap.add_argument("--regime", default=None, help="one regime dir name (default: every analysis dir with w16_per_store_query.csv)")
    ap.add_argument("--picks", default=os.path.join(EXP, "inputs", "specs", "mixes_w16-picks.json"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--note", default="", help="a line under the title (a first pass: which regimes are not measured yet)")
    ap.add_argument("--extralarge", type=int, default=2)
    ap.add_argument("--order", default="grid", choices=("grid", "formats-first"), help="grid: the cells in grid order, the mixes, the "
                    "formats (per group: by the I02 total). formats-first: the formats, the cells by properties setting, the mixes")
    ap.add_argument("--skip", default="", help="store stems to leave out, comma list (e.g. mixes over their byte limit on these counts)")
    a = ap.parse_args()
    global FORMATS_FIRST
    FORMATS_FIRST = a.order == "formats-first"
    skip = set(filter(None, a.skip.split(",")))
    import json
    picks = json.load(open(a.picks))
    apply_house_style(extralarge=a.extralarge)
    base = os.path.join(a.root, "analysis")
    regimes = [a.regime] if a.regime else [d for d in sorted(os.listdir(base)) if os.path.exists(os.path.join(base, d, "w16_per_store_query.csv"))]
    if not regimes:
        raise SystemExit(f"no analysis dir with w16_per_store_query.csv under {base} (run the scorer with --all-arms)")
    out = figure_dir(os.path.join(base, "plots"), a.out)
    for regime in regimes:
        A = os.path.join(base, regime)
        per_query(regime, A, out, a.sf, a.note, a.extralarge, skip)
        per_group(regime, A, out, a.sf, a.note, a.extralarge, skip)
        if not FORMATS_FIRST:
            by_mix(regime, A, out, a.sf, a.note, a.extralarge, picks)


if __name__ == "__main__":
    main()
