"""BI16 Fake news detection (spec 8.4 BI/read/16; reference neo4j/queries/bi-16.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import MESSAGE_LABELS

DAY = 86_400_000


def _letter(x, tag_param, date_param, entry="props"):
    """Steps for one (tag, day): rows `{x}_res` (_vid = person1, cm{X} = their messages tagged `tag` created on the
    UTC day of `date`), persons kept when at most $maxKnowsLimit of their friends also created such a message.
    entry="props": one creationDate read per entry of the tag's hasTag-in list, then the day filter;
    entry="index": the day's messages from the creationDate index (read_index_range over the day: exact on a
    value-sorted index, the day bucket on Dgraph's day-granular one) intersected with the tag's list -- no props read."""
    cd = E.field("creationDate")
    day = lambda e: E.floordiv(e, E.lit(DAY))
    head = [
        Step(f"{x}_tag",   "read_index_eq", label="Tag", prop="name", value=E.param(tag_param)),
        Step(f"{x}_m0",    "read_neighbors", srcs=ref(f"{x}_tag"), etype="hasTag", dir="in"),
        Step(f"{x}_m1",    "dedup", items=ref(f"{x}_m0"))]
    if entry == "index":
        head += [
        Step(f"{x}_day",   "read_index_range", labels=list(MESSAGE_LABELS), prop="creationDate", lo=E.let(f"{x}_lo"), hi=E.let(f"{x}_hi")),
        Step(f"{x}_mv",    "intersect", items=ref(f"{x}_m1"), other=ref(f"{x}_day"))]                       # the tag's entries created that day
    else:
        head += [
        Step(f"{x}_mp",    "read_vertex_props", vids=ref(f"{x}_m1"), props=["creationDate"], label=list(MESSAGE_LABELS)),
        Step(f"{x}_md",    "filter", rows=ref(f"{x}_mp"), pred=E.and_(E.notnull(cd), E.eq(day(cd), day(E.param(date_param))))),
        Step(f"{x}_mv",    "pluck", rows=ref(f"{x}_md"), field="_vid")]
    return head + [
        Step(f"{x}_cr",    "read_neighbors", srcs=ref(f"{x}_mv"), etype="hasCreator", dir="out", pairs=True),   # src = message, dst = person1
        Step(f"{x}_cv",    "pluck", rows=ref(f"{x}_cr"), field="dst"),
        Step(f"{x}_cm",    "group_count", items=ref(f"{x}_cv")),                                                 # one creator per message
        Step(f"{x}_cm_n",  "rename", rows=ref(f"{x}_cm"), names={"item": "_vid", "count": f"cm{x}"}),
        Step(f"{x}_p",     "dedup", items=ref(f"{x}_cv")),
        Step(f"{x}_kn",    "read_neighbors", srcs=ref(f"{x}_p"), etype="knows", dir="both", pairs=True),         # src = person1, dst = friend
        Step(f"{x}_kn_i",  "filter_in", rows=ref(f"{x}_kn"), field="dst", allowed=ref(f"{x}_p")),
        Step(f"{x}_kv",    "pluck", rows=ref(f"{x}_kn_i"), field="src"),
        Step(f"{x}_cp",    "group_count", items=ref(f"{x}_kv")),
        Step(f"{x}_cp_n",  "rename", rows=ref(f"{x}_cp"), names={"item": "_vid", "count": "_cp2"}),
        Step(f"{x}_all",   "left_join", left=ref(f"{x}_cm_n"), right=ref(f"{x}_cp_n"), left_on="_vid", right_on="_vid", default={"_cp2": 0}),
        Step(f"{x}_res",   "filter", rows=ref(f"{x}_all"), pred=E.le(E.field("_cp2"), E.param("maxKnowsLimit"))),
    ]


def bi16_index(args):
    """The index twin: each side's messages of the day from the creationDate index, intersected with the tag's list
    (no creationDate read per entry); the rest as bi16. Measured beside the base; the plan-choice rule picks."""
    return _bi16(args, entry="index")


def bi16(args):
    return _bi16(args, entry="props")


def _bi16(args, entry):
    """Persons with a Message tagged $tagA created on the day of $dateA and one tagged $tagB on the day of $dateB,
    each side kept only when at most $maxKnowsLimit of their friends created such a message on that side; top 20 by
    messageCountA + messageCountB desc, id asc.

    Per side: Tag.name index -> hasTag in (the list holds Forums too) -> one props read per entry under the message
    labels (the labelled read is the type test: a Forum gives a null row on a row store, and on Dgraph its creationDate
    predicate is read but its hasCreator-out misses) -> the day filter -> hasCreator out per message; knows both
    ways from the creators for the friend count. The two sides join on the person (both present); ids read for the
    20 winners (a vid orders as its id)."""
    day_lo = lambda p: E.mul(E.floordiv(E.param(p), E.lit(DAY)), E.lit(DAY))
    return Plan(_letter("A", "tagA", "dateA", entry) + _letter("B", "tagB", "dateB", entry) + [
        Step("both",     "join_multi", left=ref("A_res"), right=ref("B_res"), left_on="_vid", right_on="_vid"),    # inner: a person on one side only drops
        Step("scored",   "derive", rows=ref("both"), into="_score", fn=E.add(E.field("cmA"), E.field("cmB"))),
        Step("top",      "top_k", rows=ref("scored"), keys=[("_score", "desc"), ("_vid", "asc")], k=20),
        Step("top_v",    "pluck", rows=ref("top"), field="_vid"),
        Step("ids",      "read_vertex_props", vids=ref("top_v"), props=["id"], label="Person"),
        Step("ids_n",    "rename", rows=ref("ids"), names={"id": "personId"}),
        Step("with_id",  "join", left=ref("top"), right=ref("ids_n"), left_on="_vid", right_on="_vid"),
        Step("out0",     "rename", rows=ref("with_id"), names={"cmA": "messageCountA", "cmB": "messageCountB"}),
        Step("out",      "project", rows=ref("out0"), fields=["personId", "messageCountA", "messageCountB"]),
    ], params={"tagA": {"type": "str"}, "dateA": {"type": "int"}, "tagB": {"type": "str"}, "dateB": {"type": "int"},
               "maxKnowsLimit": {"type": "int"}},
                let={"A_lo": day_lo("dateA"), "A_hi": E.add(day_lo("dateA"), E.lit(DAY)),
                     "B_lo": day_lo("dateB"), "B_hi": E.add(day_lo("dateB"), E.lit(DAY))})


SIM = {"bi16": bi16, "bi16_index": bi16_index}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"bi16": _KNOWS_HINTS}
