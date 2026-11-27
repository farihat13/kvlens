"""LDBC IC10 plans (sim + engine)."""

from datetime import datetime, timezone
from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import if_, field, or_, and_, eq, ge, lt, month_of, day_of, param, let, add
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import MESSAGE_LABELS, _anchor_person, person_vid, PERSON_PARAM, PERSON_LET


# ----------------------------------------------------------------------- IC10

def _bday_window():
    """Birthday in [month/21 .. (month+1)/22): the same UTC month/day test as the engines,
    authored as an expression AST over the `month` param (see planir/expr.py month_of/day_of).
    The next month is month%12+1; with no mod op in the grammar, express it as
    if_(month==12, 1, month+1) (identical over the valid 1..12 domain)."""
    m = param("month")
    m2 = if_(eq(m, 12), 1, add(m, 1))
    return or_(and_(eq(month_of(field("birthday")), m), ge(day_of(field("birthday")), 21)),
               and_(eq(month_of(field("birthday")), m2), lt(day_of(field("birthday")), 22)))


def _ic10_steps(args, fetch_detail, logical=False):
    """IC10: friend-of-friend recommendation. Candidates = persons at EXACTLY
    distance 2 with a birthday in the [month 21st .. next month 22nd) window.
    Score = (candidate's posts tagged with an interest of `person`) minus
    (posts not so tagged); top 10 by (score desc, personId asc) + gender/city.
    The birthday filter runs FIRST (cuts candidates ~12x) so the per-candidate
    post/tag scans stay small. Zero-post candidates score 0."""
    p = let("me_vid")
    steps = [
        Step("me",         "read_vertex_by_id", vid=p, label="Person"),
        Step("nb1",        "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("friends",    "dedup", items=ref("nb1")),
        Step("nb2",        "read_neighbors", srcs=ref("friends"), etype="knows", dir="both"),
        Step("nb2d",       "dedup", items=ref("nb2")),
        Step("seen",       "concat", a=ref("friends"), b=ref("me")),
        Step("fof",        "exclude", items=ref("nb2d"), remove=ref("seen")),
        Step("fof_props",  "read_vertex_props", vids=ref("fof"),
             props=(["birthday", "id"] if fetch_detail or logical
                    else ["birthday", "id", "firstName", "lastName", "gender"]),
             label="Person"),
        Step("cand",       "filter", rows=ref("fof_props"), pred=_bday_window()),
        Step("cand_v",     "pluck", rows=ref("cand"), field="_vid"),
        Step("interests",  "read_neighbors", srcs=ref("me"), etype="hasInterest", dir="out"),
        Step("my_tags",    "dedup", items=ref("interests")),
        Step("cmsgs",      "read_neighbors", srcs=ref("cand_v"), etype="hasCreator", dir="in", pairs=True),
        Step("cmsg_v",     "pluck", rows=ref("cmsgs"), field="dst"),
        Step("cposts",     "filter_by_label", vids=ref("cmsg_v"), label="Post"),
        Step("cmsg_r",     "rename", rows=ref("cmsgs"), names={"src": "cand", "dst": "_post"}),
        Step("cand_posts", "filter_in", rows=ref("cmsg_r"), field="_post", allowed=ref("cposts")),
        Step("post_tags",  "read_neighbors", srcs=ref("cposts"), etype="hasTag", dir="out", pairs=True),
        Step("post_flag",  "flag_in", rows=ref("post_tags"), field="dst", allowed=ref("my_tags"), into="_c"),
        # a post is "common" if ANY of its tags is an interest (bool sorts True first)
        Step("post_common","group_top", rows=ref("post_flag"), by="src", keys=[("_c", "desc")]),
        Step("tagged_v",   "pluck", rows=ref("post_common"), field="src"),
        Step("tagless",    "exclude", items=ref("cposts"), remove=ref("tagged_v")),   # tagless posts
        Step("tagless_ann","annotate", items=ref("tagless"), field="_c", value=False),
        Step("tagless_r",  "rename", rows=ref("tagless_ann"), names={"_vid": "src"}),
        Step("all_posts",  "concat", a=ref("post_common"), b=ref("tagless_r")),
        Step("post_score", "derive", rows=ref("all_posts"), into="_pt",
             fn=if_(field("_c"), 1, -1)),
        Step("post_scr",   "rename", rows=ref("post_score"), names={"src": "_post2"}),
        Step("cand_post_sc","join", left=ref("cand_posts"), right=ref("post_scr"), left_on="_post",
             right_on="_post2"),
        Step("score",      "group_sum", rows=ref("cand_post_sc"), by="cand", field="_pt"),
        Step("scored_v",   "pluck", rows=ref("score"), field="item"),
        Step("zero_cand",  "exclude", items=ref("cand_v"), remove=ref("scored_v")),
        Step("zero_ann",   "annotate", items=ref("zero_cand"), field="sum", value=0),
        Step("zero_r",     "rename", rows=ref("zero_ann"), names={"_vid": "item"}),
        Step("all_scores", "concat", a=ref("score"), b=ref("zero_r")),
        Step("scored",     "rename", rows=ref("all_scores"), names={"sum": "commonInterestScore"}),
        Step("cand_r",     "rename", rows=ref("cand"), names={"_vid": "_cand2"}),
        Step("scored_cand","join", left=ref("scored"), right=ref("cand_r"), left_on="item",
             right_on="_cand2"),
        Step("scored_named","rename", rows=ref("scored_cand"), names={"id": "personId"}),
        Step("top10",      "top_k", rows=ref("scored_named"),
             keys=[("commonInterestScore", "desc"), ("personId", "asc")], k=10),
        Step("top_v",      "pluck", rows=ref("top10"), field="item"),
    ]
    if logical:                                  # the optimizer's fetch choice point
        steps += [
            Step("profiled", "fetch_props", rows=ref("top10"), vids=ref("top_v"),
                 props=["firstName", "lastName", "gender"], label="Person",
                 read_name="detail", widen="fof_props"),
        ]
        merged = "profiled"
    elif fetch_detail:
        steps += [
            Step("detail",   "read_vertex_props", vids=ref("top_v"),
                 props=["firstName", "lastName", "gender"], label="Person"),
            Step("profiled", "zip_rows", left=ref("top10"), right=ref("detail")),
        ]
        merged = "profiled"
    else:
        merged = "top10"
    steps += [
        Step("city",       "read_neighbors", srcs=ref("top_v"), etype="isLocatedIn", dir="out"),
        Step("city_n",     "read_vertex_props", vids=ref("city"), props=["name"], label="Place"),
        Step("city_named", "rename", rows=ref("city_n"), names={"name": "cityName"}),
        Step("withcity",   "zip_rows", left=ref(merged), right=ref("city_named")),
        Step("labeled",    "rename", rows=ref("withcity"),
             names={"firstName": "personFirstName", "lastName": "personLastName"}),
        Step("out",        "project", rows=ref("labeled"),
             fields=["personId", "personFirstName", "personLastName",
                     "commonInterestScore", "gender", "cityName"]),
    ]
    return Plan(steps, params={**PERSON_PARAM, "month": {"type": "int"}}, let=PERSON_LET)


def ic10_logical(args):
    """The LOGICAL ic10 for the optimizer's search: one fetch choice point
    (detail-fetch the top 10 vs widen the fof birthday read); the cposts type
    filter stays the logical filter_by_label (caps-forced lowering, not a cost
    choice)."""
    return _ic10_steps(args, fetch_detail=True, logical=True)


# ---- the list-join twin (queries.TWIN_TRANSFORM)

def _ic10_twin(plan, edit):
    """`plan` (a Plan or a baked Step list) with `edit` applied to a fresh copy of its steps"""
    steps = [Step(s.out, s.op, **dict(s.args)) for s in (plan.steps if isinstance(plan, Plan) else plan)]
    steps = edit(steps)
    return Plan(steps, params=plan.params, let=plan.let, output=plan.output) if isinstance(plan, Plan) else steps


def _ic10_replace(steps, first, last, new):
    """`steps` with the run first..last (step names, inclusive) replaced by `new`"""
    names = [s.out for s in steps]
    return steps[:names.index(first)] + new + steps[names.index(last) + 1:]


def ic10_interest_lists(plan):
    """IC10's twin A (the plan-change rule): a candidate's post is "common" when it is in the hasTag IN list of one of
    the start person's interest tags (`tag_posts`: one list per interest tag), instead of reading every candidate
    post's hasTag OUT list (`post_tags`). Same answer. What an engine needs to choose: the interest tags' list lengths
    against the number of candidate posts."""
    def edit(steps):
        new = [Step("tag_posts", "read_neighbors", srcs=ref("my_tags"), etype="hasTag", dir="in", pairs=True),
               Step("tagged_v", "pluck", rows=ref("tag_posts"), field="dst"),
               Step("common_v", "intersect", items=ref("cposts"), other=ref("tagged_v")),
               Step("common_ann", "annotate", items=ref("common_v"), field="_c", value=True),
               Step("post_common", "rename", rows=ref("common_ann"), names={"_vid": "src"}),
               Step("tagless", "exclude", items=ref("cposts"), remove=ref("common_v"))]   # no interest tag
        return _ic10_replace(steps, "post_tags", "tagless", new)
    return _ic10_twin(plan, edit)


LOGICAL = {"ic10": ic10_logical}


def ic10(args):
    """Two-phase (dgraph-optimal): birthday+id per fof candidate (2 GETs),
    names+gender for the top 10 only."""
    return _ic10_steps(args, fetch_detail=True)


def ic10_onephase(args):
    """One-phase (nebula/arango override): the birthday read returns the whole
    row, so the profile comes with it."""
    return _ic10_steps(args, fetch_detail=False)


SIM = {
    "ic10": {"default": ic10, "nebula": ic10_onephase, "arangodb": ic10_onephase}
}


# ── IC10 engine-faithful plans ─────────────────────────────────────────────────────────────────
# Each builder transcribes the engine's captured sf0.3 plan (ldbc/<db>/ic10/p1/plan.txt). Every KV step cites the
# plan node it models; in-memory steps cost nothing and are grouped.
#
# IC10 = friend recommendation: persons at distance EXACTLY 2 born in [month/21 .. next month/22),
# scored by commonInterestScore = common − (total − common) posts, top 10 by (score desc, id asc).
# The Nebula and ArangoDB queries apply the birthday window; our Dgraph query does not (see
# ic10_dgraph_engine).

IC10_FIELDS = ["personId", "personFirstName", "personLastName",
               "commonInterestScore", "gender", "cityName"]


def _bday_pred(month):
    """The birthday window [month/21 .. nextMonth/22), month bound eagerly (engine plans have no
    param() binding). Same month_of/day_of test the SIM's _bday_window builds, with literal ints."""
    m = int(month)
    m2 = 1 if m == 12 else m + 1
    return or_(and_(eq(month_of(field("birthday")), m), ge(day_of(field("birthday")), 21)),
               and_(eq(month_of(field("birthday")), m2), lt(day_of(field("birthday")), 22)))


def _ic10_score(cands_ref, posts_ref, common_ref):
    """In memory: commonInterestScore per candidate of `cands_ref` = common − (total − common), from
    `posts_ref` rows {src: candidate, dst: post} and `common_ref` (the posts carrying a tag of
    interest). A candidate with no post scores 0. Ends in `score`: rows {_vid, commonInterestScore}."""
    return [
        Step("sc_flag", "flag_in", rows=ref(posts_ref), field="dst", allowed=ref(common_ref),
             into="_c"),
        Step("sc_pt", "derive", rows=ref("sc_flag"), into="_pt", fn=if_(field("_c"), 1, -1)),
        Step("sc_sum", "group_sum", rows=ref("sc_pt"), by="src", field="_pt"),
        Step("sc_hit", "pluck", rows=ref("sc_sum"), field="item"),
        Step("sc_none", "exclude", items=ref(cands_ref), remove=ref("sc_hit")),
        Step("sc_zero", "annotate", items=ref("sc_none"), field="sum", value=0),
        Step("sc_zr", "rename", rows=ref("sc_zero"), names={"_vid": "item"}),
        Step("sc_all", "concat", a=ref("sc_sum"), b=ref("sc_zr")),
        Step("score", "rename", rows=ref("sc_all"),
             names={"item": "_vid", "sum": "commonInterestScore"}),
    ]


def _ic10_with_city(rows_ref, pairs_ref, names_ref):
    """In memory: attach cityName to `rows_ref` (keyed by _vid) from `pairs_ref` {src: person,
    dst: city} and `names_ref` (one {name} row per pair, parallel). Ends in `withcity`."""
    return [
        Step("wc_zip", "zip_rows", left=ref(pairs_ref), right=ref(names_ref)),
        Step("wc_ren", "rename", rows=ref("wc_zip"), names={"src": "_p", "name": "cityName"}),
        Step("wc_p", "project", rows=ref("wc_ren"), fields=["_p", "cityName"]),
        Step("withcity", "join", left=ref(rows_ref), right=ref("wc_p"), left_on="_vid",
             right_on="_p"),
    ]


def ic10_dgraph_engine(args):
    """IC10 dgraph, from dgraph/ic10/p1/plan.txt (blocks 0-9).

    The answer is a deviation of our query text (engine/data/queries/dgraph/interactive-complex-10.graphql):
    it applies NO birthday window ("the client is expected to"), and the client (kvengines.ldbc.parsers)
    (DgraphLDBCParser._parse_ic10) does not apply one either: it sorts block 9's 20 rows by (score desc,
    personId asc) and keeps 10. So the answer is the top 10 of EVERY friend-of-friend, where LDBC ranks
    only those born in the window: the rows match live, not the optimal (LDBC) answer. Block 8's `ord`
    is a float64, and at |score| >= 8 one float step is larger than one id step, so two foafs can tie
    on it; the tie order is Dgraph's sort, not ours. A tie across the 20th place would change which 20
    are read (the city-name GETs could move by 1); the client re-sorts by the exact (score, id), so
    the answer moves only if the tie reaches the top 10. Not seen at the 6 seen params.

    The cost is block 7: `cpc as count(~hasCreator @filter(type(post) AND uid_in(hasTag, uid(IT))))`.
    Both conjuncts of the AND see every message of every foaf, so the engine reads the hasTag posting
    of each one, comments too (the IC6 shape). Block 6 reads each foaf's ~hasCreator posting once for
    pc and block 7 once more; block 8 reads `id` of every foaf; block 9 sorts on `ord` in memory and
    reads its 20 winners' five predicates, isLocatedIn and the city name.

    sf0.3 vs live: S, G and N exact at all 6 seen params, and the answers match live. p1: S = 2 anchor
    + 2 (_l1) + 1 + 20 (_l2) + 1 + 2 (IT) + (1279 + 1) block 6 + (1279 + 1 + 422,851) block 7 + 20
    isLocatedIn = 425,459; G = 1279 ids + 5 x 20 + 20 names = 1,399; N = 0."""
    p = person_vid(args["person"])
    return _anchor_person(p) + [                       # block 0: eq(id) + type(person)
        # block 1: _l1(func: uid(P)) { a as knows  b as ~knows } -- 1 SEEK per direction
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("l1d", "dedup", items=ref("l1")),                                 # uid(a, b)
        # block 2: F1 as uid(a, b) @filter(type(person) AND NOT uid(P)) -- 1 type-index SEEK
        Step("l1p", "read_filter_by_label_index", vids=ref("l1d"), label="Person"),
        Step("F1", "exclude", items=ref("l1p"), remove=ref("me")),
        # block 3: _l2(func: uid(a, b)) { c as knows  d as ~knows } -- 1 SEEK per (source, direction)
        Step("l2", "read_neighbors", srcs=ref("l1d"), etype="knows", dir="both"),
        Step("l2d", "dedup", items=ref("l2")),                                 # uid(c, d)
        # block 4: F as uid(c, d) @filter(type(person) AND NOT uid(P, F1)) -- 1 type-index SEEK
        Step("l2p", "read_filter_by_label_index", vids=ref("l2d"), label="Person"),
        Step("l2x", "exclude", items=ref("l2p"), remove=ref("me")),
        Step("F", "exclude", items=ref("l2x"), remove=ref("F1")),
        # block 5: IT as hasInterest @filter(type(tag)), from P -- 1 SEEK + 1 type-index SEEK
        Step("it0", "read_neighbors", srcs=ref("me"), etype="hasInterest", dir="out"),
        Step("IT", "read_filter_by_label_index", vids=ref("it0"), label="Tag"),
        # block 6: pc as count(~hasCreator @filter(type(post))), per foaf -- 1 SEEK per foaf, and
        # 1 type-index SEEK for the filter over every message
        Step("m6", "read_neighbors", srcs=ref("F"), etype="hasCreator", dir="in", pairs=True),
        Step("m6v", "pluck", rows=ref("m6"), field="dst"),
        Step("p6", "read_filter_by_label_index", vids=ref("m6v"), label="Post"),
        Step("posts", "filter_in", rows=ref("m6"), field="dst", allowed=ref("p6")),
        # block 7: cpc as count(~hasCreator @filter(type(post) AND uid_in(hasTag, uid(IT)))), per
        # foaf -- the ~hasCreator posting read again (1 SEEK per foaf). Both conjuncts of the AND see
        # every message (the plan: uid_in src = every message): 1 type-index SEEK, and the hasTag
        # posting of every message, comments too (1 SEEK each)
        Step("m7", "read_neighbors", srcs=ref("F"), etype="hasCreator", dir="in"),
        Step("p7", "read_filter_by_label_index", vids=ref("m7"), label="Post"),
        Step("ht", "read_neighbors", srcs=ref("m7"), etype="hasTag", dir="out", pairs=True),
        Step("htIT", "filter_in", rows=ref("ht"), field="dst", allowed=ref("IT")),
        Step("withIT", "pluck", rows=ref("htIT"), field="src"),
        Step("common", "intersect", items=ref("p7"), other=ref("withIT")),
        # block 8: idv as id, per foaf -- 1 GET each
        Step("idv", "read_vertex_props", vids=ref("F"), props=["id"]),
        # block 8: uncommon = pc - cpc, score = cpc - uncommon, ord = score + (1e15 - idv) / 1e15
        # -- math, in memory
    ] + _ic10_score("F", "posts", "common") + [
        Step("scored", "join", left=ref("score"), right=ref("idv"), left_on="_vid"),
        Step("ordv", "derive", rows=ref("scored"), into="ord",
             fn=E.add(E.field("commonInterestScore"), E.div(E.sub(10 ** 15, E.field("id")), 10 ** 15))),
        # block 9: recs(func: uid(F), orderdesc: val(ord), first: 20) -- a value-variable sort, in
        # memory (query/query.go sortAndPaginateUsingVar)
        Step("top20", "top_k", rows=ref("ordv"), keys=[("ord", "desc")], k=20),
        Step("top_v", "pluck", rows=ref("top20"), field="_vid"),
        # block 9: personId: id, firstName, lastName, gender, birthday -- 1 GET each per winner
        Step("detail", "read_vertex_props", vids=ref("top_v"),
             props=["id", "firstName", "lastName", "gender", "birthday"]),
        Step("profiled", "join", left=ref("top20"), right=ref("detail"), left_on="_vid"),
        # block 9: isLocatedIn { name } -- 1 SEEK per winner, 1 GET per distinct city
        Step("city", "read_neighbors", srcs=ref("top_v"), etype="isLocatedIn", dir="out",
             pairs=True),
        Step("city_v", "pluck", rows=ref("city"), field="dst"),
        Step("city_n", "read_vertex_props", vids=ref("city_v"), props=["name"]),
    ] + _ic10_with_city("profiled", "city", "city_n") + [
        # the client (kvengines.ldbc.parsers.dgraph DgraphLDBCParser._parse_ic10): sort by (score desc, personId asc),
        # keep 10 -- no birthday filter; in memory
        Step("named", "rename", rows=ref("withcity"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        Step("top10", "top_k", rows=ref("named"),
             keys=[("commonInterestScore", "desc"), ("personId", "asc")], k=10),
        Step("out", "project", rows=ref("top10"), fields=IC10_FIELDS),
    ]


def ic10_nebula_engine(args):
    """IC10 Nebula, from nebula/ic10/p1/plan.txt.

    Three Traverse nodes list all 8 tags as vertexProps, so each reads 8 GETs per distinct SOURCE: node
    19 (knows, from every foaf end), node 29 (hasCreator, from the birthday candidates) and node 34
    (hasTag, from every post); node 54 AppendVertices reads all 8 tags of every message the candidates
    wrote before its filter keeps the Posts. The direct-friend test `OPTIONAL MATCH (friend)-[:KNOWS]-
    (root)` is expanded before root is matched: node 19 walks every foaf's knows edges and node 20 reads
    all 8 tags of every distinct neighbour (IC7's isNew shape). The `_tag`-only AppendVertices (nodes 51,
    55) cost nothing (is1.py, convention 3).

    sf0.3 vs live: S, G and N exact at all 6 seen params; the answers match the optimal plan and live.
    p1: S = 1 + 22 (knows 2..2) + 1286 (isLocatedIn) + 2572 (node 19) + 116 (node 29) + 9821 (node 34)
    = 13,818; G = 1 root probe + 1286 foaf rows + 816 cities + 8 x (1286 + 3163 + 116 + 36,732 + 9821)
    = 411,047; N = 24 + 10 + 1767 + 1286 + 52,249 + 36,732 + 9302 = 101,370."""
    p = person_vid(args["person"])
    month = int(args["month"])
    return [
        Step("me", "read_vertex_by_id", vid=p),                    # nodes 3/1/2: the vid IS the key
        # node 50 Traverse hasInterest OUT_EDGE from the root, `first step filter: Person._tag`, no
        # vertexProps: 1 GET on the root; 1 SEEK + 1 NEXT per edge
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("rt", "read_neighbors", srcs=ref("me"), etype="hasInterest", dir="out"),
        # node 51 AppendVertices Tag, props `_tag` only: no storage call (is1.py, convention 3);
        # a node marker, output unused
        Step("rtag", "read_vertex_props", vids=ref("rt"), label="Tag", props=["id"]),
        # nodes 6/7 Project, Aggregate collect(DISTINCT id(rt)): in memory
        Step("my_tags", "dedup", items=ref("rt")),
        # node 9 Traverse knows BOTH 2..2 from the root, dedup 1, no vertexProps: step 1 from the
        # root, step 2 from its distinct ends -- 1 SEEK per (source, direction) + 1 NEXT per edge
        Step("e1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("L1", "dedup", items=ref("e1")),
        Step("e2", "read_neighbors", srcs=ref("L1"), etype="knows", dir="both", pairs=True),
        # a path does not reuse its edge: the step back to the root is dropped (one knows edge per
        # pair) -- in memory; one row per path, the ends deduped for the next node (dedup 1)
        Step("paths", "filter", rows=ref("e2"), pred=E.ne(E.field("dst"), p)),
        Step("ends0", "pluck", rows=ref("paths"), field="dst"),
        Step("ends", "dedup", items=ref("ends0")),
        # node 52 Traverse isLocatedIn OUT_EDGE from each distinct path end: vertexProps Person
        # [firstName, lastName, gender, birthday] + `first step filter: Person._tag` -> one GET reads
        # the row (is1.py, convention 2); 1 SEEK + 1 NEXT
        Step("fprops", "tag_probe", vids=ref("ends"), label="Person",
             props=["id", "firstName", "lastName", "gender", "birthday"]),
        Step("fcity", "read_neighbors", srcs=ref("ends"), etype="isLocatedIn", dir="out",
             pairs=True),
        # node 59 Filter id(friend) != root: in memory (no path ends at the root)
        # node 60 AppendVertices Place [name], dedup 1: 1 GET per distinct city
        Step("fcity_v", "pluck", rows=ref("fcity"), field="dst"),
        Step("city_n", "read_vertex_props", vids=ref("fcity_v"), label="Place", props=["name"]),
        # nodes 58/49/56 joins, 17 Dedup (WITH DISTINCT friend, city): in memory
        # node 19 Traverse knows BOTH 1..1 from each distinct friend, dedup 1, vertexProps = all 8
        # tags: 8 GETs per friend; 2 SEEK per friend + 1 NEXT per edge (edgeProps creationDate ride in
        # the edge's value)
        Step("fsrc", "all_tag_props", vids=ref("ends")),
        Step("fk", "read_neighbors", srcs=ref("ends"), etype="knows", dir="both", pairs=True),
        # node 20 AppendVertices, all 8 tags of every distinct knows-neighbour (dedup 1): the
        # OPTIONAL MATCH (friend)-[:KNOWS]-(root) is expanded before root is matched (IC7's isNew)
        Step("fk_v", "pluck", rows=ref("fk"), field="dst"),
        Step("fk_mat", "all_tag_props", vids=ref("fk_v")),
        # nodes 21 PathBuild, 43 HashLeftJoin on (root, friend), 24 Filter `p IS NULL`: in memory
        Step("adj", "filter", rows=ref("fk"), pred=E.eq(E.field("dst"), p)),
        Step("adj_v", "pluck", rows=ref("adj"), field="src"),
        Step("fof", "exclude", items=ref("ends"), remove=ref("adj_v")),
        # nodes 25-27: the birthday window, on node 52's row -- in memory
        Step("fof_r", "filter_in", rows=ref("fprops"), field="_vid", allowed=ref("fof")),
        Step("cand", "filter", rows=ref("fof_r"), pred=_bday_pred(month)),
        Step("cand_v", "pluck", rows=ref("cand"), field="_vid"),
        # node 29 Traverse hasCreator IN_EDGE from each candidate, dedup 1, vertexProps = all 8 tags:
        # 8 GETs per candidate; 1 SEEK + 1 NEXT per message
        Step("csrc", "all_tag_props", vids=ref("cand_v")),
        Step("cm", "read_neighbors", srcs=ref("cand_v"), etype="hasCreator", dir="in", pairs=True),
        # node 54 AppendVertices, all 8 tags of every distinct message (dedup 1); its filter
        # `Post._tag IS NOT EMPTY` keeps the Posts (in memory)
        Step("cm_v", "pluck", rows=ref("cm"), field="dst"),
        Step("cm_mat", "all_tag_props", vids=ref("cm_v")),
        Step("cposts_r", "filter", rows=ref("cm_mat"), pred=E.eq(E.get("_tag"), "Post")),
        Step("cposts", "pluck", rows=ref("cposts_r"), field="_vid"),
        Step("posts", "filter_in", rows=ref("cm"), field="dst", allowed=ref("cposts")),
        # node 34 Traverse hasTag OUT_EDGE from each post, dedup 1, vertexProps = all 8 tags: 8 GETs
        # per post; 1 SEEK + 1 NEXT per tag edge
        Step("psrc", "all_tag_props", vids=ref("cposts")),
        Step("pt", "read_neighbors", srcs=ref("cposts"), etype="hasTag", dir="out", pairs=True),
        # node 55 AppendVertices Tag, props `_tag` only: no storage call; output unused
        Step("pt_v", "pluck", rows=ref("pt"), field="dst"),
        Step("ptag", "read_vertex_props", vids=ref("pt_v"), label="Tag", props=["id"]),
        # nodes 32/37 HashLeftJoins, 36/38 Projects, 39 Aggregate count(DISTINCT post) and
        # count(DISTINCT post with id(t) IN interestTagIds) per friend, 40 Project score: in memory
        Step("pt_my", "filter_in", rows=ref("pt"), field="dst", allowed=ref("my_tags")),
        Step("common", "pluck", rows=ref("pt_my"), field="src"),
    ] + _ic10_score("cand_v", "posts", "common") + [
        Step("scored", "join", left=ref("score"), right=ref("cand"), left_on="_vid"),
    ] + _ic10_with_city("scored", "fcity", "city_n") + [
        Step("named", "rename", rows=ref("withcity"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        # node 44 TopN 10 (commonInterestScore desc, personIdInt asc): in memory
        Step("top10", "top_k", rows=ref("named"),
             keys=[("commonInterestScore", "desc"), ("personId", "asc")], k=10),
        Step("out", "project", rows=ref("top10"), fields=IC10_FIELDS),
    ]


def ic10_arangodb_engine(args):
    """IC10 arango, from arangodb/ic10/p1/plan.txt.

    [14] walks knows 2..2 per PATH and fetches the fof document at every emitted step (the FILTERs and
    the COLLECT need it), before the birthday filter. Then per candidate, [25] runs a filtered
    traversal (the start, every message, every Post again: traverse_filtered), and per post [29] reads
    every hasTag edge but fetches tags only until the first one in myTags: its `LIMIT 1` [32] takes
    [29]'s rows directly (traverse_filtered_limit). The directFriends and myTags subqueries fetch every
    friend and tag document for its _id.

    sf0.3 vs live: S, G and N exact at all 6 seen params; the answers match the optimal plan and live."""
    p = person_vid(args["person"])
    month = int(args["month"])
    return [
        # [2] LET person = DOCUMENT(...): pk + doc
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        Step("mev", "pluck", rows=ref("me"), field="_vid"),
        # [4] TraversalNode knows IN,OUT 1..1 from person, vertexProjections [_id] (directFriends):
        # the first traversal (+1 open SEEK); 2 index SEEKs + 1 NEXT per edge; each friend fetched
        # (pk + doc) for its _id
        Step("open", "traversal_open", items=ref("mev"), etype="knows"),
        Step("df", "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),
        Step("df_d", "docs_per_step", vids=ref("df"), label="Person", props=["id"]),
        # [9] TraversalNode hasInterest OUTBOUND person, vertexProjections [_id] (myTags): 1 SEEK +
        # 1 NEXT per edge; each tag fetched (pk + doc)
        Step("mt", "read_neighbors", srcs=ref("mev"), etype="hasInterest", dir="out"),
        Step("mt_d", "docs_per_step", vids=ref("mt"), label="Tag", props=["id"]),
        # [14] TraversalNode knows IN,OUT 2..2 from person, no vertexProjections, no postFilter: one
        # row per path (uniqueEdges: path), 2 index SEEKs per expanded vertex + 1 NEXT per edge;
        # only the depth-2 steps are emitted (a depth-1 step comes from the root -- in memory)
        Step("walk", "traverse_paths", srcs=ref("mev"), etype="knows", dir="both", depth=2,
             pairs=True),
        Step("d2", "filter", rows=ref("walk"), pred=E.ne(E.field("src"), p)),
        Step("d2_v", "pluck", rows=ref("d2"), field="dst"),
        # [14] each emitted fof fetched (pk + doc), per path: the FILTERs read its _id and birthday,
        # the COLLECT keeps the whole document
        Step("fof_d", "docs_per_step", vids=ref("d2_v"), label="Person",
             props=["id", "firstName", "lastName", "gender", "birthday"]),
        # [15]/[16] FILTER fof._id != person._id AND fof._id NOT IN directFriends, [19]-[22] the
        # birthday FILTER, [23] COLLECT f = fof, [60] SORT f: in memory
        Step("f0", "filter", rows=ref("fof_d"), pred=E.ne(E.field("_vid"), p)),
        Step("f0_df", "flag_in", rows=ref("f0"), field="_vid", allowed=ref("df"), into="_direct"),
        Step("f1", "filter", rows=ref("f0_df"), pred=E.not_(E.field("_direct"))),
        Step("f2", "filter", rows=ref("f1"), pred=_bday_pred(month)),
        Step("f", "dedup_rows", rows=ref("f2"), field="_vid"),
        Step("f_v", "pluck", rows=ref("f"), field="_vid"),
        # [25] TraversalNode hasCreator INBOUND f (edge(_to)), no vertexProjections, postFilter
        # IS_SAME_COLLECTION('Post', post) -- one run per f ([70] subquery): the start, every
        # message and every Post again (traverse_filtered)
        Step("ps", "traverse_filtered", srcs=ref("f_v"), etype="hasCreator", dir="in", depth=1,
             pred=E.eq(E.get("_label"), "Post"), props=["id"], label=MESSAGE_LABELS),
        # [29] TraversalNode hasTag OUTBOUND post (edge(_from)), vertexProjections [_id], postFilter
        # tag._id IN myTags, and [32] LIMIT 1 takes its rows directly (no FilterNode between) -- one
        # run per post ([72] subquery): every edge read, tags fetched until the first match
        # (traverse_filtered_limit, default DFS order)
        Step("ps_v", "pluck", rows=ref("ps"), field="_vid"),
        Step("hit", "traverse_filtered_limit", srcs=ref("ps_v"), etype="hasTag", dir="out",
             limit=1, order="dfs", props=["id"], label="Tag", pred=E.lit(True),
             allowed=ref("mt")),
        # [36]/[37] hasCommonTag, [71] postInfo, [40]/[42]/[48]/[49] postCount, commonPostCount,
        # score: in memory
        Step("common", "pluck", rows=ref("hit"), field="_src"),
        Step("posts", "rename", rows=ref("ps"), names={"_src": "src", "_vid": "dst"}),
    ] + _ic10_score("f_v", "posts", "common") + [
        Step("scored", "join", left=ref("score"), right=ref("f"), left_on="_vid"),
        Step("named", "rename", rows=ref("scored"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        # [56]/[57] SORT score DESC, TO_NUMBER(f._key) ASC LIMIT 10: in memory
        Step("top10", "top_k", rows=ref("named"),
             keys=[("commonInterestScore", "desc"), ("personId", "asc")], k=10),
        Step("top_v", "pluck", rows=ref("top10"), field="_vid"),
        # [51] TraversalNode isLocatedIn OUTBOUND f (edge(_from)), no vertexProjections, [63] LIMIT 1
        # -- one run per winner ([66] subquery): 1 SEEK + 1 NEXT, the city fetched (pk + doc)
        Step("city", "read_neighbors", srcs=ref("top_v"), etype="isLocatedIn", dir="out",
             pairs=True),
        Step("city_v", "pluck", rows=ref("city"), field="dst"),
        Step("city_n", "docs_per_step", vids=ref("city_v"), label="Place", props=["name"]),
    ] + _ic10_with_city("top10", "city", "city_n") + [
        Step("out", "project", rows=ref("withcity"), fields=IC10_FIELDS),       # [58]/[59] RETURN
    ]


ENGINE = {
    "ic10": {"dgraph": ic10_dgraph_engine, "nebula": ic10_nebula_engine,
             "arangodb": ic10_arangodb_engine}
}


def ic10_fof_from_person_run(plan):
    """IC10's PERSON-RUN twin (the sequential label scan, read_vertices_by_label with `keep`): the same plan with the
    friend-of-friend props read (`fof_props`: one point read per candidate) replaced by ONE scan of the Person label's
    own key run kept to the fof set: one SEEK, then one NEXT per key of the run, the keep test in memory and free. The
    rows come in vid order, one row per Person in `keep`; IC10 filters, dedups and sorts later, so the answer is the
    same. Valid where the label's records are one key run (`label_rows_one_run`); a candidate picked by cost (the plan
    rule). An engine could choose it from |Person| and an estimate of |fof|, never from the answer. A Plan or a baked
    Step list; fresh Step objects, arg values shared (as ic5_list_join)."""

    def twin(steps):
        out = []
        for s in steps:
            if s.out == "fof_props":
                assert s.op == "read_vertex_props" and s.args.get("label") == "Person", s
                out.append(Step("fof_props", "read_vertices_by_label", label="Person", props=s.args["props"], keep=ref("fof")))
            else:
                out.append(Step(s.out, s.op, **dict(s.args)))
        return out

    if isinstance(plan, Plan):
        return Plan(twin(plan.steps), params=plan.params, let=plan.let)
    return twin(plan)


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic10": _KNOWS_HINTS}
