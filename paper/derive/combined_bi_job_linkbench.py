#!/usr/bin/env python3
"""Derive combined-bi-job-linkbench's table: LDBC BI, IMDb / JOB and LinkBench at SF3, the nine uniform layouts, the
fewest-combined mix (per type and direction, no byte limit) and the ceiling, under the combined cost
(wops + bytes / 4,096 per query).

Reads COMBINED_OTHER_SF3's combined_options.csv (experiments/mix_benefit/combined_other_benchmarks.py made it) and the
three mixes' proven MIX_SEARCH_SF3 rows; checks it holds the nine, the mix and the ceiling for each benchmark, and that
each mix is its proven row's assignment. Writes build/data/combined-bi-job-linkbench.csv: 33 rows (benchmark, option),
every column of the file. BI and JOB are scored by the geomean over their queries (the plotted form) and the additive
form the mix is solved on; LinkBench by its request log's counts.

    uv run python paper/derive/combined_bi_job_linkbench.py
"""
import csv
from kvevaluator.layouts import SETTINGS, cell_id

from snapshots import PAPER, COMBINED_OTHER_SF3, assignment_text, mix_search_row, write_table

OPTIONS = COMBINED_OTHER_SF3 / "combined_options.csv"
BENCHMARKS = ("LDBC BI", "IMDb / JOB", "LinkBench")
# each mix's row: its dataset and the paper's scale of it, its rows' dir, its weighting
ROWS = {"LDBC BI": ("ldbc", "3", "b01", "additive"), "IMDb / JOB": ("imdb", "full", "j01", "additive"),
        "LinkBench": ("linkbench", "n1m", "l01", "sum")}
WANT = [cell_id(p, a) for a in SETTINGS for p in SETTINGS] + ["mix", "floor"]


def main():
    with open(OPTIONS, newline="") as fh:
        reader = csv.DictReader(fh)
        columns, rows = list(reader.fieldnames), list(reader)
    for b in BENCHMARKS:
        have = sorted(r["option"] for r in rows if r["benchmark"] == b)
        if have != sorted(WANT):
            raise SystemExit(f"{OPTIONS}: {b} holds {have}, not the nine, the mix and the ceiling (floor)")
    for b, (dataset, scale, wid, weighting) in ROWS.items():
        row = mix_search_row(PAPER, scale, wid, "per-type-and-direction", dataset=dataset, question="fewest",
                             model="wops_bytes4k", limit=None, weighting=weighting)
        mix = next(r for r in rows if r["benchmark"] == b and r["option"] == "mix")
        if mix["assignment"] != assignment_text(row["assignment"]):
            raise SystemExit(f"{OPTIONS}: {b}'s mix is not its result row's ({wid}, {weighting})")
    if sorted({r["benchmark"] for r in rows}) != sorted(BENCHMARKS):
        raise SystemExit(f"{OPTIONS}: benchmarks {sorted({r['benchmark'] for r in rows})}, not {list(BENCHMARKS)}")
    write_table("combined-bi-job-linkbench", columns, rows, key=("benchmark", "option"))


if __name__ == "__main__":
    main()
