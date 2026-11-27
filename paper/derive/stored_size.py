#!/usr/bin/env python3
"""Derive stored-size's tables: per dataset and scale (LDBC SF3, IMDb full, LinkBench n1m) and layout, how many
items a store holds and their bytes, per store part (vertex_props, adjacency, index), and what a DynamoDB-style service
bills for storing them. The layouts: the 49 uniform layouts and the mixes the paper names (DATASETS), each sized on the
data it was found for.

No store is built: a layout's or mix's tally is assembled part by part (kvcost.layout_cost.stored_size) from the tally
libraries (TALLY_*, each a stats/<dataset>/<scale>/tallies dir: keys, key bytes and value bytes per store part, counted
from the record stream by the C++ tally); library_list_sha256 is stored_size.fingerprint of the library read. The
billed size is cost_models' (stored_bytes, storage_dollars), written beside the raw columns, never in place of them;
chunk_vs_item_limit compares the largest Chunked item's value with cost_models.ITEM_LIMIT_BYTES.

Writes build/data/stored-size-{ldbc-sf3,imdb-full,linkbench-n1m}.csv, each the 49 and its mixes (checked:
every layout and mix has a row, each total is the sum of its parts). Seconds.

    uv run python paper/derive/stored_size.py
"""
import json
from pathlib import Path

from kvcost.layout_cost import cost_models as CM
from kvcost.layout_cost import stored_size as Z
from kvcost.layout_search import export
from kvcost.predict_accuracy.layouts import short
from snapshots import (PAPER, JOB_LATENCY_1106, RESULTS, STEADY_LATENCY_SF3_1103, TALLY_IMDB_FULL, TALLY_LDBC_SF3,
                       TALLY_LINKBENCH_N1M, mix_search_row, write_table)

from kvcost.predict_accuracy import uniform_49_tallies as U49

PARTS = Z.PARTS
A7 = "per-type-any-size"          # mix-search's option set: each part any of the seven settings (Split, Condensed, Chunked at 1-256 KB)
PTD = "per-type-and-direction"    # 64 KB Chunked only
# one `mix_search_row` selector per model (the question "fewest": the model's lowest total)
BYTES4K, WOPS, BYTES, BILL = ({"question": "fewest", "model": m} for m in ("wops_bytes4k", "wops", "bytes", "bill_exact"))


def timed(root, workload, kind):
    """(name, source) of a timed mix's spec: <root>/specs/<set>/lat_<workload>_<kind>_mix_any_size.json, the one such file"""
    name = f"lat_{workload}_{kind}_mix_any_size"
    found = sorted(root.glob(f"specs/*/{name}.json"))
    if len(found) != 1:
        raise SystemExit(f"stored_size: {len(found)} spec files named {name} under {root}/specs, not one")
    return name, found[0]


def searched(name, folder, option_set, where):
    """(name, source) of a mix-search row's mix: the dataset's summary/<dataset>/<scale>/mix-search/<folder>/<option
    set>.json's row with `where`"""
    return name, (folder, option_set, where)


def spec_file_mixes(root, workloads, kinds):
    return [timed(root, w, k) for w in workloads for k in kinds]


# dataset -> (csv name, scale, tally library, its mixes as (name, spec file | (mix-search folder, option set, where)))
DATASETS = {
    "ldbc-sf3": ("ldbc", "3", TALLY_LDBC_SF3,
                 spec_file_mixes(STEADY_LATENCY_SF3_1103, ("i02", "b01"), ("measured", "predicted", "bill")) + [
                     searched("i01_fewest_wops_bytes4k_any_size", "i01", A7, BYTES4K),
                     searched("i01_lowest_bill_any_size", "i01", A7, BILL),
                     searched("i01_fewest_wops_per_type_direction", "i01", PTD, WOPS),
                     searched("i01_fewest_bytes_per_type_direction", "i01", PTD, BYTES),
                     searched("i01_fewest_wops_bytes4k_per_type_direction", "i01", PTD, BYTES4K)]),
    "imdb-full": ("imdb", "full", TALLY_IMDB_FULL,
                  spec_file_mixes(JOB_LATENCY_1106, ("j01",), ("measured", "predicted", "bill")) + [
                      searched("j01_fewest_wops_bytes4k_per_type_direction", "j01", PTD, BYTES4K)]),
    "linkbench-n1m": ("linkbench", "n1m", TALLY_LINKBENCH_N1M, [
        searched("l01_fewest_wops_bytes4k_any_size", "l01", A7, BYTES4K),
        searched("l01_lowest_bill_any_size", "l01", A7, BILL),
        searched("l01_fewest_wops_bytes4k_per_type_direction", "l01", PTD, BYTES4K)]),
}
EXPECTED_MIXES = {"ldbc-sf3": 11, "imdb-full": 4, "linkbench-n1m": 3}
COLUMNS = (["dataset", "scale", "kind", "cell", "layout", "mix_source", "items", "key_bytes", "value_bytes"]
           + [f"{p}_{k}" for p in PARTS for k in ("items", "key_bytes", "value_bytes")]
           + ["chunk_value_log2_max", "library", "library_list_sha256",
              "billed_bytes", "billed_gib", "storage_dollars_month", "chunk_vs_item_limit"])


def mix_spec(source, dataset, scale):
    """(spec, text of its source) of a mix: a spec file read as it is, or the spec a mix-search row of the dataset at
    the scale builds"""
    if isinstance(source, Path):
        with open(source) as fh:
            return json.load(fh), f"spec file {source.relative_to(RESULTS)}"
    folder, option_set, where = source
    row = mix_search_row(PAPER, scale, folder, option_set, dataset=dataset, **where)
    spec, _ = export.row_spec(row)
    return spec, f"mix-search/{folder}/{option_set}.json: {where['question']} {where['model']} (run {row['run_id']})"


def item_limit(log2):
    """cost_models.ITEM_LIMIT_BYTES against the largest Chunked item's value (floor(log2 bytes))"""
    if log2 is None:
        return "none"
    if 2 ** log2 >= CM.ITEM_LIMIT_BYTES:             # the value alone is 2^log2 bytes or more
        return "exceeds"
    if 2 ** (log2 + 1) <= CM.ITEM_LIMIT_BYTES:       # the value is under 2^(log2 + 1) bytes: room for any key
        return "below"
    return "may_exceed"


def row_of(dataset, scale, kind, cell, layout, source, size, library, library_sha):
    billed = CM.stored_bytes(size["keys"], size["key_bytes"], size["value_bytes"])
    row = {"dataset": dataset, "scale": scale, "kind": kind, "cell": cell, "layout": layout, "mix_source": source,
           "items": size["keys"], "key_bytes": size["key_bytes"], "value_bytes": size["value_bytes"],
           "chunk_value_log2_max": "" if size["chunk_value_log2_max"] is None else size["chunk_value_log2_max"],
           "library": library, "library_list_sha256": library_sha, "billed_bytes": billed,
           "billed_gib": billed / CM.GB, "storage_dollars_month": CM.storage_dollars(billed),
           "chunk_vs_item_limit": item_limit(size["chunk_value_log2_max"])}
    for p in PARTS:
        row.update({f"{p}_items": size[f"{p}_keys"], f"{p}_key_bytes": size[f"{p}_key_bytes"],
                    f"{p}_value_bytes": size[f"{p}_value_bytes"]})
    if any(sum(row[f"{p}_{k}"] for p in PARTS) != row[k] for k in ("items", "key_bytes", "value_bytes")):
        raise SystemExit(f"stored_size: {cell}: the parts do not add to the total")
    return row


def main():
    for table, (dataset, scale, library, mixes) in DATASETS.items():
        lib = Z.Frozen(str(library))
        sha = Z.fingerprint(str(library))
        rel = str(library.relative_to(RESULTS))
        rows = [row_of(dataset, scale, "uniform", c, short(c), "inputs/specs (uniform49)", Z.size_of(U49.spec_of(c), lib), rel, sha)
                for c in U49.CELLS]
        for name, source in mixes:
            spec, text = mix_spec(source, dataset, scale)
            rows.append(row_of(dataset, scale, "mix", name, "mix", text, Z.size_of(spec, lib), rel, sha))
        n_uniform = sum(r["kind"] == "uniform" for r in rows)
        n_mix = len(rows) - n_uniform
        if n_uniform != 49 or n_mix != EXPECTED_MIXES[table]:
            raise SystemExit(f"{table}: {n_uniform} uniform layouts and {n_mix} mixes, not 49 and {EXPECTED_MIXES[table]}")
        print(f"{table}: {n_uniform} uniform layouts + {n_mix} mixes = {len(rows)} rows")
        write_table(f"stored-size-{table}", COLUMNS, rows, key=("kind", "cell"))


if __name__ == "__main__":
    main()
