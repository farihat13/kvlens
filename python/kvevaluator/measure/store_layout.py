"""A store's index + filter layout (its manifest's `index_filter_blocks`) against the one this binary ingests: the shared
check every latency driver and store-currency test calls. A store is
`partitioned` and a bounded cache holds its index and filter partitions, so every format pays for the
metadata it touches; a `whole` store (or a manifest with no field, an older store) is the old layout."""
import json
import subprocess

from kvevaluator.capture.binary import binary_path
from kvevaluator.capture.store_identity import name_holds_codec
from kvevaluator.contract import load, store_build

_DEFAULT = {}                       # binary path -> its ingest default (KVEVAL_CPP_BIN may name another binary)


def ingest_default():
    """The layout this binary gives a new store (`kvevaluator --version`'s `ingest_index_filter_blocks`); a binary
    from before the field ingested whole stores."""
    b = binary_path()
    if b not in _DEFAULT:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, check=True).stdout
        _DEFAULT[b] = json.loads(out).get("ingest_index_filter_blocks", "whole")
    return _DEFAULT[b]


def of_manifest(manifest):
    """a manifest's layout: whole when it names none (a store from before the field)"""
    return manifest.get("index_filter_blocks", "whole")


def is_current(manifest):
    """True when the store has the layout this binary ingests"""
    return of_manifest(manifest) == ingest_default()


# a manifest that records no filter: its store has RocksDB's 10-bit Bloom filter (store_config.json
# up to version 3)
BLOOM_BEFORE_THE_FIELD = 10


# a capture record that keeps no codec of its store: every store it can be from was built without one
# (a -<codec> store's old record reads as none too, and is measured again once)
CODEC_BEFORE_THE_FIELD = "none"


def build_settings(manifest):
    """(block codec, Bloom bits per key) a store's SSTs were written with, from its manifest or a capture record's copy
    of it (0 bits: no filter)"""
    return (manifest.get("compression", CODEC_BEFORE_THE_FIELD),
            manifest.get("bloom_bits_per_key", BLOOM_BEFORE_THE_FIELD))


def build_settings_stale(path, manifest):
    """why a store's codec or filter is not what this tree builds for its name (inputs/contract/store_config.json: the codec
    of a name that carries none, the filter of every store), else None. Readers that compare counts may skip it with the
    index + filter layout (store_staleness(layout=False)): the counts are taken above RocksDB; a size or a time is not"""
    codec, bloom = build_settings(manifest)
    if not name_holds_codec(path, codec):
        return (f"compression {codec!r}, its name says {store_build('compression')!r} (store_config.json version "
                f"{_config_version()}: a store built before it)")
    if bloom != store_build("bloom_bits_per_key"):
        return (f"bloom_bits_per_key {bloom}, this tree builds {store_build('bloom_bits_per_key')} (store_config.json "
                f"version {_config_version()}: a store built before it)")
    return None


def _config_version():
    return load("store_config")["version"]


def refuse_stale(manifests, allow_whole=False, what="a latency run"):
    """SystemExit naming every store whose layout is not this binary's, unless allow_whole (a named control).
    manifests: {name: manifest dict}"""
    stale = {n: of_manifest(m) for n, m in sorted(manifests.items()) if not is_current(m)}
    if stale and not allow_whole:
        raise SystemExit(f"{what}: {len(stale)} store(s) with index + filter blocks other than this binary's "
                         f"{ingest_default()!r}: " + ", ".join(f"{n} ({v})" for n, v in stale.items())
                         + " -- re-ingest them (python3 -m kvevaluator.measure.reingest_stores --sf <SF> --stale --to-default-layout), "
                         "or --allow-whole-index-filter (a control)")
    return stale
