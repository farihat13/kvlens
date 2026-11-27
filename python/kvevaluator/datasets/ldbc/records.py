"""Where LDBC's record stream is dumped from (the dataset manifest's records_source; measure/records.records_file):
the scale's CSV tree, read by the C++ reader (loader._cpp_binary)."""
import os

# what the dump imports (measure/records_key.python_part runs it in a fresh process; the modules it loads key the cache)
DUMP_IMPORTS = ("from kvevaluator.datasets.ldbc import loader\n"
                "from kvevaluator.datasets.ldbc.schema import get_schema\n"
                "get_schema('canonical')\n")
USES_CPP_READER = True     # the C++ CSV reader's sources are part of the cache's key
PROBE_SCALE = "0.1"   # a larger scale's key holds this scale's dumped bytes in place of the code's files (records_key.code_part)
PROBE_TREE = "python/kvevaluator/datasets/ldbc/records_probe/CsvBasic-LongDateFormatter"   # and the dump of this edge-case tree (its README)


def source(scale, data=None, variant=None):
    """(the CSV tree, what identifies its content: every file's size and mtime)"""
    if data:
        raise SystemExit("ldbc: the record stream is dumped from the scale's CSV tree; a data dir is not taken "
                         "(a data variant is: variant=)")
    from kvevaluator.datasets.ldbc.loader import _root
    root = _root(scale, variant)
    raw = {}
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            st = os.stat(p)
            raw[os.path.relpath(p, root)] = [st.st_size, st.st_mtime_ns]
    if not raw:
        from kvevaluator.datasets import fetch_hint
        raise SystemExit(f"[records_key] no CSV files under {root}: fetch the data with `{fetch_hint('ldbc', scale)}`")
    return root, dict(sorted(raw.items()))


def place_probe_tree(data_dir):
    """lay PROBE_TREE out under the data dir `data_dir` as the probe scale's CSV tree (paths.ldbc_dir's layout), so a
    dump of the probe scale with KVEVAL_DATA_DIR=data_dir reads it (records_key.probe_tree_sha256)"""
    from kvevaluator.paths import ROOT
    scale = os.path.join(data_dir, "datasets", "ldbc", f"sf{PROBE_SCALE}")
    os.makedirs(scale)
    os.symlink(os.path.join(str(ROOT), PROBE_TREE), os.path.join(scale, os.path.basename(PROBE_TREE)))


def check_dumpable(scale, data=None, variant=None):
    """refuse a dump from CSVs fattened in place, or from a data variant with no CSV tree"""
    from kvevaluator.datasets.ldbc.loader import _root
    if variant is None and os.path.exists(os.path.join(_root(scale), "dynamic", "FATTENED")):
        raise SystemExit(f"[records] sf{scale}: the CSVs are FATTENED in place -- restore them before the "
                         f"plain record stream is dumped")
    if variant is not None and not os.path.isdir(_root(scale, variant)):
        raise SystemExit(f"[records] no CSV tree for the data variant {variant!r} at {_root(scale, variant)}")


def dump_argv(scale, data=None, variant=None):
    """records_dump's arguments for this scale"""
    return ["--sf", str(scale)] + ([] if variant is None else ["--variant", variant])
