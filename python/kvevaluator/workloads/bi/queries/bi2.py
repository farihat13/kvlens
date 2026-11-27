"""BI2 Tag evolution (spec 8.4 BI/read/2; reference neo4j/queries/bi-2.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import DAY, message_prop_steps, with_label_run


def bi2(args):
    """The row stores' body: the props read under the message labels is the type test (a Forum: a null row)."""
    return _bi2(args, typed="null")


def bi2_dgraph(args):
    """dgraph's body: the Tag's hasTag-in list holds Forums too; the type postings restrict it (message_prop_steps)."""
    return _bi2(args, typed="posting")


def _bi2(args, typed):
    """The Tags of $tagClass (hasType, direct), each with its Message count in [$date, $date + 100 d) and in
    [$date + 100 d, $date + 200 d) (a Tag with no message in a window counts 0: the reference's OPTIONAL MATCH);
    diff = |count1 - count2|; top 100 by diff desc, tag.name asc.

    Entry by the TagClass.name value index, hasType in to the Tags, hasTag in to every message of each Tag
    (pairs: the Tag stays with its message), the date read once per distinct message, both window counts as
    flow. The Tag names are read for every Tag of the class (the sort key)."""
    date = E.param("date")
    cd = E.field("creationDate")
    return Plan([
        Step("tc",       "read_index_eq", label="TagClass", prop="name", value=E.param("tagClass")),
        Step("tags0",    "read_neighbors", srcs=ref("tc"), etype="hasType", dir="in"),
        Step("tags",     "dedup", items=ref("tags0")),
        Step("tagged",   "read_neighbors", srcs=ref("tags"), etype="hasTag", dir="in", pairs=True),   # src = tag, dst = message
        Step("msg_v0",   "pluck", rows=ref("tagged"), field="dst"),
        Step("msg_v",    "dedup", items=ref("msg_v0")),
    ] + message_prop_steps("msg_v", ["creationDate"], typed) + [       # the messages among them, with their dates
        Step("dated",    "join_multi", left=ref("tagged"), right=ref("msg_d"), left_on="dst", right_on="_vid"),   # Forums drop out
        Step("w1",       "filter", rows=ref("dated"), pred=E.and_(E.ge(cd, date), E.lt(cd, E.let("d100")))),
        Step("w2",       "filter", rows=ref("dated"), pred=E.and_(E.ge(cd, E.let("d100")), E.lt(cd, E.let("d200")))),
        Step("w1_tag",   "pluck", rows=ref("w1"), field="src"),
        Step("w2_tag",   "pluck", rows=ref("w2"), field="src"),
        Step("c1",       "group_count", items=ref("w1_tag")),
        Step("c2",       "group_count", items=ref("w2_tag")),
        Step("c1_n",     "rename", rows=ref("c1"), names={"item": "_vid", "count": "countWindow1"}),
        Step("c2_n",     "rename", rows=ref("c2"), names={"item": "_vid", "count": "countWindow2"}),
        Step("names",    "read_vertex_props", vids=ref("tags"), props=["name"], label="Tag"),
        Step("with_c1",  "left_join", left=ref("names"), right=ref("c1_n"), left_on="_vid", right_on="_vid",
             default={"countWindow1": 0}),
        Step("with_c2",  "left_join", left=ref("with_c1"), right=ref("c2_n"), left_on="_vid", right_on="_vid",
             default={"countWindow2": 0}),
        Step("diffed",   "derive", rows=ref("with_c2"), into="diff",
             fn=E.if_(E.lt(E.sub(E.field("countWindow1"), E.field("countWindow2")), E.lit(0)),
                      E.sub(E.field("countWindow2"), E.field("countWindow1")),
                      E.sub(E.field("countWindow1"), E.field("countWindow2")))),
        Step("top",      "top_k", rows=ref("diffed"), keys=[("diff", "desc"), ("name", "asc")], k=100),
        Step("out",      "project", rows=ref("top"), fields=["name", "countWindow1", "countWindow2", "diff"]),
    ], params={"tagClass": {"type": "str"}, "date": {"type": "int"}},
       let={"d100": E.add(date, E.lit(100 * DAY)), "d200": E.add(date, E.lit(200 * DAY))})


def bi2_arangodb(args):
    """arangodb's body: its list-label props read resolves any vid's collection (a Forum's row comes back), so the
    plan asks for the label test per vid (the probe) before the read."""
    return _bi2(args, typed="probe")


def bi2_run(args):
    """The label-run twin of bi2: the tagged messages' dates read by one kept scan of the Post and of the Comment key
    run (_shared.with_label_run), not one point read per message; a Forum is in neither run, so it gives no row."""
    return with_label_run(bi2(args), {"msg_d_all"})


SIM = {"bi2": {"default": bi2, "dgraph": bi2_dgraph, "arangodb": bi2_arangodb},
       "bi2_run": bi2_run}
