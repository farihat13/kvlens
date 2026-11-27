"""Two count sources' cost rows compared over the layouts both hold (kvcost predict-accuracy --count-source A --against
B reads each side's layout-costs place): does A (e.g. predicted) name B's (e.g. measured) cheapest layout, and where
does its pick sit in B's spread.

The rows are joined on the layout id (kvevaluator.layouts.cell_id; an id in an older spelling is refused) and shown
by A's short name. A layout on one side only, or missing a kept query on either side, is left out and named. A's and
B's frequencies must agree. Per model, per query and for the workload:
  picks.csv    one row per (what, model): A's pick, B's best, hit, x over best, B's rank of the pick, the runner-up
               gaps, top-5 overlap, Spearman, the spread (Sp-Co, Co-Co, median, worst, gap share), and the layouts A
               puts within cost_models.WITHIN of its cheapest (within_k) and whether they hold B's best
  layouts.csv  per (what, model, layout) of the workload rows: A's and B's totals, A / B, both ranks, B over B's best
A's costs are compared at 12 significant digits, B's exact (the rule the layout report, kvcost.predict_accuracy.layouts, uses). Opens no store,
runs no plan.
"""
import csv
import math

from kvcost.layout_cost import compare as CP
from kvcost.layout_cost import cost_models as CM
from kvevaluator import layouts as L

WORKLOAD = "workload"


def names_by_id(rows, side):
    """{layout id: its short name} of a table; an id in the old spelling, or one id given two names, is refused"""
    names = {}
    for r in rows:
        c, name = r["layout"], r.get("name") or r["layout"]
        try:
            L.check_no_old_form(c, f"predict-accuracy: {side}'s layout")
        except ValueError as e:
            raise SystemExit(str(e))
        if names.setdefault(c, name) != name:
            raise SystemExit(f"predict-accuracy: {side} names the layout {c} two ways ({names[c]}, {name})")
    return names


def costs(rows, model, keep_q, keep_f, keep):
    """({(layout, family, query): cost under model}, {(family, query): frequency}, [(family, query) in table order]);
    a row whose layout is not in keep is skipped"""
    cost, freq, order = {}, {}, []
    for r in rows:
        if (keep_q and r["query"] not in keep_q) or (keep_f and r["family"] not in keep_f) or r[model] in ("", None):
            continue
        if r["layout"] not in keep:
            continue
        k = (r["family"], r["query"])
        cost[(r["layout"], *k)] = float(r[model])
        if k not in freq:
            order.append(k)
        freq[k] = float(r["frequency"])
    return cost, freq, order


def compare_tables(a_rows, b_rows, models, keep_q=None, keep_f=None):
    """(picks rows, layouts rows, {side: [names on that side only]}, [names left out: a kept query missing]) of A
    against B (see the module doc)"""
    na, nb = names_by_id(a_rows, "A"), names_by_id(b_rows, "B")
    one_side = {"A": sorted(na[c] for c in set(na) - set(nb)), "B": sorted(nb[c] for c in set(nb) - set(na))}
    names = {c: na[c] for c in na if c in nb}
    picks, per_layout, left = [], [], set()
    for model in models:
        ca, fa, order = costs(a_rows, model, keep_q, keep_f, names)
        cb, fb, _ = costs(b_rows, model, keep_q, keep_f, names)
        items = [k for k in order if k in fb]
        if not items:
            raise SystemExit(f"predict-accuracy: no query in both tables under {model} (check --queries)")
        bad = [k for k in items if fa[k] != fb[k]]
        if bad:
            raise SystemExit(f"predict-accuracy: the tables give {bad[0]} other frequencies ({fa[bad[0]]} vs "
                             f"{fb[bad[0]]}): they price different workloads")
        complete = sorted(c for c in names if all((c, *k) in ca and (c, *k) in cb for k in items))
        left |= {names[c] for c in names if c not in complete}
        if len(complete) < 2:
            raise SystemExit(f"predict-accuracy: {len(complete)} layout(s) priced on every query in both tables under {model}; "
                             f"the pick, x over the best and Spearman compare at least 2 (cost both sides on 2 or more "
                             f"layouts)")

        def short(c):
            return names[c]
        a_q = CP.naming_side({(c, k): ca[(c, *k)] for c in complete for k in items})
        b_q = {(c, k): cb[(c, *k)] for c in complete for k in items}
        for k in items:
            a = {c: a_q[(c, k)] for c in complete}
            b = {c: b_q[(c, k)] for c in complete}
            picks.append({**CP.pick_row(k[1], model, a, b, short), "kind": "query", **within(a, b)})
        a = CP.weighted_totals(a_q, complete, items, fa, naming=True)
        b = CP.weighted_totals(b_q, complete, items, fb, naming=False)
        picks.append({**CP.pick_row(WORKLOAD, model, a, b, short), "kind": WORKLOAD, **within(a, b)})
        per_layout += CP.layout_rows(WORKLOAD, model, a, b, short)
    return picks, per_layout, one_side, sorted(left)


def within(a, b):
    """the layouts A puts within cost_models.WITHIN of its cheapest, and whether they hold B's best"""
    near = [c for c in a if a[c] <= min(a.values()) * (1 + CM.WITHIN)]
    best = min(b.values())
    return {"within_k": len(near), "within_holds_best": int(any(b[c] == best for c in near))}


def shown(v, fmt, unit=""):
    """a number as the summary prints it, with its unit; n/a when it is not defined (no Sp-Co among the layouts, say)"""
    return "n/a" if isinstance(v, float) and math.isnan(v) else format(v, fmt) + unit


def write(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, list(rows[0]), lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v) for k, v in r.items()})


def summary_lines(picks, model, a_name="A", b_name="B"):
    """the workload rows of `model` in plain words: A names a layout (its cheapest), B judges it"""
    out = []
    for p in picks:
        if p["kind"] != WORKLOAD or p["currency"] != model:
            continue
        nq = sum(1 for x in picks if x["kind"] == "query" and x["currency"] == p["currency"])
        out.append(f"{p['currency']}, {nq} {'query' if nq == 1 else 'queries'} on {p['layouts']} layouts. {a_name}'s "
                   f"cheapest layout is {p['named_short']}; on {b_name} it costs "
                   f"{shown(p['named_measured_over_best'], '.3f', 'x')} {b_name}'s cheapest ({p['measured_best_short']}) "
                   f"and ranks {p['measured_rank_of_named']} of {p['layouts']}. On {b_name}, against its cheapest: Sp-Co "
                   f"costs {shown(p['sp_co_over_best'], '.2f', 'x')}, Co-Co {shown(p['co_co_over_best'], '.2f', 'x')}, "
                   f"the median layout {shown(p['median_over_best'], '.2f', 'x')}, the worst ({p['worst']}) "
                   f"{shown(p['worst_over_best'], '.2f', 'x')}. {a_name}'s pick removes {shown(p['gap_share'], '.0%')} of "
                   f"Sp-Co's extra cost over {b_name}'s cheapest. The two rankings agree to Spearman "
                   f"{shown(p['spearman'], '.3f')}. Within {CM.WITHIN:.0%} of its cheapest, {a_name} puts {p['within_k']} "
                   f"{'layout' if p['within_k'] == 1 else 'layouts'}; {b_name}'s cheapest is "
                   f"{'among' if p['within_holds_best'] else 'not among'} them.")
    return out
