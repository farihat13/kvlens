"""The JOB workload's loader: the neutral record stream from a data tag's vertex and edge files
(build_tables.py), exactly the tuples records_dump serializes and a format materializes:

    ("v", vid, label, props)
    ("e", edge type, src_vid, dst_vid, props, src_label, dst_label)

A NULL column is an absent property. Edges come sorted by (source, target) per edge type, as the files are.
"""
import csv
import os

from kvevaluator.schema.graph import Graph
from kvevaluator.schema.vids import vid
from kvevaluator.datasets.imdb import mapping

csv.field_size_limit(1 << 30)


def data_dir(explicit=None):
    d = explicit or os.environ.get("IMDB_DATA")
    if not d:
        raise SystemExit("job loader: pass the data tag's dir (output/imdb/<tag>) or set $IMDB_DATA")
    return d


def _props(row, props):
    out = {}
    for p, _, t in props:
        v = row[p]
        if v != "":
            out[p] = int(v) if t == "int" else v
    return out


def stream(data=None, schema=None):
    """Vertices (label by label, id order) then edges (edge type by edge type)."""
    d = data_dir(data)
    for label, (_, props) in mapping.VERTICES.items():
        with open(os.path.join(d, "vertices", f"{label}.csv"), newline="") as f:
            for row in csv.DictReader(f):
                yield ("v", vid(label, row["id"]), label, _props(row, props))
    for etype, (_, sl, _, dl, _, props, _) in mapping.EDGES.items():
        with open(os.path.join(d, "edges", f"{etype}.csv"), newline="") as f:
            for row in csv.DictReader(f):
                yield ("e", etype, vid(sl, row["src"]), vid(dl, row["dst"]), _props(row, props), sl, dl)


def load(data=None, schema=None):
    """Build an in-memory Graph from the stream (the Python oracle's small-data path)."""
    g = Graph()
    for rec in stream(data, schema):
        if rec[0] == "v":
            g.add_vertex(rec[1], rec[2], rec[3])
        else:
            g.add_edge(rec[1], rec[2], rec[3], rec[4], rec[5], rec[6])
    return g
