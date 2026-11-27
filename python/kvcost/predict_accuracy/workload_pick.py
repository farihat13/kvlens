"""A workload's layout named from statistics, against the measured 49, by its totals: for a workload with a write family
(LDBC Interactive with its writes, LinkBench), the predicted totals (predict-accuracy's uniform.csv of the predicted
set) beside the measured 49's totals in their kvcost cost-layouts place (layouts.csv, the half chunk view's uniform
rows), in that table's own currencies.

    kvcost predict-accuracy --dataset D=S --workload W Sp-Sp Sp-Co ... Ch256k-Ch256k --count-source predicted-tier1     (a workload with writes)
    python -m kvcost.predict_accuracy.workload_pick --predicted <dir>/uniform.csv \
        --measured <output>/summary/<ds>/<sc>/layout-costs/<workload>/measured --out <dir>

Both sides total a layout the same way: each query's cost per run times its frequency, summed (compose.workload_cost),
in cost_models.currencies' two units: ops = read weighted ops (G + 1.1 S + 0.01 N) + the writes' own reads + PUTs +
DELs; bytes = bytes read + the bytes the writes read first + bytes written; wops_bytes4k = ops + bytes / 4096. Per
model (wops, wops_bytes4k): the predicted pick, its measured rank and its measured cost over the measured best, the
measured best, and Spearman of the layouts (pick.csv); per layout both totals and ranks (layouts.csv). The tables are
joined on the layout's cell id; a layout on one side only is refused. Opens no store, runs no plan.
"""
import argparse
import csv
import os
from collections import defaultdict

from kvcost.layout_cost.cost_models import wops_bytes4k
from kvcost.plotting.stats import spearman

MODELS = ("wops", "wops_bytes4k")


def predicted_totals(path):
    """{cell: {"ops", "bytes"}}: predict-accuracy's uniform.csv, each query's per-run wops and bytes
    (cost_models.currencies) times its frequency, summed over the workload's queries; or, a dir, the totals of a
    cost-layouts place of the predicted set (measured_totals)"""
    if os.path.isdir(path):
        return measured_totals(path)
    tot = defaultdict(lambda: {"ops": 0.0, "bytes": 0.0})
    with open(path) as f:
        for r in csv.DictReader(f):
            often = float(r["frequency"])
            tot[r["layout"]]["ops"] += often * float(r["wops"])
            tot[r["layout"]]["bytes"] += often * float(r["bytes"])
    return dict(tot)


def measured_totals(place):
    """{cell: {"ops", "bytes"}}: the measured uniform layouts' total_ops and total_bytes in a cost-layouts place (half
    chunk view)"""
    from kvcost.layout_cost import layout_costs as LC
    return {r["layout"]: {"ops": float(r["total_ops"]), "bytes": float(r["total_bytes"])}
            for r in LC.read_layouts(place, "half", kind="uniform")}


def cost(t, model):
    return t["ops"] if model == "wops" else wops_bytes4k(t["ops"], t["bytes"])


def ranks(values):
    """{key: 1-based rank}, cheapest first, ties broken by key so the order is fixed"""
    return {k: i + 1 for i, k in enumerate(sorted(values, key=lambda k: (values[k], k)))}


def run(predicted, measured, out):
    """pick.csv and layouts.csv into `out`: the predicted totals (predict-accuracy's uniform.csv) against the measured
    49 (a cost-layouts place)"""
    pred, meas = predicted_totals(predicted), measured_totals(measured)
    if set(pred) != set(meas):
        raise SystemExit(f"workload_pick: the layouts differ: predicted only {sorted(set(pred) - set(meas))}, measured "
                         f"only {sorted(set(meas) - set(pred))}")
    cells = sorted(pred)
    picks, rows = [], []
    for m in MODELS:
        p = {c: cost(pred[c], m) for c in cells}
        q = {c: cost(meas[c], m) for c in cells}
        rp, rq = ranks(p), ranks(q)
        pick = min(cells, key=lambda c: (p[c], c))
        best = min(cells, key=lambda c: (q[c], c))
        rho = spearman([p[c] for c in cells], [q[c] for c in cells])
        picks.append({"model": m, "layouts": len(cells), "predicted_pick": pick, "measured_rank_of_pick": rq[pick],
                      "pick_over_measured_best": round(q[pick] / q[best], 4), "measured_best": best,
                      "spearman": round(rho, 4) if rho is not None else ""})
        rows += [{"model": m, "cell": c, "predicted": round(p[c], 3), "measured": round(q[c], 3),
                  "predicted_rank": rp[c], "measured_rank": rq[c], "measured_over_best": round(q[c] / q[best], 4)}
                 for c in sorted(cells, key=lambda c: rq[c])]
    os.makedirs(out, exist_ok=True)
    for name, data in (("pick.csv", picks), ("layouts.csv", rows)):
        with open(os.path.join(out, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
    for r in picks:
        print(f"workload_pick: {r['model']}: predicted {r['predicted_pick']}, measured rank {r['measured_rank_of_pick']} "
              f"of {r['layouts']}, {r['pick_over_measured_best']}x the measured best {r['measured_best']}; "
              f"Spearman {r['spearman']} -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--predicted", required=True, help="kvcost predict-accuracy's uniform.csv of the predicted set, or "
                    "kvcost cost-layouts --count-source <set>'s place")
    ap.add_argument("--measured", required=True, help="kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k's place (layout-costs/<workload>/measured)")
    ap.add_argument("--out", required=True, help="the dir pick.csv and layouts.csv go to")
    a = ap.parse_args()
    run(a.predicted, a.measured, a.out)


if __name__ == "__main__":
    main()
