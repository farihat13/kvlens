#!/usr/bin/env python3
"""The layout report (python -m kvcost.predict_accuracy.layouts) on a predicted capture at a given scale, I02 on all 19 queries or on the 12 that
tier 1 alone prices at SF3 (ic1, ic3, ic5, ic6, ic9, ic10, ic11 left out, by --queries). I01 is the check workload.
Owner: experiments/predict_statistics (run-predict-statistics-results.sh, stage tier1).
argv: sf, predicted capture dir, out dir, 19|12, grid, check reference csv, dataset, cost workload, check workload (the
driver names them: ldbc, i02, i01)"""
import sys

from kvcost.layout_cost import benchmarks
from kvcost.predict_accuracy import layouts

LEFT_OUT = {"ic1", "ic3", "ic5", "ic6", "ic9", "ic10", "ic11"}
def main():
    sf, pred, out, n, grid, ref, dataset, cost_w, check_w = sys.argv[1:10]
    assert n in ("19", "12"), n
    argv = ["--dataset", f"{dataset}={sf}", "--predicted", pred, "--grid", grid,
            "--cost-workload", cost_w, "--check-workload", check_w, "--check-reference", ref, "--expect-layouts", "49",
            "--out", out]
    if n == "12":
        (w,) = benchmarks.select(dataset, sf, cost_w).values()
        keep = [q for _, q, _, _ in w["items"] if q not in LEFT_OUT]
        assert len(keep) == 12, keep
        argv += ["--queries", ",".join(keep)]
    sys.exit(layouts.main(argv))


if __name__ == "__main__":
    main()
