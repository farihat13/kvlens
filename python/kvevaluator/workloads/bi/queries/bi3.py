"""BI3 Popular topics in a country (spec 8.4 BI/read/3; reference neo4j/queries/bi-3.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import message_vids_steps, chase_levelwise_steps


def bi3(args):
    return _bi3(args, typed="none", chase="level")


def bi3_dgraph(args):
    """dgraph's body: the tags' hasTag-in list holds Forums too; the type postings restrict it (message_vids_steps)."""
    return _bi3(args, typed="posting", chase="level")


def bi3_source(args):
    """The per-source chase twin (read_chase), the record of that op's cost (see bi12_source)."""
    return _bi3(args, typed="none", chase="source")


def bi3_source_dgraph(args):
    return _bi3(args, typed="posting", chase="source")


def _bi3(args, typed, chase):
    """The Forums whose moderator lives in a City of $country, each with the number of its Messages (a Comment
    belongs to its root Post's Forum: the reference's `<-[:REPLY_OF*0..]-`) that carry a Tag of $tagClass
    (direct hasType); top 20 by messageCount desc, forum.id asc, with the forum's id, title, creationDate and
    the moderator's id.

    Entry from both ends: TagClass.name -> hasType in -> hasTag in (the class's messages) -> read_chase replyOf
    to the root Post -> containerOf in (the forum); Place.name -> isPartOf in (the cities) -> isLocatedIn in
    (the residents) -> hasModerator in (their forums). The two forum sets meet as flow; counts, the sort and
    the 20 winners' props follow."""
    return Plan([
        # the class's messages and their forums
        Step("tc",       "read_index_eq", label="TagClass", prop="name", value=E.param("tagClass")),
        Step("tags0",    "read_neighbors", srcs=ref("tc"), etype="hasType", dir="in"),
        Step("tags",     "dedup", items=ref("tags0")),
        Step("msgs0",    "read_neighbors", srcs=ref("tags"), etype="hasTag", dir="in"),
        Step("msgs",     "dedup", items=ref("msgs0")),
    ] + message_vids_steps("msgs", typed, "mv") + [                                   # the messages among them
        Step("msg_rows", "annotate", items=ref("mv"), field="_k", value=1),
    ] + (
        [Step("roots",    "read_chase", srcs=ref("mv"), etype="replyOf", dir="out"),
         Step("rooted",   "attach", rows=ref("msg_rows"), values=ref("roots"), into="_root")] if chase == "source"
        else chase_levelwise_steps("msg_rows", "replyOf", out="rooted")
    ) + [
        Step("roots_v",  "pluck", rows=ref("rooted"), field="_root"),
        Step("root_d",   "dedup", items=ref("roots_v")),
        Step("forum_of", "read_neighbors", srcs=ref("root_d"), etype="containerOf", dir="in", pairs=True),   # src = post, dst = forum
        # the country's residents' forums
        Step("country",  "read_index_eq", label="Place", prop="name", value=E.param("country")),
        Step("cities0",  "read_neighbors", srcs=ref("country"), etype="isPartOf", dir="in"),
        Step("cities",   "dedup", items=ref("cities0")),
        Step("people0",  "read_neighbors", srcs=ref("cities"), etype="isLocatedIn", dir="in"),
        Step("people",   "dedup", items=ref("people0")),
        Step("modof",    "read_neighbors", srcs=ref("people"), etype="hasModerator", dir="in", pairs=True),   # src = person, dst = forum
        Step("forums_v", "pluck", rows=ref("modof"), field="dst"),
        # the meet: the class's messages whose forum has a resident moderator
        Step("kept",     "join_multi", left=ref("rooted"), right=ref("forum_of"), left_on="_root", right_on="src",   # + dst = forum (inner: a Forum in the list is its own root and has no containerOf-in)
             where_key=E.field("dst"), where_in=ref("forums_v")),
        Step("kept_f",   "pluck", rows=ref("kept"), field="dst"),
        Step("counts",   "group_count", items=ref("kept_f")),                 # messages are distinct: count DISTINCT message
        Step("counts_n", "rename", rows=ref("counts"), names={"item": "_vid", "count": "messageCount"}),
        Step("top",      "top_k", rows=ref("counts_n"), keys=[("messageCount", "desc"), ("_vid", "asc")], k=20),
        Step("top_v",    "pluck", rows=ref("top"), field="_vid"),
        Step("fprops",   "read_vertex_props", vids=ref("top_v"), props=["id", "title", "creationDate"], label="Forum"),
        Step("with_f",   "join", left=ref("top"), right=ref("fprops"), left_on="_vid", right_on="_vid"),
        Step("modof_n",  "rename", rows=ref("modof"), names={"src": "_mod", "dst": "_forum"}),
        Step("with_m",   "join", left=ref("with_f"), right=ref("modof_n"), left_on="_vid", right_on="_forum"),
        Step("mod_v",    "pluck", rows=ref("with_m"), field="_mod"),
        Step("mod_ids",  "read_vertex_props", vids=ref("mod_v"), props=["id"], label="Person"),
        Step("mod_n",    "rename", rows=ref("mod_ids"), names={"id": "personId"}),
        Step("with_p",   "join", left=ref("with_m"), right=ref("mod_n"), left_on="_mod", right_on="_vid"),
        Step("out",      "project", rows=ref("with_p"), fields=["id", "title", "creationDate", "personId", "messageCount"]),
    ], params={"tagClass": {"type": "str"}, "country": {"type": "str"}})


SIM = {"bi3": {"default": bi3, "dgraph": bi3_dgraph},
       "bi3_source": {"default": bi3_source, "dgraph": bi3_source_dgraph}}
