"""BI18 Friend recommendation (spec 8.4 BI/read/18; reference neo4j/queries/bi-18.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E


def bi18(args):
    """Pairs (person1, person2) of distinct Persons interested in $tag who do not know each other, by the number of
    distinct mutual friends; top 20 by that count desc, person1.id asc, person2.id asc.

    hasInterest has no reverse (the schema: forward-only), so the interested Persons come from the Person
    enumeration plus one hasInterest-out read each (BI8's entry). knows both ways from them (pairs), then knows
    both ways from the distinct friends; the far person kept when interested and not the start; "do not know each
    other" is a tuple-key test against the interested persons' own knows pairs; distinct (person1, friend, person2)
    then a count per pair. Ids read for the 20 winners (a vid orders as its id). Every far end is a Person by the
    schema: no label test."""
    return Plan([
        Step("tag",      "read_index_eq", label="Tag", prop="name", value=E.param("tag")),
        Step("pv",       "read_vertices_by_label", label="Person"),
        Step("hi",       "read_neighbors", srcs=ref("pv"), etype="hasInterest", dir="out", pairs=True),       # src = person, dst = tag
        Step("hi_t",     "filter_in", rows=ref("hi"), field="dst", allowed=ref("tag")),
        Step("p",        "pluck", rows=ref("hi_t"), field="src"),                                              # the interested persons
        Step("k1",       "read_neighbors", srcs=ref("p"), etype="knows", dir="both", pairs=True),             # src = person1, dst = friend
        Step("k1_n",     "rename", rows=ref("k1"), names={"src": "_p1", "dst": "_mf"}),
        Step("k1_key",   "derive", rows=ref("k1_n"), into="_k", fn=E.tuple_(E.field("_p1"), E.field("_mf"))),
        Step("knows",    "pluck", rows=ref("k1_key"), field="_k"),                                             # (interested, friend) pairs
        Step("mf0",      "pluck", rows=ref("k1_n"), field="_mf"),
        Step("mf",       "dedup", items=ref("mf0")),
        Step("k2",       "read_neighbors", srcs=ref("mf"), etype="knows", dir="both", pairs=True),            # src = friend, dst = person2
        Step("k2_i",     "filter_in", rows=ref("k2"), field="dst", allowed=ref("p")),
        Step("k2_n",     "rename", rows=ref("k2_i"), names={"src": "_mf", "dst": "_p2"}),
        Step("tri",      "join_multi", left=ref("k1_n"), right=ref("k2_n"), left_on="_mf", right_on="_mf"),
        Step("tri_ne",   "filter", rows=ref("tri"), pred=E.ne(E.field("_p1"), E.field("_p2"))),
        Step("tri_key",  "derive", rows=ref("tri_ne"), into="_k12", fn=E.tuple_(E.field("_p1"), E.field("_p2"))),
        Step("tri_f",    "flag_in", rows=ref("tri_key"), field="_k12", allowed=ref("knows"), into="_known"),
        Step("tri_u",    "filter", rows=ref("tri_f"), pred=E.not_(E.field("_known"))),
        Step("tri_k3",   "derive", rows=ref("tri_u"), into="_k3", fn=E.tuple_(E.field("_p1"), E.field("_mf"), E.field("_p2"))),
        Step("tri_d",    "dedup_rows", rows=ref("tri_k3"), field="_k3"),
        Step("cnt",      "group_agg", rows=ref("tri_d"), by=["_p1", "_p2"], aggs={"mutualFriendCount": ["count", None]}),
        Step("top",      "top_k", rows=ref("cnt"), keys=[("mutualFriendCount", "desc"), ("_p1", "asc"), ("_p2", "asc")], k=20),
        Step("p1v",      "pluck", rows=ref("top"), field="_p1"),
        Step("p2v",      "pluck", rows=ref("top"), field="_p2"),
        Step("p1i",      "read_vertex_props", vids=ref("p1v"), props=["id"], label="Person"),
        Step("p1n",      "rename", rows=ref("p1i"), names={"id": "person1Id"}),
        Step("p2i",      "read_vertex_props", vids=ref("p2v"), props=["id"], label="Person"),
        Step("p2n",      "rename", rows=ref("p2i"), names={"id": "person2Id"}),
        Step("j1",       "join", left=ref("top"), right=ref("p1n"), left_on="_p1", right_on="_vid"),
        Step("j2",       "join", left=ref("j1"), right=ref("p2n"), left_on="_p2", right_on="_vid"),
        Step("out",      "project", rows=ref("j2"), fields=["person1Id", "person2Id", "mutualFriendCount"]),
    ], params={"tag": {"type": "str"}})


SIM = {"bi18": bi18}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"bi18": _KNOWS_HINTS}
