"""BI1 Posting summary (spec 8.4 BI/read/1; reference neo4j/queries/bi-1.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import (MESSAGE_LABELS, HAS_CONTENT_FIELD, message_window_steps,
                                               message_scan_steps)


def bi1(args):
    """Every Message created before $datetime, grouped by year x isComment x length bucket (< 40 / < 80 /
    < 160 / else): count, average and sum of length, and the group's share of ALL messages before
    $datetime (the reference: the total counts every message, the groups only those with content).

    Entry through the sorted creationDate index of both message labels (read_index_range [0, $datetime)),
    the IC3 pattern: exact on the value-sorted indexes, a day-granular superset on dgraph's token index that
    the `before` filter trims. The type and the props per message come from `message_window_steps`, three
    bodies by capability (the label-scoped read, the type postings, the type probe); everything after is 0-KV
    flow. `bi1_scan` is the type-pass twin (enumerate both labels, read the date with the props)."""
    return _bi1(args, entry="index", typed="probe")


def bi1_dgraph(args):
    """dgraph's body: its creationDate index is one posting per DAY over EVERY label that carries the predicate
    (Person, Forum, Post, Comment), so the window read returns persons and forums too, and a uid says nothing
    about its type: the type postings restrict the window (two posting intersections, its cheapest)."""
    return _bi1(args, entry="index", typed="posting")


def bi1_scoped(args):
    """The label-scoped body (nebula, arangodb, the per-type layouts): the Post-label props read on the window
    returns nulls for a Comment, so the hits ARE the Posts; the rest read under the Comment label. One props
    read per message and no type probe (the probe cost as many GETs as the index walk: 143,372 at sf0.1 p50)."""
    return _bi1(args, entry="index", typed="scoped")


def bi1_scan(args):
    """The type-pass twin: enumerate Post and Comment by the type index and read the date with the props (the
    label is known from the enumeration), then filter the date -- one pass over the chunks on a chunked layout
    (fused there), the type posting + a props GET per vertex elsewhere."""
    return _bi1(args, entry="scan", typed=None)


def _bi1(args, entry, typed):
    dt = E.param("datetime")
    length = E.field("length")
    props = ["creationDate", "length"]
    if entry == "index":
        head = [Step("win", "read_index_range", labels=list(MESSAGE_LABELS), prop="creationDate", lo=E.lit(0), hi=dt)]
        head += message_window_steps("win", props, typed, out="props")     # rows: _vid, props, _isPost
    else:
        head = message_scan_steps(props, out="props")                      # rows: _vid, props, _isPost
    return Plan(head + [
        Step("before",  "filter", rows=ref("props"), pred=E.lt(E.field("creationDate"), dt)),
        Step("total",   "count", items=ref("before")),
        Step("withc",   "filter", rows=ref("before"), pred=E.gt(E.field(HAS_CONTENT_FIELD), E.lit(0))),
        Step("year",    "derive", rows=ref("withc"), into="year", fn=E.year_of(E.field("creationDate"))),
        Step("kind",    "derive", rows=ref("year"), into="isComment", fn=E.not_(E.field("_isPost"))),
        Step("bucket",  "derive", rows=ref("kind"), into="lengthCategory",
             fn=E.if_(E.lt(length, 40), 0, E.if_(E.lt(length, 80), 1, E.if_(E.lt(length, 160), 2, 3)))),
        Step("grp",     "group_agg", rows=ref("bucket"), by=["year", "isComment", "lengthCategory"],
             aggs={"messageCount": ["count", None], "sumMessageLength": ["sum", "length"]}),
        Step("avg",     "derive", rows=ref("grp"), into="averageMessageLength",
             fn=E.div(E.field("sumMessageLength"), E.field("messageCount"))),
        # the scalar total joins every group row through a constant key (a subquery + cross join)
        Step("tot",     "as_row", value=ref("total"), field="_total"),
        Step("tot_k",   "derive", rows=ref("tot"), into="_k", fn=E.lit(1)),
        Step("grp_k",   "derive", rows=ref("avg"), into="_k", fn=E.lit(1)),
        Step("joined",  "join", left=ref("grp_k"), right=ref("tot_k"), left_on="_k", right_on="_k"),
        Step("pct",     "derive", rows=ref("joined"), into="percentageOfMessages",
             fn=E.div(E.field("messageCount"), E.field("_total"))),
        Step("sorted",  "top_k", rows=ref("pct"), keys=[("year", "desc"), ("isComment", "asc"), ("lengthCategory", "asc")], k=None),
        Step("out",     "project", rows=ref("sorted"),
             fields=["year", "isComment", "lengthCategory", "messageCount", "averageMessageLength",
                     "sumMessageLength", "percentageOfMessages"]),
    ], params={"datetime": {"type": "int"}})


SIM = {"bi1": {"default": bi1, "dgraph": bi1_dgraph, "nebula": bi1_scoped, "arangodb": bi1_scoped,
               "pertype_csr": bi1_scoped},
       "bi1_scan": bi1_scan}
