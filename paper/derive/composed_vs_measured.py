#!/usr/bin/env python3
"""Derive composed-vs-measured's table: composed against measured KV cost, every committed check, one row per check
(THREE_CELLS_SF3: six uniform layouts priced from three; COMPOSED_CHECK_SF3 and COMPOSED_CHECK_JOB: the mixed layouts
built for the steady latency runs, each experiments/mix_benefit/composed_vs_measured.py's output).

  build/data/composed-vs-measured.csv     row (the tabular's order), label, scale, layouts, rows (step rows compared),
                                          ops (all: every op count equal, else the script stops), bytes_differ and
                                          bytes_measured (blank where every row's bytes are equal), source (the dirs)
  build/numbers/composed_vs_measured.tex  composed.mixed.bytes.equal (the share of the bytes equal over every mixed
                                          layout built), composed.mixed.sf3.bytes.equal and
                                          composed.mixed.imdb.bytes.equal; rounded down, as the table

A row of a check = one (query, param, plan, step, store part) of the picked plan, reported and held-out params. "Ops
equal" = every operation count (S, G, N, P, D) of every row; "Bytes equal" = the share of the bytes (all lanes) that
equal the measured, "all" when none differ (every difference is a miss key's: the checks fail otherwise).

    uv run python paper/derive/composed_vs_measured.py
"""
import csv
import math

import snapshots
from snapshots import COMPOSED_CHECK_JOB, COMPOSED_CHECK_SF3, RESULTS, THREE_CELLS_SF3, write_numbers, write_table

MIX_CHECKS = (   # (label, scale, number key, the dirs of composed_vs_measured.py runs summed into the row)
    ("mixed", "SF3", "sf3", (COMPOSED_CHECK_SF3 / "interactive", COMPOSED_CHECK_SF3 / "bi")),
    ("mixed", "IMDb", "imdb", (COMPOSED_CHECK_JOB,)),
)   # the mixed layouts built; SF30 builds no mixes


def three_cells():
    """[(label, scale, layouts, rows, ops equal, bytes equal)] for the LDBC families and LinkBench"""
    out = []
    rows = list(csv.DictReader(open(THREE_CELLS_SF3 / "summary.csv")))
    for label, scale, keep in (("uniform, from 3", "SF3", lambda w: not w.startswith("linkbench")),
                               ("uniform, from 3", "LinkBench", lambda w: w.startswith("linkbench"))):
        part = [r for r in rows if keep(r["workload"])]
        n, exact = sum(int(r["rows"]) for r in part), sum(int(r["rows_exact"]) for r in part)
        if not part or n != exact:
            raise SystemExit(f"{THREE_CELLS_SF3}: {label}: {exact} of {n} rows exact")
        out.append((label, scale, len({r["cell"] for r in part}), n, "all", 0.0, "", THREE_CELLS_SF3 / "summary.csv"))
    return out


def mix_check(label, scale, dirs):
    """one row over a check's dirs: the layouts, the step rows of the picked plans, the bytes that differ and the
    measured bytes; stops when a dir compared 0 rows or any operation count differs"""
    n = diff = total = 0
    mixes = set()
    for d in dirs:
        picked = [r for r in csv.DictReader(open(d / "composed_vs_measured_by_row.csv")) if r["plans"] == "the picked plan"]
        ops = [r for r in picked if r["channel"] == "all_op_counts"]
        rows, bad = sum(int(r["rows_compared"]) for r in ops), sum(
            int(r["rows_differ"]) + int(r["rows_measured_only"]) + int(r["rows_composed_only"]) for r in ops)
        if rows == 0 or bad:
            raise SystemExit(f"{d}: {bad} of {rows} rows differ in an operation count")
        by_workload = [r for r in csv.DictReader(open(d / "composed_vs_measured_by_workload.csv")) if r["channel"] == "bytes"]
        n, diff = n + rows, diff + sum(abs(float(r["difference"])) for r in by_workload)
        total += sum(float(r["measured"]) for r in by_workload)
        mixes |= {(d, r["mix_set"], r["mix"]) for r in picked}
    return (label, scale, len(mixes), n, "all", diff, total, dirs)


def share_equal(diff, total):
    """the share of the bytes equal, in percent, rounded down so a difference never prints as 100 (the table's rule)"""
    return "100" if diff == 0 else f"{math.floor((1 - diff / total) * 1e7) / 1e5:.5f}"


COLUMNS = ("row", "label", "scale", "layouts", "rows", "ops", "bytes_differ", "bytes_measured", "source")


def main():
    mixed = [mix_check(label, scale, dirs) for label, scale, _, dirs in MIX_CHECKS]
    rows = []
    for i, (label, scale, k, n, ops, diff, total, source) in enumerate(three_cells() + mixed, 1):
        src = ";".join(str(p.relative_to(RESULTS)) for p in (source if isinstance(source, tuple) else (source,)))
        rows.append(dict(zip(COLUMNS, (i, label, scale, k, n, ops, diff, total, src))))
        print(f"{k} {label}: {scale}, {n:,} rows, ops {ops}, bytes {diff} of {total} differ")
    write_table("composed-vs-measured", COLUMNS, rows, key=("row",))
    numbers = {f"composed.mixed.{key}.bytes.equal": share_equal(m[5], m[6]) for (_, _, key, _), m in zip(MIX_CHECKS, mixed)}
    numbers["composed.mixed.bytes.equal"] = share_equal(sum(m[5] for m in mixed), sum(m[6] for m in mixed))
    write_numbers("composed_vs_measured", numbers, "the mixed layouts built for the steady latency figures, composed "
                  f"from the grid against measured: {', '.join(f'{m[2]} at {m[1]}' for m in mixed)} (percent of the bytes equal)")


if __name__ == "__main__":
    snapshots.tables_arg(__doc__)
    main()
