"""BI13 Zombies in a country (spec 8.4 BI/read/13; reference neo4j/queries/bi-13.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import MESSAGE_LABELS, with_label_run


def bi13(args):
    """Zombies: Persons living in a City of $country, created before $endDate, with fewer messages created before
    $endDate than months of membership (12 (end.year - cd.year) + (end.month - cd.month) + 1; the reference's
    integer `messageCount / months < 1`). Per zombie, over ALL their messages: zombieLikeCount = likes by zombies,
    totalLikeCount = likes by Persons created before $endDate; zombieScore = their ratio (0 when no like). Top 100
    by score desc, id asc.

    Place.name -> isPartOf-in -> isLocatedIn-in (the residents; a City's list holds Universities too, and the
    Person-labelled props read returns null for those: the label test is the labelled read's null), one props read
    each (creationDate); hasCreator-in
    per candidate and one props read per message under the two message labels (hasCreator-in's far end is a Post or
    a Comment: the labelled read is the type test, paid as the format charges a two-label read); likes-in per zombie
    message; one props read per distinct liker (creationDate). The counts, the months and the ratio are flow."""
    end = E.param("endDate")
    cd = E.field("creationDate")
    return Plan([
        Step("c",        "read_index_eq", label="Place", prop="name", value=E.param("country")),
        Step("ci0",      "read_neighbors", srcs=ref("c"), etype="isPartOf", dir="in"),
        Step("ci",       "dedup", items=ref("ci0")),
        Step("p0",       "read_neighbors", srcs=ref("ci"), etype="isLocatedIn", dir="in"),
        Step("p",        "dedup", items=ref("p0")),
        Step("pp",       "read_vertex_props", vids=ref("p"), props=["creationDate"], label="Person"),
        Step("cand",     "filter", rows=ref("pp"), pred=E.and_(E.notnull(cd), E.lt(cd, end))),     # a City's isLocatedIn-in holds Universities too: the Person read's null drops them
        Step("months",   "derive", rows=ref("cand"), into="_months",
             fn=E.add(E.add(E.mul(E.lit(12), E.sub(E.year_of(end), E.year_of(cd))), E.sub(E.month_of(end), E.month_of(cd))), E.lit(1))),
        Step("cv",       "pluck", rows=ref("months"), field="_vid"),
        Step("m",        "read_neighbors", srcs=ref("cv"), etype="hasCreator", dir="in", pairs=True),        # src = person, dst = message
        Step("m_n",      "rename", rows=ref("m"), names={"src": "_person", "dst": "_msg"}),
        Step("mv",       "pluck", rows=ref("m_n"), field="_msg"),
        Step("mp",       "read_vertex_props", vids=ref("mv"), props=["creationDate"], label=list(MESSAGE_LABELS)),
        Step("mp_w",     "filter", rows=ref("mp"), pred=E.lt(cd, end)),
        Step("mp_n",     "rename", rows=ref("mp_w"), names={"_vid": "_msg", "creationDate": "_mcd"}),
        Step("mw",       "join", left=ref("mp_n"), right=ref("m_n"), left_on="_msg", right_on="_msg"),    # + _person
        Step("mw_v",     "pluck", rows=ref("mw"), field="_person"),
        Step("mc",       "group_count", items=ref("mw_v")),
        Step("mc_n",     "rename", rows=ref("mc"), names={"item": "_vid", "count": "_mc"}),
        Step("cm",       "left_join", left=ref("months"), right=ref("mc_n"), left_on="_vid", right_on="_vid", default={"_mc": 0}),
        Step("z",        "filter", rows=ref("cm"), pred=E.lt(E.field("_mc"), E.field("_months"))),           # messageCount / months < 1 (integer division)
        Step("zv",       "pluck", rows=ref("z"), field="_vid"),
        Step("zm",       "filter_in", rows=ref("m_n"), field="_person", allowed=ref("zv")),                   # the zombies' messages, every date
        Step("zmv",      "pluck", rows=ref("zm"), field="_msg"),
        Step("lk",       "read_neighbors", srcs=ref("zmv"), etype="likes", dir="in", pairs=True),             # src = message, dst = liker
        Step("lk_n",     "rename", rows=ref("lk"), names={"src": "_msg", "dst": "_liker"}),
        Step("lv0",      "pluck", rows=ref("lk_n"), field="_liker"),
        Step("lv",       "dedup", items=ref("lv0")),
        Step("lp",       "read_vertex_props", vids=ref("lv"), props=["creationDate"], label="Person"),
        Step("lp_w",     "filter", rows=ref("lp"), pred=E.lt(cd, end)),
        Step("lp_v",     "pluck", rows=ref("lp_w"), field="_vid"),
        Step("lkz",      "flag_in", rows=ref("lk_n"), field="_liker", allowed=ref("zv"), into="_byZombie"),
        Step("lkt",      "flag_in", rows=ref("lkz"), field="_liker", allowed=ref("lp_v"), into="_byOld"),
        Step("lkp",      "join", left=ref("lkt"), right=ref("zm"), left_on="_msg", right_on="_msg"),           # + _person (the zombie)
        Step("lkd",      "derive", rows=ref("lkp"), into="_z", fn=E.if_(E.field("_byZombie"), E.lit(1), E.lit(0))),
        Step("lkd2",     "derive", rows=ref("lkd"), into="_t", fn=E.if_(E.field("_byOld"), E.lit(1), E.lit(0))),
        Step("lc",       "group_agg", rows=ref("lkd2"), by=["_person"], aggs={"zombieLikeCount": ["sum", "_z"], "totalLikeCount": ["sum", "_t"]}),
        Step("lc_n",     "rename", rows=ref("lc"), names={"_person": "_vid"}),
        Step("zs",       "left_join", left=ref("z"), right=ref("lc_n"), left_on="_vid", right_on="_vid", default={"zombieLikeCount": 0, "totalLikeCount": 0}),
        Step("scored",   "derive", rows=ref("zs"), into="zombieScore",
             fn=E.div(E.field("zombieLikeCount"), E.if_(E.eq(E.field("totalLikeCount"), E.lit(0)), E.lit(1), E.field("totalLikeCount")))),   # 0 / 1 when no like: the value model has no float literal
        Step("top",      "top_k", rows=ref("scored"), keys=[("zombieScore", "desc"), ("_vid", "asc")], k=100),
        Step("top_v",    "pluck", rows=ref("top"), field="_vid"),
        Step("ids",      "read_vertex_props", vids=ref("top_v"), props=["id"], label="Person"),
        Step("ids_n",    "rename", rows=ref("ids"), names={"id": "zombieId"}),
        Step("with_id",  "join", left=ref("top"), right=ref("ids_n"), left_on="_vid", right_on="_vid"),
        Step("out",      "project", rows=ref("with_id"), fields=["zombieId", "zombieLikeCount", "totalLikeCount", "zombieScore"]),
    ], params={"country": {"type": "str"}, "endDate": {"type": "int"}})


def bi13_run(args):
    """The label-run twin of bi13: the candidates' messages' dates read by one kept scan of the Post and of the Comment
    key run (_shared.with_label_run), not one point read per message."""
    return with_label_run(bi13(args), {"mp"})


SIM = {"bi13": bi13, "bi13_run": bi13_run}
