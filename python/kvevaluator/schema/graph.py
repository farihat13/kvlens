"""Neutral in-memory graph that every format materializes from, plus the shared per-value byte-size recursion.
A datum's size is its content size, the same across formats by default; a format's `value_bytes` may pass its own
leaf widths to `content_bytes`.
"""
from dataclasses import dataclass, field

from kvevaluator import jsonbytes


@dataclass
class Vertex:
    vid: str
    label: str
    props: dict


@dataclass
class Edge:
    etype: str
    src: str
    dst: str
    props: dict = field(default_factory=dict)
    src_label: str = None       # vertex labels ride along: arango's _from/_to are
    dst_label: str = None       # collection-qualified, so its format needs them


class Graph:
    def __init__(self):
        self.vertices = {}      # vid -> Vertex (insertion-ordered)
        self.edges = []         # list[Edge]

    def add_vertex(self, vid, label, props):
        self.vertices[vid] = Vertex(vid, label, props)

    def add_edge(self, etype, src, dst, props=None, src_label=None, dst_label=None):
        self.edges.append(Edge(etype, src, dst, props or {}, src_label, dst_label))


def content_bytes(v, int_bytes=8, bool_bytes=1):
    """Byte size of a stored value's CONTENT (str = its length; int/bool at the given leaf widths)."""
    if v is None:
        return 0
    if isinstance(v, (str, bytes, bytearray)):      # bytes: its length (a LinkBench data blob)
        return len(v)
    if isinstance(v, bool):                         # bool BEFORE int (bool subclasses int)
        return bool_bytes
    if isinstance(v, int):
        return int_bytes
    if isinstance(v, dict):
        if jsonbytes.TAG in v:                      # a bytes value in its JSON form ({"$bytes": base64}): its raw length
            return len(jsonbytes.object_hook(v))
        return sum(content_bytes(x, int_bytes, bool_bytes) for x in v.values())
    if isinstance(v, (list, tuple)):
        return sum(content_bytes(x, int_bytes, bool_bytes) for x in v)
    return 0


def value_bytes(v):
    """The NEUTRAL content size (default leaf widths): the format-agnostic answer size and the Format.value_bytes fallback."""
    return content_bytes(v)
