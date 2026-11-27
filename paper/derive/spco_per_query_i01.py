#!/usr/bin/env python3
"""Derive spco-per-query-i01's table: Sp-Co's cost per run of each of LDBC Interactive's 27 queries (I01) at SF3.

Each query's wops and bytes per run, the mean over its params, a write's last chunk charged half a chunk: the Sp-Co rows
of uniform-49-layouts-workloads' per-query costs (combined model, workload ldbc-sf3-interactive), read by that
derive's own query_rows from kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k's query_costs.csv. Checked: the 27 costs (wops_bytes4k) summed by I01's frequencies equal Sp-Co's total in
CHUNK_SIZE_UNIFORM_SF3's layouts.csv, relative 1e-12.

Writes build/data/spco-per-query-i01.csv: query, group (complex IC, short IS, update IU), frequency (I01's count), wops,
bytes; in I01's order.

    uv run python paper/derive/spco_per_query_i01.py
"""
import math

import uniform_49_layouts_workloads as U49
from snapshots import write_table

NAME = "spco-per-query-i01"
COLUMNS = ["query", "group", "frequency", "wops", "bytes"]
WORKLOAD = "ldbc-sf3-interactive"


def main():
    (spec,) = [s for s in U49.WORKLOADS if s[0] == WORKLOAD]
    w, _, per = U49.query_rows(spec, "combined")
    per = per[U49.REF]
    rows = [{"query": q, "group": U49.GROUPS[q[:2]], "frequency": often, "wops": float(ops), "bytes": float(nbytes)}
            for (_, q, often, _), (ops, nbytes, _) in zip(w["items"], per)]
    total = math.fsum(often * cost for (_, _, often, _), (_, _, cost) in zip(w["items"], per))
    pinned = U49.pinned_49(spec[6])[U49.REF]
    if not U49.close(total, pinned):
        raise SystemExit(f"{NAME}: Sp-Co's I01 total {total!r}, the pinned table's {pinned!r}")
    if len(rows) != 27:
        raise SystemExit(f"{NAME}: {len(rows)} queries, not 27")
    print(f"{NAME}: {len(rows)} queries; their frequency-weighted total {total:.6g} equals the pinned Sp-Co row")
    write_table(NAME, COLUMNS, rows, key=None)


if __name__ == "__main__":
    main()
