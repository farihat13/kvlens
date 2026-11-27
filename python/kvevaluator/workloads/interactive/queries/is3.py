"""LDBC IS3 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import PERSON_RENAME, _anchor_person, person_vid, PERSON_PARAM, PERSON_LET


IS3_FIELDS = ["personId", "personFirstName", "personLastName", "friendshipCreationDate"]


def is3(args):
    """IS3: all friends of a person + the friendship date, sorted by
    (friendshipCreationDate desc, personId asc). The edge date is a covered read
    (dgraph facet / nebula edge value / arango edge doc)."""
    p = let("me_vid")
    return Plan([
        Step("me",           "read_vertex_by_id", vid=p, label="Person"),
        Step("friends",      "read_neighbors", srcs=ref("me"), etype="knows", dir="both",
             edge_props=["creationDate"], pairs=True),
        Step("friend_vids",  "pluck", rows=ref("friends"), field="dst"),
        Step("friend_props", "read_vertex_props", vids=ref("friend_vids"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("rows",         "zip_rows", left=ref("friends"), right=ref("friend_props")),
        Step("sorted",       "top_k", rows=ref("rows"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=None),
        Step("labeled",      "rename", rows=ref("sorted"),
             names={**PERSON_RENAME, "creationDate": "friendshipCreationDate"}),
        Step("out",          "project", rows=ref("labeled"), fields=IS3_FIELDS),
    ], params=PERSON_PARAM, let=PERSON_LET)


def _is3_dgraph_block(d, dir):
    """One child block of the person (`knows` or `~knows`): the posting (1 SEEK; the
    `@facets(creationDate)` rides in it, 0 extra ops), `@filter(type(person))` (1 type-index
    SEEK, 0 when the posting is empty), then id/firstName/lastName (1 GET each per friend)."""
    return [
        Step(f"fr_{d}", "read_neighbors", srcs=ref("me"), etype="knows", dir=dir,
             edge_props=["creationDate"], pairs=True),
        Step(f"frv_{d}", "pluck", rows=ref(f"fr_{d}"), field="dst"),
        Step(f"ft_{d}", "read_filter_by_label_index", vids=ref(f"frv_{d}"), label="Person"),
        Step(f"props_{d}", "read_vertex_props", vids=ref(f"ft_{d}"),
             props=["id", "firstName", "lastName"]),
        Step(f"withd_{d}", "join", left=ref(f"props_{d}"), right=ref(f"fr_{d}"),
             left_on="_vid", right_on="dst"),
    ]


def is3_dgraph_engine(args):
    """Friends + knows date, from dgraph/is3/p1/plan.txt (block 0). The two directions are two
    separate child blocks, each with its own type filter and its own prop reads; a direction
    with no friends costs its posting SEEK only. LDBC then sorts by the facet desc, friend id
    asc (client side)."""
    return _anchor_person(person_vid(args["person"])) + (      # block 0: eq(id) + type(person)
        _is3_dgraph_block("out", "out")                        # block 0: knows @filter(type(person))
        + _is3_dgraph_block("in", "in")                        # block 0: ~knows @filter(type(person))
    ) + [
        # in memory: both blocks' rows, the client sort
        Step("both", "concat", a=ref("withd_out"), b=ref("withd_in")),
        Step("withd", "rename", rows=ref("both"), names={"creationDate": "knowsDate"}),
        Step("sorted", "top_k", rows=ref("withd"),
             keys=[("knowsDate", "desc"), ("id", "asc")], k=None),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("sorted"),
             names={**PERSON_RENAME, "knowsDate": "friendshipCreationDate"}),
        Step("out", "project", rows=ref("labeled"), fields=IS3_FIELDS),
    ]


def is3_nebula_engine(args):
    """Friends + knows date, from nebula/is3/p1/plan.txt. live S=2 G=10 N=9 -- exact.

    G=10, not 9: the nine friend rows plus ONE for the anchor's own tag-existence probe
    (`first step filter: Person._tag IS NOT EMPTY` on node 10). The knows facet rides along
    in the edge value, so `creationDate` is free -- same as dgraph's @facets."""
    p = person_vid(args["person"])
    return [
        Step("me", "read_vertex_by_id", vid=p),                      # Start: vid IS the key, free
        # node 10 Traverse: `first step filter: Person._tag` -- 1 GET on the source
        Step("mtag", "tag_probe", vids=ref("me"), label="Person"),
        # node 10 Traverse: knows BOTH, edgeProps creationDate (in the edge value)
        Step("fr", "read_neighbors", srcs=ref("me"), etype="knows", dir="both",
             edge_props=["creationDate"]),
        Step("frv", "pluck", rows=ref("fr"), field="dst"),
        # node 11 AppendVertices: Person tag (firstName, lastName), dedup: 1
        Step("props", "read_vertex_props", vids=ref("frv"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("dates", "pluck", rows=ref("fr"), field="creationDate"),
        Step("withd", "attach", rows=ref("props"), values=ref("dates"), into="knowsDate"),
        Step("sorted", "top_k", rows=ref("withd"),
             keys=[("knowsDate", "desc"), ("id", "asc")], k=None),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("sorted"),
             names={**PERSON_RENAME, "knowsDate": "friendshipCreationDate"}),
        Step("out", "project", rows=ref("labeled"), fields=IS3_FIELDS),
    ]


def is3_arangodb_engine(args):
    """Friends + knows date, from arangodb/is3/p1/plan.txt. live S=3 G=27 N=9 -- exact.

    The cleanest arithmetic in the suite: S = 1 (traversal_open) + 2 (knows ANY = two edge
    indexes); N = 9 edges; G = 9 edge documents (the query wants `e.creationDate`, so the
    covering index is not enough) + 9 friends x 2 for pk + doc."""
    p = person_vid(args["person"])
    return [
        # no IndexNode in the captured plan: the traversal starts from the literal
        # 'Person/<id>' string, so the anchor itself costs nothing
        # [2] TraversalNode knows edge(_from)+edge(_to), 1..1 -- the first traversal
        Step("open", "traversal_open", items=[p], etype="knows"),
        # [2] edgeProjections include creationDate -> +1 GET per edge: the edge document
        Step("fr", "read_neighbors", srcs=ref("open"), etype="knows", dir="both",
             edge_props=["creationDate"]),
        Step("frv", "pluck", rows=ref("fr"), field="dst"),
        # [2] vertexProjections [_key, firstName, lastName]: the friend document per step
        Step("props", "docs_per_step", vids=ref("frv"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("dates", "pluck", rows=ref("fr"), field="creationDate"),
        Step("withd", "attach", rows=ref("props"), values=ref("dates"), into="knowsDate"),
        Step("sorted", "top_k", rows=ref("withd"),
             keys=[("knowsDate", "desc"), ("id", "asc")], k=None),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("sorted"),
             names={**PERSON_RENAME, "knowsDate": "friendshipCreationDate"}),
        Step("out", "project", rows=ref("labeled"), fields=IS3_FIELDS),
    ]


# single candidate: the logical plan IS the physical one (passthrough search)
LOGICAL = {"is3": is3}


ENGINE = {
    "is3": {"dgraph": is3_dgraph_engine, "nebula": is3_nebula_engine, "arangodb": is3_arangodb_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"is3": _KNOWS_HINTS}
