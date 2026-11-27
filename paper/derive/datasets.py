#!/usr/bin/env python3
"""Derive the datasets claim: each dataset's vertex and edge counts, its types and its store's size, and the numbers the
text gives for each dataset's workload.

  <tables>/datasets.csv       one row per dataset (row: the table's order) and layout (Sp-Sp, the largest uniform
                              layout; Ch64k-Ch64k, the smallest): vertices, edges, vertex_types, edge_types, sst_bytes
  <tables>/datasets-sizes.csv one row per dataset and the seven layouts that give both parts one setting: sst_bytes
  <tables>/workloads.tex      workloads.ldbc.{ic,is,iu}.{queries,pct}, workloads.ldbc.params,
                              workloads.bi.{queries,params,list,left-out},
                              workloads.linkbench.{requests,reads.pct,writes.pct}

Reads, in the tree --from names at the run's scales (--dataset): each dataset's store manifests (stores/<dataset>/
<scale>/, found by layout: snapshots.store_manifests) and its workloads' run records on the seven, and the workloads
themselves (I01, B01, L01: their queries and frequencies). A dataset the run leaves out has no rows and no keys.
Counts and types come from the store manifests our ingest wrote (records.counts); LinkBench's run records keep only
totals, so its types come from its schema. Sizes are the stores' SST bytes under the tree's store settings (the
paper's archive: RocksDB without compression; a re-run: inputs/contract/store_config.json's codec). A layout's size is
read from every run record of it in the dataset's captures, which must agree; its Sp-Sp and Ch64k-Ch64k must equal
datasets.csv's. A dataset is named as the paper names it at the paper's scale (LDBC SNB, SF3), else with its scale.

    uv run python paper/derive/datasets.py [--from paper-results|output|<tree>] [--dataset ldbc=3,...] [--tables DIR]
"""
import argparse
import csv
import json
import re
from collections import defaultdict

from snapshots import add_tree_args, pct, scale_in, store_manifests, tree_of, write_numbers, write_table
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import query_weighting as QW
from kvevaluator import datasets as DS
from kvevaluator import sizes
from kvcost.layout_cost.workloads import linkbench as linkbench_workload
from kvevaluator.output_tree import Capture, capture_in, writes_in
from kvevaluator.datasets import schema_path
from kvevaluator.layouts import CHUNK_SIZES_KB, setting_id

LAYOUTS = ("props_split_adj_split", "props_chunked_adj_chunked")     # Sp-Sp, Ch64k-Ch64k: the largest and the smallest uniform layout
LINKBENCH_SCHEMA = schema_path("linkbench")
# the layouts that give both parts one setting: Sp-Sp, Co-Co, then ChK-ChK at each chunk size
ONE_SETTING = ("props_split_adj_split", "props_condensed_adj_condensed") + tuple(
    f"props_{setting_id('chunked', kb)}_adj_{setting_id('chunked', kb)}" for kb in CHUNK_SIZES_KB)
# the workloads whose run records give a dataset's store sizes (LDBC: Interactive's; BI's captures, in the same place,
# were measured on stores of their own)
SIZE_WORKLOADS = {"ldbc": ("interactive", "microbench", "writes"), "imdb": ("job",), "linkbench": ("linkbench", "linkbench_writes")}


def from_records(manifest):
    """(vertices, edges, vertex types, edge types, sst bytes): types = the labels in records.counts (a vertex label is
    capitalized, an edge type is not, as the schemas name them)"""
    r = manifest["records"]
    vtypes = sum(1 for k in r["counts"] if k[0].isupper())
    etypes = sum(1 for k in r["counts"] if not k[0].isupper())
    if sum(v for k, v in r["counts"].items() if k[0].isupper()) != r["vertices"]:
        raise SystemExit("the vertex labels' counts do not add up to the vertices")
    return r["vertices"], r["edges"], vtypes, etypes, manifest["sst_bytes"]


def ldbc(captures, stores):
    """the counts and types from the stores' manifests (they carry records.counts), the sizes from the stores
    Interactive's counts were measured on (its run records); one graph: Sp-Sp's keys must be equal in both"""
    out = {}
    for c, p in store_manifests(stores, LAYOUTS).items():
        m = json.load(open(p))
        now = json.load(open(capture_in(captures, "interactive").record(c)))["store_manifest"]
        if c == "props_split_adj_split" and now["keys"] != m["keys"]:
            raise SystemExit(f"Sp-Sp has {now['keys']} keys in Interactive's run record, {m['keys']} in {p}: not the same graph")
        out[c] = from_records(m)[:4] + (now["sst_bytes"],)
    return {c: out[c] for c in LAYOUTS}


def imdb(captures, stores):
    """Sp-Sp and Ch64k-Ch64k from their stores' manifests"""
    found = store_manifests(stores, LAYOUTS)
    return {c: from_records(json.load(open(found[c]))) for c in LAYOUTS}


def linkbench(captures, stores):
    schema = json.load(open(LINKBENCH_SCHEMA))
    out = {}
    for c in LAYOUTS:
        with open(capture_in(captures, "linkbench", "log").record(c)) as fh:
            m = json.load(fh)["store_manifest"]
        n = m["ingest_counts"]
        out[c] = (n["vertices"], n["edges"], len(schema["vertices"]), len(schema["edges"]), m["sst_bytes"])
    return out


def one_size(records, what):
    """the one SST size all of a layout's run records give (a dict name -> record), else refuse; a writes run's record
    carries no store manifest and is left out"""
    sizes = {name: r["store_manifest"]["sst_bytes"] for name, r in records.items() if "store_manifest" in r}
    if len(set(sizes.values())) != 1:
        raise SystemExit(f"{what}: {len(sizes)} run records with a store manifest, sizes {sorted(set(sizes.values()))}")
    return next(iter(sizes.values())), len(sizes)


def sizes_in_dirs(place, dataset):
    """{layout: (SST bytes, run records read)} of the seven, from every run record of the dataset's SIZE_WORKLOADS in its
    captures place"""
    out = {}
    for layout in ONE_SETTING:
        found = sorted(f for wl in SIZE_WORKLOADS[dataset] for f in (place / wl).glob(f"**/{layout}.json"))
        out[layout] = one_size({f: json.load(open(f)) for f in found}, f"{place} {layout}")
    return out


BASELINE = "props_split_adj_split"   # one measured cell's run files give the queries and params (every cell runs the same)
BI_DEFINED = 20                         # LDBC SNB BI defines BI1-BI20


def params_per_query(path, queries):
    """{query: number of params} in a grid run file, for the queries it lists"""
    seen = defaultdict(set)
    for r in csv.DictReader(open(path)):
        if r["query"] in queries:
            seen[r["query"]].add(r["param_idx"])
    return {q: len(v) for q, v in seen.items()}


def one_count(counts, what):
    if len(set(counts.values())) != 1:
        raise SystemExit(f"{what}: the queries have different numbers of params: {counts}")
    return next(iter(counts.values()))


def ranges(numbers, prefix):
    """[1, 2, 3, 5] -> 'BI1--BI3, BI5'"""
    runs, start = [], None
    for i, n in enumerate(numbers):
        start = n if start is None else start
        if i + 1 == len(numbers) or numbers[i + 1] != n + 1:
            runs.append(f"{prefix}{start}" if start == n else f"{prefix}{start}--{prefix}{n}")
            start = None
    return ", ".join(runs)


def workload_keys(where, run):
    """the workload facts the text states, for the datasets of the run (where: {dataset: (captures, stores)}): LDBC
    Interactive's mix (I01: LDBC's frequencies), BI's queries (B01's), LinkBench's request log (L01's counts)"""
    out = {}
    if "ldbc" in where:
        captures = where["ldbc"][0]
        # LDBC Interactive: I01's items, by family (complex reads, short reads, inserts)
        (i01,) = [w for w in benchmarks.workloads("ldbc", sizes.scale(run, "ldbc")).values() if w["id"] == "i01"]
        items = i01["items"]
        by = defaultdict(list)
        for _, q, n, _ in items:
            by[re.match(r"[a-z]+", q).group()].append((q, n))
        if set(by) != {"ic", "is", "iu"}:
            raise SystemExit(f"I01's families: {sorted(by)}")
        total = sum(n for _, q, n, _ in items)
        for fam in ("ic", "is", "iu"):
            out[f"workloads.ldbc.{fam}.queries"] = str(len(by[fam]))
            out[f"workloads.ldbc.{fam}.pct"] = pct(sum(n for _, n in by[fam]) / total)
        reads = {q for fam in ("ic", "is") for q, _ in by[fam]}
        out["workloads.ldbc.params"] = str(one_count(
            {**params_per_query(capture_in(captures, "interactive").path_of("counts", BASELINE), reads),
             **params_per_query(writes_in(captures, "ldbc").path_of("counts", BASELINE), {q for q, _ in by["iu"]})},
            "LDBC Interactive"))
        # LDBC BI: the queries the grid runs (B01's), and their params
        bi = params_per_query(capture_in(captures, "bi").path_of("counts", BASELINE),
                              {f"bi{i}" for i in range(1, BI_DEFINED + 1)})
        if set(bi) != {q for _, q, *_ in QW.equal_share("b01")["items"]}:
            raise SystemExit("BI: the queries measured differ from B01's")
        ran = sorted(int(q[2:]) for q in bi)
        left = [i for i in range(1, BI_DEFINED + 1) if i not in ran]
        out["workloads.bi.queries"] = str(len(bi))
        out["workloads.bi.params"] = str(one_count(bi, "LDBC BI"))
        out["workloads.bi.list"] = ranges(ran, "BI")
        out["workloads.bi.left-out"] = ", ".join(f"BI{i}" for i in left[:-1]) + f" and BI{left[-1]}"
    if "linkbench" in where:
        # LinkBench: its request log's counts (L01), read and write request types as its workload module names them
        (l01,) = [w for w in benchmarks.workloads("linkbench", sizes.scale(run, "linkbench")).values() if w["id"] == "l01"]
        lb = {q: float(n) for _, q, n, _ in l01["items"]}
        if set(lb) - set(linkbench_workload.READS) - set(linkbench_workload.WRITES):
            raise SystemExit(f"LinkBench: request types neither read nor write: {set(lb) - set(linkbench_workload.READS) - set(linkbench_workload.WRITES)}")
        n = sum(lb.values())
        if n != int(n):
            raise SystemExit(f"LinkBench: the log's counts do not sum to a whole number: {n}")
        out["workloads.linkbench.requests"] = f"{int(n):,}"
        out["workloads.linkbench.reads.pct"] = pct(sum(lb[q] for q in lb if q in linkbench_workload.READS) / n)
        out["workloads.linkbench.writes.pct"] = pct(sum(lb[q] for q in lb if q in linkbench_workload.WRITES) / n)
    return out


COLUMNS = ("row", "dataset", "layout", "vertices", "edges", "vertex_types", "edge_types", "sst_bytes")
SIZE_COLUMNS = ("row", "dataset", "layout", "sst_bytes")


# the datasets in the table's order: (dataset, its reader, the paper's name at the paper's scale, its name at another)
DATASETS = (("ldbc", ldbc, "LDBC SNB, SF3", "LDBC SNB"), ("imdb", imdb, "IMDb", "IMDb"),
            ("linkbench", linkbench, "LinkBench, 1M nodes", "LinkBench"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    a = ap.parse_args()
    tree, paper = tree_of(a.tree), sizes.resolve(sizes.PAPER)
    where, datasets = {}, []
    for ds, read, paper_name, name in DATASETS:
        sc = scale_in(a, ds)
        if sc is None:
            print(f"{ds}: not in this run (--dataset): its rows and keys are left out")
            continue
        where[ds] = (tree.place_dir("captures", ds, sc), tree.place_dir("stores", ds, sc))
        datasets.append((ds, paper_name if sc == sizes.scale(paper, ds) else f"{name}, {DS.get(ds).scale_name(sc)}",
                         read(*where[ds])))
    # the workloads run on each dataset are described in the text, not in this table
    rows = []
    for i, (_, name, d) in enumerate(datasets, 1):
        if len({v[:4] for v in d.values()}) != 1:
            raise SystemExit(f"{name}: the layouts disagree on the counts")
        for c, (v, e, vt, et, sst) in d.items():
            rows.append({"row": i, "dataset": name, "layout": c, "vertices": v, "edges": e, "vertex_types": vt,
                         "edge_types": et, "sst_bytes": sst})
            print(f"{name}, {c}: {v:,} vertices, {e:,} edges, {vt} vertex types, {et} edge types, {sst:,} SST bytes")
    write_table("datasets", COLUMNS, rows, key=("row", "layout"), out_dir=a.tables)
    size_rows = []
    for i, (ds, name, d) in enumerate(datasets, 1):
        sized = sizes_in_dirs(where[ds][0], ds)
        for c in LAYOUTS:
            if sized[c][0] != d[c][4]:
                raise SystemExit(f"{name}, {c}: {sized[c][0]:,} SST bytes in its run records, {d[c][4]:,} in the datasets table")
        for layout in ONE_SETTING:
            sst, n = sized[layout]
            size_rows.append({"row": i, "dataset": name, "layout": layout, "sst_bytes": sst})
            print(f"{name}, {layout}: {sst:,} SST bytes ({n} run records agree)")
    # the table's order: by dataset, then the layouts as ONE_SETTING lists them (Sp-Sp, Co-Co, ChK-ChK by chunk size)
    write_table("datasets-sizes", SIZE_COLUMNS, size_rows, key=None, out_dir=a.tables)
    keys = workload_keys(where, a.run)
    for k, v in keys.items():
        print(f"  {k} = {v}")
    write_numbers("workloads", keys, "I01, B01 and L01 (inputs/workloads); the run records and counts of the tree's "
                  "captures on Sp-Sp", out_dir=a.tables)


if __name__ == "__main__":
    main()
