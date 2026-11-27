"""BI9 Top thread initiators (spec 8.4 BI/read/9; reference neo4j/queries/bi-9.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import chase_levelwise_steps, with_label_run


def bi9(args):
    """Per Person: the number of Posts they created in [$startDate, $endDate] (threadCount) and the number of
    Messages in those Posts' reply trees created in the window, the root Post included (messageCount); top 100 by
    messageCount desc, person.id asc, with the person's id and names.

    Both halves enter through the creationDate index: the window's Posts (one index range) and the window's
    Comments (another); each Comment is chased to its root Post (the level-wise chase, chase_levelwise_steps; the
    reference's `<-[:REPLY_OF*0..]-`) and kept when the root is one of the window's Posts. Counts are flow;
    the ids and names are read for the 100 winners only (the IC two-phase pattern; a vid orders as its id, so
    the tie-break sorts on `_vid`). The row stores' per-label indexes are exact; dgraph's body restricts each
    window by the label's type posting (bi1_dgraph's reason)."""
    return _bi9(args, typed_index=True, chase="level")


def bi9_dgraph(args):
    return _bi9(args, typed_index=False, chase="level")


def bi9_source(args):
    """The per-source chase twin (read_chase), the record of that op's cost (see bi12_source)."""
    return _bi9(args, typed_index=True, chase="source")


def bi9_source_dgraph(args):
    return _bi9(args, typed_index=False, chase="source")


def _bi9(args, typed_index, chase):
    start, end = E.param("startDate"), E.param("endDate")
    in_window = E.and_(E.ge(E.field("creationDate"), start), E.le(E.field("creationDate"), end))
    steps = [Step("posts_w", "read_index_range", labels=["Post"], prop="creationDate", lo=start, hi=E.let("end1")),
             Step("coms_w",  "read_index_range", labels=["Comment"], prop="creationDate", lo=start, hi=E.let("end1"))]
    if typed_index:
        posts_in, coms_in = "posts_w", "coms_w"
    else:
        steps += [Step("posts_t", "filter_by_label", vids=ref("posts_w"), label="Post"),
                  Step("coms_t",  "filter_by_label", vids=ref("coms_w"), label="Comment")]
        posts_in, coms_in = "posts_t", "coms_t"
    return Plan(steps + [
        Step("posts_p",  "read_vertex_props", vids=ref(posts_in), props=["creationDate"], label="Post"),
        Step("posts_x",  "filter", rows=ref("posts_p"), pred=in_window),           # dgraph's day superset trimmed
        Step("post_v",   "pluck", rows=ref("posts_x"), field="_vid"),
        Step("creators", "read_neighbors", srcs=ref("post_v"), etype="hasCreator", dir="out", pairs=True),
        Step("creator_v", "pluck", rows=ref("creators"), field="dst"),
        Step("threads",  "group_count", items=ref("creator_v")),
        Step("threads_n", "rename", rows=ref("threads"), names={"item": "_vid", "count": "threadCount"}),
        # the window's comments, each to its root Post; kept when the root is one of the window's Posts
        Step("coms_p",   "read_vertex_props", vids=ref(coms_in), props=["creationDate"], label="Comment"),
        Step("coms_x",   "filter", rows=ref("coms_p"), pred=in_window),
    ] + (
        [Step("com_v",    "pluck", rows=ref("coms_x"), field="_vid"),
         Step("roots",    "read_chase", srcs=ref("com_v"), etype="replyOf", dir="out"),
         Step("rooted",   "attach", rows=ref("coms_x"), values=ref("roots"), into="_root")] if chase == "source"
        else chase_levelwise_steps("coms_x", "replyOf", out="rooted")
    ) + [
        Step("in_tree",  "filter_in", rows=ref("rooted"), field="_root", allowed=ref("post_v")),
        Step("root_v",   "pluck", rows=ref("in_tree"), field="_root"),
        Step("per_root", "group_count", items=ref("root_v")),
        Step("per_root_n", "rename", rows=ref("per_root"), names={"item": "src", "count": "_replies"}),
        Step("root_owner", "join", left=ref("per_root_n"), right=ref("creators"), left_on="src", right_on="src"),
        Step("per_person", "group_sum", rows=ref("root_owner"), by="dst", field="_replies"),
        Step("per_person_n", "rename", rows=ref("per_person"), names={"item": "_vid", "sum": "_replySum"}),
        Step("joined",   "left_join", left=ref("threads_n"), right=ref("per_person_n"), left_on="_vid", right_on="_vid",
             default={"_replySum": 0}),
        Step("scored",   "derive", rows=ref("joined"), into="messageCount", fn=E.add(E.field("threadCount"), E.field("_replySum"))),
        Step("top",      "top_k", rows=ref("scored"), keys=[("messageCount", "desc"), ("_vid", "asc")], k=100),
        Step("top_v",    "pluck", rows=ref("top"), field="_vid"),
        Step("names",    "read_vertex_props", vids=ref("top_v"), props=["id", "firstName", "lastName"], label="Person"),
        Step("with_names", "join", left=ref("top"), right=ref("names"), left_on="_vid", right_on="_vid"),
        Step("out",      "project", rows=ref("with_names"), fields=["id", "firstName", "lastName", "threadCount", "messageCount"]),
    ], params={"startDate": {"type": "int"}, "endDate": {"type": "int"}},
       let={"end1": E.add(end, E.lit(1))})       # [$startDate, $endDate] inclusive: the index range ends 1 ms after


def bi9_run(args):
    """The label-run twin of bi9: the window's Post and Comment dates read by one kept scan of each label's key run
    (_shared.with_label_run), not one point read per message."""
    return with_label_run(bi9(args), {"posts_p", "coms_p"})


def bi9_source_run(args):
    """The label-run twin of bi9_source (the per-source chase body), as bi9_run."""
    return with_label_run(bi9_source(args), {"posts_p", "coms_p"})


SIM = {"bi9": {"default": bi9, "dgraph": bi9_dgraph},
       "bi9_source": {"default": bi9_source, "dgraph": bi9_source_dgraph},
       "bi9_run": bi9_run, "bi9_source_run": bi9_source_run}
