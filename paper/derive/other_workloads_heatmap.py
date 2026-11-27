#!/usr/bin/env python3
"""Derive other-workloads-heatmap's table: the nine uniform layouts on the four benchmarks the figure draws, each row's
summed costs under the three models and its ratio to the benchmark's best uniform layout.

Rows kept (benchmark, how its queries are counted): LDBC Interactive with LDBC's frequencies, LDBC BI and IMDb with the
JOB queries once each, LinkBench with its request log's frequencies. wops = G + 1.1 S + 0.01 N + P + D; bytes = read
(misses included) + written + read by writes; wops_bytes4k = wops + bytes / 4,096.

Reads MIX_BENEFIT_JOB_SF3's cross_benchmark_frequencies.csv (experiments/mix_benefit/cross_benchmark_frequencies.py),
checks each ratio against the file's own summed costs, and writes build/data/other-workloads-heatmap.csv: those 36 rows
with every column of that file.

    uv run python paper/derive/other_workloads_heatmap.py
"""
import csv

from snapshots import MIX_BENEFIT_JOB_SF3, write_table

SOURCE = MIX_BENEFIT_JOB_SF3 / "cross-benchmark-frequencies/cross_benchmark_frequencies.csv"
DRAWN = (("LDBC Interactive", "LDBC frequencies"), ("LDBC BI", "once each"), ("IMDb / JOB", "once each"),
         ("LinkBench", "its log's frequencies"))
MODELS = ("wops", "bytes", "wops_bytes4k")
RATIO_TOL = 1e-3            # the file keeps ratios to 4-5 significant digits


def main():
    with open(SOURCE, newline="") as fh:
        reader = csv.DictReader(fh)
        columns = list(reader.fieldnames)
        rows = [r for r in reader if (r["benchmark"], r["frequencies"]) in DRAWN]
    for bench, freq in DRAWN:
        mine = [r for r in rows if (r["benchmark"], r["frequencies"]) == (bench, freq)]
        if len({r["cell"] for r in mine}) != 9 or len(mine) != 9:
            raise SystemExit(f"{bench}, {freq}: {len(mine)} layouts, not the nine")
        for model in MODELS:
            best = min(float(r[model]) for r in mine)
            for r in mine:
                if abs(float(r[f"{model}_ratio"]) - float(r[model]) / best) > RATIO_TOL * float(r[model]) / best:
                    raise SystemExit(f"{bench}, {r['cell']}, {model}: ratio {r[model + '_ratio']} is not "
                                     f"{float(r[model]) / best:.4f}, its cost over the best")
    write_table("other-workloads-heatmap", columns, rows, key=("benchmark", "frequencies", "cell"))


if __name__ == "__main__":
    main()
