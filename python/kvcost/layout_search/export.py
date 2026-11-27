"""The mix search's inputs and outputs as files a reader can open: the uniform layouts' prices, each query's costs, the
search's trace, the result rows, and a row's mix as a buildable meta spec (`kvcost search-mix` writes rows.csv,
mixes/best.json and, with --trace, trace.csv; uniform_rows and cost_cache_rows have no command since cost-layouts writes its
one place, kvcost.layout_cost.layout_costs: tests/unit/check_mix_search_export.py reads them; the walkthrough,
the layout-cost walkthrough, inputs/workloads/ldbc/examples/, shows each one).

Every cost here comes from the code the search uses: compose.query_pick for a plan and its cost vector (the one plan
rule), the Problem's cost cache for an entry (each row is checked equal to it), search.solve's trace for the
search. Nothing is priced a second way. Costs are one run of the query (a cost cache entry, a uniform price); the trace's
floors and totals are weighted by how often each query runs, as the search sees them.
"""
import csv
import itertools
import math
import os

from kvcost.layout_cost import cost_table as CT
from kvevaluator.layouts import cell_id
from kvcost.layout_cost import mixes
from kvcost.layout_cost.cost_models import currencies
from kvcost.layout_cost.compose import CHANNELS, LAYOUTS, params_of, query_pick
from kvcost.layout_search import search as S

TOL = 1e-9          # a cost cache row against the Problem's entry: relative


def write_csv(path, cols, rows):
    """rows (dicts) to a CSV with these columns, in the order given (the callers sort them)"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="") as f:
        out = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        out.writeheader()
        for r in rows:
            out.writerow({c: _cell(r.get(c)) for c in cols})


def _cell(v):
    """a CSV cell: floats without float noise (repr round-trips), None as empty"""
    if v is None:
        return ""
    if isinstance(v, float):
        return "inf" if v == math.inf else repr(v)
    return v


def _row_cost(d, params, assign, units):
    """(plan, {wops, bytes, each raw channel}) of one query under `assign`, at the plan the rule picks; (None, None)
    when no plan is usable"""
    plan, tots, _ = query_pick(d, params, assign, units)
    if plan is None:
        return None, None
    vec = tots[plan]
    cur = currencies(vec)
    return plan, {"wops": cur["ops"], "bytes": cur["bytes"], **{c: vec[c] for c in CHANNELS}}


COST_COLS = ["plan", "wops", "bytes", *CHANNELS]


def uniform_layouts(data):
    """[(cell name, props setting, adjacency setting)]: the nine uniform layouts in the cells' order (adjacency-major:
    Sp-Sp, Co-Sp, Ch64k-Sp, Sp-Co, ...), then Chunked/Chunked at each other chunk size the load holds (Composer.sizes)"""
    out = [(cell_id(p, a), p, a) for a in LAYOUTS for p in LAYOUTS]
    return out + [(cell, oid, oid) for oid, _, cell in data.composer.sizes]


def cost_row(d, family, q, often, params, assign, units, layout):
    """one cost table row (cost_table.row, source measured) of a query under `assign`: the plan the rule picks and its
    one-run vector, the mean over the params"""
    ps = params_of(d, params)
    plan, tots, _ = query_pick(d, ps, assign, units)
    return CT.row(CT.MEASURED, layout, family, q, often, plan, len(ps), tots[plan] if plan is not None else None,
                  weights=d["composer"].w)


def uniform_rows(w, data):
    """one cost table row (cost_table.COLUMNS) per (query, uniform layout): how often the query runs, the plan the rule
    picks there and its one-run cost (the models, the priced vector, the raw channels)"""
    rows = []
    for family, q, often, params in w["items"]:
        d = data[(family, q)]
        units = set(S.query_parts(d))
        for cell, p, a in uniform_layouts(data):
            rows.append(cost_row(d, family, q, often, params, S.uniform(units, p, a), units, cell))
    return rows


def cost_cache_rows(pb, data, w, qi):
    """one row per entry of query qi's costs in the mix cost cache: the setting of each part it reads (its parts in sorted order), the plan
    the rule picks and its one-run cost. Each entry's wops and bytes are checked equal to the Problem's cost cache, the one
    the search reads (SystemExit on a difference)"""
    family, q, _, params = w["items"][qi]
    d = data[(family, q)]
    units = S.query_parts(d)
    qe = pb.queries[qi]
    search_pos = [pb.order[p] for p in qe["parts"]]      # the cost cache's axes: the query's parts in search order
    if sorted(search_pos) != units:
        raise SystemExit(f"{q}: the cost cache's parts {search_pos} are not the query's {units}")
    opts = {u: pb.options[pb.order.index(u)] for u in units}
    rows = []
    for combo in itertools.product(*(opts[u] for u in units)):
        assign = dict(zip(units, combo))
        plan, cost = _row_cost(d, params_of(d, params), assign, set(units))
        idx = 0
        for p in qe["parts"]:
            idx = idx * pb.size[p] + pb.options[p].index(assign[pb.order[p]])
        want = (qe["one_run"]["ops"][-1][idx], qe["one_run"]["bytes"][-1][idx])
        got = (math.inf, math.inf) if cost is None else (cost["wops"], cost["bytes"])
        for name, g, x in zip(("wops", "bytes"), got, want):
            if not (g == x or abs(g - x) <= TOL * max(1.0, abs(x))):
                raise SystemExit(f"{q} at {mixes.assignment_label(assign)}: {name} {g!r} here, {x!r} in the search's cost cache")
        rows.append({**{mixes.unit_name(u): assign[u] for u in units}, "plan": plan, **(cost or {"wops": math.inf,
                                                                                               "bytes": math.inf})})
    return [mixes.unit_name(u) for u in units], rows


def row_assignment(row):
    """{unit: setting id} of a result row (results.assignment_of: its parts as --mix spells them, settings in short)"""
    from kvcost.layout_search import results as R
    return R.assignment_of(row["assignment"])


ROW_COLS = ["workload", "option_set", "question", "model", "limit", "weighting", "sizes", "proven", "nodes", "fits",
            "wops", "bytes", "score", "assignment"]


def result_rows(rows):
    """(columns, rows) of a result file's rows: the question, its answer's totals (wops, bytes; the model's own total
    is in the result file's totals), the geomean / additive score (empty under sum), the mix as one label and one
    column per part (every part any row sets, sorted)"""
    units = sorted({u for r in rows for u in row_assignment(r)}, key=lambda u: (u[0], u[1:]))
    out = []
    for r in rows:
        a = row_assignment(r)
        out.append({"workload": r.get("workload"), "option_set": r.get("option_set"), "question": r["question"],
                    "model": r["model"], "limit": r.get("limit"), "weighting": r.get("weighting"),
                    "sizes": ";".join(s[0] for s in r.get("sizes") or []), "proven": r["proven"], "nodes": r["nodes"],
                    "fits": r["fits"], "wops": r["totals"]["wops"], "bytes": r["totals"]["bytes"],
                    "score": (r.get("score") or {}).get("value"),      # the search's total under geomean / additive
                    "assignment": mixes.assignment_label(a), **{mixes.unit_name(u): a.get(u) for u in units}})
    return ROW_COLS + [mixes.unit_name(u) for u in units], out


def row_spec(row):
    """(spec, its base cell) that builds a result row's mix (mixes.spec_of); the spec read back
    (mixes.assignment) must give the row's assignment again (SystemExit otherwise)"""
    assign = row_assignment(row)
    spec, base = mixes.spec_of(assign)
    settings = mixes.grid_settings({v for v in assign.values() if mixes.sized_pack(v)})   # + each chunk size the row uses
    back = mixes.assignment(spec, sorted(assign, key=lambda u: (u[0], u[1:])), settings, "row")
    if back != assign:
        diff = sorted((mixes.unit_name(u), assign.get(u), back.get(u)) for u in set(assign) | set(back)
                      if assign.get(u) != back.get(u))
        raise SystemExit(f"the spec built from the row reads back as another mix: {diff}")
    return spec, base


TRACE_COLS = ["step", "verdict", "node", "child", "part", "option", "mix_so_far"]


def trace_rows(trace, model):
    """(columns, rows) of search.solve's trace (sum weighting only: the CLI refuses another): one row per decision.
    floor / best are the solve's (primary, other) totals, weighted by how often each query runs: the primary is
    `model`'s lane, the other bytes (wops when the model is bytes). mix_so_far lists the parts by name, not in search
    order (the part column follows the search order)"""
    other = "wops" if model == "bytes" else "bytes"
    cols = TRACE_COLS + [f"floor_{model}", f"floor_{other}", f"best_{model}", f"best_{other}"]
    out = []
    for i, t in enumerate(trace):
        fl, be = t["floor"] or (None, None), t["best"] or (None, None)
        out.append({"step": i, "verdict": t["verdict"], "node": t["node"], "child": t["child"],
                    "part": None if t["part"] is None else mixes.unit_name(t["part"]), "option": t["option"],
                    "mix_so_far": mixes.assignment_label(t["mix"]),
                    f"floor_{model}": fl[0], f"floor_{other}": fl[1], f"best_{model}": be[0], f"best_{other}": be[1]})
    return cols, out
