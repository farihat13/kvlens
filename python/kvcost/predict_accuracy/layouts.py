"""The cheapest of a predicted capture's layouts, named from statistics, against the measured one:
`kvcost predict-accuracy` on the 49 for a read-only workload, and `python -m kvcost.predict_accuracy.layouts` with a
check workload or an earlier table. Opens no store; runs no plan.

The workloads are named by flags, never here: --dataset, --cost-workload (the workload predicted and compared),
--check-workload (one measured whole whose composed totals are checked first, with --check-reference), --queries (a
subset of the cost workload's queries). The layouts are every cell of the predicted capture, ordered Split, Condensed,
then Chunked by chunk size.

Predicted: level predicted (statistics only), read through kvcost.predict_accuracy.predicted; per (layout, query) the
plan rule's pick over the mean of the params. A body with a step not estimated is left out (counted); a query with no
body estimated on any layout is left out of both sides and named.
Measured: the layout composed per part from a grid capture (--grid; compose.query_pick: the plan rule on the held-out
params, the cost on the reported ones), at the predicted capture's param set. Checked first, and fatal: the check
workload's composed totals equal the reference's rows (a kvcost cost-layouts place: its layouts.csv, the composition's
chunk view). Or, with --measured-table D in place of --grid, the measured
side an earlier run wrote (D/by_query.csv, D/by_param.csv); every (layout, query) of the cost workload must
be in it, at the workload's frequency.

Two currencies, the plan rule's first: wops_bytes4k = wops + bytes read / 4096, and wops (bytes read = hit keys and
values plus miss keys, all 7 byte lanes). The cost workload's total is each query's mean over the params at its
frequency. The baseline is the best constant answer (one layout for every query). The spread is
kvcost.layout_cost.compare's: the pick, Sp-Co, Co-Co, the median layout and the worst, each x over the measured best,
and the gap share = (Sp-Co - pick) / (Sp-Co - best).

Writes to <out>/layout_choice/: by_query.csv, by_param.csv, picks.csv, picks_by_param.csv, constant_layouts.csv,
totals.csv, tie_rule.csv and summary.md.
"""
import csv
import math
import os
import sys

from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import compare as CP
from kvevaluator.output_tree import Capture
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import compose
from kvcost.layout_cost.settings import parse_sizes
from kvcost.layout_search import search as cost_cache
from kvcost.predict_accuracy import predicted as PD
from kvevaluator import layouts as L

GRID_CHUNK = "64k"        # the grid's own Chunked setting (inputs/specs/grid): `chunked` with no size
KINDS = {"split": (0, "Sp"), "condensed": (1, "Co"), "chunked": (2, "Ch")}
CURRENCIES = {"wops_bytes4k": lambda w, b: CM.wops_bytes4k(w, b), "wops": lambda w, b: w}   # the plan rule's form first
WITHIN = (0.03, 0.10)


def _size_kb(size):
    return int(size[:-1])


def setting_key(setting):
    """a setting's sort key: Split, Condensed, then Chunked by chunk size"""
    kind, _, size = setting.partition("_c")
    return KINDS[kind][0], _size_kb(size or GRID_CHUNK) if kind == "chunked" else 0


def short_setting(setting):
    kind, _, size = setting.partition("_c")
    return KINDS[kind][1] + ((size or GRID_CHUNK) if kind == "chunked" else "")


def settings_of(cell):
    """(props setting, adjacency setting) of a uniform layout's cell id (kvevaluator.layouts.cell_settings)"""
    try:
        return L.cell_settings(cell)
    except ValueError as e:
        raise SystemExit(f"predict-accuracy: {e} (a uniform layout is props_<setting>_adj_<setting>)")


def short(cell):
    p, a = settings_of(cell)
    return f"{short_setting(p)}-{short_setting(a)}"


def order_cells(cells):
    """the layouts by setting: props first, then adjacency"""
    return sorted(cells, key=lambda c: tuple(setting_key(s) for s in settings_of(c)))


def chunk_sizes(cells):
    """the chunk sizes the layouts name besides the grid's own"""
    sizes = {s.partition("_c")[2] for c in cells for s in settings_of(c) if s.startswith("chunked_c")}
    return sorted(sizes - {GRID_CHUNK}, key=_size_kb)


# ---- measured -----------------------------------------------------------------------------------------------------------

def load_measured(grid, cells, families, param_set, dataset, sf, rule=None):
    """`dataset`'s grid (a place dir: <workload>/<params> below it): its captures of `families` (the workloads' own), at
    `param_set`, with the ChK-ChK cells of the chunk sizes the layouts name, in the same place (output_tree.layout_capture);
    rule: the PlanRule (default the default rule; kvcost predict-accuracy's --pick-on, picks.pick_on_rule)"""
    cost_cache.set_cache(None)
    sizes = parse_sizes(chunk_sizes(cells), grid)
    # an accuracy check: the estimate against the bytes its run found, so both sides read the measured chunk view
    composer = compose.Composer(criterion=None, sizes=sizes, plan_rule=rule or compose.PlanRule(),
                              families=families, param_set=param_set, chunk_view="measured")
    return compose.load(grid, False, dataset, composer=composer)


def load_measured_table(table, cells, w):
    """the measured side of an earlier run, from its layout_choice dir `table`: ({(cell, query): (body,
    wops, bytes read)}, {(cell, query, param): (wops, bytes read)}), as measured_by_query returns them. Refused: a
    (cell, query) of the workload the table lacks, or a weight that is not the workload's frequency"""
    rows = {}
    with open(os.path.join(table, "by_query.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["cell"], r["query"])] = r
    out, bad = {}, []
    for _, q, often, _ in w["items"]:
        for c in cells:
            r = rows.get((c, q))
            if r is None:
                bad.append(f"{c} {q}: not in the table")
            elif float(r["weight"]) != float(often):
                bad.append(f"{c} {q}: weight {r['weight']}, the workload's {often}")
            elif r["measured_body"]:
                out[(c, q)] = (r["measured_body"], float(r["measured_wops"]), float(r["measured_bytes"]))
    if bad:
        raise SystemExit(f"predict-accuracy --measured-table {table}: {len(bad)} pairs refused, e.g. "
                         f"{'; '.join(bad[:3])} -- REFUSED")
    per_param = {}
    with open(os.path.join(table, "by_param.csv"), newline="") as f:
        for r in csv.DictReader(f):
            if (r["cell"], r["query"]) in out and r["measured_wops"] != "":
                per_param[(r["cell"], r["query"], r["param"])] = (float(r["measured_wops"]), float(r["measured_bytes"]))
    return out, per_param


def scale_label(dataset, sf):
    """a scale as a reader says it: SF3 (LDBC), imdb full (a dataset with its own data tag)"""
    from kvevaluator import datasets
    return f"{dataset} {sf}" if datasets.get(dataset).own_axis else f"SF{sf}"


def assign_of(cell, units):
    p, a = settings_of(cell)
    return {u: (p if u[0] == "props" else a) for u in units}


def measured_by_query(data, w, cells):
    """({(cell, query): (body, wops, bytes read)}, {(cell, query, param): (wops, bytes read)}) on the workload's items; a
    param's cost is the query's plan's: with a held-out set the rule picks on it, never on the param priced (checked);
    without one (BI, picked in-sample over the params) one param alone may prefer another plan, so it is not re-picked"""
    units = compose.workload_units(w, data)
    out, per_param = {}, {}
    for fam, q, _, params in w["items"]:
        d = data[(fam, q)]
        qunits = {u for per in d["cost"].values() for u in per if u is not None}
        ps = compose.params_of(d, params)
        in_sample = not d.get("cost_h")
        for c in cells:
            best, tots, _ = compose.query_pick(d, ps, assign_of(c, units), qunits)
            if best is None:
                continue
            v = tots[best]
            out[(c, q)] = (best, v["read_ops"], v["read_bytes"])
            for p in ps:
                b1, t1, _ = compose.query_pick(d, [p], assign_of(c, units), qunits)
                if in_sample and t1 and best in t1:
                    b1 = best
                if b1 != best:
                    raise SystemExit(f"predict-accuracy: {c} {q} param {p}: the plan {b1} is not the query's {best}")
                per_param[(c, q, p)] = (t1[b1]["read_ops"], t1[b1]["read_bytes"])
            mean = sum(per_param[(c, q, p)][0] for p in ps) / len(ps)
            if abs(mean - v["read_ops"]) > 1e-6 * max(1.0, v["read_ops"]):
                raise SystemExit(f"predict-accuracy: {c} {q}: the params' mean {mean} is not the query's {v['read_ops']}")
    return out, per_param


def reference_rows(reference, view):
    """{layout: its row} of the uniform layouts in a kvcost cost-layouts place (a layout-costs dir, or its layouts.csv),
    in one chunk view"""
    from kvcost.layout_cost import layout_costs as LC
    place = reference if os.path.isdir(reference) else os.path.dirname(os.path.abspath(reference))
    rows = LC.read_layouts(place, view, kind="uniform")
    if not rows:
        raise SystemExit(f"predict-accuracy: --check-reference {reference} holds no uniform layout in the {view} chunk "
                         f"view (kvcost cost-layouts --dataset D=S --workload W <the 49> writes it) -- REFUSED")
    return {r["layout"]: r for r in rows}


def check_totals(data, w, cells, reference):
    """the composed totals of the workload `w` on every layout against the reference's rows (None: not compared):
    (layouts compared, [differences], totals). A reference in another chunk view than the composition's is refused: its
    write bytes differ on every layout with a Chunked part"""
    units = compose.workload_units(w, data)
    snap = None
    if reference is not None:
        snap = reference_rows(reference, data.composer.chunk_view)
        lacking = sorted(set(snap) - set(cells))
        if lacking:
            raise SystemExit(f"predict-accuracy: the predicted capture lacks {len(lacking)} of the reference's "
                             f"{len(snap)} layouts, e.g. {lacking[0]}: a partial capture would name the best of "
                             f"fewer layouts (kvcost predict) -- REFUSED")
    bad, totals = [], {}
    for c in cells:
        t = compose.cost_mix(w, data, assign_of(c, units))
        cur = compose.currencies(t)
        totals[c] = cur
        if snap is None:
            continue
        s = snap.get(c)
        if s is None:
            bad.append(f"{c}: no row in the reference")
            continue
        for k, mine in (("total_ops", round(cur["ops"], 3)), ("total_bytes", round(cur["bytes"], 1))):
            if abs(float(s[k]) - mine) > 1e-6 * max(1.0, abs(mine)):
                bad.append(f"{c} {k}: composed {mine} vs reference {s[k]}")
    return len(cells), bad, totals


# ---- predicted ----------------------------------------------------------------------------------------------------------

def load_predicted(cap_dir, cells, queries, params):
    """({(cell, query, body): (mean wops, mean bytes read)} over the bodies estimated on every param asked,
    {(cell, query, body, param): (wops, bytes)}, {(cell, query, body)} left out, and {(cell, query): (body, wops,
    bytes)}: the plan rule's pick (predicted.pick))"""
    out, per_param, refused, pick = {}, {}, set(), {}
    for c in cells:
        bodies, bad = PD.by_body(cap_dir, c, queries, params)
        refused |= {(c, q, b) for q, b in bad}
        for (q, b), ps in bodies.items():
            m = PD.mean(list(ps.values()))
            out[(c, q, b)] = (PD.cost(m, "wops"), m["read_bytes"])
            for p, v in ps.items():
                per_param[(c, q, b, p)] = (PD.cost(v, "wops"), v["read_bytes"])
        best, _ = PD.pick(cap_dir, c, queries, params, view="measured")
        for q, (b, m, _) in best.items():
            pick[(c, q)] = (b, PD.cost(m, "wops"), m["read_bytes"])
    return out, per_param, refused, pick


def costed_queries(pred, cells, queries):
    """the queries the predicted side costs on at least one layout, and those it costs on none (a step of every body
    not estimated, e.g. a sampled tally with no owner of a small type): those are left out of both sides and named"""
    none = [q for q in queries if not any((c, q) in pred for c in cells)]
    return [q for q in queries if q not in none], none


# ---- the comparison (kvcost.layout_cost.compare) -------------------------------------------------------------------------

def pick_row(name, cur, pred, meas, kind):
    """one picks.csv row (compare.pick_row) and its kind: query, workload, check or workload-at-measured-plan"""
    return {**CP.pick_row(name, cur, pred, meas, short), "kind": kind}


def constant_rows(cur, meas_q, meas_qp, total, cells, queries, qps):
    """per layout: the score of naming it for every query (the baseline a prediction must beat)"""
    out = []
    for c in cells:
        hq = sum(meas_q[(c, q)] <= min(meas_q[(x, q)] for x in cells) * (1 + 1e-9) for q in queries)
        hqp = sum(meas_qp[(c, q, p)] <= min(meas_qp[(x, q, p)] for x in cells) * (1 + 1e-9) for q, p in qps)
        out.append({"currency": cur, "layout": short(c), "cell": c, "queries_cheapest": hq, "query_params_cheapest": hqp,
                    "workload_over_best": total[c] / min(total.values())})
    return sorted(out, key=lambda r: (-r["queries_cheapest"], r["workload_over_best"], r["cell"]))


def write_csv(path, rows, fields=None):
    """the rows at full precision (repr of a float), so a table read back (--measured-table) gives the numbers it was
    made from, and the rank correlations on them"""
    fields = fields or list(rows[0])
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fields, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({k: (repr(v) if isinstance(v, float) else v) for k, v in r.items()})


def label(w):
    """a workload's name in the cost cache: its id, upper case (I02)"""
    return w["id"].upper()


def one_workload(dataset, sf, wid):
    ws = benchmarks.select(dataset, sf, wid)
    if len(ws) != 1:
        raise SystemExit(f"predict-accuracy: {wid!r} names {len(ws)} workloads of {dataset}: name one")
    (w,) = ws.values()
    return w


def run(a):
    """a: sf, predicted (a predicted capture dir), grid, benchmark, cost_workload, check_workload, check_reference,
    queries (None: the workload's), param_rows ("lo-hi"), out (the layout_choice/ dir is made below it)"""
    pdir = a.predicted
    out = os.path.join(a.out, "layout_choice")
    os.makedirs(out, exist_ok=True)
    w = one_workload(a.dataset, a.sf, a.cost_workload)
    if a.queries:
        keep = a.queries.split(",")
        unknown = sorted(set(keep) - {q for _, q, _, _ in w["items"]})
        if unknown:
            raise SystemExit(f"predict-accuracy: --queries {unknown} are not {a.cost_workload}'s")
        w = {**w, "items": [it for it in w["items"] if it[1] in keep]}
    cw = one_workload(a.dataset, a.sf, a.check_workload) if a.check_workload else None
    W, CW = label(w), (label(cw) if cw else None)
    if a.param_rows == "all":      # each query its own params (LinkBench: param_idx 1..its count in the request log)
        params, span = None, "all its params"
    else:
        lo, hi = (int(x) for x in a.param_rows.split("-"))
        params, span = [str(i) for i in range(lo, hi + 1)], f"params {lo}-{hi}"
    # the measured side binds the predicted capture's param set (its params dir); a set with no reference checks nothing
    param_set = Capture(pdir).param_set()
    cells = order_cells(PD.cells(pdir))
    if not cells:
        raise SystemExit(f"predict-accuracy: {pdir} holds no predicted layout (kvcost predict)")
    expect = getattr(a, "expect_layouts", None)
    if expect is not None and len(cells) != expect:
        raise SystemExit(f"predict-accuracy: {pdir} holds {len(cells)} predicted layouts, --expect-layouts "
                         f"{expect}: a partial capture would name the best of fewer layouts (kvcost predict) -- REFUSED")
    table = getattr(a, "measured_table", None)
    if not table and not a.grid:
        raise SystemExit("predict-accuracy: needs --grid (the measured captures) or --measured-table")
    if table and (a.grid or cw is not None):
        raise SystemExit("predict-accuracy: --measured-table replaces --grid, and its check workload was the "
                         "earlier run's: give neither --grid nor --check-workload -- REFUSED")
    if not table:
        families = benchmarks.families_read(a.dataset, [x for x in (w, cw) if x])     # BI too: LDBC's optional reads
        data = load_measured(a.grid, cells, families, param_set, a.dataset, a.sf, getattr(a, "plan_rule", None))
    n_check, bad, check = 0, [], {}
    if cw is not None:
        if a.check_reference in (None, "none"):
            if param_set is None:
                sys.exit(f"predict-accuracy: --check-workload {a.check_workload} needs --check-reference on the "
                         f"default param set (none is for a param set with no reference)")
            n_check, bad, check = check_totals(data, cw, cells, None)
            print(f"predict-accuracy: composed {CW} on the {param_set} param set, {n_check} layouts, not "
                  f"checked (no reference)")
        else:
            n_check, bad, check = check_totals(data, cw, cells, a.check_reference)
            print(f"predict-accuracy: composed {CW} against {a.check_reference}: {n_check} layouts, "
                  f"{len(bad)} differ")
        for b in bad[:10]:
            print("   DIFF", b)
        if bad or n_check != len(cells):
            sys.exit("predict-accuracy: the composed measured side is not the reference's: FAIL")
    weights = {q: often for _, q, often, _ in w["items"]}
    queries = list(weights)                      # the workload's order
    order = {q: i for i, q in enumerate(queries)}
    meas, meas_p = load_measured_table(table, cells, w) if table else measured_by_query(data, w, cells)
    pred_all, pred_p, refused, pred = load_predicted(pdir, cells, set(weights), params)
    qps = sorted({(q, p) for (_, q, p) in meas_p}, key=lambda x: (order[x[0]], int(x[1])))
    print(f"predict-accuracy: {len(cells)} layouts; measured {len(meas)} (layout, query) pairs, {len(meas_p)} "
          f"with their params; predicted {len(pred_all)} (layout, query, body) at level predicted, {len(pred)} (layout, query) "
          f"pairs, {len(refused)} bodies left out (a step not estimated)")
    all_queries = queries                        # by_query.csv and by_param.csv keep every query (a blank predicted side)
    queries, not_costed = costed_queries(pred, cells, queries)
    if not_costed:
        print(f"predict-accuracy: {len(not_costed)} of {len(all_queries)} queries costed on no layout, left out "
              f"of both sides ({W} over the other {len(queries)}): {', '.join(not_costed)}")
    if not queries:
        raise SystemExit(f"predict-accuracy: {pdir} costs none of {W}'s queries on any layout -- REFUSED")
    by_query, by_param = [], []
    for c in cells:
        for q in all_queries:
            p, m = pred.get((c, q)), meas.get((c, q))
            at_m = pred_all.get((c, q, m[0])) if m else None
            by_query.append({"cell": c, "layout": short(c), "query": q, "weight": weights[q],
                             "predicted_body": p[0] if p else "", "predicted_wops": p[1] if p else "",
                             "predicted_bytes": p[2] if p else "",
                             "measured_body": m[0] if m else "", "measured_wops": m[1] if m else "",
                             "measured_bytes": m[2] if m else "",
                             "predicted_wops_at_measured_body": at_m[0] if at_m else "",
                             "predicted_bytes_at_measured_body": at_m[1] if at_m else ""})
            for (q2, prm) in [x for x in qps if x[0] == q]:
                pp = pred_p.get((c, q, p[0], prm)) if p else None
                mp = meas_p.get((c, q, prm))
                by_param.append({"cell": c, "layout": short(c), "query": q, "param": prm,
                                 "predicted_wops": pp[0] if pp else "", "predicted_bytes": pp[1] if pp else "",
                                 "measured_wops": mp[0] if mp else "", "measured_bytes": mp[1] if mp else ""})
    write_csv(os.path.join(out, "by_query.csv"), by_query)
    write_csv(os.path.join(out, "by_param.csv"), by_param)
    picks, ties, consts, totals, qp_rows = [], [], [], [], []
    complete = [c for c in cells if all((c, q) in pred and (c, q) in meas for q in queries)]
    if not complete:
        lacking = sorted({q for c in cells for q in queries if (c, q) not in pred or (c, q) not in meas})
        raise SystemExit(f"predict-accuracy: no layout is costed on every query of {W} (each lacks one of "
                         f"{', '.join(lacking)}) -- REFUSED")
    qps = [(q, prm) for q, prm in qps if q in queries]
    for cur, f in CURRENCIES.items():
        # the predicted side names, the measured side judges (compare.naming_side: a tie in the model stays a tie)
        pq = CP.naming_side({(c, q): f(pred[(c, q)][1], pred[(c, q)][2]) for c in complete for q in queries})
        mq = {(c, q): f(meas[(c, q)][1], meas[(c, q)][2]) for c in complete for q in queries}
        pqp = CP.naming_side({(c, q, prm): f(*pred_p[(c, q, pred[(c, q)][0], prm)]) for c in complete for q, prm in qps})
        mqp = {(c, q, prm): f(*meas_p[(c, q, prm)]) for c in complete for q, prm in qps}
        for q in queries:
            picks.append(pick_row(q, cur, {c: pq[(c, q)] for c in complete}, {c: mq[(c, q)] for c in complete}, "query"))
            ties += CP.tie_rows(q, cur, {c: pq[(c, q)] for c in complete}, {c: mq[(c, q)] for c in complete})
        for q, prm in qps:
            qp_rows.append(CP.pick_row(f"{q}#{prm}", cur, {c: pqp[(c, q, prm)] for c in complete},
                                       {c: mqp[(c, q, prm)] for c in complete}, short))
        pw = CP.weighted_totals(pq, complete, queries, weights, naming=True)
        mw = CP.weighted_totals(mq, complete, queries, weights, naming=False)
        picks.append(pick_row(W, cur, pw, mw, "workload"))
        if cw is not None:
            mc = {c: f(check[c]["ops"], check[c]["bytes"]) for c in complete}
            picks.append(pick_row(f"{CW} (measured; {W} predicted)", cur, pw, mc, "check"))
        # each query priced at the measured side's plan: what is left when the plan choice is taken as given
        at_plan = [c for c in complete if all((c, q, meas[(c, q)][0]) in pred_all for q in queries)]
        at_m = CP.weighted_totals({(c, q): f(*pred_all[(c, q, meas[(c, q)][0])]) for c in at_plan for q in queries},
                                  at_plan, queries, weights, naming=True)
        if at_m:
            picks.append(pick_row(f"{W} (predicted at the measured plan)", cur, at_m, {c: mw[c] for c in at_m},
                                  "workload-at-measured-plan"))
        ties += CP.tie_rows(W, cur, pw, mw)
        consts += constant_rows(cur, mq, mqp, mw, complete, queries, qps)
        rp = {c: i + 1 for i, c in enumerate(sorted(complete, key=lambda c: (pw[c], c)))}
        rm = {c: i + 1 for i, c in enumerate(sorted(complete, key=lambda c: (mw[c], c)))}
        rc = {c: i + 1 for i, c in enumerate(sorted(complete, key=lambda c: (mc[c], c)))} if cw is not None else {}
        totals += [{"currency": cur, "cell": c, "layout": short(c), "predicted": pw[c], "measured": mw[c],
                    "predicted_over_measured": pw[c] / mw[c],
                    **({"check_measured": mc[c]} if cw is not None else {}), "predicted_rank": rp[c],
                    "measured_rank": rm[c], **({"check_measured_rank": rc[c]} if cw is not None else {})}
                   for c in sorted(complete, key=lambda c: rp[c])]
    write_csv(os.path.join(out, "picks.csv"), picks)
    write_csv(os.path.join(out, "picks_by_param.csv"), qp_rows)
    write_csv(os.path.join(out, "tie_rule.csv"), ties)
    write_csv(os.path.join(out, "constant_layouts.csv"), consts)
    write_csv(os.path.join(out, "totals.csv"), totals)
    write_summary(out, a, picks, qp_rows, ties, consts, totals, len(meas), len(pred), len(refused), pdir, n_check,
                  W, CW, len(complete), param_set, span, not_costed)
    print(f"predict-accuracy: DONE -> {out}")


def is_query(p):
    return p["kind"] == "query"


def headline(cur, picks, qp_rows, consts, W):
    q = [p for p in picks if p["currency"] == cur and is_query(p)]
    qp = [p for p in qp_rows if p["currency"] == cur]
    w = next(p for p in picks if p["currency"] == cur and p["kind"] == "workload")
    c = next(r for r in consts if r["currency"] == cur)
    within = " / ".join(str(sum(p["named_measured_over_best"] <= 1 + x + 1e-9 for p in q)) for x in WITHIN)
    return (f"| {cur} | {sum(p['hit'] for p in q)} of {len(q)} | {within} | {sum(p['hit'] for p in qp)} of {len(qp)} | "
            f"{w['named_short']}: rank {w['measured_rank_of_named']}, {w['named_measured_over_best']:.3f}x | "
            f"{100 * w['predicted_runner_up_gap']:.1f}% | {w['spearman']:.3f} | {w['top5_overlap']}/5 | "
            f"{c['layout']}: {c['queries_cheapest']} of {len(q)}, {c['query_params_cheapest']} of {len(qp)}, "
            f"{W} {c['workload_over_best']:.3f}x |")


def spread_line(p):
    """one row of summary.md's spread table, from a picks.csv row"""
    def x(v):
        return "-" if math.isnan(v) else f"{v:.2f}x"
    gap = "-" if math.isnan(p["gap_share"]) else f"{100 * p['gap_share']:.0f}%"
    return (f"| {p['currency']} | {p['what']} | {p['measured_best_short']} 1.00x | {p['named_short']} "
            f"{x(p['named_measured_over_best'])} | {x(p['sp_co_over_best'])} | {x(p['co_co_over_best'])} | "
            f"{x(p['median_over_best'])} | {p['worst']} {x(p['worst_over_best'])} | {gap} |")


def baseline_text(picks, consts):
    """the baseline paragraph, its counts from the constant rows: per currency, the best constant layout and the
    queries it is the cheapest on"""
    per = []
    for cur in CURRENCIES:
        c = next(r for r in consts if r["currency"] == cur)
        n = sum(1 for p in picks if p["currency"] == cur and is_query(p))
        per.append(f"in {cur}, {c['layout']} on {c['queries_cheapest']} of {n} queries")
    return ("**The baseline** is the best constant answer: one layout named for every query (" + "; ".join(per) +
            "). The more queries one layout is the cheapest on, the higher a constant pick scores, so the comparison "
            "to read is the one against the baseline.")


def write_summary(out, a, picks, qp_rows, ties, consts, totals, n_meas, n_pred, n_refused, pdir, n_check, W, CW, n,
                  param_set, span, not_costed=()):
    if CW is None:
        checked = ""
    elif param_set is not None and a.check_reference in (None, "none"):
        checked = f" on the {param_set} param set (--params {param_set}; no {CW} reference on it)"
    else:
        checked = f"; its {CW} totals equal `{a.check_reference}`'s on {n_check} of {n} layouts"
    L = [f"# The best of {n} uniform layouts from statistics", "",
         f"> Generated by "
         f"`kvcost predict-accuracy --dataset {a.dataset}={a.sf} --workload {a.cost_workload} <the 49>` (or "
         f"`python -m kvcost.predict_accuracy.layouts`); data in this dir.", "",
         f"Scale {scale_label(a.dataset, a.sf)}. Predicted: level predicted (statistics only), `{pdir}`. Measured: " +
         (f"read from an earlier run's table, `{a.measured_table}`" if getattr(a, "measured_table", None)
          else f"composed per part from `{a.grid}`") + checked + f". Pairs: {n_meas} measured (layout, query), {n_pred} "
         f"predicted, {n_refused} predicted bodies left out (a step not estimated)." +
         (f" Costed on no layout, left out of both sides ({W} is over the other queries): {', '.join(not_costed)}."
          if not_costed else ""), "",
         "Two currencies: **wops_bytes4k** = wops + bytes read / 4096 (it counts what a Chunked read pays in bytes) "
         f"and **wops** = G + 1.1 S + 0.01 N. Each query is its mean over {span}; {W} weights each "
         "query by its frequency in the workload. Each side takes its plan per (layout, query) by the one plan rule: "
         f"predicted, on the predicted {span}; measured, on the held-out params. A measured tie with the best "
         "counts as a hit.", "",
         baseline_text(picks, consts), "",
         "## Headline", "",
         "| currency | queries: names the measured cheapest | within 3% / 10% | (query, param): names the cheapest | "
         f"{W}: named layout, measured rank, cost over the best | {W}: predicted gap to runner-up | {W}: Spearman of "
         f"the {n} | {W}: top-5 overlap | the best constant layout: queries, (query, param), {W} |",
         "|---|---|---|---|---|---|---|---|---|"]
    for cur in CURRENCIES:
        L.append(headline(cur, picks, qp_rows, consts, W))
    L += ["", "## The spread", "",
          "Per workload, measured, each layout x over the measured best: the pick (the layout named), the fixed Sp-Co "
          "and Co-Co, the median of the layouts and the worst. Gap share = (Sp-Co - pick) / (Sp-Co - best): the part "
          "of the fixed Sp-Co's excess over the best that the pick removes.", "",
          "| currency | workload | measured best | pick | Sp-Co | Co-Co | median | worst | gap share |",
          "|---|---|---|---|---|---|---|---|---|"]
    for cur in CURRENCIES:
        for p in picks:
            if p["currency"] == cur and not is_query(p):
                L.append(spread_line(p))
    for cur in CURRENCIES:
        L += ["", f"## Per query, {cur}", "",
              "| query | named | measured best | named / best (measured) | measured rank of named | predicted gap to "
              "runner-up | measured gap to runner-up | top-5 overlap | Spearman |", "|---|---|---|---|---|---|---|---|---|"]
        for p in picks:
            if p["currency"] != cur:
                continue
            L.append(f"| {p['what']} | {p['named_short']} | {p['measured_best_short']} | {p['named_measured_over_best']:.3f} | "
                     f"{p['measured_rank_of_named']} | {100 * p['predicted_runner_up_gap']:.1f}% | "
                     f"{100 * p['measured_runner_up_gap']:.1f}% | {p['top5_overlap']}/5 | {p['spearman']:.3f} |")
        L += ["", f"The best constant layouts ({cur}): " + "; ".join(
            f"{r['layout']} {r['queries_cheapest']} queries, {r['query_params_cheapest']} (query, param), {W} "
            f"{r['workload_over_best']:.3f}x" for r in [r for r in consts if r["currency"] == cur][:3]) + "."]
    cur = next(iter(CURRENCIES))
    n_q = sum(1 for p in picks if p["currency"] == cur and is_query(p))
    L += ["", f"## The tie rule ({cur}): \"one of these k layouts\"", "",
          f"For a threshold x, the set is every layout predicted within x of the predicted cheapest: over the {n_q} queries, "
          "how many sets hold the measured cheapest, the mean k, and the worst measured cost in a set over the measured "
          f"cheapest; then {W}.", "",
          "| threshold | queries whose set holds the measured best | mean k | worst in set / best (max over queries) | "
          f"{W}: holds it | {W}: k | {W}: worst in set / best |", "|---|---|---|---|---|---|---|"]
    for x in CP.THRESHOLDS:
        qs = [t for t in ties if t["currency"] == cur and t["threshold"] == x and t["what"] != W]
        w = next(t for t in ties if t["currency"] == cur and t["threshold"] == x and t["what"] == W)
        L.append(f"| {100 * x:.0f}% | {sum(t['contains_measured_best'] for t in qs)} / {len(qs)} | "
                 f"{sum(t['k'] for t in qs) / len(qs):.1f} | {max(t['worst_measured_over_best_in_set'] for t in qs):.3f} | "
                 f"{'yes' if w['contains_measured_best'] else 'no'} | {w['k']} | {w['worst_measured_over_best_in_set']:.3f} |")
    L += ["", f"## {W} per layout ({cur}; the predicted top 10, then the measured top 10)", "",
          "| layout | predicted | measured | predicted / measured | predicted rank | measured rank |"
          + (f" {CW} measured rank |" if CW else ""), "|---|---|---|---|---|---|" + ("---|" if CW else "")]
    tc = [t for t in totals if t["currency"] == cur]
    top = tc[:10] + [t for t in sorted(tc, key=lambda t: t["measured_rank"])[:10] if t not in tc[:10]]
    for t in top:
        L.append(f"| {t['layout']} | {t['predicted']:.4g} | {t['measured']:.4g} | "
                 f"{t['predicted_over_measured']:.2f} | {t['predicted_rank']} | {t['measured_rank']} |"
                 + (f" {t['check_measured_rank']} |" if CW else ""))
    with open(os.path.join(out, "summary.md"), "w") as f:
        f.write("\n".join(L) + "\n")


def main(argv=None):
    """the layout report on a predicted capture named by path, with a check workload or an earlier run's table:
    python -m kvcost.predict_accuracy.layouts --dataset D=S --predicted P (--grid G | --measured-table T)
    --cost-workload W [--check-workload C --check-reference PLACE] [--expect-layouts N] [--queries a,b]
    [--param-rows 1-5|all] [--pick-on held-out|in-sample] --out D -> D/layout_choice/"""
    import argparse
    from kvcost.cli import workloads as WK
    from kvcost.cli.cli import plan_rule
    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--dataset", required=True, metavar="DATASET=SCALE")
    ap.add_argument("--predicted", required=True, help="the predicted capture (level predicted)")
    ap.add_argument("--grid", help="the grid capture the measured side is composed from (grid_chunked_<size> below it)")
    ap.add_argument("--measured-table", help="in place of --grid: an earlier run's layout_choice dir (by_query.csv, "
                    "by_param.csv)")
    ap.add_argument("--cost-workload", required=True, help="the workload predicted and compared (e.g. i02)")
    ap.add_argument("--check-workload", help="a workload measured whole whose composed totals are checked first (e.g. i01)")
    ap.add_argument("--check-reference", help="the cost-layouts place (or its layouts.csv) the check workload's composed "
                    "totals must equal; none on a param set with no reference")
    ap.add_argument("--expect-layouts", type=int, help="the number of layouts the predicted capture must hold")
    ap.add_argument("--queries", help="keep only these of the cost workload's queries")
    ap.add_argument("--param-rows", default="1-5", help="lo-hi, or all (each query its own params)")
    benchmarks.add_pick_on_arg(ap)
    ap.add_argument("--out", required=True, help="the report dir (layout_choice/ below it)")
    a = ap.parse_args(argv)
    a.dataset, a.sf = a.dataset.split("=", 1)
    a.plan_rule = plan_rule(WK.get(WK.of(a.dataset)), a)
    run(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
