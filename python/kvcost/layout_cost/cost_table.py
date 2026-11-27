"""The one cost table `kvcost cost-layouts` writes for every source (measured, composed, predicted, predicted-anchor,
a mix): one row per (layout, query), the body the plan rule picks there and its cost for one run of the query (the mean
over its params), each model's number beside the raw channels it is made from (capturing-data: raw first).

    source          measured, or the predicted capture set it was read from (predicted-tier1, predicted-tier2, ...)
    layout          the layout's id: a cell (props_split_adj_condensed), or a mix's name
    name            its short name (Sp-Co, Ch4k-Ch4k; a mix: its name)
    family, query, frequency   the query and how often the workload runs it
    plan            the body the plan rule picks on this layout
    params          the params its cost is the mean over
    weights         the op-weight vector G:S:N the plan was picked and wops costed under (kvcost --op-weights)
    wops, bytes, wops_bytes4k  the models (cost_models: one definition)
    read_ops ... write_rmw_bytes   the priced vector (compose.read_cost, compose.write_vector)
    S ... write_value              the raw channels (compose.CHANNELS)

Floats are written as repr (they read back exactly). Readers: the paper's derive step.
"""
import csv

from kvevaluator import weights as W
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost.compose import CHANNELS

VECTOR = ("read_ops", "read_bytes", "miss_key_bytes", "write_ops", "write_bytes", "write_rmw_ops", "write_rmw_bytes")
COLUMNS = ["source", "layout", "name", "family", "query", "frequency", "plan", "params", "weights",
           *CM.MODELS, *VECTOR, *CHANNELS]
MEASURED = "measured"


def short_name(layout):
    """a layout's short name (Sp-Co, Ch4k-Ch4k), or the layout as given (a mix's name)"""
    from kvevaluator import layouts as L
    L.check_no_old_form(layout, "layout")          # an old spelling is refused, never shown as a mix's name
    try:
        return L.name_of(layout)
    except (KeyError, ValueError, TypeError, AttributeError):
        return layout


def row(source, layout, family, query, frequency, plan, params, vec, name=None, weights=None):
    """one table row from a priced vector (its models from cost_models.cost; a channel the vector lacks is empty); vec
    None: no plan is usable there, every cost empty. `name`: the short name when the caller knows the layout's spec
    (a predicted capture: kvevaluator.layouts.cell_of_spec), else read from the id (short_name). `weights`: the
    op-weight vector it was costed under (None: the default)"""
    vec = vec or {}
    return {"source": source, "layout": layout, "name": name or short_name(layout), "family": family, "query": query,
            "frequency": frequency, "plan": plan, "params": params, "weights": W.text(weights or W.DEFAULT),
            **{m: (CM.cost(vec, m) if vec else None) for m in CM.MODELS}, **{k: vec.get(k) for k in VECTOR + CHANNELS}}


def write(path, rows):
    """the rows to `path` in COLUMNS' order"""
    from kvcost.layout_search.export import write_csv
    write_csv(path, COLUMNS, rows)
    return path


def read(path):
    """a cost table's rows (dicts of strings, as written)"""
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        missing = [c for c in ("layout", "family", "query", "frequency", *CM.MODELS) if c not in (r.fieldnames or [])]
        if missing:
            raise SystemExit(f"{path}: not a cost table (no {', '.join(missing)} column): kvcost cost-layouts writes one")
        rows = list(r)
    for x in rows:          # a table written before the column was costed under the default vector
        x.setdefault("weights", W.text(W.DEFAULT))
    return rows


def weights_of(rows, path):
    """the one op-weight vector (G:S:N text) a table's rows were costed under; a table that mixes two is refused"""
    got = sorted({r["weights"] for r in rows})
    if not got:
        raise SystemExit(f"{path}: no rows")
    if len(got) != 1:
        raise SystemExit(f"{path}: rows costed under {len(got)} weight vectors ({', '.join(got)}): one table, one vector")
    return got[0]
