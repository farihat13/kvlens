"""The facts C++ and Python share, read from the tracked data files inputs/contract/*.json: the KV-op table, the
capability vocabulary and its classes, the capture columns. The C++ evaluator compiles the same files in
(cpp/kvevaluator/src/contract); cpp/kvevaluator/src/contract/README.md says what each field means and how to change one."""
import json
from functools import lru_cache

from kvevaluator.paths import CONTRACT_DIR


@lru_cache(maxsize=None)
def load(name):
    """one data file (kv_ops, capabilities, capture_columns), parsed once"""
    with open(CONTRACT_DIR / f"{name}.json") as f:
        return json.load(f)


def kv_ops():
    """the op table: one dict per op, in the file's order (reads first, then writes)"""
    return load("kv_ops")["ops"]


def capabilities():
    """{capability: its entry (class, implies, store_derived, stamped_at_emit, doc, evidence)}"""
    return load("capabilities")["capabilities"]


def store_build(name):
    """a setting a new store is built with (store_config.json's rocksdb block, compiled into the binary too):
    compression (the block codec of a store whose name carries none), bloom_bits_per_key (0: no filter)"""
    return load("store_config")["rocksdb"][name]


def chunk_coarse():
    """the pack target (bytes) from which a store holds coarse_chunks"""
    return load("capabilities")["chunk_coarse"]


def last_chunk_fill():
    """a Chunked family's last chunk's assumed share of its pack target (the half chunk view; a sampled tally)"""
    return load("capabilities")["last_chunk_fill"]


def columns(name, byte_suffix=""):
    """the columns a name stands for in capture_columns.json: a group, a response_keys entry (its keys, in order), a
    sequence or a file kind (each a list of names, expanded in order). A byte lane (a byte_lanes_* group) takes
    `byte_suffix` (the grid's files name a byte lane without one; cpplatency's steps.csv with "_bytes")."""
    c = load("capture_columns")
    if name in c["groups"]:
        return [col + byte_suffix if name.startswith("byte_lanes_") else col for col in c["groups"][name]]
    if name in c["response_keys"]:
        return list(c["response_keys"][name])
    seq = c["sequences"].get(name, c["files"].get(name))
    if seq is None:
        raise KeyError(f"inputs/contract/capture_columns.json: no group, sequence or file {name!r}")
    return [col for part in seq for col in columns(part, byte_suffix)]


def response_keys(name):
    """{column: the response field it reads} of a response_keys entry (hists, chunks)"""
    return dict(load("capture_columns")["response_keys"][name])
