"""BI5 Most active posters of a given topic (spec 8.4 BI/read/5; reference neo4j/queries/bi-5.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import message_vids_steps


def bi5(args):
    """The no-test body: the Forums in the tag's list drop at their message-only reads (misses)."""
    return _bi5(args, typed="none")


def bi5_dgraph(args):
    """dgraph's body: the Tag's hasTag-in list holds Forums too; the type postings restrict it (message_vids_steps)."""
    return _bi5(args, typed="posting")


def _bi5(args, typed):
    """Per Person with a Message tagged $tag: messageCount, likeCount (likes on those messages), replyCount
    (direct reply Comments to them); score = messageCount + 2 replyCount + 10 likeCount; top 100 by score desc,
    person.id asc. A message with no like / reply counts 0 (the reference's OPTIONAL MATCH).

    Tag.name index -> hasTag in (the tag's messages) -> hasCreator out, likes in and replyOf in per message
    (pairs: the message stays with each hit); the per-message counts and the per-person sums are flow; the
    winners' ids are read for the top 100 only (a vid orders as its id)."""
    return Plan([
        Step("tag",      "read_index_eq", label="Tag", prop="name", value=E.param("tag")),
        Step("msgs0",    "read_neighbors", srcs=ref("tag"), etype="hasTag", dir="in"),
        Step("msgs",     "dedup", items=ref("msgs0")),
    ] + message_vids_steps("msgs", typed, "mv") + [                                                      # the messages among them
        Step("creators", "read_neighbors", srcs=ref("mv"), etype="hasCreator", dir="out", pairs=True),     # src = msg, dst = person
        Step("likes",    "read_neighbors", srcs=ref("mv"), etype="likes", dir="in", pairs=True),           # src = msg, dst = liker
        Step("replies",  "read_neighbors", srcs=ref("mv"), etype="replyOf", dir="in", pairs=True),         # src = msg, dst = reply
        Step("like_v",   "pluck", rows=ref("likes"), field="src"),
        Step("reply_v",  "pluck", rows=ref("replies"), field="src"),
        Step("like_c",   "group_count", items=ref("like_v")),
        Step("reply_c",  "group_count", items=ref("reply_v")),
        Step("like_n",   "rename", rows=ref("like_c"), names={"item": "src", "count": "likeCount"}),
        Step("reply_n",  "rename", rows=ref("reply_c"), names={"item": "src", "count": "replyCount"}),
        Step("with_l",   "left_join", left=ref("creators"), right=ref("like_n"), left_on="src", right_on="src",
             default={"likeCount": 0}),
        Step("with_r",   "left_join", left=ref("with_l"), right=ref("reply_n"), left_on="src", right_on="src",
             default={"replyCount": 0}),
        Step("per_person", "group_agg", rows=ref("with_r"), by=["dst"],
             aggs={"messageCount": ["count", None], "likeCount": ["sum", "likeCount"], "replyCount": ["sum", "replyCount"]}),
        Step("scored",   "derive", rows=ref("per_person"), into="score",
             fn=E.add(E.add(E.field("messageCount"), E.mul(E.lit(2), E.field("replyCount"))), E.mul(E.lit(10), E.field("likeCount")))),
        Step("top",      "top_k", rows=ref("scored"), keys=[("score", "desc"), ("dst", "asc")], k=100),
        Step("top_v",    "pluck", rows=ref("top"), field="dst"),
        Step("ids",      "read_vertex_props", vids=ref("top_v"), props=["id"], label="Person"),
        Step("with_id",  "join", left=ref("top"), right=ref("ids"), left_on="dst", right_on="_vid"),
        Step("out",      "project", rows=ref("with_id"), fields=["id", "replyCount", "likeCount", "messageCount", "score"]),
    ], params={"tag": {"type": "str"}})


SIM = {"bi5": {"default": bi5, "dgraph": bi5_dgraph}}
