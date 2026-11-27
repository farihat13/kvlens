"""BI7 Related topics (spec 8.4 BI/read/7; reference neo4j/queries/bi-7.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import message_vids_steps


def bi7(args):
    """The no-test body: the Forums in the tag's list drop at their message-only reads (misses)."""
    return _bi7(args, typed="none")


def bi7_dgraph(args):
    """dgraph's body: the Tag's hasTag-in list holds Forums too; the type postings restrict it (message_vids_steps)."""
    return _bi7(args, typed="posting")


def _bi7(args, typed):
    """The Tags of the direct reply Comments to Messages tagged $tag, counted per Tag over the reply Comments
    that do NOT themselves carry $tag (count DISTINCT comment); top 100 by count desc, tag.name asc.

    Tag.name index -> hasTag in -> replyOf in (the direct replies) -> hasTag out per reply (pairs); the replies
    that carry $tag are the pairs whose dst is the tag -- removed as a set (the anti-join by composition); the
    names of the related tags are read once each (the sort key)."""
    return Plan([
        Step("tag",      "read_index_eq", label="Tag", prop="name", value=E.param("tag")),
        Step("msgs0",    "read_neighbors", srcs=ref("tag"), etype="hasTag", dir="in"),
        Step("msgs",     "dedup", items=ref("msgs0")),
    ] + message_vids_steps("msgs", typed, "mv") + [                                   # the messages among them
        Step("replies0", "read_neighbors", srcs=ref("mv"), etype="replyOf", dir="in"),
        Step("replies",  "dedup", items=ref("replies0")),
        Step("rtags",    "read_neighbors", srcs=ref("replies"), etype="hasTag", dir="out", pairs=True),   # src = reply, dst = tag
        Step("tagged",   "filter_in", rows=ref("rtags"), field="dst", allowed=ref("tag")),                # the replies carrying $tag
        Step("tagged_v", "pluck", rows=ref("tagged"), field="src"),
        Step("clean",    "exclude", items=ref("replies"), remove=ref("tagged_v")),
        Step("kept",     "filter_in", rows=ref("rtags"), field="src", allowed=ref("clean")),
        Step("rel_v",    "pluck", rows=ref("kept"), field="dst"),
        Step("counts",   "group_count", items=ref("rel_v")),                  # one pair per (reply, tag): distinct replies
        Step("counts_n", "rename", rows=ref("counts"), names={"item": "_vid"}),
        Step("rel_d",    "dedup", items=ref("rel_v")),
        Step("names",    "read_vertex_props", vids=ref("rel_d"), props=["name"], label="Tag"),
        Step("named",    "join", left=ref("counts_n"), right=ref("names"), left_on="_vid", right_on="_vid"),
        Step("top",      "top_k", rows=ref("named"), keys=[("count", "desc"), ("name", "asc")], k=100),
        Step("out",      "project", rows=ref("top"), fields=["name", "count"]),
    ], params={"tag": {"type": "str"}})


SIM = {"bi7": {"default": bi7, "dgraph": bi7_dgraph}}
