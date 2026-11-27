"""Per-run features for a latency proxy, from a latency capture's steps.csv: counts + bytes the search also has."""
import csv
import math
import os
import sys
from collections import defaultdict

from kvevaluator.capture.step_record import HIT_BYTE_LANES, MISS_BYTE_LANES, READ_BYTE_LANES

BLOCK = 4096                               # the stores' RocksDB data block size (bytes)
# a steps.csv names its byte lanes <lane>_bytes (capture/step_record.py's lanes, one definition)
GET_LANES = tuple(f"{c}_bytes" for c in HIT_BYTE_LANES[:2])      # get_key, get_value
SCAN_LANES = tuple(f"{c}_bytes" for c in HIT_BYTE_LANES[2:])     # scan_key, scan_value
MISS_LANES = tuple(f"{c}_bytes" for c in MISS_BYTE_LANES)
# the byte total ("total bytes"): every read lane incl. the miss keys
BYTE_LANES = tuple(f"{c}_bytes" for c in READ_BYTE_LANES)
# which store component an op reads, from the op name (props / adjacency / index); the rest are flow ops.
# EVERY read op is listed (the first table left read_top_k_by_index, read_filter_by_label_*,
# read_anchor_props, read_chase, read_closure and read_vertices_by_label unclassified, which dropped their bytes from
# every "by component" feature -- dgraph IS lost 99.9% of its bytes, IC 26-28%); step_features refuses a read op with
# bytes that maps to no component
OP_CLASS = {"read_vertex_by_id": "props", "read_vertex_props": "props", "read_anchor_props": "props",
            "read_filter_by_label_probe": "props",      # a GET of the vertex to check its label
            "read_vertices_by_label": "props",          # a whole-type scan of vertex rows
            "read_neighbors": "adj", "read_edge_exists": "adj",
            "read_chase": "adj", "read_closure": "adj",  # walks over adjacency
            "read_index_eq": "index", "read_index_range": "index", "index_range_buckets": "index",
            "read_top_k_by_index": "index", "read_filter_by_label_index": "index"}
# the HOLD a format declares per component -- the codec family of the bytes an op reads (a byte
# price per codec, derived from the layout declaration). The vocabulary is the meta format's (cpp/kvevaluator/src/
# formats/meta/config.cc from_preset, mirrored here for the four presets): a component is grouped by `item` (one KV
# per item: dgraph's singleton props, nebula's per-edge keys, a sorted-keys index), by `owner` (one KV per vertex: a
# row, a posting list, a posting index) or `chunked` (owner + a pack: pertype_csr's 64 KB column / CSR chunks).
# arangodb is not a preset: its layout (one document per vertex, one edge document per edge, a sorted index) is
# declared here in the same vocabulary. A hybrid or a meta spec is not in the captures this study reads.
LAYOUT = {"dgraph": {"props": "item", "adj": "owner", "index": "owner"},
          "nebula": {"props": "owner", "adj": "item", "index": "item"},
          "dgneb": {"props": "owner", "adj": "owner", "index": "owner"},
          "pertype_csr": {"props": "chunked", "adj": "chunked", "index": "owner"},
          "arangodb": {"props": "owner", "adj": "item", "index": "item"}}
CODEC_KEYS = [f"bytes_{c}_{h}" for c in ("props", "adj", "index") for h in ("item", "owner", "chunked")]
# measured channels kept beside the features, to check them (never used as a feature: the search lacks them)
MEASURED = ("block_read_count", "block_read_byte", "block_cache_hit_count", "decode_ns", "chunks_decoded")


def f(r, c):
    return float(r.get(c) or 0)


def step_features(r, fmt=None):
    """one step row -> its additive features; `fmt` (the arm's base format) adds the per-codec byte split"""
    S, G, N = f(r, "S"), f(r, "G"), f(r, "N")
    get_b = sum(f(r, c) for c in GET_LANES)
    scan_b = sum(f(r, c) for c in SCAN_LANES)
    x = {"S": S, "G": G, "N": N, "bytes": sum(f(r, c) for c in BYTE_LANES),
         "get_bytes": get_b, "scan_bytes": scan_b, "miss_bytes": sum(f(r, c) for c in MISS_LANES),
         # the per-op ceil form: each op lands on >= 1 block, a big value spans more
         "est_blocks": (G * math.ceil(get_b / G / BLOCK) if G else 0) + (S * math.ceil(scan_b / S / BLOCK) if S else 0),
         # DISCOVERED (from the error analysis; checked against block_read_count): the per-op
         # ceil above over-counts pertype_csr's 64 KB chunk reads (8.2x cold on c64k), but the mechanism first written
         # here ("a landing is one block read") is NOT what the counters show: the row formats read 0.09-0.15 blocks
         # per landing cold (most landings hit the block cache within the query) and pertype_csr 1.5-2.7 big blocks per
         # landing. `landings` = S + G, `scan_blocks` (the bytes a scan streams beyond its landing, per 4 KB -- which
         # re-charges the chunk read as a SEEK plus a scan) and `big_value_bytes` are kept as the features that were
         # tested and lost; the counters themselves are diagnostics only (the interface-only rule).
         "landings": S + G,
         "scan_blocks": max(0.0, scan_b - S * BLOCK) / BLOCK,
         "big_value_bytes": max(0.0, get_b - G * BLOCK) + max(0.0, scan_b - S * BLOCK)}
    cls = OP_CLASS.get(r["op"], "index" if r["op"].startswith("read_index") else None)
    if cls is None and r["op"].startswith("read_") and x["bytes"] > 0:
        raise SystemExit(f"features: read op {r['op']!r} reads bytes but maps to no component (OP_CLASS)")
    for k in ("props", "adj", "index"):
        x[f"bytes_{k}"] = x["bytes"] if cls == k else 0.0
    # the key / value split (an interface quantity: the lanes), and the per-codec split by the declared hold
    x["key_bytes"] = f(r, "get_key_bytes") + f(r, "scan_key_bytes")
    x["value_bytes"] = f(r, "get_value_bytes") + f(r, "scan_value_bytes")
    for ck in CODEC_KEYS:
        x[ck] = 0.0
    if fmt is not None and cls is not None:
        if fmt not in LAYOUT:
            raise SystemExit(f"features: no declared layout for format {fmt!r} (LAYOUT)")
        x[f"bytes_{cls}_{LAYOUT[fmt][cls]}"] = x["bytes"]
    for c in MEASURED:
        x[c] = f(r, c)
    return x


def load_capture(path, fmt=None):
    """steps.csv -> {(query, param_idx): {feature: value, "lat": plan wall ns}}: features summed over the steps"""
    runs = defaultdict(lambda: defaultdict(float))
    lat = {}
    for r in csv.DictReader(open(path)):
        k = (r["query"], r["param_idx"])
        if r["step"] == "__plan__":
            lat[k] = f(r, "wall_ns")
            continue
        for c, v in step_features(r, fmt).items():
            runs[k][c] += v
    out = {}
    for k in sorted(lat):
        out[k] = dict(runs[k])
        out[k]["lat"] = lat[k]
    return out


def load_regime(regime_dir, arms=None):
    """<regime>/<arm>/steps.csv for every arm present -> {arm: load_capture(...)}"""
    present = sorted(a for a in os.listdir(regime_dir) if os.path.exists(os.path.join(regime_dir, a, "steps.csv")))
    return {a: load_capture(os.path.join(regime_dir, a, "steps.csv"), base_of(a) if base_of(a) in LAYOUT else None)
            for a in present if arms is None or a in arms}


def regimes_of(root):
    """the regime dirs under a capture's ldbc/ (a dir holding <arm>/steps.csv)"""
    return sorted(r for r in os.listdir(root) if os.path.isdir(os.path.join(root, r)) and
                  any(os.path.exists(os.path.join(root, r, a, "steps.csv")) for a in os.listdir(os.path.join(root, r))))


def base_of(arm):
    """an arm's format family (chunk variants share one)"""
    for b in ("pertype_csr", "dgneb", "dgraph", "nebula", "arangodb", "props_", "meta"):
        if arm.startswith(b):
            return b
    return arm
