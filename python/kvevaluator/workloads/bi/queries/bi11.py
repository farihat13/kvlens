"""BI11 Friend triangles (spec 8.4 BI/read/11; reference neo4j/queries/bi-11.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E


def bi11(args):
    """The number of distinct Person triples in $country (persons living in a City of it) with all three knows
    edges created in [$startDate, $endDate]. One row: count.

    Place.name -> isPartOf-in -> isLocatedIn-in (the residents, P); knows from P in both directions with the edge's
    creationDate (the facet read with the list), kept when the friend is in P, the date is in the range and
    a < b (each undirected edge once; a vid's order is its id's); the triangles by one join of the edge set with
    itself on the middle vertex (a < b < c) and a membership test of the closing edge (a, c) as a tuple key. Every
    far end here is a Person by the schema: no label test."""
    start, end = E.param("startDate"), E.param("endDate")
    cd = E.field("creationDate")
    return Plan([
        Step("c",        "read_index_eq", label="Place", prop="name", value=E.param("country")),
        Step("ci0",      "read_neighbors", srcs=ref("c"), etype="isPartOf", dir="in"),
        Step("ci",       "dedup", items=ref("ci0")),
        Step("p0",       "read_neighbors", srcs=ref("ci"), etype="isLocatedIn", dir="in"),
        Step("p",        "dedup", items=ref("p0")),
        Step("kn",       "read_neighbors", srcs=ref("p"), etype="knows", dir="both", edge_props=["creationDate"], pairs=True),
        Step("kn_in",    "filter_in", rows=ref("kn"), field="dst", allowed=ref("p")),
        Step("kn_w",     "filter", rows=ref("kn_in"), pred=E.and_(E.ge(cd, start), E.le(cd, end), E.lt(E.field("src"), E.field("dst")))),
        Step("e",        "rename", rows=ref("kn_w"), names={"src": "_a", "dst": "_b"}),
        Step("e_key",    "derive", rows=ref("e"), into="_ab", fn=E.tuple_(E.field("_a"), E.field("_b"))),
        Step("keys",     "pluck", rows=ref("e_key"), field="_ab"),
        Step("e2",       "rename", rows=ref("kn_w"), names={"src": "_b", "dst": "_c"}),
        Step("e2_p",     "project", rows=ref("e2"), fields=["_b", "_c"]),
        Step("tri",      "join_multi", left=ref("e"), right=ref("e2_p"), left_on="_b", right_on="_b",      # a < b < c
             where_key=E.tuple_(E.field("_a"), E.field("_c")), where_in=ref("keys")),                    # and a knows c
        Step("n",        "count", items=ref("tri")),
        Step("out",      "as_row", value=ref("n"), field="count"),
    ], params={"country": {"type": "str"}, "startDate": {"type": "int"}, "endDate": {"type": "int"}})


SIM = {"bi11": bi11}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"bi11": _KNOWS_HINTS}
