"""BI10 Experts in social circle (spec 8.4 BI/read/10; reference neo4j/queries/bi-10.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E

MIN_DIST, MAX_DIST = 3, 4     # the spec's path-distance parameters, fixed in the plan (the IR has no loop; the BI
                              # parameter generator keeps them at 3 / 4)


def bi10(args):
    """Persons at knows-distance 3..4 from $personId (the BFS levels: not reachable in fewer hops) living in a City
    of $country, with a Message that has a Tag of $tagClass; per (person, tag) over ALL tags of such messages the
    number of distinct messages; top 100 by count desc, tag.name asc, person.id asc.

    The person by id, then an unrolled BFS over knows (both ways): each level reads the new frontier only (a visited
    set), so every reached person's list is read once; candidates = levels 3 and 4. isLocatedIn out per candidate, kept
    when the city is one of the country's (Place.name, then one isPartOf-in read); hasCreator in per expert, hasTag out per message; the class's tags by
    TagClass.name -> hasType in. The pairs, the count and the top 100 are flow (tag names read for the rows that tie
    on the count at the cut, then the final order). Every far end has one label by the schema: no label test."""
    steps = [Step("me", "read_vertex_by_id", vid=E.let("me_vid"), label="Person"),   # the query names the person
             Step("S0", "dedup", items=ref("me"))]
    f, seen = "S0", "S0"
    for k in range(1, MAX_DIST + 1):
        steps += [Step(f"N{k}", "read_neighbors", srcs=ref(f), etype="knows", dir="both"),
                  Step(f"D{k}", "dedup", items=ref(f"N{k}")),
                  Step(f"L{k}", "exclude", items=ref(f"D{k}"), remove=ref(seen)),           # the persons first reached at hop k
                  Step(f"S{k}", "concat", a=ref(seen), b=ref(f"L{k}"))]
        f, seen = f"L{k}", f"S{k}"
    steps += [
        Step("cand",     "concat", a=ref(f"L{MIN_DIST}"), b=ref(f"L{MAX_DIST}")),
        Step("ctry",     "read_index_eq", label="Place", prop="name", value=E.param("country")),
        Step("cc",       "read_neighbors", srcs=ref("ctry"), etype="isPartOf", dir="in"),                     # the country's cities: one read
        Step("cities",   "dedup", items=ref("cc")),
        Step("city",     "read_neighbors", srcs=ref("cand"), etype="isLocatedIn", dir="out", pairs=True),     # src = person, dst = city
        Step("exp_r",    "filter_in", rows=ref("city"), field="dst", allowed=ref("cities")),
        Step("experts",  "pluck", rows=ref("exp_r"), field="src"),
        Step("tc",       "read_index_eq", label="TagClass", prop="name", value=E.param("tagClass")),
        Step("T0",       "read_neighbors", srcs=ref("tc"), etype="hasType", dir="in"),
        Step("T",        "dedup", items=ref("T0")),
        Step("msgs",     "read_neighbors", srcs=ref("experts"), etype="hasCreator", dir="in", pairs=True),   # src = expert, dst = message
        Step("mv",       "pluck", rows=ref("msgs"), field="dst"),                                             # one creator per message: distinct
        Step("mt",       "read_neighbors", srcs=ref("mv"), etype="hasTag", dir="out", pairs=True),           # src = message, dst = tag
        Step("mt_c",     "filter_in", rows=ref("mt"), field="dst", allowed=ref("T")),
        Step("qm0",      "pluck", rows=ref("mt_c"), field="src"),
        Step("qm",       "dedup", items=ref("qm0")),                                                          # messages with a tag of the class
        Step("mt_q",     "filter_in", rows=ref("mt"), field="src", allowed=ref("qm")),                       # all their tags
        Step("mt_n",     "rename", rows=ref("mt_q"), names={"src": "_msg", "dst": "_tag"}),
        Step("msgs_n",   "rename", rows=ref("msgs"), names={"src": "_person", "dst": "_msg"}),
        Step("pmt",      "join", left=ref("mt_n"), right=ref("msgs_n"), left_on="_msg", right_on="_msg"),    # (_msg, _tag, _person)
        Step("cnt",      "group_agg", rows=ref("pmt"), by=["_person", "_tag"], aggs={"messageCount": ["count", None]}),
        Step("cut",      "top_k", rows=ref("cnt"), keys=[("messageCount", "desc")], k=100, keep_ties=True),
        Step("tv0",      "pluck", rows=ref("cut"), field="_tag"),
        Step("tv",       "dedup", items=ref("tv0")),
        Step("tn",       "read_vertex_props", vids=ref("tv"), props=["name"], label="Tag"),
        Step("tn_n",     "rename", rows=ref("tn"), names={"name": "tagName"}),
        Step("named",    "join", left=ref("cut"), right=ref("tn_n"), left_on="_tag", right_on="_vid"),
        Step("top",      "top_k", rows=ref("named"), keys=[("messageCount", "desc"), ("tagName", "asc"), ("_person", "asc")], k=100),
        Step("pv0",      "pluck", rows=ref("top"), field="_person"),
        Step("pv",       "dedup", items=ref("pv0")),
        Step("ids",      "read_vertex_props", vids=ref("pv"), props=["id"], label="Person"),
        Step("ids_n",    "rename", rows=ref("ids"), names={"id": "personId"}),
        Step("with_id",  "join", left=ref("top"), right=ref("ids_n"), left_on="_person", right_on="_vid"),
        Step("out",      "project", rows=ref("with_id"), fields=["personId", "tagName", "messageCount"]),
    ]
    return Plan(steps, params={"personId": {"type": "int"}, "country": {"type": "str"}, "tagClass": {"type": "str"}},
                let={"me_vid": E.compose_vid("Person", E.param("personId"))})


SIM = {"bi10": bi10}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"bi10": _KNOWS_HINTS}
