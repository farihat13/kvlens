#!/usr/bin/env python3
"""Predict mode's layout choice at SF3, in numbers: does predict mode, from statistics alone (level predicted, no store),
name the cheapest of the 49 uniform layouts? Under wops_bytes4k (wops + bytes / 4096), per query and on I02, against the
measured cost composed from the grid, and its answer: every layout predicted within 5% of the predicted best.

Reads the output of kvcost predict-accuracy --dataset ldbc=<SF> --workload i02 Sp-Sp Sp-Co ... Ch256k-Ch256k --count-source <set> (after kvcost
predict ... uniform-49), summary/ldbc/sf<SF>/predict-statistics/i02/<set>/, as its
layout_choice/{picks,picks_by_param,constant_layouts}.csv and uniform_totals.csv (the predicted costs of the 49). No snapshot holds one yet, and no paper claim
reads it: when one does, this becomes its derive in the artifact's paper/derive/ (the keys below as the paper's number
keys). Writes <out>/predict_accuracy.csv (one row per query and I02: the named layout, the measured best, named /
best) and <out>/predict_accuracy_numbers.json, these keys:
  predict.sf3.queries, predict.sf3.queries.named   the queries, and those whose named layout is the measured cheapest
  predict.sf3.queries.within<3|10>                 the queries whose named layout costs within 3% / 10% of the best
  predict.sf3.qparams, predict.sf3.qparams.named   the same per (query, param)
  predict.sf3.w17.<named|rank|over|spearman|top5>  I02: the named layout, its measured rank, its measured cost over
                                                   the best, the Spearman of the 49, the top-5 overlap
  predict.sf3.constant.<layout|queries|w17over>    the best constant answer (one layout for every query)
  predict.sf3.within5.<count|layouts|over>         the answer: how many layouts, which, the last one's predicted cost
                                                   over the predicted best

    uv run python experiments/predict_statistics/layout_choice_accuracy.py --from <that dir> --out <dir>
"""
import argparse
import csv
import json
import os

CURRENCY = "wops_bytes4k"
COLUMNS = ["what", "named", "measured_best", "hit", "named_measured_over_best", "measured_rank_of_named"]


def read(path):
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def over(r):
    """the named layout's measured cost over the measured best"""
    return float(r["named_measured_over_best"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", dest="run", required=True, help="kvcost predict-accuracy Sp-Sp Sp-Co ... Ch256k-Ch256k's output dir for I02 "
                                                             "(summary/ldbc/sf<SF>/predict-statistics/i02/<set>/)")
    ap.add_argument("--out", required=True, help="where predict_accuracy.csv and predict_accuracy_numbers.json go")
    a = ap.parse_args()
    lc = os.path.join(a.run, "layout_choice")
    picks = [r for r in read(os.path.join(lc, "picks.csv")) if r["currency"] == CURRENCY]
    qparams = [r for r in read(os.path.join(lc, "picks_by_param.csv")) if r["currency"] == CURRENCY]
    const = next(r for r in read(os.path.join(lc, "constant_layouts.csv")) if r["currency"] == CURRENCY)
    totals = read(os.path.join(a.run, "uniform_totals.csv"))
    if {r["model"] for r in totals} != {CURRENCY}:
        raise SystemExit(f"layout_choice_accuracy: uniform_totals.csv's costs are not in {CURRENCY}")
    queries = [r for r in picks if r["kind"] == "query"]     # the rows past the 19 are the workloads
    (i02,) = [r for r in picks if r["what"] == "I02"]
    near = [r for r in totals if r["within"] == "1"]
    if len(queries) != 19 or len(qparams) != 95 or len(totals) != 49 or not near:
        raise SystemExit(f"layout_choice_accuracy: {len(queries)} queries, {len(qparams)} (query, param), "
                         f"{len(totals)} layouts, {len(near)} within: expected 19, 95, 49 and at least one")
    values = {
        "predict.sf3.queries": str(len(queries)),
        "predict.sf3.queries.named": str(sum(r["hit"] == "1" for r in queries)),
        "predict.sf3.queries.within3": str(sum(over(r) <= 1.03 + 1e-9 for r in queries)),
        "predict.sf3.queries.within10": str(sum(over(r) <= 1.10 + 1e-9 for r in queries)),
        "predict.sf3.qparams": str(len(qparams)),
        "predict.sf3.qparams.named": str(sum(r["hit"] == "1" for r in qparams)),
        "predict.sf3.w17.named": i02["named_short"],
        "predict.sf3.w17.rank": i02["measured_rank_of_named"],
        "predict.sf3.w17.over": f"{over(i02):.3f}",
        "predict.sf3.w17.spearman": f"{float(i02['spearman']):.3f}",
        "predict.sf3.w17.top5": i02["top5_overlap"],
        "predict.sf3.constant.layout": const["layout"],
        "predict.sf3.constant.queries": const["queries_cheapest"],
        "predict.sf3.constant.w17over": f"{float(const['workload_over_best']):.3f}",
        "predict.sf3.within5.count": str(len(near)),
        "predict.sf3.within5.layouts": ", ".join(r["layout"] for r in near),
        "predict.sf3.within5.over": f"{float(near[-1]['over_best']):.3f}",
    }
    rows = [{"what": r["what"], "named": r["named_short"], "measured_best": r["measured_best_short"], "hit": r["hit"],
             "named_measured_over_best": r["named_measured_over_best"],
             "measured_rank_of_named": r["measured_rank_of_named"]} for r in picks]
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "predict_accuracy.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["what"]))
    with open(os.path.join(a.out, "predict_accuracy_numbers.json"), "w") as fh:
        json.dump({"currency": CURRENCY, "from": a.run, "values": values}, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"wrote {a.out}/predict_accuracy.csv ({len(rows)} rows), predict_accuracy_numbers.json ({len(values)} keys)")


if __name__ == "__main__":
    main()
