"""BI6 Most authoritative users on a given topic (spec 8.4 BI/read/6; reference neo4j/queries/bi-6.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E


def bi6(args):
    """Per Person (person1) with a Message tagged $tag: authorityScore = the number of distinct likes on any Message of
    any person2 who liked a tagged message of person1 -- i.e. the sum, over the distinct person2, of the likes on all
    of person2's messages (a message has one creator, so the distinct like edges partition by person2). A person1
    with no liker scores 0 (the reference's OPTIONAL MATCH). Top 100 by score desc, person1.id asc.

    Tag.name index -> hasTag in (the tag's messages; the list holds Forums too) -> hasCreator out and likes in per
    message. No label test: a Forum's hasCreator-out and likes-in reads miss (charged as misses) and the result is
    unchanged. person2's messages by hasCreator in (Post | Comment by the schema), their likes in, counted per
    message, summed per person2; the (person1, person2) pairs are made distinct before the sum (count(DISTINCT like)).
    The winners' ids are read for the top 100 only (a vid orders as its id)."""
    return Plan([
        Step("tag",      "read_index_eq", label="Tag", prop="name", value=E.param("tag")),
        Step("msgs0",    "read_neighbors", srcs=ref("tag"), etype="hasTag", dir="in"),
        Step("msgs",     "dedup", items=ref("msgs0")),
        Step("creators", "read_neighbors", srcs=ref("msgs"), etype="hasCreator", dir="out", pairs=True),   # src = msg, dst = person1
        Step("likers",   "read_neighbors", srcs=ref("msgs"), etype="likes", dir="in", pairs=True),         # src = msg, dst = person2
        Step("cr_n",     "rename", rows=ref("creators"), names={"src": "_msg", "dst": "_p1"}),
        Step("lk_n",     "rename", rows=ref("likers"), names={"src": "_msg", "dst": "_p2"}),
        Step("pp0",      "join_multi", left=ref("cr_n"), right=ref("lk_n"), left_on="_msg", right_on="_msg"),
        Step("pp1",      "derive", rows=ref("pp0"), into="_k", fn=E.tuple_(E.field("_p1"), E.field("_p2"))),
        Step("pp",       "dedup_rows", rows=ref("pp1"), field="_k"),                                        # distinct (person1, person2)
        Step("p2v0",     "pluck", rows=ref("pp"), field="_p2"),
        Step("p2v",      "dedup", items=ref("p2v0")),
        Step("m2",       "read_neighbors", srcs=ref("p2v"), etype="hasCreator", dir="in", pairs=True),     # src = person2, dst = msg2
        Step("m2v",      "pluck", rows=ref("m2"), field="dst"),                                             # one creator per message: distinct already
        Step("l3",       "read_neighbors", srcs=ref("m2v"), etype="likes", dir="in", pairs=True),          # src = msg2, dst = person3
        Step("l3v",      "pluck", rows=ref("l3"), field="src"),
        Step("l3c",      "group_count", items=ref("l3v")),
        Step("l3n",      "rename", rows=ref("l3c"), names={"item": "_msg2", "count": "_likes"}),
        Step("m2n",      "rename", rows=ref("m2"), names={"src": "_p2", "dst": "_msg2"}),
        Step("m2l",      "left_join", left=ref("m2n"), right=ref("l3n"), left_on="_msg2", right_on="_msg2", default={"_likes": 0}),
        Step("pop",      "group_agg", rows=ref("m2l"), by=["_p2"], aggs={"_pop": ["sum", "_likes"]}),       # person2's popularity
        Step("ppp",      "left_join", left=ref("pp"), right=ref("pop"), left_on="_p2", right_on="_p2", default={"_pop": 0}),
        Step("sc",       "group_agg", rows=ref("ppp"), by=["_p1"], aggs={"authorityScore": ["sum", "_pop"]}),
        Step("p1s",      "dedup_rows", rows=ref("cr_n"), field="_p1"),
        Step("scored",   "left_join", left=ref("p1s"), right=ref("sc"), left_on="_p1", right_on="_p1", default={"authorityScore": 0}),
        Step("top",      "top_k", rows=ref("scored"), keys=[("authorityScore", "desc"), ("_p1", "asc")], k=100),
        Step("top_v",    "pluck", rows=ref("top"), field="_p1"),
        Step("ids",      "read_vertex_props", vids=ref("top_v"), props=["id"], label="Person"),
        Step("idn",      "rename", rows=ref("ids"), names={"id": "personId"}),
        Step("with_id",  "join", left=ref("top"), right=ref("idn"), left_on="_p1", right_on="_vid"),
        Step("out",      "project", rows=ref("with_id"), fields=["personId", "authorityScore"]),
    ], params={"tag": {"type": "str"}})


SIM = {"bi6": bi6}
