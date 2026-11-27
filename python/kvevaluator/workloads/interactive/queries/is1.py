"""LDBC IS1 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import _anchor_person, person_vid, PERSON_PARAM, PERSON_LET


# --------------------------------------------------------------- short reads

IS1_FIELDS = ["firstName", "lastName", "birthday", "locationIP", "browserUsed", "cityId", "gender",
              "creationDate"]


def is1(args):
    """IS1: a person's profile + the id of their city."""
    p = let("me_vid")
    return Plan([
        Step("me",         "read_vertex_by_id", vid=p, label="Person"),
        Step("profile",    "read_vertex_props", vids=ref("me"),
             props=["firstName", "lastName", "birthday", "locationIP",
                    "browserUsed", "gender", "creationDate"], label="Person"),
        Step("city",       "read_neighbors", srcs=ref("me"), etype="isLocatedIn", dir="out"),
        Step("city_id",    "read_vertex_props", vids=ref("city"), props=["id"], label="Place"),
        Step("city_named", "rename", rows=ref("city_id"), names={"id": "cityId"}),
        Step("joined",     "zip_rows", left=ref("profile"), right=ref("city_named")),
        Step("out",        "project", rows=ref("joined"), fields=IS1_FIELDS),
    ], params=PERSON_PARAM, let=PERSON_LET)


def is1_dgraph_engine(args):
    """Person profile + city, from dgraph/is1/p1/plan.txt (block 0). live S=3 G=8."""
    return _anchor_person(person_vid(args["person"])) + [   # block 0: eq(id) + type(person)
        # block 0: the 7 scalar predicates of the person, 1 GET each
        Step("props", "read_vertex_props", vids=ref("me"),
             props=["firstName", "lastName", "birthday", "locationIP", "browserUsed",
                    "gender", "creationDate"]),
        # block 0: isLocatedIn -- the forward posting (1 SEEK)
        Step("city", "read_neighbors", srcs=ref("me"), etype="isLocatedIn", dir="out"),
        # block 0: isLocatedIn { cityId: id } -- 1 GET
        Step("city_id", "read_vertex_props", vids=ref("city"), props=["id"]),
        # LDBC output columns (in memory): the person's one row + its one city
        Step("city_named", "rename", rows=ref("city_id"), names={"id": "cityId", "_vid": "_city"}),
        Step("joined", "zip_rows", left=ref("props"), right=ref("city_named")),
        Step("out", "project", rows=ref("joined"), fields=IS1_FIELDS),
    ]


# ── IS1-IS7, nebula ──────────────────────────────────────────────────────────
# Three nebula conventions, each read off the captured plans and confirmed against live
# /kvstats to the op (all six cells are exact, no residual):
#
#   1. The anchor is FREE. `id(n) == $personId` needs no index -- the nebula vid IS the
#      external id, so it is the storage key. Contrast dgraph, where the same anchor costs
#      an eq(id) index SEEK plus a type-index SEEK.
#
#   2. `first step filter: X._tag IS NOT EMPTY` on a Traverse costs 1 GET on the SOURCE
#      vertex. It is a tag-existence probe. When that Traverse also lists `vertexProps` for
#      the same tag, the two collapse into the one GET that reads the row (IS1). When the
#      Traverse has no first-step-filter line at all, there is no source read (IS5) -- which
#      is why IS5 costs a GET less than IS3 despite the same shape.
#
#   3. `AppendVertices` whose `props` are only `_tag` costs NOTHING: graphd runs with
#      `--optimize_appendvertices=true` (engine/data/backup/nebula/runs-sf*/etc/nebula-graphd.conf),
#      and AppendVerticesExecutor then skips the storage call (noNeedFetchProp ->
#      handleNullProp). IS1's Place is that node. Modelled as a pure-id read_vertex_props, which is free.
def is1_nebula_engine(args):
    """Person profile + city, from nebula/is1/p1/plan.txt. live S=1 G=1 N=1 -- exact."""
    p = person_vid(args["person"])
    return [
        Step("me", "read_vertex_by_id", vid=p),                      # Start: vid IS the key, free
        # node 9 Traverse: vertexProps Person (7 props) + `first step filter: Person._tag` --
        # one GET reads the row
        Step("props", "read_vertex_props", vids=ref("me"), label="Person",
             props=["firstName", "lastName", "birthday", "locationIP", "browserUsed",
                    "gender", "creationDate"]),
        # node 9 Traverse: isLocatedIn OUT_EDGE
        Step("city", "read_neighbors", srcs=ref("me"), etype="isLocatedIn", dir="out"),
        # node 10 AppendVertices: Place `_tag` only -> no storage call (convention 3)
        Step("cityr", "read_vertex_props", vids=ref("city"), label="Place", props=["id"]),
        Step("cityid", "pluck", rows=ref("cityr"), field="id"),
        Step("out", "attach", rows=ref("props"), values=ref("cityid"), into="cityId"),
    ]


def is1_arangodb_engine(args):
    """Person profile + city, from arangodb/is1/p1/plan.txt. live S=2 G=4 N=1 -- exact.

    G=4 is two materializations: the person (IndexNode primary + the doc its attributes need)
    and the city. The city is the interesting one -- the projection is `id(city)` and node 5
    lists `vertexProjections=[_key]`, yet arango still fetches pk + document. The `_key` is
    already in the edge index's value; reading the document for it is pure engine waste, and
    it is why this costs 4 GETs where nebula's identical query costs 1."""
    p = person_vid(args["person"])
    return [
        # [8] IndexNode primary(_key) + the document its attributes need = 2 GETs total.
        # NOT a read_vertex_by_id followed by read_vertex_props: that charges the pk twice (measured 2, not 3).
        Step("props", "read_vertex_props", vids=[p], label="Person",
             props=["firstName", "lastName", "birthday", "locationIP", "browserUsed",
                    "gender", "creationDate"]),
        Step("me", "pluck", rows=ref("props"), field="_vid"),
        # [5] TraversalNode isLocatedIn edge(_from), 1..1 -- the first traversal
        Step("open", "traversal_open", items=ref("me"), etype="isLocatedIn"),
        Step("city", "read_neighbors", srcs=ref("open"), etype="isLocatedIn", dir="out"),
        # [5] vertexProjections [_key], yet the city is materialized (fact 1)
        Step("cityr", "docs_per_step", vids=ref("city"), label="Place", props=["id"]),
        Step("cityid", "pluck", rows=ref("cityr"), field="id"),
        Step("out", "attach", rows=ref("props"), values=ref("cityid"), into="cityId"),
    ]


# single candidate: the logical plan IS the physical one (passthrough search)
LOGICAL = {"is1": is1}


ENGINE = {
    "is1": {"dgraph": is1_dgraph_engine, "nebula": is1_nebula_engine, "arangodb": is1_arangodb_engine}
}
