"""BI14 International dialog (spec 8.4 BI/read/14; reference neo4j/queries/bi-14.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E


def _country_persons(tag, country_param):
    """Steps: the Persons living in a City of the country named by `country_param`, as rows (src = person, dst = city)
    named `{tag}_rows`, their vid list `{tag}_v`."""
    return [
        Step(f"{tag}_c",    "read_index_eq", label="Place", prop="name", value=E.param(country_param)),
        Step(f"{tag}_ci0",  "read_neighbors", srcs=ref(f"{tag}_c"), etype="isPartOf", dir="in"),
        Step(f"{tag}_ci",   "dedup", items=ref(f"{tag}_ci0")),
        Step(f"{tag}_pc",   "read_neighbors", srcs=ref(f"{tag}_ci"), etype="isLocatedIn", dir="in", pairs=True),   # src = city, dst = person
        Step(f"{tag}_rows", "rename", rows=ref(f"{tag}_pc"), names={"src": "_city", "dst": "_person"}),
        Step(f"{tag}_v",    "pluck", rows=ref(f"{tag}_rows"), field="_person"),
    ]


def _interactions(tag, persons):
    """Steps: from the vid list `persons`, the (replier, author) pairs `{tag}_reply` -- a person of the list wrote a
    direct reply Comment to a message of someone -- and the (liker, author) pairs `{tag}_like`. hasCreator-in's far end
    is a Post or a Comment by the schema (never a Forum), so no label test is needed; replyOf-out's far end is a message,
    whose creator is read by hasCreator-out."""
    return [
        Step(f"{tag}_m",    "read_neighbors", srcs=ref(persons), etype="hasCreator", dir="in", pairs=True),     # src = person, dst = message
        Step(f"{tag}_mv0",  "pluck", rows=ref(f"{tag}_m"), field="dst"),
        Step(f"{tag}_mv",   "dedup", items=ref(f"{tag}_mv0")),
        Step(f"{tag}_par",  "read_neighbors", srcs=ref(f"{tag}_mv"), etype="replyOf", dir="out", pairs=True),    # src = reply, dst = parent
        Step(f"{tag}_pv0",  "pluck", rows=ref(f"{tag}_par"), field="dst"),
        Step(f"{tag}_pv",   "dedup", items=ref(f"{tag}_pv0")),
        Step(f"{tag}_pa",   "read_neighbors", srcs=ref(f"{tag}_pv"), etype="hasCreator", dir="out", pairs=True),  # src = parent, dst = author
        Step(f"{tag}_pa_n", "rename", rows=ref(f"{tag}_pa"), names={"src": "_parent", "dst": "_author"}),
        Step(f"{tag}_par_n", "rename", rows=ref(f"{tag}_par"), names={"src": "_reply", "dst": "_parent"}),
        Step(f"{tag}_m_n",  "rename", rows=ref(f"{tag}_m"), names={"src": "_replier", "dst": "_reply"}),
        Step(f"{tag}_r1",   "join_multi", left=ref(f"{tag}_m_n"), right=ref(f"{tag}_par_n"), left_on="_reply", right_on="_reply"),
        Step(f"{tag}_r2",   "join_multi", left=ref(f"{tag}_r1"), right=ref(f"{tag}_pa_n"), left_on="_parent", right_on="_parent"),
        Step(f"{tag}_reply", "derive", rows=ref(f"{tag}_r2"), into="_key", fn=E.tuple_(E.field("_replier"), E.field("_author"))),
        # likes given by the persons: liker -> message -> its author
        Step(f"{tag}_l",    "read_neighbors", srcs=ref(persons), etype="likes", dir="out", pairs=True),          # src = liker, dst = message
        Step(f"{tag}_lv0",  "pluck", rows=ref(f"{tag}_l"), field="dst"),
        Step(f"{tag}_lv",   "dedup", items=ref(f"{tag}_lv0")),
        Step(f"{tag}_la",   "read_neighbors", srcs=ref(f"{tag}_lv"), etype="hasCreator", dir="out", pairs=True),  # src = message, dst = author
        Step(f"{tag}_la_n", "rename", rows=ref(f"{tag}_la"), names={"src": "_msg", "dst": "_author"}),
        Step(f"{tag}_l_n",  "rename", rows=ref(f"{tag}_l"), names={"src": "_liker", "dst": "_msg"}),
        Step(f"{tag}_l1",   "join_multi", left=ref(f"{tag}_l_n"), right=ref(f"{tag}_la_n"), left_on="_msg", right_on="_msg"),
        Step(f"{tag}_like", "derive", rows=ref(f"{tag}_l1"), into="_key", fn=E.tuple_(E.field("_liker"), E.field("_author"))),
    ]


def bi14(args):
    """Pairs (person1 in a City of $country1, person2 in a City of $country2) that know each other, scored 4 if
    person1 replied to a message of person2 + 1 if person2 replied to one of person1 + 10 if person1 liked a message
    of person2 + 1 if person2 liked one of person1; per City of $country1 the top pair (score desc, then the lowest
    (person1.id, person2.id)); top 100 by score desc, person1.id, person2.id; with city1's name.

    Both countries' residents by Place.name -> isPartOf-in -> isLocatedIn-in (a City's list holds Universities too:
    their knows reads miss, one SEEK each, and they drop); knows from the country-1 residents, kept when the friend
    lives in country 2; the four interaction kinds as pair-key sets built from the messages of the DISTINCT PAIR
    MEMBERS only (hasCreator-in -> replyOf / likes, then the far message's creator), tested by tuple membership
    (flag_in); the per-city top row by group_top; ids and the city name read for the winners only. Open-kind messages
    occur only as hasCreator-in / likes-out far ends (Post | Comment by the schema): no label test."""
    return Plan(
        _country_persons("a", "country1") + _country_persons("b", "country2") + [
        Step("kn",       "read_neighbors", srcs=ref("a_v"), etype="knows", dir="both", pairs=True),              # src = p1, dst = friend
        Step("kn_b",     "filter_in", rows=ref("kn"), field="dst", allowed=ref("b_v")),                          # the friend lives in country 2
        Step("pairs0",   "rename", rows=ref("kn_b"), names={"src": "_p1", "dst": "_p2"}),
        Step("pairs",    "join_multi", left=ref("pairs0"), right=ref("a_rows"), left_on="_p1", right_on="_person"),   # + _city (of p1)
        Step("pa_v0",    "pluck", rows=ref("pairs"), field="_p1"),
        Step("pa_v",     "dedup", items=ref("pa_v0")),                                                   # only a pair member can score
        Step("pb_v0",    "pluck", rows=ref("pairs"), field="_p2"),
        Step("pb_v",     "dedup", items=ref("pb_v0")),
        ] + _interactions("ia", "pa_v") + _interactions("ib", "pb_v") + [
        Step("k12",      "derive", rows=ref("pairs"), into="_k12", fn=E.tuple_(E.field("_p1"), E.field("_p2"))),
        Step("k21",      "derive", rows=ref("k12"), into="_k21", fn=E.tuple_(E.field("_p2"), E.field("_p1"))),
        Step("ia_rk",    "pluck", rows=ref("ia_reply"), field="_key"),
        Step("ib_rk",    "pluck", rows=ref("ib_reply"), field="_key"),
        Step("ia_lk",    "pluck", rows=ref("ia_like"), field="_key"),
        Step("ib_lk",    "pluck", rows=ref("ib_like"), field="_key"),
        Step("f1",       "flag_in", rows=ref("k21"), field="_k12", allowed=ref("ia_rk"), into="_r12"),    # p1 replied to p2
        Step("f2",       "flag_in", rows=ref("f1"), field="_k21", allowed=ref("ib_rk"), into="_r21"),     # p2 replied to p1
        Step("f3",       "flag_in", rows=ref("f2"), field="_k12", allowed=ref("ia_lk"), into="_l12"),     # p1 liked p2's
        Step("f4",       "flag_in", rows=ref("f3"), field="_k21", allowed=ref("ib_lk"), into="_l21"),     # p2 liked p1's
        Step("scored",   "derive", rows=ref("f4"), into="score",
             fn=E.add(E.add(E.if_(E.field("_r12"), E.lit(4), E.lit(0)), E.if_(E.field("_r21"), E.lit(1), E.lit(0))),
                      E.add(E.if_(E.field("_l12"), E.lit(10), E.lit(0)), E.if_(E.field("_l21"), E.lit(1), E.lit(0))))),
        Step("percity",  "group_top", rows=ref("scored"), by="_city", keys=[("score", "desc"), ("_p1", "asc"), ("_p2", "asc")]),
        Step("top",      "top_k", rows=ref("percity"), keys=[("score", "desc"), ("_p1", "asc"), ("_p2", "asc")], k=100),
        Step("p1v",      "pluck", rows=ref("top"), field="_p1"),
        Step("p2v",      "pluck", rows=ref("top"), field="_p2"),
        Step("cv",       "pluck", rows=ref("top"), field="_city"),
        Step("p1i",      "read_vertex_props", vids=ref("p1v"), props=["id"], label="Person"),
        Step("p1n",      "rename", rows=ref("p1i"), names={"id": "person1Id"}),
        Step("p2i",      "read_vertex_props", vids=ref("p2v"), props=["id"], label="Person"),
        Step("p2n",      "rename", rows=ref("p2i"), names={"id": "person2Id"}),
        Step("cn0",      "read_vertex_props", vids=ref("cv"), props=["name"], label="Place"),
        Step("cn",       "rename", rows=ref("cn0"), names={"name": "city1Name"}),
        Step("j1",       "join", left=ref("top"), right=ref("p1n"), left_on="_p1", right_on="_vid"),
        Step("j2",       "join", left=ref("j1"), right=ref("p2n"), left_on="_p2", right_on="_vid"),
        Step("j3",       "join", left=ref("j2"), right=ref("cn"), left_on="_city", right_on="_vid"),
        Step("out",      "project", rows=ref("j3"), fields=["person1Id", "person2Id", "city1Name", "score"]),
    ], params={"country1": {"type": "str"}, "country2": {"type": "str"}})


SIM = {"bi14": bi14}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"bi14": _KNOWS_HINTS}
