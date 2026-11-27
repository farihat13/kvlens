"""Where LinkBench's record stream is dumped from (the dataset manifest's records_source; measure/records.records_file)."""
import os

DUMP_IMPORTS = "from kvevaluator.datasets.linkbench import loader\n"   # what the dump imports (records_key.python_part)
USES_CPP_READER = False
# no PROBE_SCALE: the stream is keyed by the dump's files at every scale (records_key.code_part)
RECORDS_VERSION_BEFORE_KEY = 2   # the format of a cache made before the freshness key (its .src.json); read by the adoption


def source(scale, data=None, variant=None):
    """(the data dir, what identifies its content): the exported JSONL dir the caller names, else the scale's
    <data dir>/datasets/linkbench/<scale>/data when it exists, by its files' sizes; (None, None) when there is none (a cached stream
    is reused as it is)"""
    if variant is not None:
        raise SystemExit("linkbench has no data variants")
    if not data:
        from kvevaluator.datasets import data_in
        default = str(data_in("linkbench", scale) / "data")
        if not os.path.isdir(default):
            return None, None
        data = default
    return data, {"jsonl_bytes": {fn: os.path.getsize(os.path.join(data, fn)) for fn in ("nodes.jsonl", "links.jsonl")}}


def check_dumpable(scale, data=None, variant=None):
    if not data:
        from kvevaluator.datasets import fetch_hint
        raise SystemExit(f"linkbench scale {scale!r}: no cached records and no data: `{fetch_hint('linkbench', scale)}` "
                         f"makes it")


def dump_argv(scale, data=None, variant=None):
    return ["--workload", "linkbench", "--data", data]
