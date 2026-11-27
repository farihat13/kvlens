"""LDBC IC11 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import lt, and_, notnull, eq, ne, get, field, param, let
from kvevaluator.workloads.interactive._shared import (PERSON_RENAME, _anchor_person, _friend_set, _scan_lookup, person_vid,
                                                 PERSON_PARAM, PERSON_LET)


# ----------------------------------------------------------------------- IC11

IC11_FIELDS = ["personId", "personFirstName", "personLastName", "organizationName", "workFrom"]

def _ic11_steps(args, label_only=False, logical=False):
    """IC11: friends+fof who work at a company in `countryName` starting before
    `year` -> top 10 by (workFrom asc, personId asc, organizationName desc).
    workFrom is a covered edge prop; org->country membership goes FORWARD
    (org isLocatedIn place, few orgs) -- the reverse from the country would drag
    in every message located there.

    label_only: the Place.name lookup becomes read_vertices_by_label(Place) + name read +
    filter instead of the read_index_eq SEEK (the label-index-only design)."""
    p = let("me_vid")
    if logical:                                  # the optimizer's entry choice point
        country_entry = [Step("country", "entry_eq", label="Place", prop="name",
                              value=param("countryName"))]
    else:
        country_entry = (_scan_lookup("country", "Place", "name", param("countryName")) if label_only
                         else [Step("country", "read_index_eq", label="Place", prop="name", value=param("countryName"))])
    return Plan(_friend_set(p, 2) + [
        Step("works",       "read_neighbors", srcs=ref("f"), etype="workAt", dir="out",
             edge_props=["workFrom"], pairs=True),
        Step("works_pre",   "filter", rows=ref("works"), pred=lt(field("workFrom"), param("year"))),
    ] + country_entry + [
        Step("orgv",        "pluck", rows=ref("works_pre"), field="dst"),
        Step("orgs",        "dedup", items=ref("orgv")),
        Step("org_loc",     "read_neighbors", srcs=ref("orgs"), etype="isLocatedIn", dir="out", pairs=True),
        Step("in_country",  "filter_in", rows=ref("org_loc"), field="dst", allowed=ref("country")),
        Step("country_orgs","pluck", rows=ref("in_country"), field="src"),
        Step("works_here",  "filter_in", rows=ref("works_pre"), field="dst", allowed=ref("country_orgs")),
        Step("org_name",    "read_vertex_props", vids=ref("country_orgs"), props=["name"], label="Organisation"),
        Step("org_named",   "rename", rows=ref("org_name"), names={"name": "organizationName"}),
        Step("works_named", "join", left=ref("works_here"), right=ref("org_named"), left_on="dst", right_on="_vid"),
        Step("personv",     "pluck", rows=ref("works_named"), field="src"),
        Step("persons",     "dedup", items=ref("personv")),
        Step("pid",         "read_vertex_props", vids=ref("persons"), props=["id"], label="Person"),
        Step("pid_named",   "rename", rows=ref("pid"), names={"id": "personId"}),
        Step("ranked",      "join", left=ref("works_named"), right=ref("pid_named"), left_on="src", right_on="_vid"),
        Step("top10",       "top_k", rows=ref("ranked"),
             keys=[("workFrom", "asc"), ("personId", "asc"), ("organizationName", "desc")],
             k=10),
        # firstName/lastName are projection-only (workFrom/personId/organizationName are the sort
        # keys, all read already), so defer the names to the 10 winners. Mirrors IC5.
        Step("topv",        "pluck", rows=ref("top10"), field="src"),
        Step("names",       "read_vertex_props", vids=ref("topv"), props=["firstName", "lastName"],
             label="Person"),
        Step("names_r",     "rename", rows=ref("names"),
             names={"firstName": "personFirstName", "lastName": "personLastName"}),
        Step("named",       "join", left=ref("top10"), right=ref("names_r"), left_on="src", right_on="_vid"),
        Step("out",         "project", rows=ref("named"), fields=IC11_FIELDS),
    ], params={**PERSON_PARAM, "countryName": {}, "year": {"type": "int"}}, let=PERSON_LET)


def ic11_logical(args):
    """The LOGICAL ic11 for the optimizer's search: one entry_eq choice point
    (Place.name index vs label scan)."""
    return _ic11_steps(args, logical=True)


LOGICAL = {"ic11": ic11_logical}


def ic11(args):
    return _ic11_steps(args)


def ic11_labelonly(args):
    return _ic11_steps(args, label_only=True)


# ── IC11 ─────────────────────────────────────────────────────────────────────
# "Friends and FoF who started working at a company in $countryName before $workFromYear."
#
# Nebula (p1), from the captured plan:
#   26  Traverse knows BOTH 1..2, dedup           528 rows
#   31  AppendVertices Person [firstName,lastName]
#   32  Filter friend != person   ->  9 Dedup     the distinct friend set
#   11  Traverse WORK_AT OUT, vertexProps tagId 2 (the FRIEND's row, fused into the
#         first-step Person filter -- IS1's rule)  744 workAt edges
#   28  Traverse IS_LOCATED_IN OUT, vertexProps tagId 6 (the COMPANY's name, fused)
#   36  Filter workFrom < $workFromYear           215 survive
#   37  AppendVertices Place [name]               the countries
#   38  Filter place name == $countryName
#
# Exact on S, G and N at params 1/2/3. **But read the row check with care here**: the
# $countryName filter is so selective at sf0.1 that the answer is 0, 1 and 0 rows. A
# 0-row match is agreement about emptiness, not about the projection, so this cell rests
# almost entirely on the KV counts. The same caveat applies to IC3 (0 rows at every param).
def ic11_nebula_engine(args):
    p = person_vid(args["person"])
    year = int(args["year"])
    country = args["countryName"]
    return [
        Step("me", "read_vertex_by_id", vid=p),
        # node 26 Traverse knows BOTH 1..2, dedup: 1: first-step Person._tag (1 GET), then
        # 1 SEEK per direction per frontier vertex + 1 NEXT per edge
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("h1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("h1d", "dedup", items=ref("h1")),
        Step("h2", "read_neighbors", srcs=ref("h1d"), etype="knows", dir="both"),
        Step("fu", "concat", a=ref("h1d"), b=ref("h2")),
        Step("fd", "dedup", items=ref("fu")),
        # trail rule: p is never a 2-step path end (ic9.py note 2); nodes 30/32/33/9 in memory
        Step("fr", "exclude", items=ref("fd"), remove=ref("me")),
        # node 31 AppendVertices Person [firstName,lastName], dedup: 1
        Step("fp", "read_vertex_props", vids=ref("fr"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("frn", "rename", rows=ref("fp"),
             names={"id": "friendId", "firstName": "friendFirstName",
                    "lastName": "friendLastName"}),
        # node 11 Traverse workAt OUT per friend (edgeProps workFrom): vertexProps Person
        # [firstName,lastName] -- 1 GET per source friend -- + 1 SEEK + 1 NEXT per edge
        Step("ftag", "read_vertex_props", vids=ref("fr"), label="Person",
             props=["firstName", "lastName"]),
        Step("wa", "read_neighbors", srcs=ref("fr"), etype="workAt", dir="out",
             edge_props=["workFrom"], pairs=True),
        Step("co", "pluck", rows=ref("wa"), field="dst"),
        # node 28 Traverse isLocatedIn OUT per distinct company (ALL workAt targets: the year
        # filter, node 36, comes after): vertexProps Organisation [name] fused with the
        # first-step Organisation._tag filter (1 GET), + 1 SEEK + 1 NEXT
        Step("cn", "tag_probe", vids=ref("co"), label="Organisation", props=["name"]),
        Step("loc", "read_neighbors", srcs=ref("co"), etype="isLocatedIn", dir="out", pairs=True),
        # node 36: the workFrom filter, applied BEFORE the place read
        Step("keep", "filter", rows=ref("wa"),
             pred=and_(notnull(get("workFrom")), lt(field("workFrom"), year))),
        Step("kc", "pluck", rows=ref("keep"), field="dst"),
        Step("kloc", "filter_in", rows=ref("loc"), field="src", allowed=ref("kc")),
        Step("pv", "pluck", rows=ref("kloc"), field="dst"),
        # node 37 AppendVertices Place [name], dedup: 1, over the year-filtered rows
        Step("pl", "read_vertex_props", vids=ref("pv"), label="Place", props=["name"]),
        Step("plc", "rename", rows=ref("pl"), names={"name": "placeName"}),
        Step("cloc", "join", left=ref("kloc"), right=ref("plc"),
             left_on="dst", right_on="_vid"),
        Step("inc", "filter", rows=ref("cloc"), pred=eq(field("placeName"), country)),
        Step("incc", "pluck", rows=ref("inc"), field="src"),
        Step("wk", "filter_in", rows=ref("keep"), field="dst", allowed=ref("incc")),
        Step("cnr", "rename", rows=ref("cn"), names={"name": "organizationName"}),
        Step("wc", "join", left=ref("wk"), right=ref("cnr"), left_on="dst", right_on="_vid"),
        Step("wf", "join", left=ref("wc"), right=ref("frn"), left_on="src", right_on="_vid"),
        # nodes 38 Filter, 35 Project, 25 join, 17 Project, 20 TopN 10: in memory
        Step("top10", "top_k", rows=ref("wf"),
             keys=[("workFrom", "asc"), ("friendId", "asc"), ("organizationName", "desc")],
             k=10),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("top10"),
             names={"friendId": "personId", "friendFirstName": "personFirstName",
                    "friendLastName": "personLastName"}),
        Step("out", "project", rows=ref("labeled"), fields=IC11_FIELDS),
    ]


# Arango (p1). Three filtered traversals, each a different shape, and together they are what
# pinned down `traverse_filtered`'s two halves (vertex materialization vs edge-doc read):
#
#   [3]  knows 1..2 ANY, vertex filter friend._id != startPerson
#          E=10 expansions, T=528 steps, V=528 survive -> 2 x (10+528) + 2 x 528  = 2132
#   [6]  COLLECT f = friend                                                  346 groups
#   [7]  workAt OUT, EDGE filter workEdge.workFrom < $year. `company` is never referenced
#          in the filter, so the VERTEX is not materialized at all -- only the edge doc.
#          T=744 edge reads, V=215 survivors re-read                          =  959
#   [10] isLocatedIn OUT, vertex filter country.name == $countryName
#          the start `company` is materialized once per expansion   2 x 215    =  430
#          E=215, T=215, V=0                     2 x (215 + 215) + 0          =  860
#                                                                             --------
#                                                                                4381 = live
#
# Two things worth stating plainly about this cell.
#
# 1. **The [10] start materialization is measured, not derived.** `FOR co IN 1..1 OUTBOUND
#    company isLocatedIn RETURN 1` -- country unreferenced, no filter -- still costs 430
#    GETs, while IC9's structurally identical `FOR msg IN 1..1 INBOUND f hasCreator RETURN 1`
#    costs 0. The difference tracks whether the start vertex was already a materialized
#    document (`f` is a COLLECT group key; `company` is a previous traversal's output), but
#    that is a hypothesis. It is charged here as an explicit step so it is visible rather
#    than folded into an operator as if it were a general rule.
# 2. **The row check is vacuous at sf0.1** -- the $countryName filter leaves 0 rows at every
#    param, same as nebula IC11 and IC3. The KV counts carry this cell.
def ic11_arangodb_engine(args):
    p = person_vid(args["person"])
    year = int(args["year"])
    country = args["countryName"]
    return [
        # [3] TraversalNode knows IN,OUT 1..2, postFilter friend._id != start, no
        # vertexProjections (COLLECT groups on the document): the first traversal's open SEEK,
        # then traverse_filtered.
        Step("open", "traversal_open", items=[p], etype="knows"),
        # widening the prop list is FREE -- docs_per_step charges pk + doc however many
        # attributes are wanted -- so the friend's names come out of the read the filter
        # already paid for, which is why the engine's RETURN costs nothing extra
        Step("wkeep", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both",
             depth=2, props=["id", "firstName", "lastName"], label="Person",
             pred=ne(field("_vid"), p)),
        Step("frn", "rename", rows=ref("wkeep"),
             names={"id": "friendId", "firstName": "friendFirstName",
                    "lastName": "friendLastName"}),
        Step("frd", "dedup_rows", rows=ref("frn"), field="_vid"),
        Step("wv", "pluck", rows=ref("wkeep"), field="_vid"),
        Step("fr", "dedup", items=ref("wv")),                     # node [6] COLLECT
        # [7] TraversalNode workAt OUTBOUND f, edgeProjections workFrom, postFilter on the EDGE
        # (workEdge.workFrom < year): the edge doc per step + again per survivor; the filter
        # does not touch the vertex, so no vertex read here
        Step("wa", "traverse_filtered", srcs=ref("fr"), etype="workAt", dir="out", depth=1,
             edge_props=["workFrom"],
             pred=and_(notnull(get("workFrom")), lt(field("workFrom"), year))),
        Step("co", "pluck", rows=ref("wa"), field="_vid"),
        # [7] vertexProjections [_id,name]: each SURVIVING company's document (pk + doc), per
        # row -- the read note 1 above measures (430 GETs with `country` unreferenced)
        Step("cdoc", "docs_per_step", vids=ref("co"), label="Organisation",
             props=["id", "name"]),
        # [10] TraversalNode isLocatedIn OUTBOUND company (per row), vertexProjections [name],
        # postFilter country.name == @countryName: traverse_filtered
        Step("loc", "traverse_filtered", srcs=ref("co"), etype="isLocatedIn", dir="out",
             depth=1, props=["name"], label="Place",
             pred=eq(get("name"), country)),
        # [16] SORT, [17] LIMIT 10, [18] CalculationNode, [19] RETURN: in memory
        Step("okc", "pluck", rows=ref("loc"), field="_src"),
        Step("wk", "filter_in", rows=ref("wa"), field="_vid", allowed=ref("okc")),
        Step("cn", "rename", rows=ref("cdoc"), names={"name": "organizationName"}),
        Step("cnd", "dedup_rows", rows=ref("cn"), field="_vid"),
        Step("wc", "join", left=ref("wk"), right=ref("cnd"), left_on="_vid", right_on="_vid"),
        Step("wf", "join", left=ref("wc"), right=ref("frd"), left_on="_src", right_on="_vid"),
        Step("top10", "top_k", rows=ref("wf"),
             keys=[("workFrom", "asc"), ("friendId", "asc"), ("organizationName", "desc")],
             k=10),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("top10"),
             names={"friendId": "personId", "friendFirstName": "personFirstName",
                    "friendLastName": "personLastName"}),
        Step("out", "project", rows=ref("labeled"), fields=IC11_FIELDS),
    ]


# Dgraph, from dgraph/ic11/p1/plan.txt (sf0.1 p1: S = 1097, G = 95, N = 0).
# Blocks 0-7 build the friend set F (1..2 hops, as IC9), the country CTY and QUAL, the companies in CTY that some
# friend joined before the year (`workAt @facets(lt(workFrom, $workFromYear)) @filter(eq(type, "company") AND
# uid_in(isLocatedIn, uid(CTY)))`: 1 workAt posting per friend, the facet filter free in the posting (IS3's rule),
# 1 isLocatedIn posting per candidate company). Block 8 keeps the friends with a workAt edge into QUAL (1 workAt
# posting per friend), reads their three name fields, and re-reads each one's OWN workAt edges filtered by the
# facet again and by QUAL: that per-person filter is what keeps the year tied to the person. The nested block
# sorts each person's companies by name (sortWithoutIndex) and reads the company name + its (empty) workAt.
# The client ranks every row and keeps 10.
#
# Store note:
# **No `type` index.** `eq(type,"company")` and `eq(type,"country")` read ONE index
#     posting each on the engine (eqfilterpath=index). The store has no `type` index, so the
#     plan lands that one SEEK on the dgraph.type posting (Organisation / Place) instead:
#     same op count, different key, as ic3.py and the fqid anchor do. (Reading `type`
#     per candidate is a path the engine does not take at eqfilterpath=index.)

def ic11_dgraph_engine(args):
    p = person_vid(args["person"])
    year = int(args["year"])
    country = args["countryName"]
    return _anchor_person(p) + [
        # blocks 1-5: knows + ~knows, type(person) AND NOT uid(P); the same from l1, type AND
        # NOT uid(P, F1); F = uid(F1, F2) (identical shape to IC9)
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("l1p", "read_filter_by_label_index", vids=ref("l1"), label="Person"),
        Step("f1", "exclude", items=ref("l1p"), remove=ref("me")),
        Step("l2", "read_neighbors", srcs=ref("f1"), etype="knows", dir="both"),
        Step("l2p", "read_filter_by_label_index", vids=ref("l2"), label="Person"),
        Step("f2a", "exclude", items=ref("l2p"), remove=ref("me")),
        Step("f2", "exclude", items=ref("f2a"), remove=ref("f1")),
        Step("fu", "concat", a=ref("f1"), b=ref("f2")),
        Step("fr", "dedup", items=ref("fu")),
        # block 6: eq(name, $countryName) (1 index SEEK) @filter(eq(type, "country") -- 1 SEEK,
        # the Place posting as stand-in key (note 2) -- AND type(place) -- 1 SEEK)
        # (the two filters are parallel conjuncts over the same candidates)
        Step("cn", "read_index_eq", label="Place", prop="name", value=country),
        Step("cty1", "read_filter_by_label_index", vids=ref("cn"), label="Place"),
        Step("cty2", "read_filter_by_label_index", vids=ref("cn"), label="Place"),
        Step("cty", "intersect", items=ref("cty1"), other=ref("cty2")),
        # block 7: workAt per friend (1 SEEK each; the workFrom facet filter rides in the
        # posting, free) @filter(eq(type, "company") -- 1 SEEK, the Organisation posting as
        # stand-in key -- AND uid_in(isLocatedIn, CTY) -- 1 posting read per candidate)
        Step("wa", "read_neighbors", srcs=ref("fr"), etype="workAt", dir="out",
             edge_props=["workFrom"], pairs=True),
        Step("waok", "filter", rows=ref("wa"),
             pred=and_(notnull(get("workFrom")), lt(field("workFrom"), year))),
        Step("cov", "pluck", rows=ref("waok"), field="dst"),
        Step("comp", "read_filter_by_label_index", vids=ref("cov"), label="Organisation"),
        Step("cloc", "read_neighbors", srcs=ref("comp"), etype="isLocatedIn", dir="out",
             pairs=True),
        Step("clok", "filter_in", rows=ref("cloc"), field="dst", allowed=ref("cty")),
        Step("qual", "pluck", rows=ref("clok"), field="src"),
        # block 8: results(uid(F)) @filter(uid_in(workAt, QUAL)) -- 1 workAt posting read per
        # friend (p1: src=346 dest=22); no order, no first: every qualifying person
        Step("fwa", "read_neighbors", srcs=ref("fr"), etype="workAt", dir="out", pairs=True),
        Step("fwq", "filter_in", rows=ref("fwa"), field="dst", allowed=ref("qual")),
        Step("hit", "pluck", rows=ref("fwq"), field="src"),
        Step("topv", "dedup", items=ref("hit")),
        Step("pp", "read_vertex_props", vids=ref("topv"), props=["id", "firstName", "lastName"]),
        Step("ppr", "rename", rows=ref("pp"), names=PERSON_RENAME),
        # block 8: id, firstName, lastName per result; the nested `workAt @filter(uid(QUAL))
        # @facets(workFrom) (orderasc: name) { name, workFrom: workAt }` block -- the workAt
        # posting per result, the nested sort's name fetch, name per company, and the child
        # `workAt` predicate read on each company (1 SEEK, an empty posting)
        Step("owa", "read_neighbors", srcs=ref("topv"), etype="workAt", dir="out",
             edge_props=["workFrom"], pairs=True),
        # `@facets(lt(workFrom, $workFromYear))`: each person's OWN edges (the facet rides in the posting,
        # free); a person left with no company joins nothing below -- the client drops it too
        Step("owq0", "filter", rows=ref("owa"),
             pred=and_(notnull(get("workFrom")), lt(field("workFrom"), year))),
        Step("owq", "filter_in", rows=ref("owq0"), field="dst", allowed=ref("qual")),
        # `(orderasc: name)`: sortWithoutIndex per result person -- 1 SEEK per (person, company)
        Step("osort", "sort_without_index_per_src", rows=ref("owq"), prop="name"),
        Step("ocv", "pluck", rows=ref("owq"), field="dst"),
        Step("ocn", "read_vertex_props", vids=ref("ocv"), props=["name"]),
        Step("ocw", "read_neighbors", srcs=ref("ocv"), etype="workAt", dir="out"),
        Step("ocr", "rename", rows=ref("ocn"), names={"name": "organizationName"}),
        Step("ocd", "dedup_rows", rows=ref("ocr"), field="_vid"),
        Step("ow", "join", left=ref("owq"), right=ref("ocd"), left_on="dst", right_on="_vid"),
        Step("joined", "join", left=ref("ow"), right=ref("ppr"), left_on="src", right_on="_vid"),
        # the client (kvengines.ldbc.parsers.dgraph DgraphLDBCParser._parse_ic11): drop a person with no company, sort
        # (workFrom asc, personId asc, organizationName desc), keep 10 -- in memory
        Step("top10", "top_k", rows=ref("joined"),
             keys=[("workFrom", "asc"), ("personId", "asc"), ("organizationName", "desc")], k=10),
        Step("out", "project", rows=ref("top10"), fields=IC11_FIELDS),
    ]


SIM = {
    "ic11": ic11
}

LABEL_ONLY = {
    "ic11": ic11_labelonly
}

ENGINE = {
    "ic11": {"nebula": ic11_nebula_engine, "arangodb": ic11_arangodb_engine, "dgraph": ic11_dgraph_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic11": _KNOWS_HINTS}
