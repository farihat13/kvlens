"""A workload priced on predicted layouts (`kvcost cost-layouts --count-source <set>`): every cell of a predicted capture is a
whole layout; per (layout, query) the plan rule's body (predicted.pick), its cost the mean over the params the
capture holds; the workload's total per layout at its frequencies, and the answer: every layout predicted within
cost_models.WITHIN of the predicted best, cheapest first. A workload with write items (I01) prices them from the
predicted writes capture (kvcost predict on a run: one capture per family), a write's cost from compose.write_vector.
The rows are the one cost table every source writes (kvcost.layout_cost.cost_table), source = the capture set.
"""
import csv
import os

from kvevaluator.plan_choice.optimizer.criteria import with_weights
from kvcost.cli import runs
from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import cost_table as CT
from kvcost.predict_accuracy import predicted as PD
from kvevaluator import layouts as L


def _common_cells(caps, source):
    """the layouts every family's capture holds; a layout one family lacks would cost only part of the workload there:
    refused by name, never dropped"""
    have = {fam: set(PD.cells(d)) for fam, d in caps.items()}
    gaps = sorted(f"{fam} {c}" for fam in have for c in set.union(*have.values()) - have[fam])
    if gaps:
        raise SystemExit(f"cost-layouts --count-source {source}: no predicted capture for {', '.join(gaps)} in "
                         f"{', '.join(caps.values())} (kvcost predict)")
    return sorted(set.intersection(*have.values()))


def uniform_cost(sf, workload, source, root, model, param_set=None, w=None, *, view):
    """(uniform rows, {cell: total under model}, {cell: [queries with no body priced]}) of a workload on every layout of
    the predicted capture set `source` below the tree `root` (kvcost.cli.predict.capture_dirs, at `param_set`). `w`: the op-weight vector of the pick and the costs (None: the default);
    `view`: the chunk view of the writes (compose.chunk_view), as a measured search's"""
    fams = sorted({fam for fam, *_ in workload["items"]})
    from kvcost.cli.predict import capture_dirs
    caps = {fam: capture_dirs(sf, fam, (source,), root, param_set)[0] for fam in fams}
    cells = _common_cells(caps, source)
    if not cells:
        raise SystemExit(f"cost-layouts --count-source {source}: no predicted layout in {', '.join(caps.values())} (kvcost predict)")
    criterion = None if w is None else with_weights(None, w)      # the default rule, or wops_bytes4k under `w`
    rows, tot, missing = [], {}, {}
    for c in cells:
        name = L.name_of(c) if L.is_cell(c) else None
        picks = {fam: PD.pick(caps[fam], c, {q for f, q, *_ in workload["items"] if f == fam},
                              bill=CM.needs_bill(model) == "bounds", criterion=criterion, weights=w, view=view)[0]
                 for fam in fams}
        t = 0.0
        for fam, q, often, _params in workload["items"]:
            if q not in picks[fam]:
                missing.setdefault(c, []).append(q)
                continue
            body, m, ps = picks[fam][q]
            rows.append(CT.row(source, c, fam, q, often, body, len(ps), m, name, weights=w))
            t += often * CM.cost(m, model)
        if c not in missing:
            tot[c] = t
    return rows, tot, missing


def short(cell):
    return runs.layout_name(cell)


def names_of(rows):
    """{layout id: its short name} as the cost table rows name it (layouts.name_of, else the id): one name per layout in
    every file and line of a run"""
    return {r["layout"]: r["name"] for r in rows}


def within(tot, share=CM.WITHIN):
    """[(cell, total)] predicted within `share` of the predicted best, cheapest first (ties: compared at 12 significant
    digits, then by name)"""
    best = min(PD.comparable(v) for v in tot.values())
    keep = [c for c, v in tot.items() if PD.comparable(v) <= best * (1 + share)]
    return sorted(((c, tot[c]) for c in keep), key=lambda x: (PD.comparable(x[1]), x[0]))


def write_totals(path, tot, model, names):
    """one row per layout: the workload's total under `model`, over the predicted best, and whether it is within
    cost_models.WITHIN (the answer); cheapest first. `names`: {layout id: short name} (names_of the run's rows)"""
    near = dict(within(tot))
    best = min(tot.values())
    with open(path, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["layout", "cell", "model", "total", "over_best", "within"])
        for c, v in sorted(tot.items(), key=lambda x: (PD.comparable(x[1]), x[0])):
            w.writerow([names.get(c) or short(c), c, model, f"{v:.6g}", f"{v / best:.6g}", int(c in near)])
    return os.path.abspath(path)


def write(path, rows):
    return os.path.abspath(CT.write(path, rows))
