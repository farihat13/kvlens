"""Where IMDb's record stream is dumped from (the dataset manifest's records_source; measure/records.records_file)."""
import hashlib
import os

DUMP_IMPORTS = "from kvevaluator.datasets.imdb import loader\n"   # what the dump imports (records_key.python_part)
USES_CPP_READER = False
# no PROBE_SCALE: the stream is keyed by the dump's files at every scale (records_key.code_part)
RECORDS_VERSION_BEFORE_KEY = 1   # the format of a cache made before the freshness key (its .src.json); read by the adoption


def source(scale, data=None, variant=None):
    """(the data dir, what identifies its content): output/imdb/<tag> unless given, by its manifest's sha256 (every
    count of the build)"""
    if variant is not None:
        raise SystemExit("imdb has no data variants")
    from kvevaluator.datasets.imdb import paths as job_paths
    data = data or job_paths.tag_dir(str(scale))
    man = os.path.join(data, "manifest.json")
    if not os.path.exists(man):
        from kvevaluator.datasets import fetch_hint
        raise SystemExit(f"imdb: no {man} -- make the data tag: `{fetch_hint('imdb', scale)}`")
    with open(man, "rb") as f:
        return data, {"manifest_sha256": hashlib.sha256(f.read()).hexdigest()}


def check_dumpable(scale, data=None, variant=None):
    pass                   # source() refuses a tag with no data


def dump_argv(scale, data=None, variant=None):
    return ["--workload", "job", "--data", data]
