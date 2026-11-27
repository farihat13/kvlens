"""Graph source for the evaluator. A tiny demo graph (the design-notes worked
example, plus an incoming edge so direction is testable); load(sf) reads LDBC."""
from kvevaluator.schema.graph import Graph
from kvevaluator.schema.schema import Schema
from kvevaluator.schema.schemadef import STR, INT, DATE

# The demo facts the selftest expectations encode -- kept adjacent to the graph
# they describe: Tag/TagClass.name feed read_index_eq; Person.creationDate is day-indexed
# so read_index_range has a value-range to scan; knows is traversed dir="both", so dgraph
# needs its reverse blobs. No list-valued cols, no type groups.
# vertex_props/edge_endpoints are what the WRITE side reads (Schema.props_of /
# incident_etypes): deleting a vertex has to tombstone one posting per declared column and reach
# every edge type that can touch the label. They are declared here for the same reason the rest of
# this block is -- the demo schema is hand-built, not compiled from a .schema file.
DEMO_SCHEMA = Schema(indexed_cols=frozenset({("Tag", "name"), ("TagClass", "name")}),
                     date_cols=frozenset({("Person", "creationDate")}),
                     reverse_etypes=frozenset({"knows"}),
                     etypes=frozenset({"knows"}),
                     edge_endpoints={"knows": (("Person", "Person"),)},
                     vertex_props={"Person": ("name", "age", "creationDate"),
                                   "Tag": ("name",), "TagClass": ("name",)},
                     # Ordered (prop, type) per label/etype -- the codecs' decode schema
                     # (nebula RowV2 is positional). Hand-declared like the rest of this
                     # block; compile_schema fills these for the LDBC schemas.
                     vertex_prop_types={"Person": (("name", STR), ("age", INT),
                                                   ("creationDate", DATE)),
                                        "Tag": (("name", STR),),
                                        "TagClass": (("name", STR),)},
                     edge_prop_types={"knows": (("since", INT),)})


def schema(sf=None):
    """The Schema matching load(sf): demo facts for the tiny graph, LDBC else."""
    if sf is None:
        return DEMO_SCHEMA
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
    return LDBC_SCHEMA


def tiny():
    """A knows B, C ; D knows A. Directed, like the LDBC dataset stores knows (each
    friendship once) -- so out(A) = {B, C}, in(A) = {D}, both(A) = {B, C, D}.
    Persons carry a day-indexed creationDate (days 100/102/104/106); the GAPS keep
    dgraph's day-granular read_index_range walk exact for a [day101, day105) window."""
    d = 86400000                                     # ms per day
    g = Graph()
    g.add_vertex("A", "Person", {"name": "Alice", "age": 50, "creationDate": 100 * d})
    g.add_vertex("B", "Person", {"name": "Bob", "age": 35, "creationDate": 102 * d})
    g.add_vertex("C", "Person", {"name": "Tom", "age": 15, "creationDate": 104 * d})
    g.add_vertex("D", "Person", {"name": "Dan", "age": 40, "creationDate": 106 * d})
    g.add_edge("knows", "A", "B", {"since": 2021}, "Person", "Person")
    g.add_edge("knows", "A", "C", {"since": 2025}, "Person", "Person")
    g.add_edge("knows", "D", "A", {"since": 2020}, "Person", "Person")
    # two same-named tags + a tagclass: read_index_eq / read_filter_by_label_probe coverage
    # (prefixed vids so arango's collection-from-prefix rule applies)
    g.add_vertex("tag_1", "Tag", {"name": "cool"})
    g.add_vertex("tag_2", "Tag", {"name": "cool"})
    g.add_vertex("tagclass_1", "TagClass", {"name": "misc"})
    return g


def load(sf=None, schema=None):
    if sf is None:
        return tiny()                   # the demo graph ignores `schema` (declares all props)
    from kvevaluator.datasets.ldbc import loader
    return loader.load(sf, schema=schema)
