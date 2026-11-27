"""The neutral record stream for kvevaluator ingest: exactly the tuples loader.stream()
yields, one JSON array per line:

    ["v", vid, label, props]
    ["e", etype, src, dst, props, src_label, dst_label]

Vertices before edges; vids are the 16-hex encoded uids. Run:
    python3 -m kvevaluator.measure.records_dump --sf 0.1 [--schema canonical] > records.jsonl
"""
import json
import sys

from kvevaluator import jsonbytes

# The record stream's format version: a part of every stream's freshness key (measure/records_key.py, the code part).
# The rule: bump it with any change that can move the bytes a dump writes at a scale larger than sf0.1 but not at
# sf0.1 itself (ids past sf0.1's range, a hub shape, a value only larger data holds). A change that moves sf0.1's bytes
# needs no bump: the larger streams are keyed on sf0.1's dumped bytes (the probe) and are refused when those move.
DUMP_FORMAT_VERSION = 1


def dump(sf, schema_name="canonical", out=sys.stdout, workload="ldbc", data=None, variant=None):
    """Serialize a workload's (v/e) record stream. ldbc: keyed by `sf` + the compiled schema (+ `variant`: a data
    variant's CSV tree, loader._root). linkbench: keyed by `data` (the nodes.jsonl/links.jsonl dir, or
    $LINKBENCH_DATA) -- its scale axis is node count, not sf, so `sf` is unused there."""
    if workload == "linkbench":
        from kvevaluator.datasets.linkbench import loader
        stream = loader.stream(data)
    elif workload == "job":          # keyed by `data`: the data tag's dir (output/imdb/<tag>)
        from kvevaluator.datasets.imdb import loader
        stream = loader.stream(data)
    else:
        from kvevaluator.datasets.ldbc import loader
        from kvevaluator.datasets.ldbc.schema import get_schema
        stream = loader.stream(sf, schema=get_schema(schema_name), variant=variant)
    n = 0
    for rec in stream:
        out.write(json.dumps(list(rec), separators=(",", ":"), ensure_ascii=False, default=jsonbytes.default))
        out.write("\n")
        n += 1
    return n


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--schema", default="canonical")
    ap.add_argument("--workload", default="ldbc", choices=["ldbc", "linkbench", "job"])
    ap.add_argument("--data", default=None, help="linkbench: the nodes.jsonl/links.jsonl dir; job: output/imdb/<tag>")
    ap.add_argument("--variant", default=None, help="ldbc: a data variant's CSV tree (sf<SF>-<variant>/)")
    a = ap.parse_args(argv)
    n = dump(a.sf, a.schema, workload=a.workload, data=a.data, variant=a.variant)
    print(f"[records_dump] {n} records", file=sys.stderr)


if __name__ == "__main__":
    main()
