"""BI8 Central person for a tag (spec 8.4 BI/read/8; reference neo4j/queries/bi-8.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import message_prop_steps


def bi8(args):
    """The row stores' body: the props read under the message labels is the type test (a Forum: a null row)."""
    return _bi8(args, typed="null")


def bi8_dgraph(args):
    """dgraph's body: the Tag's hasTag-in list holds Forums too; the type postings restrict it (message_prop_steps)."""
    return _bi8(args, typed="posting")


def _bi8(args, typed):
    """The Persons interested in $tag or who wrote a Message with $tag created in ($startDate, $endDate) (both
    bounds strict, as the reference); score = 100 if interested + the number of such Messages; friendsScore = the
    sum of the scores of the Person's knows-neighbors (a friend outside the set scores 0); top 100 by
    score + friendsScore desc, person.id asc.

    Tag.name index -> hasTag in -> the messages' dates -> hasCreator out (the authors). The interested Persons:
    the schema declares hasInterest FORWARD ONLY (Person -> Tag, no reverse adjacency, `reverse: false`), so no
    layout can enter it from the Tag; the plan the schema allows enumerates the Persons (type index) and reads
    each one's interest list, keeping those that list $tag -- a Person scan, charged to every format alike (the
    Interactive suite never reads hasInterest backwards; BI does, and this is its price under the declared
    schema). The union is the person set; knows both per person (pairs) with the friend's score joined (default
    0); sums and ranks are flow; the winners' ids are read for the top 100."""
    start, end = E.param("startDate"), E.param("endDate")
    return Plan([
        Step("tag",       "read_index_eq", label="Tag", prop="name", value=E.param("tag")),
        Step("persons_all", "read_vertices_by_label", label="Person"),
        Step("interests", "read_neighbors", srcs=ref("persons_all"), etype="hasInterest", dir="out", pairs=True),   # src = person, dst = tag
        Step("inter_rows", "filter_in", rows=ref("interests"), field="dst", allowed=ref("tag")),
        Step("inter0",    "pluck", rows=ref("inter_rows"), field="src"),
        Step("interested", "dedup", items=ref("inter0")),
        Step("msgs0",     "read_neighbors", srcs=ref("tag"), etype="hasTag", dir="in"),
        Step("msgs",      "dedup", items=ref("msgs0")),
    ] + message_prop_steps("msgs", ["creationDate"], typed) + [       # the messages among them (Forums carry hasTag too)
        Step("msg_w",     "filter", rows=ref("msg_d"), pred=E.and_(E.gt(E.field("creationDate"), start), E.lt(E.field("creationDate"), end))),
        Step("msg_wv",    "pluck", rows=ref("msg_w"), field="_vid"),
        Step("authors",   "read_neighbors", srcs=ref("msg_wv"), etype="hasCreator", dir="out"),
        Step("per_author", "group_count", items=ref("authors")),
        Step("per_author_n", "rename", rows=ref("per_author"), names={"item": "_vid", "count": "_msgs"}),
        Step("union",     "concat", a=ref("interested"), b=ref("authors")),
        Step("persons",   "dedup", items=ref("union")),
        Step("rows",      "annotate", items=ref("persons"), field="_k", value=1),
        Step("flagged",   "flag_in", rows=ref("rows"), field="_vid", allowed=ref("interested"), into="_interested"),
        Step("with_msgs", "left_join", left=ref("flagged"), right=ref("per_author_n"), left_on="_vid", right_on="_vid",
             default={"_msgs": 0}),
        Step("scored",    "derive", rows=ref("with_msgs"), into="score",
             fn=E.add(E.if_(E.field("_interested"), E.lit(100), E.lit(0)), E.field("_msgs"))),
        Step("friends",   "read_neighbors", srcs=ref("persons"), etype="knows", dir="both", pairs=True),   # src = person, dst = friend
        Step("fscore",    "left_join", left=ref("friends"), right=ref("scored"), left_on="dst", right_on="_vid",
             default={"score": 0}),
        Step("fsum",      "group_sum", rows=ref("fscore"), by="src", field="score"),
        Step("fsum_n",    "rename", rows=ref("fsum"), names={"item": "_vid", "sum": "friendsScore"}),
        Step("with_f",    "left_join", left=ref("scored"), right=ref("fsum_n"), left_on="_vid", right_on="_vid",
             default={"friendsScore": 0}),
        Step("totaled",   "derive", rows=ref("with_f"), into="_total", fn=E.add(E.field("score"), E.field("friendsScore"))),
        Step("top",       "top_k", rows=ref("totaled"), keys=[("_total", "desc"), ("_vid", "asc")], k=100),
        Step("top_v",     "pluck", rows=ref("top"), field="_vid"),
        Step("ids",       "read_vertex_props", vids=ref("top_v"), props=["id"], label="Person"),
        Step("with_id",   "join", left=ref("top"), right=ref("ids"), left_on="_vid", right_on="_vid"),
        Step("out",       "project", rows=ref("with_id"), fields=["id", "score", "friendsScore"]),
    ], params={"tag": {"type": "str"}, "startDate": {"type": "int"}, "endDate": {"type": "int"}})


def bi8_arangodb(args):
    """arangodb's body: its list-label props read resolves any vid's collection (a Forum's row comes back), so the
    plan asks for the label test per vid (the probe) before the read."""
    return _bi8(args, typed="probe")


SIM = {"bi8": {"default": bi8, "dgraph": bi8_dgraph, "arangodb": bi8_arangodb}}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"bi8": _KNOWS_HINTS}
