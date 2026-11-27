"""kvcost plot-costs' figures, from cost-layouts' rows only (query_costs.csv, its half view): per query, each layout's
cost over a baseline layout (three models); and the totals, once each, per layout. Counts are deterministic: no error
bars."""
import math
import os
import re

import matplotlib
from kvevaluator.paths import ROOT
matplotlib.use("Agg")
import matplotlib.pyplot as plt

EXP = str(ROOT)
from kvcost.plotting.style import apply_house_style, cell_bar_kw, cell_order, format_color
from kvcost.layout_cost import cost_models as CM
from kvevaluator import weights as W

PER_FILE = 30          # queries per per-query figure (JOB's 113 split into several files)
MODEL_LABEL = {"wops": "wops", "bytes": "bytes", "wops_bytes4k": "ops + bytes / 4 KB"}


ALL = "all families"


class View:
    """one layout's rows from cost-layouts' query_costs.csv (a uniform layout, or a mix): per query its one-run cost in
    the three models, and its totals once each, per family and over all"""

    def __init__(self, layout, rows, how, name=None):
        from kvevaluator import layouts as L
        self.cell = layout if L.is_cell(layout) else None
        self.label = L.name_of(layout) if self.cell else (name or layout)
        self.how = how                  # "uniform" or "mix"
        self.rows = {}
        self.weights = sorted({r.get("weights") or W.text(W.DEFAULT) for r in rows})   # the vector it was costed under
        for r in rows:
            w, b = float(r["wops"]), float(r["bytes"])
            self.rows[(r["family"], r["query"])] = {"wops": w, "bytes": b, "wops_bytes4k": CM.wops_bytes4k(w, b)}
        fams = sorted({k[0] for k in self.rows})
        self.totals = [{"scope": scope, "weighting": "once each",
                        **{m: sum(v[m] for k, v in self.rows.items() if scope == ALL or k[0] == scope) for m in CM.MODELS}}
                       for scope in fams + [ALL]]

    @property
    def name(self):
        return self.label

    def bar_kw(self):
        if self.cell and self.cell.startswith("props_"):
            kw = dict(cell_bar_kw(self.cell))
        else:
            kw = {"color": mix_color(self.label), "edgecolor": "white", "hatch": "oo"}
        return kw


def mix_color(label):
    """format_color, whose note for a name with no house colour (every mix's: a user names it) is said once by kvcost
    as harmless instead of as style.py's request to add one; the colour is the same"""
    import contextlib
    import io
    note = io.StringIO()
    with contextlib.redirect_stderr(note):
        color = format_color(label)
    if note.getvalue():
        print(f"  {label}: no house colour (a mix), drawn in a stable fallback colour (harmless)")
    return color


def order(views):
    """cells in the house order (adjacency outer), then mixes by name"""
    cells = cell_order([v.cell for v in views if v.cell and v.cell.startswith("props_")])
    rank = {c: i for i, c in enumerate(cells)}
    return sorted(views, key=lambda v: (0, rank[v.cell]) if v.cell in rank else (1, v.label))


def _ratio(v, ref, k, model):
    x, r = v.rows.get(k), ref.rows.get(k)
    if not x or not r:
        return None
    a, b = float(x[model]), float(r[model])
    return a / b if a > 0 and b > 0 else None


def per_query(views, ref, out_dir, source):
    """one PNG per (family, chunk of PER_FILE queries): rows = the three models, bars = the views, y = view / ref (log)"""
    paths = []
    keys = sorted(ref.rows, key=lambda k: (k[0], _qkey(k[1])))
    for fam, group, split in _groups(keys):
        qs = [k for k in keys if k[0] == fam and _prefix(k[1]) in group]
        for part in range(0, len(qs), PER_FILE):
            chunk = qs[part:part + PER_FILE]
            n = len(views)
            width = max(8.0, 0.12 * n * len(chunk) + 2.5)
            fig, axes = plt.subplots(3, 1, figsize=(width, 7.2), sharex=True)
            w = 0.8 / n
            skipped = 0
            for ax, model in zip(axes, CM.MODELS):
                for i, v in enumerate(views):
                    xs, ys = [], []
                    for j, k in enumerate(chunk):
                        r = _ratio(v, ref, k, model)
                        if r is None:
                            skipped += 1
                            continue
                        xs.append(j - 0.4 + w * (i + 0.5))
                        ys.append(r)
                    ax.bar(xs, ys, width=w, label=v.name if model == CM.MODELS[0] else None, **v.bar_kw())
                ax.axhline(1.0, color="black", linewidth=0.8)
                ax.set_yscale("log")
                _ratio_axis(ax)
                ax.set_ylabel(f"{MODEL_LABEL[model]}\n/ {ref.label}")
            axes[-1].set_xticks(range(len(chunk)))
            axes[-1].set_xticklabels([k[1] for k in chunk], rotation=90)
            suffix = f", queries {part + 1}-{part + len(chunk)}" if len(qs) > PER_FILE else ""
            what = f"{fam} {next(iter(group)).upper()}" if split else fam
            axes[0].set_title(f"Which layout is cheaper per query? {what}{suffix}, over {ref.name}")
            ncol = min(n, 3)
            # the legend under the drawn x labels (long query names reach far down), the caption under the legend
            fig.canvas.draw()
            r, inv = fig.canvas.get_renderer(), fig.transFigure.inverted()
            low = axes[-1].get_tightbbox(r).transformed(inv).y0
            leg = fig.legend(loc="upper center", bbox_to_anchor=(0.5, low - 0.01), ncol=ncol, frameon=False)
            fig.canvas.draw()
            cap_y = leg.get_window_extent(fig.canvas.get_renderer()).transformed(inv).y0 - 0.01
            note = (f"Per query, the mean over its params of each model, divided by {ref.name}'s; log scale. "
                    f"Models: wops = {formulas(views)['wops']}; bytes = {formulas(views)['bytes']}. Counts are deterministic: "
                    f"no error bars. {source}")
            fig.text(0.0, cap_y, _wrap(note, int(width * 11)), fontsize=10, va="top")
            tag = fam.replace(" ", "_") + (f"_{next(iter(group))}" if split else "")
            name = f"per_query_{tag}" + (f"_{part // PER_FILE + 1}" if len(qs) > PER_FILE else "") + ".png"
            p = os.path.join(out_dir, name)
            fig.savefig(p, dpi=150, bbox_inches="tight")
            plt.close(fig)
            paths.append(p)
    return paths


def _prefix(q):
    """'ic' of 'ic12': the letters of a query named letters + number, else None"""
    m = re.fullmatch(r"([a-z]+)(\d+)", q)
    return m.group(1) if m else None


def _qkey(q):
    m = re.fullmatch(r"([a-z]+)(\d+)", q)
    return (m.group(1), int(m.group(2)), "") if m else ("", 0, q)


def _groups(keys):
    """(family, prefixes, split) per figure: a family whose queries are all letters + number with more than one prefix (LDBC's
    IC and IS) is split by prefix, as the house rule keeps IC and IS in separate figures; any other family is one group"""
    out = []
    for fam in sorted({k[0] for k in keys}):
        pre = {_prefix(k[1]) for k in keys if k[0] == fam}
        if None in pre or len(pre) == 1:
            out.append((fam, pre, False))
        else:
            out += [(fam, {p}, True) for p in sorted(pre)]
    return out


def totals(views, ref, out_dir, source):
    """one PNG: per model, per family (once each) and over all families, each view's total over the ref's"""
    scopes = [t["scope"] for t in ref.totals if t["weighting"] == "once each"]
    if len(scopes) == 2:                  # one family: its total and "all families" are the same bars
        scopes = scopes[:1]
    fig, axes = plt.subplots(1, 3, figsize=(max(9.0, 0.5 * len(views) * len(scopes) + 3), 3.6))
    n, w = len(views), 0.8 / len(views)
    for ax, model in zip(axes, CM.MODELS):
        reft = {t["scope"]: float(t[model]) for t in ref.totals if t["weighting"] == "once each"}
        ymax, ymin = 0.0, 1.0
        for i, v in enumerate(views):
            vt = {t["scope"]: float(t[model]) for t in v.totals if t["weighting"] == "once each"}
            xs = [j - 0.4 + w * (i + 0.5) for j, s in enumerate(scopes) if reft.get(s) and s in vt]
            ys = [vt[s] / reft[s] for s in scopes if reft.get(s) and s in vt]
            ys_pos = [(x, y) for x, y in zip(xs, ys) if y > 0]           # log scale: a zero total has no bar
            ax.bar([x for x, _ in ys_pos], [y for _, y in ys_pos], width=w,
                   label=v.name if model == CM.MODELS[0] else None, **v.bar_kw())
            for x, y in ys_pos:
                ax.text(x, y * 1.08, f"{y:.2g}x", ha="center", va="bottom", fontsize=7, rotation=90)
            ymax = max([ymax] + [y for _, y in ys_pos])
            ymin = min([ymin] + [y for _, y in ys_pos])
        ax.axhline(1.0, color="black", linewidth=0.8)
        ax.set_yscale("log")                                 # the totals span more than 100x across layouts
        ax.set_ylim(min(ymin, 1.0) / 2, max(ymax, 1.0) * 6)
        _ratio_axis(ax)
        ax.set_xticks(range(len(scopes)))
        ax.set_xticklabels(scopes, rotation=30, ha="right")
        ax.set_title(MODEL_LABEL[model])
    axes[0].set_ylabel(f"total / {ref.label}")
    fig.suptitle(f"Each layout's total over {ref.name}, every query of the workload once", y=1.08)
    fig.legend(loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=min(n, 4), frameon=False)
    fig.text(0.0, -0.24 - 0.05 * math.ceil(n / 4), _wrap(f"Once each: every query of the workload once, the mean over its params; "
             f"log scale. Models: wops = {formulas(views)['wops']}; bytes = {formulas(views)['bytes']}. Counts are deterministic: "
             f"no error bars. {source}", 120), fontsize=9, va="top")
    p = os.path.join(out_dir, "totals.png")
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return p


def _ratio_axis(ax):
    """a log ratio axis labelled on 1 / 2 / 5 of each decade ('0.2x', '1x', '50x'), minors blank (a narrow range
    shows no major tick otherwise)"""
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter
    lo, hi = ax.get_ylim()
    wide = math.log10(hi / lo) > 2.5 if lo > 0 else True       # a wide range: decades only, so labels do not crowd
    ax.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0,) if wide else (1.0, 2.0, 5.0), numticks=12))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:g}x"))
    ax.yaxis.set_minor_formatter(NullFormatter())


def _wrap(text, width):
    import textwrap
    return "\n".join(textwrap.wrap(text, width))


def formulas(views):
    """the models' formulas under the one op-weight vector the views were costed under (cost_table's weights column)"""
    got = sorted({x for v in (views.values() if isinstance(views, dict) else views) for x in v.weights})
    if len(got) != 1:
        raise SystemExit(f"plot-costs: the rows were costed under {len(got)} weight vectors ({', '.join(got)})")
    return CM.formulas(W.parse(got[0]))


def read_views(rows_dir):
    """{layout: View} of a cost-layouts place: every layout and mix in its query_costs.csv (the half view), each mix
    named as layouts.csv names it"""
    from kvcost.layout_cost import layout_costs as LC
    names = {r["layout"]: r["name"] for r in LC.read_layouts(rows_dir)}
    by = {}
    for r in LC.read_query_costs(rows_dir)[1].values():
        for lay, row in r.items():
            if row["wops"] != "":
                by.setdefault(lay, []).append(row)
    return {lay: View(lay, rows, rows[0]["kind"], names.get(lay)) for lay, rows in by.items()}


def draw(views, ref, out_dir, source):
    """the per-query figures and the totals figure of `views` over `ref` (Views); the reference is the 1x line only"""
    apply_house_style(extralarge=1)
    views = order([v for v in views if v is not ref])
    if not views:
        raise SystemExit("no layout to draw beside the reference")
    os.makedirs(out_dir, exist_ok=True)
    return per_query(views, ref, out_dir, source) + [totals(views, ref, out_dir, source)]
