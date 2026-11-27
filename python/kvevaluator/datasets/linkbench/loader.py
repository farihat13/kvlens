"""LinkBench loader -- the neutral record stream from the real LinkBench datagen output
(nodes.jsonl / links.jsonl, exported by experiments/linkbench/FileDumpGraphStore during the LOAD
phase). Yields exactly the tuples records_dump serializes and a format materializes:

    ("v", vid, "Node", props)
    ("e", "link1" | "link2", src_vid, dst_vid, props, "Node", "Node")   (one edge type per LinkBench link_type)

vids are int-encoded via vids.vid("Node", id) (needs "Node" in vids._TAG). The data dir is the
directory holding nodes.jsonl/links.jsonl, from `data_dir` or $LINKBENCH_DATA. `schema` is accepted
for signature parity with the LDBC loader (LinkBench has one design, no prop filtering).
"""
import base64
import json
import os
import subprocess
import tempfile

from kvevaluator import jsonbytes
from kvevaluator.schema.graph import Graph
from kvevaluator.schema.vids import vid


def data_dir(explicit=None):
    d = explicit or os.environ.get("LINKBENCH_DATA")
    if not d:
        raise SystemExit("linkbench loader: set $LINKBENCH_DATA (or pass data_dir) to the dir "
                         "holding nodes.jsonl/links.jsonl")
    return d


def _node_props(r):
    return {"ntype": r["type"], "version": r["version"], "time": r["time"], "data": base64.b64decode(r["data"])}


# LinkBench link_type -> our edge type (one edge type per link type; see linkbench.schema)
LINK_TYPES = {123456789: "link1", 123456790: "link2"}


def _link_props(r):
    return {"visibility": r["visibility"], "version": r["version"], "time": r["time"], "data": base64.b64decode(r["data"])}


def stream(data=None, schema=None):
    """Vertices (in id order) then edges: the record contract records_dump writes. `data` is the JSONL dir (else
    $LINKBENCH_DATA).

    Edges are SORTED by (src, edge type, dst): the dgraph codec needs unique, sorted adjacency per (src, edge type),
    and LinkBench's links arrive in generation order. Each LinkBench link_type is its own edge type (LINK_TYPES), so
    a pair's two links are two edges; a repeated (id1, link_type, id2) is refused. The sort is EXTERNAL (a temp file +
    unix `sort`), O(1) memory at any node count; fixed-width hex vids make a byte sort uid order.
    """
    d = data_dir(data)
    with open(os.path.join(d, "nodes.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            yield ("v", vid("Node", r["id"]), "Node", _node_props(r))
    with tempfile.NamedTemporaryFile("w", dir=d, suffix=".edges.tmp", delete=False) as tmp:
        tmppath = tmp.name
        with open(os.path.join(d, "links.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                s, t = vid("Node", r["id1"]), vid("Node", r["id2"])
                et = LINK_TYPES.get(r["link_type"])
                if et is None:
                    raise SystemExit(f"linkbench loader: link_type {r['link_type']} has no edge type (LINK_TYPES)")
                tmp.write(f"{s}\t{et}\t{t}\t{jsonbytes.dumps(_link_props(r), separators=(',', ':'))}\n")
    try:
        env = {**os.environ, "LC_ALL": "C"}             # byte order: fixed-width hex vids -> uid order
        srt = subprocess.Popen(["sort", "-S", "4G", "-t", "\t", "-k1,1", "-k2,2", "-k3,3", tmppath],
                               stdout=subprocess.PIPE, text=True, env=env)   # -S caps the sort buffer
        prev, repeats = None, 0
        for line in srt.stdout:
            s, et, t, pj = line.rstrip("\n").split("\t", 3)
            if (s, et, t) == prev:                      # LinkBench keys links by (id1, link_type, id2): a
                repeats += 1                            # repeat would be a generator bug -- counted, refused
                continue
            prev = (s, et, t)
            yield ("e", et, s, t, jsonbytes.loads(pj), "Node", "Node")
        if repeats:
            raise SystemExit(f"linkbench loader: {repeats} repeated (id1, link_type, id2) links in the data")
        if srt.wait() != 0:
            raise SystemExit(f"linkbench loader: external edge sort failed (exit {srt.returncode})")
    finally:
        os.unlink(tmppath)


def load(data=None, schema=None):
    """Build an in-memory Graph from the stream (the abstract oracle's small-data path)."""
    g = Graph()
    for rec in stream(data, schema):
        if rec[0] == "v":
            g.add_vertex(rec[1], rec[2], rec[3])
        else:
            g.add_edge(rec[1], rec[2], rec[3], rec[4], rec[5], rec[6])
    return g
