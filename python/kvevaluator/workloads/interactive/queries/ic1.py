"""LDBC IC1 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import param, let
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import _anchor_person, _scan_lookup, person_vid, PERSON_PARAM, PERSON_LET


# ------------------------------------------------------------------------ IC1

IC1_DETAIL = ["birthday", "creationDate", "gender", "browserUsed", "locationIP",
              "email", "language"]


IC1_FIELDS = ["personId", "personLastName", "distanceFromPerson"] + IC1_DETAIL + \
             ["cityName", "universities", "companies"]


def _org_block(kind, srcs_ref, etype, eprop, name_field, place_field):
    """IC1's universities/companies sub-plan: person -[etype {eprop}]-> org
    -[isLocatedIn]-> place; returns steps ending in '<kind>_full' rows
    {src: person, <name_field>, <eprop>, <place_field>}."""
    k = kind
    return [
        Step(k + "_edges",  "read_neighbors", srcs=srcs_ref, etype=etype, dir="out",
             edge_props=[eprop], pairs=True),
        Step(k + "_orgv",   "pluck", rows=ref(k + "_edges"), field="dst"),
        Step(k + "_org",    "dedup", items=ref(k + "_orgv")),
        Step(k + "_orgnm",  "read_vertex_props", vids=ref(k + "_org"), props=["name"],
             label="Organisation"),
        Step(k + "_orgnm2", "rename", rows=ref(k + "_orgnm"), names={"name": name_field}),
        Step(k + "_orgj",   "join", left=ref(k + "_edges"), right=ref(k + "_orgnm2"),
             left_on="dst", right_on="_vid"),
        Step(k + "_ploc",   "read_neighbors", srcs=ref(k + "_org"), etype="isLocatedIn",
             dir="out", pairs=True),
        Step(k + "_plv",    "pluck", rows=ref(k + "_ploc"), field="dst"),
        Step(k + "_pl",     "dedup", items=ref(k + "_plv")),
        Step(k + "_plnm",   "read_vertex_props", vids=ref(k + "_pl"), props=["name"], label="Place"),
        Step(k + "_plnm2",  "rename", rows=ref(k + "_plnm"), names={"name": place_field}),
        Step(k + "_plj",    "join", left=ref(k + "_ploc"), right=ref(k + "_plnm2"),
             left_on="dst", right_on="_vid"),
        Step(k + "_placed", "rename", rows=ref(k + "_plj"), names={"src": "_org", "dst": "_pl"}),
        Step(k + "_full",   "join", left=ref(k + "_orgj"), right=ref(k + "_placed"),
             left_on="dst", right_on="_org"),
    ]


def _ic1_steps(args, rank_props, fetch_detail, label_only=False, logical=False):
    """IC1: persons named `firstName` within 3 undirected KNOWS hops of `person`,
    top 20 by (distance asc, lastName asc, id asc) + full profile, city,
    universities (name, classYear, city), companies (name, workFrom, country).

    Entry: read_index_eq(firstName) INTERSECT BFS level sets. The rejected candidate -- BFS then a firstName read
    per reached person -- pays |BFS3| property reads; the index entry is 1 SEEK (+t NEXTs) with a free intersect.
    BFS level sets d1/d2/d3 give min-distance for free (each level excludes everything reached earlier).
    """
    p = let("me_vid")
    # BFS out to 3 undirected KNOWS hops; each level excludes everyone already seen,
    # so lvl1/lvl2/lvl3 carry min-distance for free
    steps = [
        Step("me",    "read_vertex_by_id", vid=p, label="Person"),
        Step("nb1",   "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("nb1d",  "dedup", items=ref("nb1")),
        Step("lvl1",  "exclude", items=ref("nb1d"), remove=ref("me")),
        Step("nb2",   "read_neighbors", srcs=ref("lvl1"), etype="knows", dir="both"),
        Step("nb2d",  "dedup", items=ref("nb2")),
        Step("seen1", "concat", a=ref("lvl1"), b=ref("me")),
        Step("lvl2",  "exclude", items=ref("nb2d"), remove=ref("seen1")),
        Step("nb3",   "read_neighbors", srcs=ref("lvl2"), etype="knows", dir="both"),
        Step("nb3d",  "dedup", items=ref("nb3")),
        Step("seen2", "concat", a=ref("seen1"), b=ref("lvl2")),
        Step("lvl3",  "exclude", items=ref("nb3d"), remove=ref("seen2")),
    ]
    if logical:                                  # the optimizer's entry choice point
        steps += [Step("fmatch", "entry_eq", label="Person", prop="firstName",
                       value=param("firstName"))]
    else:
        steps += (_scan_lookup("fmatch", "Person", "firstName", param("firstName")) if label_only
                  else [Step("fmatch", "read_index_eq", label="Person", prop="firstName", value=param("firstName"))])
    steps += [
        Step("hit1",  "intersect", items=ref("lvl1"), other=ref("fmatch")),
        Step("hit2",  "intersect", items=ref("lvl2"), other=ref("fmatch")),
        Step("hit3",  "intersect", items=ref("lvl3"), other=ref("fmatch")),
        Step("dist1", "annotate", items=ref("hit1"), field="distanceFromPerson", value=1),
        Step("dist2", "annotate", items=ref("hit2"), field="distanceFromPerson", value=2),
        Step("dist3", "annotate", items=ref("hit3"), field="distanceFromPerson", value=3),
        Step("hits12","concat", a=ref("dist1"), b=ref("dist2")),
        Step("cand",  "concat", a=ref("hits12"), b=ref("dist3")),
        Step("cand_v","pluck", rows=ref("cand"), field="_vid"),
        Step("rankp", "read_vertex_props", vids=ref("cand_v"), props=rank_props, label="Person"),
        Step("ranked","zip_rows", left=ref("cand"), right=ref("rankp")),
        Step("top20", "top_k", rows=ref("ranked"),
             keys=[("distanceFromPerson", "asc"), ("lastName", "asc"), ("id", "asc")],
             k=20),
        Step("top_v", "pluck", rows=ref("top20"), field="_vid"),
    ]
    if logical:                                  # the optimizer's fetch choice point
        steps += [
            Step("profiled", "fetch_props", rows=ref("top20"), vids=ref("top_v"),
                 props=IC1_DETAIL, label="Person", read_name="detail", widen="rankp"),
        ]
        persons = "profiled"
    elif fetch_detail:
        steps += [
            Step("detail",   "read_vertex_props", vids=ref("top_v"), props=IC1_DETAIL, label="Person"),
            Step("profiled", "zip_rows", left=ref("top20"), right=ref("detail")),
        ]
        persons = "profiled"
    else:
        persons = "top20"
    steps += [
        Step("city",       "read_neighbors", srcs=ref("top_v"), etype="isLocatedIn", dir="out"),
        Step("city_n",     "read_vertex_props", vids=ref("city"), props=["name"], label="Place"),
        # the city row's own _vid must not survive the zip: it would overwrite the person's,
        # and the university/company nests below key on the person's _vid
        Step("city_named", "rename", rows=ref("city_n"), names={"name": "cityName", "_vid": "_city"}),
        Step("withcity",   "zip_rows", left=ref(persons), right=ref("city_named")),
    ]
    steps += _org_block("u", ref("top_v"), "studyAt", "classYear",
                        "universityName", "universityCity")
    steps += _org_block("w", ref("top_v"), "workAt", "workFrom",
                        "companyName", "companyCountry")
    steps += [
        Step("withuni", "nest", rows=ref("withcity"), children=ref("u_full"), on="_vid",
             child_on="src", into="universities",
             fields=["universityName", "classYear", "universityCity"]),
        Step("withco",  "nest", rows=ref("withuni"), children=ref("w_full"), on="_vid",
             child_on="src", into="companies",
             fields=["companyName", "workFrom", "companyCountry"]),
        Step("labeled", "rename", rows=ref("withco"),
             names={"id": "personId", "lastName": "personLastName"}),
        Step("out", "project", rows=ref("labeled"), fields=IC1_FIELDS),
    ]
    return Plan(steps, params={**PERSON_PARAM, "firstName": {}}, let=PERSON_LET)


def ic1_logical(args):
    """The LOGICAL ic1 for the optimizer's search: TWO choice points -- the
    firstName lookup (`entry_eq`: index vs label scan) and the profile fetch
    (`fetch_props`: detail-fetch the top 20 vs widen the rank read). The 2x2
    of rules reproduces exactly the four hand builders below."""
    return _ic1_steps(args, rank_props=["lastName", "id"], fetch_detail=True,
                      logical=True)


# ---- the list-join twin (queries.TWIN_TRANSFORM)

def _ic1_twin(plan, edit):
    """`plan` (a Plan or a baked Step list) with `edit` applied to a fresh copy of its steps"""
    steps = [Step(s.out, s.op, **dict(s.args)) for s in (plan.steps if isinstance(plan, Plan) else plan)]
    steps = edit(steps)
    return Plan(steps, params=plan.params, let=plan.let, output=plan.output) if isinstance(plan, Plan) else steps


def _ic1_replace(steps, first, last, new):
    """`steps` with the run first..last (step names, inclusive) replaced by `new`"""
    names = [s.out for s in steps]
    return steps[:names.index(first)] + new + steps[names.index(last) + 1:]


def ic1_expand_name_matches(plan):
    """IC1's twin (the plan-change rule): level 3 from the other side. The base reads both knows lists of EVERY level-2
    person (`nb3`) to build level 3, though only persons with the given first name matter. Here the name index's
    matches (`fmatch`, read anyway) minus those already at distance 1 or 2 and the start person (`far_match`) have
    their knows lists read once, and a match is at distance 3 when one of its neighbours is in level 2. Same answer,
    same distances. What an engine needs to choose: the first-name index's hit count against the level-2 size, both
    known before the choice.
    """
    def edit(steps):
        by = {s.out: s for s in steps}
        new = [by["seen2"], by["fmatch"], by["hit1"], by["hit2"],
               Step("far_match", "exclude", items=ref("fmatch"), remove=ref("seen2")),
               Step("match_nb", "read_neighbors", srcs=ref("far_match"), etype="knows", dir="both", pairs=True),
               Step("match_l2", "filter_in", rows=ref("match_nb"), field="dst", allowed=ref("lvl2")),
               Step("match_l2v", "pluck", rows=ref("match_l2"), field="src"),
               Step("hit3", "dedup", items=ref("match_l2v"))]
        return _ic1_replace(steps, "nb3", "hit3", new)
    return _ic1_twin(plan, edit)


LOGICAL = {"ic1": ic1_logical}


def ic1(args):
    """Two-phase (dgraph-optimal): rank matches on (lastName, id) = 2 GETs each,
    full profile (5 GETs + 2 list-pred SEEKs for email/language) for the top 20
    only."""
    return _ic1_steps(args, rank_props=["lastName", "id"], fetch_detail=True)


def ic1_onephase(args):
    """One-phase (nebula/arango override): the rank read returns the whole row,
    so the profile comes with it; a detail re-fetch would re-read 20 rows."""
    return _ic1_steps(args, rank_props=["lastName", "id"] + IC1_DETAIL,
                      fetch_detail=False)


def ic1_labelonly(args):
    """IC1 with only the label index: the firstName lookup becomes read_vertices_by_label
    (all Persons) + a firstName read per person + equality filter, instead of the
    read_index_eq point SEEK. The BFS intersect + ranking are unchanged."""
    return _ic1_steps(args, rank_props=["lastName", "id"], fetch_detail=True,
                      label_only=True)


def ic1_labelonly_onephase(args):
    """IC1 label-only, nebula/arango (one-phase rank read; see ic1_onephase)."""
    return _ic1_steps(args, rank_props=["lastName", "id"] + IC1_DETAIL,
                      fetch_detail=False, label_only=True)


# ── IC1 engine-faithful plans ──────────────────────────────────────────────────────────────────
# Each builder transcribes the engine's captured sf0.3 plan
# (ldbc/<db>/ic1/p1/plan.txt). Every KV step cites the plan
# node it models; in-memory steps cost nothing and are grouped.

_IC1_PERSON = ["id", "lastName", "firstName", "birthday", "creationDate", "gender",
               "browserUsed", "locationIP", "email", "language"]


def ic1_dgraph_engine(args, eq_path="index"):
    """IC1 dgraph, from dgraph/ic1/p1/plan.txt (blocks 0-12).

    Blocks 10-12 `friends<d>(func: uid(F<d>), orderasc: lastName, first: 20)`: lastName has no
    index (dgraph/data/ldbcTypes.schema:116), so detsort runs sortWithoutIndex -- ONE lastName
    read (1 SEEK) per matched person of the level, an in-memory sort, keep 20 (sort_without_index).
    Only then do the block's children read their predicates, for those 20.

    The engine returns up to 20 rows per level; the client (kvengines.ldbc.parsers.dgraph DgraphLDBCParser._parse_ic1)
    concatenates the three blocks, sorts by (distance, lastName, id) and keeps 20. That merge is
    the in-memory tail here, so `out` is the LDBC answer in the optimal plan's row shape.

    `eq_path` is Dgraph's equality-filter path (its instrumented build's /eqfilterpath knob): "index" (the
    engine-fidelity captures) or "per_vertex" (read firstName of every candidate, then test it; Figure 2's
    variant). Only blocks 2/4/6 differ.

    sf0.3 vs live: S, G and N exact at all 6 params. Answer: equal to the live engine's at all 6
    (every column, with universities/companies compared as sets of (name, year, place)).
    Ties: which 20 survive a level depends on the engine's (unstable) order inside a lastName tie
    at position 20. Only p19 cuts a level (29 matches); there the sim keeps the engine's 20."""
    p = person_vid(args["person"])
    fn = args["firstName"]
    steps = _anchor_person(p)                          # block 0: eq(id) + type(person)
    prev, prior = "me", []                             # prior = F1..F(d-1), for NOT uid(...)
    for lvl in (1, 2, 3):
        cand, matched = f"cand{lvl}", f"F{lvl}"
        steps += [
            # blocks 1/3/5: knows + ~knows from the previous level (all of it, not just matches)
            Step(f"l{lvl}", "read_neighbors", srcs=ref(prev), etype="knows", dir="both"),
            Step(cand, "dedup", items=ref(f"l{lvl}")),
            # blocks 2/4/6: type(person) -- 1 type-index SEEK; NOT uid(P, F..) in memory
            Step(f"typ{lvl}", "read_filter_by_label_index", vids=ref(cand), label="Person"),
        ]
        if eq_path == "index":                         # eq(firstName): 1 index SEEK
            steps.append(Step(f"ix{lvl}", "read_index_eq", label="Person", prop="firstName", value=fn))
        else:                                          # per_vertex: firstName of EVERY candidate (the filter's
            steps += [                                 # src, before type() and NOT drop any), 1 GET each
                Step(f"fn{lvl}", "read_vertex_props", vids=ref(cand), props=["firstName"]),
                Step(f"fm{lvl}", "filter", rows=ref(f"fn{lvl}"), pred=E.eq(E.field("firstName"), E.lit(fn))),
                Step(f"ix{lvl}", "pluck", rows=ref(f"fm{lvl}"), field="_vid"),
            ]
        steps += [
            Step(f"{cand}n", "intersect", items=ref(f"typ{lvl}"), other=ref(f"ix{lvl}")),
            Step(f"{cand}x0", "exclude", items=ref(f"{cand}n"), remove=[p]),
        ]
        src = f"{cand}x0"
        for i, earlier in enumerate(prior):            # one exclude per prior level (ref() is
            steps.append(Step(f"{cand}x{i+1}", "exclude", items=ref(src), remove=ref(earlier)))
            src = f"{cand}x{i+1}"                      # resolved only at the top level of args)
        t = f"top{lvl}"
        steps += [
            Step(matched, "dedup", items=ref(src)),
            # blocks 10/11/12 root `orderasc: lastName, first: 20`: sortWithoutIndex -- lastName
            # for EVERY match of the level (1 SEEK each), sort in memory, keep 20
            Step(f"sort{lvl}", "sort_without_index", vids=ref(matched), prop="lastName", k=20),
            Step(f"topv{lvl}", "pluck", rows=ref(f"sort{lvl}"), field="_vid"),
            # the block's scalar + list predicates (id .. language), for the 20 survivors only
            Step(f"prop{lvl}", "read_vertex_props", vids=ref(f"topv{lvl}"), props=_IC1_PERSON),
            Step(f"{t}d", "derive", rows=ref(f"prop{lvl}"), into="distanceFromPerson",
                 fn=E.lit(lvl)),                # friendDistance: val(d<lvl>), in memory
            # isLocatedIn @filter(type(place)) { name }: posting SEEK + 1 type SEEK + name
            Step(f"city{lvl}", "read_neighbors", srcs=ref(f"topv{lvl}"), etype="isLocatedIn",
                 dir="out", pairs=True),
            Step(f"cityv{lvl}", "pluck", rows=ref(f"city{lvl}"), field="dst"),
            Step(f"cityt{lvl}", "read_filter_by_label_index", vids=ref(f"cityv{lvl}"),
                 label="Place"),
            Step(f"cityn{lvl}", "read_vertex_props", vids=ref(f"cityt{lvl}"), props=["name"]),
            # in memory: person -> city name
            Step(f"cityf{lvl}", "filter_in", rows=ref(f"city{lvl}"), field="dst",
                 allowed=ref(f"cityt{lvl}")),
            Step(f"citynm{lvl}", "rename", rows=ref(f"cityn{lvl}"), names={"name": "cityName"}),
            Step(f"cityj{lvl}", "join", left=ref(f"cityf{lvl}"), right=ref(f"citynm{lvl}"),
                 left_on="dst"),
            Step(f"cityp{lvl}", "project", rows=ref(f"cityj{lvl}"), fields=["src", "cityName"]),
            Step(f"{t}c", "join", left=ref(f"{t}d"), right=ref(f"cityp{lvl}"), left_on="_vid",
                 right_on="src"),
        ]
        rows = f"{t}c"
        for kind, etype, eprop, into, names in (
                ("uni", "studyAt", "classYear", "universities",
                 ("universityName", "classYear", "universityCity")),
                ("comp", "workAt", "workFrom", "companies",
                 ("companyName", "workFrom", "companyCountry"))):
            k = f"{kind}{lvl}"
            steps += [
                # studyAt @facets(classYear) / workAt @facets(workFrom): posting SEEK (facet
                # rides in it), then the org's name
                Step(k, "read_neighbors", srcs=ref(f"topv{lvl}"), etype=etype, dir="out",
                     edge_props=[eprop], pairs=True),
                Step(f"{k}v", "pluck", rows=ref(k), field="dst"),
                Step(f"{k}n", "read_vertex_props", vids=ref(f"{k}v"), props=["name"]),
                # nested isLocatedIn @filter(type(place)) { name }
                Step(f"{k}loc", "read_neighbors", srcs=ref(f"{k}v"), etype="isLocatedIn",
                     dir="out", pairs=True),
                Step(f"{k}locv", "pluck", rows=ref(f"{k}loc"), field="dst"),
                Step(f"{k}loct", "read_filter_by_label_index", vids=ref(f"{k}locv"),
                     label="Place"),
                Step(f"{k}locn", "read_vertex_props", vids=ref(f"{k}loct"), props=["name"]),
                # in memory: (person, org name, facet, place name) for the nest below
                Step(f"{k}lf", "filter_in", rows=ref(f"{k}loc"), field="dst",
                     allowed=ref(f"{k}loct")),
                Step(f"{k}ln", "rename", rows=ref(f"{k}locn"), names={"name": names[2]}),
                Step(f"{k}lj", "join", left=ref(f"{k}lf"), right=ref(f"{k}ln"), left_on="dst"),
                Step(f"{k}lp", "project", rows=ref(f"{k}lj"), fields=["src", names[2]]),
                Step(f"{k}on", "rename", rows=ref(f"{k}n"), names={"name": names[0]}),
                Step(f"{k}j1", "join", left=ref(k), right=ref(f"{k}on"), left_on="dst"),
                Step(f"{k}j2", "join", left=ref(f"{k}j1"), right=ref(f"{k}lp"), left_on="dst",
                     right_on="src"),
                Step(f"{t}{kind}", "nest", rows=ref(rows), children=ref(f"{k}j2"), on="_vid",
                     child_on="src", into=into, fields=list(names)),
            ]
            rows = f"{t}{kind}"
        steps.append(Step(t, "project", rows=ref(rows),
                          fields=["_vid", "distanceFromPerson"] + _IC1_PERSON +
                                 ["cityName", "universities", "companies"]))
        prior = prior + [matched]
        prev = cand
    # the client merge (DgraphLDBCParser._parse_ic1): concat the three blocks, sort by (distance,
    # lastName, id), keep 20 -- in memory
    steps += [
        Step("m12", "concat", a=ref("top1"), b=ref("top2")),
        Step("m123", "concat", a=ref("m12"), b=ref("top3")),
        Step("top20", "top_k", rows=ref("m123"),
             keys=[("distanceFromPerson", "asc"), ("lastName", "asc"), ("id", "asc")], k=20),
        Step("labeled", "rename", rows=ref("top20"),
             names={"id": "personId", "lastName": "personLastName"}),
        Step("out", "project", rows=ref("labeled"), fields=IC1_FIELDS),
    ]
    return steps


def ic1_nebula_engine(args):
    """IC1 nebula, from nebula/ic1/p1/plan.txt.

    Node 34 (knows BOTH 1..3, dedup: 1) lists all 8 tags in vertexProps, so every step reads the
    8 tag rows of each vertex it expands FROM; node 41 then reads all 8 tags of every distinct
    path END. Nodes 10/15/22 each read the 8 tag rows of every matched friend again, as the
    source of their own Traverse.

    Approximation: the frontiers are deduped neighbour sets. The trail rule (no edge twice in a
    path) is applied where it is exact -- p is never a 2-step end -- but not at step 3, where a
    vertex reachable only by re-using an edge would still be counted.

    sf0.3 vs live: over on all three at every param -- S +2..+10, G +8..+40, N +73..+790 (<= 1.8%).
    The engine's order, not the plan (graph/executor/query/TraverseExecutor.cpp, expand() ~350): the
    next step's sources are the destinations not in adjList_ when their edge is read, and a source
    of the current step enters adjList_ only when the response iteration has passed it. So a
    first-hop friend with a friend among the friends is expanded again at step 3 unless its own rows
    came before every edge pointing to it. This twin expands all of them again (an upper bound);
    the engine leaves out 1 to 5 per param, each 2 SEEKs, 8 GETs and a NEXT per knows edge: at every
    param of sf0.3 and sf0.1 exactly one set of such friends, no two of them friends, has the gap's
    size and knows degrees. Leaving all of them out undershoots by more (sf0.3 p1: -8 S, -40 G,
    -988 N): the engine expands most of them again."""
    p = person_vid(args["person"])
    fn = args["firstName"]
    steps = [
        Step("me", "read_vertex_by_id", vid=p),
        # node 34 Traverse knows BOTH, steps 1..3, dedup: 1, vertexProps = 8 tags: per step,
        # 8 GETs per source vertex (the start's Person row answers the first-step filter),
        # 1 SEEK per direction + 1 NEXT per edge
        Step("src0", "all_tag_props", vids=ref("me")),
        Step("e1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("L1", "dedup", items=ref("e1")),
        Step("src1", "all_tag_props", vids=ref("L1")),
        Step("e2", "read_neighbors", srcs=ref("L1"), etype="knows", dir="both"),
        Step("L2d", "dedup", items=ref("e2")),
        Step("L2", "exclude", items=ref("L2d"), remove=ref("me")),     # trail rule, see above
        Step("src2", "all_tag_props", vids=ref("L2")),
        Step("e3", "read_neighbors", srcs=ref("L2"), etype="knows", dir="both"),
        Step("L3", "dedup", items=ref("e3")),
        # min(length(p)) per end (node 8), in memory: the first level an end appears in
        Step("d1", "annotate", items=ref("L1"), field="distanceFromPerson", value=1),
        Step("d2", "annotate", items=ref("L2"), field="distanceFromPerson", value=2),
        Step("d3", "annotate", items=ref("L3"), field="distanceFromPerson", value=3),
        Step("d12", "concat", a=ref("d1"), b=ref("d2")),
        Step("d123", "concat", a=ref("d12"), b=ref("d3")),
        Step("ends", "dedup_rows", rows=ref("d123"), field="_vid"),
        Step("endv", "pluck", rows=ref("ends"), field="_vid"),
        # node 41 AppendVertices: all 8 tags of every distinct path end (dedup: 1)
        Step("endrows", "all_tag_props", vids=ref("endv")),
        # node 41 vertex_filter firstName, node 42 id(friend) != p, node 8 Aggregate: in memory
        Step("withd", "join", left=ref("endrows"), right=ref("ends"), left_on="_vid",
             right_on="_vid"),
        Step("friends", "filter", rows=ref("withd"),
             pred=E.and_(E.eq(E.get("firstName"), fn), E.ne(E.field("_vid"), p))),
        Step("fv", "pluck", rows=ref("friends"), field="_vid"),
        # node 10 Traverse isLocatedIn OUT from each friend: 8-tag source read + SEEK + NEXT
        Step("f10", "all_tag_props", vids=ref("fv")),
        Step("city", "read_neighbors", srcs=ref("fv"), etype="isLocatedIn", dir="out"),
        # node 35 AppendVertices Place [name], dedup: 1
        Step("cityn", "read_vertex_props", vids=ref("city"), label="Place", props=["name"]),
        # node 15 Traverse studyAt OUT (edgeProps classYear): 8-tag source read + SEEK + NEXT
        Step("f15", "all_tag_props", vids=ref("fv")),
        # (pairs: output shape only -- keeps which friend each university row belongs to)
        Step("uni", "read_neighbors", srcs=ref("fv"), etype="studyAt", dir="out",
             edge_props=["classYear"], pairs=True),
        Step("univ", "pluck", rows=ref("uni"), field="dst"),
        # node 36 Traverse isLocatedIn OUT from each uni: Organisation [name] fused with the
        # first-step Organisation._tag filter (1 GET) + SEEK + NEXT
        Step("unin", "tag_probe", vids=ref("univ"), label="Organisation", props=["name"]),
        Step("ucity", "read_neighbors", srcs=ref("univ"), etype="isLocatedIn", dir="out"),
        # node 37 AppendVertices Place [name]
        Step("ucityn", "read_vertex_props", vids=ref("ucity"), label="Place", props=["name"]),
        # node 22 Traverse workAt OUT (edgeProps workFrom): 8-tag source read + SEEK + NEXT
        Step("f22", "all_tag_props", vids=ref("fv")),
        # (pairs: output shape only -- keeps which friend each company row belongs to)
        Step("comp", "read_neighbors", srcs=ref("fv"), etype="workAt", dir="out",
             edge_props=["workFrom"], pairs=True),
        Step("compv", "pluck", rows=ref("comp"), field="dst"),
        # node 38 Traverse isLocatedIn OUT from each company: Organisation [name] + first-step
        # filter in one GET, + SEEK + NEXT
        Step("compn", "tag_probe", vids=ref("compv"), label="Organisation", props=["name"]),
        Step("ccty", "read_neighbors", srcs=ref("compv"), etype="isLocatedIn", dir="out"),
        # node 39 AppendVertices Place [name]
        Step("cctyn", "read_vertex_props", vids=ref("ccty"), label="Place", props=["name"]),
        # node 28 Project, node 31 TopN 20 by (distance, lastName, id): in memory
        Step("top20", "top_k", rows=ref("friends"),
             keys=[("distanceFromPerson", "asc"), ("lastName", "asc"), ("id", "asc")], k=20),
        # LDBC output columns (in memory). Each friend has one city and each organisation one
        # place, and a traverse emits one row per distinct source in order, so those line up
        # by position; the org edges carry their friend (src).
        Step("citynames", "pluck", rows=ref("cityn"), field="name"),
        Step("fcity", "attach", rows=ref("friends"), values=ref("citynames"), into="cityName"),
        Step("fcityp", "project", rows=ref("fcity"), fields=["_vid", "cityName"]),
        Step("withcity", "join", left=ref("top20"), right=ref("fcityp"), left_on="_vid"),
    ]
    rows = "withcity"
    for org, place, edges, into, fields in (
            ("unin", "ucityn", "uni", "universities",
             ["universityName", "classYear", "universityCity"]),
            ("compn", "cctyn", "comp", "companies",
             ["companyName", "workFrom", "companyCountry"])):
        steps += [
            Step(f"{into}_place", "pluck", rows=ref(place), field="name"),
            Step(f"{into}_org", "attach", rows=ref(org), values=ref(f"{into}_place"),
                 into=fields[2]),
            Step(f"{into}_orgn", "rename", rows=ref(f"{into}_org"), names={"name": fields[0]}),
            Step(f"{into}_full", "join", left=ref(edges), right=ref(f"{into}_orgn"), left_on="dst"),
            Step(f"with_{into}", "nest", rows=ref(rows), children=ref(f"{into}_full"), on="_vid",
                 child_on="src", into=into, fields=fields),
        ]
        rows = f"with_{into}"
    steps += [
        Step("labeled", "rename", rows=ref(rows),
             names={"id": "personId", "lastName": "personLastName"}),
        Step("out", "project", rows=ref("labeled"), fields=IC1_FIELDS),
    ]
    return steps


def ic1_arangodb_engine(args):
    """IC1 arango, from arangodb/ic1/p1/plan.txt.

    startPerson is a string, so no DOCUMENT read. Node [3] carries a postFilter and no
    vertexProjections, so it is a filtered traversal (traverse_filtered): every expansion source
    and every path step is read to test the filter, and each survivor is read again. The sort
    and LIMIT 20 run BEFORE the city/university/company subqueries, so those run for the
    winners only.

    Rows: traverse_filtered tags each hit with its path length (`_depth`, free: the walk is
    level by level), so the winners are ranked by (min distance, lastName, id) like the AQL.

    Answers equal the live engine's at all 6 sf0.3 params (`validate.py --live`).

    [3] also emits the PATH (pathOutVariable p, kept by `COLLECT ... INTO paths = p`; the only
    captured arango plan that does), so each surviving path's vertices and edges are read to
    build it: traverse_filtered's `path_out` (3d + 2 GETs for a path of length d)."""
    p = person_vid(args["person"])
    fn = args["firstName"]
    return [
        # [3] TraversalNode knows IN,OUT 1..3, postFilter friend._id != start AND
        # friend.firstName == @firstName, no vertexProjections; first traversal: +1 open SEEK
        Step("open", "traversal_open", items=[p], etype="knows"),
        Step("hits", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both", depth=3,
             props=_IC1_PERSON, label="Person", path_out=True,       # pathOutVariable p
             pred=E.and_(E.eq(E.get("firstName"), fn), E.ne(E.field("_vid"), p))),
        # [8] COLLECT f = friend INTO paths, [62]/[63] dist = MIN(LENGTH(pp.edges)) -- each hit
        # carries its path length (_depth), so the per-friend minimum is in memory;
        # [44]/[45] SORT dist, lastName, TO_NUMBER(_key) + LIMIT 20: in memory
        Step("uniq", "group_top", rows=ref("hits"), by="_vid", keys=[("_depth", "asc")]),
        Step("win", "top_k", rows=ref("uniq"),
             keys=[("_depth", "asc"), ("lastName", "asc"), ("id", "asc")], k=20),
        Step("winv", "pluck", rows=ref("win"), field="_vid"),
        # [16] TraversalNode isLocatedIn OUTBOUND f, no vertexProjections (RETURN c), [51] LIMIT 1:
        # 1 SEEK + 1 NEXT + the city document (pk + doc)
        Step("city", "read_neighbors", srcs=ref("winv"), etype="isLocatedIn", dir="out",
             pairs=True),
        Step("cityv", "pluck", rows=ref("city"), field="dst"),
        Step("cityd", "docs_per_step", vids=ref("cityv"), label="Place", props=["name"]),
        # [21] TraversalNode studyAt OUTBOUND f, vertexProjections [_id,name], edgeProjections
        # classYear (not in the edge index -> 1 edge-doc GET per edge); the org document per step
        Step("uni", "read_neighbors", srcs=ref("winv"), etype="studyAt", dir="out",
             edge_props=["classYear"], pairs=True),
        Step("univ", "pluck", rows=ref("uni"), field="dst"),
        Step("unid", "docs_per_step", vids=ref("univ"), label="Organisation", props=["name"]),
        # [23] TraversalNode isLocatedIn OUTBOUND org, vertexProjections [name], [50] LIMIT 1:
        # once per university row
        Step("ucity", "traverse_paths", srcs=ref("univ"), etype="isLocatedIn", dir="out", depth=1),
        Step("ucityd", "docs_per_step", vids=ref("ucity"), label="Place", props=["name"]),
        # [32] TraversalNode workAt OUTBOUND f, vertexProjections [_id,name], edgeProjections
        # workFrom -> edge doc per edge; the org document per step
        Step("comp", "read_neighbors", srcs=ref("winv"), etype="workAt", dir="out",
             edge_props=["workFrom"], pairs=True),
        Step("compv", "pluck", rows=ref("comp"), field="dst"),
        Step("compd", "docs_per_step", vids=ref("compv"), label="Organisation", props=["name"]),
        # [34] TraversalNode isLocatedIn OUTBOUND org, vertexProjections [name], [49] LIMIT 1
        Step("ccty", "traverse_paths", srcs=ref("compv"), etype="isLocatedIn", dir="out", depth=1),
        Step("cctyd", "docs_per_step", vids=ref("ccty"), label="Place", props=["name"]),
        # [46]/[47] RETURN: assemble in memory. Each subquery row lines up with the step list it
        # came from (docs_per_step keeps order, no dedup), and every Organisation isLocatedIn
        # exactly one Place, so the per-row lists zip position by position.
        Step("cityn", "rename", rows=ref("cityd"), names={"name": "cityName"}),
        Step("cityz", "zip_rows", left=ref("city"), right=ref("cityn")),
        Step("cityp", "project", rows=ref("cityz"), fields=["src", "cityName"]),
        Step("wc", "join", left=ref("win"), right=ref("cityp"), left_on="_vid", right_on="src"),
        Step("unin", "rename", rows=ref("unid"), names={"name": "universityName"}),
        Step("unic", "rename", rows=ref("ucityd"), names={"name": "universityCity"}),
        Step("uniz", "zip_rows", left=ref("uni"), right=ref("unin")),
        Step("uniz2", "zip_rows", left=ref("uniz"), right=ref("unic")),
        Step("wu", "nest", rows=ref("wc"), children=ref("uniz2"), on="_vid", child_on="src",
             into="universities", fields=["universityName", "classYear", "universityCity"]),
        Step("compn", "rename", rows=ref("compd"), names={"name": "companyName"}),
        Step("compc", "rename", rows=ref("cctyd"), names={"name": "companyCountry"}),
        Step("compz", "zip_rows", left=ref("comp"), right=ref("compn")),
        Step("compz2", "zip_rows", left=ref("compz"), right=ref("compc")),
        Step("wo", "nest", rows=ref("wu"), children=ref("compz2"), on="_vid", child_on="src",
             into="companies", fields=["companyName", "workFrom", "companyCountry"]),
        Step("labeled", "rename", rows=ref("wo"),
             names={"id": "personId", "lastName": "personLastName",
                    "_depth": "distanceFromPerson"}),
        Step("out", "project", rows=ref("labeled"), fields=IC1_FIELDS),
    ]


SIM = {
    "ic1": {"default": ic1, "nebula": ic1_onephase, "arangodb": ic1_onephase}
}

LABEL_ONLY = {
    "ic1": {"default": ic1_labelonly, "nebula": ic1_labelonly_onephase, "arangodb": ic1_labelonly_onephase}
}

ENGINE = {
    "ic1": {"dgraph": ic1_dgraph_engine, "nebula": ic1_nebula_engine, "arangodb": ic1_arangodb_engine}
}

# Dgraph's IC1 plan per equality-filter path. ENGINE's entry (the engine-fidelity check's) is the index path; per_vertex is
# The complex-kv-reads figure's, run only by experiments/engine_fidelity/time_queries.py.
DGRAPH_ENGINE_BY_EQ_PATH = {"index": ic1_dgraph_engine,
                            "per_vertex": lambda args: ic1_dgraph_engine(args, eq_path="per_vertex")}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic1": _KNOWS_HINTS}
