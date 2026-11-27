"""BI4 Top message creators by country (spec 8.4 BI/read/4; reference neo4j/queries/bi-4.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E


def bi4(args):
    """The top 100 Forums created after $date by their member count per country (every (forum, country) row ordered
    by count desc, forum.id asc, country.id asc; a forum ranks by its best row); then per member of a top forum the
    number of distinct Messages in the threads of the top forums' Posts, a member with none counting 0; top 100 by
    messageCount desc, person.id asc, with id, firstName, lastName, creationDate.

    The schema has no Forum.creationDate index (plan decision 6: it waits for v4), so the forums come from the type
    enumeration plus one props read each. hasMember out per forum, isLocatedIn / isPartOf out per member; the
    (forum, country) counts, the per-forum best row and the top 100 are flow. containerOf out per top forum, the
    thread closure by replyOf in (read_closure: level-BFS, posts included), hasCreator out per message, the
    creator kept when a member of a top forum. Every far end has one label by the schema: no label test."""
    return Plan([
        Step("fv",       "read_vertices_by_label", label="Forum"),
        Step("fp",       "read_vertex_props", vids=ref("fv"), props=["creationDate"], label="Forum"),
        Step("fw",       "filter", rows=ref("fp"), pred=E.gt(E.field("creationDate"), E.param("date"))),
        Step("fwv",      "pluck", rows=ref("fw"), field="_vid"),
        Step("mem",      "read_neighbors", srcs=ref("fwv"), etype="hasMember", dir="out", pairs=True),       # src = forum, dst = member
        Step("mem_n",    "rename", rows=ref("mem"), names={"src": "_forum", "dst": "_person"}),
        Step("pv0",      "pluck", rows=ref("mem_n"), field="_person"),
        Step("pv",       "dedup", items=ref("pv0")),
        Step("city",     "read_neighbors", srcs=ref("pv"), etype="isLocatedIn", dir="out", pairs=True),      # src = person, dst = city
        Step("city_n",   "rename", rows=ref("city"), names={"src": "_person", "dst": "_city"}),
        Step("cv0",      "pluck", rows=ref("city_n"), field="_city"),
        Step("cv",       "dedup", items=ref("cv0")),
        Step("ctry",     "read_neighbors", srcs=ref("cv"), etype="isPartOf", dir="out", pairs=True),         # src = city, dst = country
        Step("ctry_n",   "rename", rows=ref("ctry"), names={"src": "_city", "dst": "_country"}),
        Step("pc",       "join_multi", left=ref("city_n"), right=ref("ctry_n"), left_on="_city", right_on="_city"),
        Step("mpc",      "join_multi", left=ref("mem_n"), right=ref("pc"), left_on="_person", right_on="_person"),
        Step("fc",       "group_agg", rows=ref("mpc"), by=["_forum", "_country"], aggs={"numberOfMembers": ["count", None]}),
        Step("fbest",    "group_top", rows=ref("fc"), by="_forum", keys=[("numberOfMembers", "desc"), ("_country", "asc")]),
        Step("topf",     "top_k", rows=ref("fbest"), keys=[("numberOfMembers", "desc"), ("_forum", "asc"), ("_country", "asc")], k=100),
        Step("tfv",      "pluck", rows=ref("topf"), field="_forum"),
        Step("tmem",     "filter_in", rows=ref("mem_n"), field="_forum", allowed=ref("tfv")),                 # the top forums' members
        Step("tmv0",     "pluck", rows=ref("tmem"), field="_person"),
        Step("tmv",      "dedup", items=ref("tmv0")),
        Step("posts",    "read_neighbors", srcs=ref("tfv"), etype="containerOf", dir="out"),
        Step("msgs",     "read_closure", srcs=ref("posts"), etype="replyOf", dir="in"),                       # the threads: posts + every reply
        Step("cr",       "read_neighbors", srcs=ref("msgs"), etype="hasCreator", dir="out", pairs=True),      # src = message, dst = creator
        Step("cr_m",     "filter_in", rows=ref("cr"), field="dst", allowed=ref("tmv")),
        Step("cr_v",     "pluck", rows=ref("cr_m"), field="dst"),
        Step("cnt",      "group_count", items=ref("cr_v")),                                                    # one creator per message: distinct messages
        Step("cnt_n",    "rename", rows=ref("cnt"), names={"item": "_person", "count": "messageCount"}),
        Step("mrows",    "dedup_rows", rows=ref("tmem"), field="_person"),                                   # one row per member
        Step("scored",   "left_join", left=ref("mrows"), right=ref("cnt_n"), left_on="_person", right_on="_person", default={"messageCount": 0}),
        Step("top",      "top_k", rows=ref("scored"), keys=[("messageCount", "desc"), ("_person", "asc")], k=100),
        Step("top_v",    "pluck", rows=ref("top"), field="_person"),
        Step("props",    "read_vertex_props", vids=ref("top_v"), props=["id", "firstName", "lastName", "creationDate"], label="Person"),
        Step("props_n",  "rename", rows=ref("props"), names={"id": "personId", "firstName": "personFirstName", "lastName": "personLastName", "creationDate": "personCreationDate"}),
        Step("with_p",   "join", left=ref("top"), right=ref("props_n"), left_on="_person", right_on="_vid"),
        Step("out",      "project", rows=ref("with_p"), fields=["personId", "personFirstName", "personLastName", "personCreationDate", "messageCount"]),
    ], params={"date": {"type": "int"}})


SIM = {"bi4": bi4}
