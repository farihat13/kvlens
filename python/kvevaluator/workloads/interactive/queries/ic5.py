"""LDBC IC5 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import gt, tuple_, field, param, let
from kvevaluator.workloads.interactive._shared import _friend_set, _anchor_person, person_vid, PERSON_PARAM, PERSON_LET
from kvevaluator.plans.ir import authoring as E


# ------------------------------------------------------------------------ IC5

def _ic5_steps(args, fetch_title):
    """IC5: forums the friends+fof joined after minDate, ranked by how many
    posts those members made IN that forum -> top 20 by (postCount desc,
    forumId asc). Membership pairs come from the hasMember reverse read
    (joinDate is a covered edge prop); posts are enumerated forum-side
    (containerOf out over candidate forums only — the friend-messages side
    would need a type check per message). Forums with 0 qualifying posts
    still rank."""
    p = let("me_vid")
    steps = _friend_set(p, 2) + [
        Step("memb",       "read_neighbors", srcs=ref("f"), etype="hasMember", dir="in",
             edge_props=["joinDate"], pairs=True),
        Step("memb_new",   "filter", rows=ref("memb"), pred=gt(field("joinDate"), param("minDate"))),
        Step("memb_pf",    "derive", rows=ref("memb_new"), into="_pf",
             fn=tuple_(field("src"), field("dst"))),              # (member, forum)
        Step("forumv",     "pluck", rows=ref("memb_new"), field="dst"),
        Step("forums",     "dedup", items=ref("forumv")),
        Step("fposts",     "read_neighbors", srcs=ref("forums"), etype="containerOf", dir="out", pairs=True),
        Step("postv",      "pluck", rows=ref("fposts"), field="dst"),
        Step("post_auth",  "read_neighbors", srcs=ref("postv"), etype="hasCreator", dir="out", pairs=True),
        Step("post_authr", "rename", rows=ref("post_auth"), names={"src": "_post", "dst": "_creator"}),
        Step("fpostr",     "rename", rows=ref("fposts"), names={"src": "forum", "dst": "_post2"}),
        Step("post_forum", "join", left=ref("post_authr"), right=ref("fpostr"), left_on="_post",
             right_on="_post2"),
        Step("post_pf",    "derive", rows=ref("post_forum"), into="_pf",
             fn=tuple_(field("_creator"), field("forum"))),
        Step("memb_pfv",   "pluck", rows=ref("memb_pf"), field="_pf"),
        # a post counts only if its author actually joined that forum (matched member/forum pair)
        Step("own_posts",  "filter_in", rows=ref("post_pf"), field="_pf", allowed=ref("memb_pfv")),
        Step("own_forum",  "pluck", rows=ref("own_posts"), field="forum"),
        Step("forum_count","group_count", items=ref("own_forum")),
        Step("counted_ids","pluck", rows=ref("forum_count"), field="item"),
        # forums with 0 qualifying posts still rank, at count 0
        Step("zero_forums","exclude", items=ref("forums"), remove=ref("counted_ids")),
        Step("zero_ann",   "annotate", items=ref("zero_forums"), field="count", value=0),
        Step("zero_rows",  "rename", rows=ref("zero_ann"), names={"_vid": "item"}),
        Step("allf",       "concat", a=ref("forum_count"), b=ref("zero_rows")),
        Step("allf_v",     "pluck", rows=ref("allf"), field="item"),
        Step("forum_id",   "read_vertex_props", vids=ref("allf_v"),
             props=(["id"] if fetch_title else ["id", "title"]), label="Forum"),
        Step("forum_rows", "zip_rows", left=ref("allf"), right=ref("forum_id")),
        Step("top20",      "top_k", rows=ref("forum_rows"), keys=[("count", "desc"), ("id", "asc")], k=20),
    ]
    if fetch_title:
        steps += [
            Step("top_v",  "pluck", rows=ref("top20"), field="_vid"),
            Step("titles", "read_vertex_props", vids=ref("top_v"), props=["title"], label="Forum"),
            Step("titled", "zip_rows", left=ref("top20"), right=ref("titles")),
        ]
        final = "titled"
    else:
        final = "top20"
    steps += [
        Step("labeled", "rename", rows=ref(final),
             names={"id": "forumId", "title": "forumTitle", "count": "postCount"}),
        Step("out", "project", rows=ref("labeled"), fields=["forumId", "forumTitle", "postCount"]),
    ]
    return Plan(steps, params={**PERSON_PARAM, "minDate": {"type": "int"}}, let=PERSON_LET)


def ic5(args):
    """Two-phase for ALL formats (no override): the id rank read is 1 GET per
    forum for dgraph and FREE for nebula/arango (key-derived ids), so ranking
    first and fetching titles for the top 20 only is optimal everywhere."""
    return _ic5_steps(args, fetch_title=True)


def ic5_list_join(plan):
    """IC5's LIST-JOIN twin (the plan-change rule): the same plan with the per-post creator read (`post_auth`: hasCreator
    OUT once per forum post) replaced by ONE hasCreator IN list per distinct member, joined with the forums' posts on
    the post id in memory (`join_multi`, an inner join that also tests each pair's (creator, forum) against the
    members' pairs as it makes it). Every other step is the base's, so the answer is the same. The member-side form
    (containerOf IN per post) is NOT a twin: no capability names the far-end label on the row. A Plan or a baked Step
    list; fresh Step objects, arg values shared (as kvevaluator.plans.ir.lowering.with_index_filter).
    """
    from kvevaluator.plans.plan import Plan

    def twin(steps):
        out = []
        for s in steps:
            if s.out == "post_auth":
                out.append(Step("mposts", "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in", pairs=True))
                out.append(Step("mpostr", "rename", rows=ref("mposts"), names={"src": "_creator", "dst": "_post"}))
            elif s.out == "post_authr":
                continue
            elif s.out in ("post_forum", "post_pf"):
                continue
            elif s.out == "own_posts":   # the join tests each pair's (creator, forum) as it makes it: own_posts' filter
                out.append(Step("own_posts", "join_multi", left=ref("fpostr"), right=ref("mpostr"), left_on="_post2",
                                right_on="_post", where_key=tuple_(field("_creator"), field("forum")),
                                where_in=ref("memb_pfv")))
            else:
                out.append(Step(s.out, s.op, **dict(s.args)))
        return out

    if isinstance(plan, Plan):
        return Plan(twin(plan.steps), params=plan.params, let=plan.let)
    return twin(plan)


# single candidate: the logical plan IS the physical one (passthrough search)
LOGICAL = {"ic5": ic5}



# ── IC5 engine-faithful plans ────────────────────────────────────────────────────────────────
# IC5 = forums the friends+fof (1..2 knows, self excluded) joined after minDate, ranked by the
# number of Posts those friends made in the forum -> top 20 by (postCount desc, forumId asc).
#
# ONE SEMANTIC divergence between the engines, which their query text makes explicit:
#   * dgraph counts `containerOf @filter(type(post) AND uid_in(hasCreator, uid(F)))` — posts in the
#     forum authored by ANY friend in F, NOT restricted to the friend who joined THAT forum. So a
#     forum gets credit for a friend's post even if that friend is not a member of it (as long as
#     some friend joined it). This OVER-counts vs the LDBC-exact answer.
#   * nebula binds the specific member: `(friend)<-[:HAS_CREATOR]-(post:Post)<-[:CONTAINER_OF]-(forum)`
#     — posts by THAT member in THAT forum (member-matched), then COUNT(DISTINCT post) per forum.
#   * arango binds it too: `FILTER author._id == f._id` (member-matched).
# The optimal `ic5` plan is member-matched, so dgraph's engine plan diverges on vs-optimal
# (a faithful engine approximation, like is2's cap -- vs-LIVE is the real gate); nebula/arango match.
#
# Nebula and ArangoDB both read every forum's title BEFORE ranking (Nebula's AppendVertices node
# 28, ArangoDB's [7] forum documents) and take forumId from the vid / _key, so their tail is in
# memory: `_ic5_rank`.

def _ic5_rank(count_ref, forums_ref, forum_rows_ref):
    """The in-memory IC5 tail of the nebula and arango engine plans (0 KV): every forum of
    `forums_ref` (distinct vids) ranks, at count 0 when `count_ref` has no row for it; its id and
    title come from `forum_rows_ref`, rows an earlier node already read; top 20 by (count desc,
    id asc); the LDBC columns."""
    return [
        Step("counted_ids", "pluck", rows=ref(count_ref), field="item"),
        Step("zero_forums", "exclude", items=ref(forums_ref), remove=ref("counted_ids")),
        Step("zero_ann", "annotate", items=ref("zero_forums"), field="count", value=0),
        Step("zero_rows", "rename", rows=ref("zero_ann"), names={"_vid": "item"}),
        Step("allf", "concat", a=ref(count_ref), b=ref("zero_rows")),
        Step("frows", "dedup_rows", rows=ref(forum_rows_ref), field="_vid"),
        Step("named", "join", left=ref("allf"), right=ref("frows"), left_on="item",
             right_on="_vid"),
        Step("top20", "top_k", rows=ref("named"), keys=[("count", "desc"), ("id", "asc")], k=20),
        Step("labeled", "rename", rows=ref("top20"),
             names={"id": "forumId", "title": "forumTitle", "count": "postCount"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["forumId", "forumTitle", "postCount"]),
    ]


def _ic5_forums_after_mindate(f_ref, mindate):
    """Dgraph's block 6: the forums the friend set `f_ref` joined after minDate -- ~hasMember reverse with
    the joinDate facet (free in the posting), filter joinDate > minDate, type(forum), dedup. minDate is
    bound eagerly from args (engine plans are plain step lists, no param() binding)."""
    return [
        Step("memb", "read_neighbors", srcs=ref(f_ref), etype="hasMember", dir="in",
             edge_props=["joinDate"], pairs=True),
        Step("memb_new", "filter", rows=ref("memb"),
             pred=E.and_(E.notnull(E.get("joinDate")), E.gt(E.field("joinDate"), mindate))),
        Step("forumv", "pluck", rows=ref("memb_new"), field="dst"),
        Step("forums_t", "read_filter_by_label_index", vids=ref("forumv"), label="Forum"),
        Step("MF", "dedup", items=ref("forums_t")),
    ]


def ic5_dgraph_engine(args):
    """IC5 dgraph, from dgraph/ic5/p1/plan.txt (blocks 0-9).

    The answer is a deviation of our query text (engine/data/queries/dgraph/interactive-complex-5.graphql;
    its copy in dgraph/ldbc-queries-latest/ documents it): block 7 counts a forum's posts by ANY friend
    in F (`uid_in(hasCreator, uid(F))`), not only by the members who joined that forum after minDate.
    So the rows differ from the optimal (LDBC) answer and match live (deviations.py). Block 8's `ord`
    is a float64; a float tie at the top-20 cut would change the live answer, not the counts (none at
    sf0.3).

    sf0.3 vs live (p1): block 6 ~hasMember from 476 friends -> 20,063 forums; block 7 reads the
    containerOf posting of each forum and the hasCreator posting of every one of its 222,652 posts;
    block 8 reads the id of every forum (20,063 GETs), block 9 the title and the id again of the 20
    it returns (40 GETs)."""
    p = person_vid(args["person"])
    mindate = int(args["minDate"])
    return _anchor_person(p) + [                       # block 0: eq(id) + type(person)
        # block 1: _l1 { knows  ~knows } -- 1 SEEK per direction
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("l1d", "dedup", items=ref("l1")),
        # block 2: F1 as uid(l1a, l1b) @filter(type(person) AND NOT uid(P))
        Step("l1p", "read_filter_by_label_index", vids=ref("l1d"), label="Person"),
        Step("F1", "exclude", items=ref("l1p"), remove=ref("me")),
        # block 3: _l2(func: uid(l1a, l1b)) { knows  ~knows } -- from ALL of level 1
        Step("l2", "read_neighbors", srcs=ref("l1d"), etype="knows", dir="both"),
        Step("l2d", "dedup", items=ref("l2")),
        # block 4: F2 as uid(l2a, l2b) @filter(type(person) AND NOT uid(P, F1))
        Step("l2p", "read_filter_by_label_index", vids=ref("l2d"), label="Person"),
        Step("l2x", "exclude", items=ref("l2p"), remove=ref("me")),
        Step("F2", "exclude", items=ref("l2x"), remove=ref("F1")),
        # block 5: F as uid(F1, F2) -- in memory
        Step("F12", "concat", a=ref("F1"), b=ref("F2")),
        Step("F", "dedup", items=ref("F12")),
        # block 6: MF as ~hasMember @facets(gt(joinDate, $minDate)) @filter(type(forum)) -- 1 SEEK
        # per friend (the facet is in the posting), + 1 type-index SEEK
    ] + _ic5_forums_after_mindate("F", mindate) + [
        # block 7: pc as count(containerOf @filter(type(post) AND uid_in(hasCreator, uid(F)))) --
        # 1 SEEK per forum; both conjuncts see every post: 1 type-index SEEK, and the hasCreator
        # posting of every post (1 SEEK each)
        Step("fposts", "read_neighbors", srcs=ref("MF"), etype="containerOf", dir="out", pairs=True),
        Step("postv", "pluck", rows=ref("fposts"), field="dst"),
        Step("posts_t", "read_filter_by_label_index", vids=ref("postv"), label="Post"),
        Step("pauth", "read_neighbors", srcs=ref("postv"), etype="hasCreator", dir="out", pairs=True),
        Step("byF", "filter_in", rows=ref("pauth"), field="dst", allowed=ref("F")),
        Step("byFv", "pluck", rows=ref("byF"), field="src"),
        Step("good_posts", "intersect", items=ref("posts_t"), other=ref("byFv")),
        Step("fposts_r", "rename", rows=ref("fposts"), names={"src": "_forum", "dst": "_post"}),
        Step("forum_posts", "filter_in", rows=ref("fposts_r"), field="_post", allowed=ref("good_posts")),
        Step("fp_forum", "pluck", rows=ref("forum_posts"), field="_forum"),
        Step("forum_count", "group_count", items=ref("fp_forum")),
        # a forum of MF with no such post still ranks, at count 0 (block 9 is over uid(MF))
        Step("counted_ids", "pluck", rows=ref("forum_count"), field="item"),
        Step("zero_forums", "exclude", items=ref("MF"), remove=ref("counted_ids")),
        Step("zero_ann", "annotate", items=ref("zero_forums"), field="count", value=0),
        Step("zero_rows", "rename", rows=ref("zero_ann"), names={"_vid": "item"}),
        Step("allf", "concat", a=ref("forum_count"), b=ref("zero_rows")),
        Step("allf_v", "pluck", rows=ref("allf"), field="item"),
        # block 8: forumIdVar as id, ord as math(pc + (1e15 - forumIdVar) / 1e15) -- 1 GET per forum
        Step("forum_id", "read_vertex_props", vids=ref("allf_v"), props=["id"], label="Forum"),
        Step("forum_rows", "zip_rows", left=ref("allf"), right=ref("forum_id")),
        # block 9: forums(func: uid(MF), orderdesc: val(ord), first: 20) -- the sort is in memory;
        # ord desc == (postCount desc, forumId asc)
        Step("top20", "top_k", rows=ref("forum_rows"), keys=[("count", "desc"), ("id", "asc")], k=20),
        Step("top_v", "pluck", rows=ref("top20"), field="_vid"),
        # block 9: forumName: title, forumId: id [vals=20 each] -- 1 GET each; block 8's id read
        # is not kept, so the id is read again
        Step("titles", "read_vertex_props", vids=ref("top_v"), props=["title"], label="Forum"),
        Step("ids", "read_vertex_props", vids=ref("top_v"), props=["id"], label="Forum"),
        Step("titled", "zip_rows", left=ref("top20"), right=ref("titles")),
        Step("idd", "zip_rows", left=ref("titled"), right=ref("ids")),    # forumId: block 9's read
        Step("labeled", "rename", rows=ref("idd"),
             names={"id": "forumId", "title": "forumTitle", "count": "postCount"}),
        Step("out", "project", rows=ref("labeled"), fields=["forumId", "forumTitle", "postCount"]),
    ]


def ic5_nebula_engine(args):
    """IC5 nebula, from nebula/ic5/p1/plan.txt. Two MATCH clauses, joined in memory:

    node 23 Traverse knows BOTH 1..2: the start's Person._tag probe, 1 SEEK per direction per
      frontier vertex. node 34 Traverse hasMember IN per friend: Person._tag probe (1 GET) + 1 SEEK;
      the joinDate filter reads the edge value. node 28 AppendVertices Forum [title]: 1 GET per
      distinct forum. node 10 Argument: the distinct friends left. node 11 Traverse hasCreator IN
      per friend. node 26 Traverse containerOf IN per message: its vertexProps list all 8 tags, so
      8 GETs per message, and 1 SEEK per message, Comments included. In memory: 30 HashLeftJoin on
      (forum, friend), 16 COUNT(DISTINCT post) per forum, 20 TopN 20.

    sf0.3 vs live: S, G and N exact at all 6 seen params (p1: S=172,484 G=1,392,764 N=318,644;
    node 26's 8 tags x 171,528 messages are 1,372,224 of the GETs). Rows match the optimal plan
    and the live engine at all 6."""
    p = person_vid(args["person"])
    mindate = int(args["minDate"])
    return [
        # nodes 3 Start, 1 PassThrough, 2 Dedup: the start vid (in memory)
        Step("me", "read_vertex_by_id", vid=p),
        # node 23 Traverse knows BOTH 1..2, dedup: 1, no vertexProps: the first-step Person._tag
        # probe on the start (1 GET), then 1 SEEK per direction per frontier vertex + 1 NEXT per
        # edge (the same node as ic9.py's node 16)
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("h1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("h1d", "dedup", items=ref("h1")),
        Step("h2", "read_neighbors", srcs=ref("h1d"), etype="knows", dir="both"),
        Step("fu", "concat", a=ref("h1d"), b=ref("h2")),
        Step("fd", "dedup", items=ref("fu")),
        Step("F", "exclude", items=ref("fd"), remove=ref("me")),  # no path ends at the start (ic9.py)
        # node 34 Traverse hasMember IN_EDGE from each friend, dedup: 1, no vertexProps: the
        # first-step Person._tag probe (1 GET per friend), 1 SEEK per friend + 1 NEXT per edge. Its
        # filter HAS_MEMBER.joinDate > minDate reads the edge value, which comes with the NEXT.
        Step("ftag", "tag_probe", vids=ref("F"), label="Person"),
        Step("memb", "read_neighbors", srcs=ref("F"), etype="hasMember", dir="in",
             edge_props=["joinDate"], pairs=True),
        Step("memb_new", "filter", rows=ref("memb"),
             pred=E.and_(E.notnull(E.get("joinDate")), E.gt(E.field("joinDate"), mindate))),
        # node 33 Filter on the person / friend ids: in memory
        # node 28 AppendVertices Forum [title], dedup: 1: 1 GET per distinct forum (every forum has
        # the tag, so no miss scan)
        Step("forumv", "pluck", rows=ref("memb_new"), field="dst"),
        Step("forum_rows", "read_vertex_props", vids=ref("forumv"), label="Forum",
             props=["id", "title"]),
        Step("forums", "dedup", items=ref("forumv")),
        # node 31 Project (friend, forum); node 10 Argument: the DISTINCT friends of those rows
        # (ArgumentExecutor.cpp keeps a VidHashSet) -- in memory
        Step("argv", "pluck", rows=ref("memb_new"), field="src"),
        Step("arg", "dedup", items=ref("argv")),
        # node 11 Traverse hasCreator IN_EDGE from each Argument friend, dedup: 1, no vertexProps,
        # no filter: 1 SEEK per friend + 1 NEXT per message
        Step("fmsg", "read_neighbors", srcs=ref("arg"), etype="hasCreator", dir="in", pairs=True),
        Step("msgv", "pluck", rows=ref("fmsg"), field="dst"),
        # node 26 Traverse containerOf IN_EDGE from each message, dedup: 1. vertexProps lists all 8
        # tags: 8 GETs per message, and the Post row answers the first-step Post._tag filter in the
        # same read (IC1 node 34, IC6 node 33). 1 SEEK per message, Comments too (the filter drops
        # rows, not reads: NebulaEngineFormat.tag_probe), + 1 NEXT per edge.
        Step("mtags", "all_tag_props", vids=ref("msgv")),
        Step("cont", "read_neighbors", srcs=ref("msgv"), etype="containerOf", dir="in", pairs=True),
        # its tag filter Post._tag IS NOT EMPTY keeps the Posts' rows (in memory)
        Step("prow", "filter", rows=ref("mtags"), pred=E.eq(E.get("_tag"), "Post")),
        Step("postv", "pluck", rows=ref("prow"), field="_vid"),
        Step("pcont", "filter_in", rows=ref("cont"), field="src", allowed=ref("postv")),
        # node 29 Project (friend, post, forum), node 30 HashLeftJoin with node 31's rows on
        # (forum, friend), node 16 Aggregate COUNT(DISTINCT post) per forum -- in memory. A post
        # counts only where its creator is the member who joined that forum after minDate.
        Step("pcr", "rename", rows=ref("pcont"), names={"src": "_post", "dst": "forum"}),
        Step("fmr", "rename", rows=ref("fmsg"), names={"src": "friend", "dst": "_post"}),
        Step("pff", "join_multi", left=ref("pcr"), right=ref("fmr"), left_on="_post",
             right_on="_post"),
        Step("pff_k", "derive", rows=ref("pff"), into="_pf",
             fn=tuple_(field("friend"), field("forum"))),
        Step("memb_k", "derive", rows=ref("memb_new"), into="_pf",
             fn=tuple_(field("src"), field("dst"))),
        Step("memb_kv", "pluck", rows=ref("memb_k"), field="_pf"),
        Step("own", "filter_in", rows=ref("pff_k"), field="_pf", allowed=ref("memb_kv")),
        Step("own_d", "dedup_rows", rows=ref("own"), field="_post"),
        Step("own_forum", "pluck", rows=ref("own_d"), field="forum"),
        Step("forum_count", "group_count", items=ref("own_forum")),
        # node 17 Project (forumId from the vid, the title node 28 read), node 20 TopN 20
    ] + _ic5_rank("forum_count", "forums", "forum_rows")


def ic5_arangodb_engine(args):
    """IC5 arango, from arangodb/ic5/p1/plan.json (plan.txt prints the edge direction backwards).

    [3] walks knows 1..2 with the postFilter friend._id != start, [6] COLLECT gives the distinct
    friends, [7] reads each friend's hasMember edges with the EDGE postFilter joinDate > minDate.
    Then a subquery runs once per [7] ROW (71,850 at p1), not per distinct forum (20,063): [11]
    reads the row's forum's posts and each post's document, [12] reads each post's creator with
    the postFilter author._id == f._id (post and creator read, each survivor again). [20] COLLECT
    sums the per-row counts per forum; sort and limit are in memory.

    The profile's `items` inside the subquery add one shadow row per run: [11] 1,094,008 =
    1,022,158 posts + 71,850, [12] 72,328 = 478 survivors + 71,850.

    sf0.3 vs live: S, G and N exact at all 6 seen params (p1: S=1,094,489 G=6,447,163
    N=2,140,594). The per-row subquery is the cost: [11] + [12] are 6,133,904 of the GETs, over
    1,022,158 post rows for 222,652 distinct posts. Rows match the optimal plan and the live
    engine at all 6."""
    p = person_vid(args["person"])
    mindate = int(args["minDate"])
    return [
        # [1] Singleton, [29] Calculation: in memory
        # [3] TraversalNode knows OUT,IN 1..2 from the start string (edge(_from), edge(_to)), no
        # vertexProjections, postFilter friend._id != startPerson: the query's first traversal
        # (+1 open SEEK), then traverse_filtered -- every expansion source and path step read,
        # every survivor read again (the same node as ic9.py / ic11.py [3])
        Step("open", "traversal_open", items=[p], etype="knows"),
        Step("walk", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both", depth=2,
             props=["id"], label="Person", pred=E.ne(E.field("_vid"), p)),
        # [6] COLLECT f = friend, [28] SORT f: the distinct friends (in memory)
        Step("wv", "pluck", rows=ref("walk"), field="_vid"),
        Step("F", "dedup", items=ref("wv")),
        # [7] TraversalNode hasMember INBOUND f (edge(_to)), edgeProjections [_from,_to,joinDate],
        # postFilter on the EDGE membership.joinDate > minDate: 1 SEEK per friend + 1 NEXT per
        # edge; the edge document per step, and again per survivor (ic11.py [7], the same shape)
        Step("memb", "traverse_filtered", srcs=ref("F"), etype="hasMember", dir="in", depth=1,
             edge_props=["joinDate"],
             pred=E.and_(E.notnull(E.get("joinDate")), E.gt(E.field("joinDate"), mindate))),
        # [7] has no vertexProjections (the forum is [11]'s start and [20]'s COLLECT key): each
        # surviving row's forum document, pk + doc -- [7]'s output or [11]'s start, the counts
        # cannot tell which (ic11.py [7]'s company, the same read)
        Step("mforum", "pluck", rows=ref("memb"), field="_vid"),
        Step("fdocs", "docs_per_step", vids=ref("mforum"), label="Forum", props=["id", "title"]),
        # [30] SubqueryStartNode: [11] and [12] run once per [7] row, not per distinct forum
        # [11] TraversalNode containerOf OUTBOUND forum (edge(_from)), vertexProjections [_id], no
        # filter: 1 SEEK per row + 1 NEXT per edge, and each post's document per step (an [_id]
        # projection still fetches it: ic4.py [5], [27])
        Step("cp", "traverse_paths", srcs=ref("mforum"), etype="containerOf", dir="out", depth=1,
             pairs=True),
        Step("cpv", "pluck", rows=ref("cp"), field="dst"),
        Step("pdocs", "docs_per_step", vids=ref("cpv"), label="Post", props=["id"]),
        # [12] TraversalNode hasCreator OUTBOUND post (edge(_from)), vertexProjections [_id],
        # postFilter author._id == f._id: 1 SEEK + 1 NEXT per post row. traverse_filtered's read
        # rule (ic6.py [23], the same shape), written out because the filter compares with the
        # ROW's friend: every expansion source (the post) and every step (its creator) is read,
        # and every survivor again.
        Step("pc", "traverse_paths", srcs=ref("cpv"), etype="hasCreator", dir="out", depth=1,
             pairs=True),
        Step("pcsrc", "docs_per_step", vids=ref("cpv"), label="Post", props=["id"]),
        Step("pcv", "pluck", rows=ref("pc"), field="dst"),
        Step("pcdst", "docs_per_step", vids=ref("pcv"), label="Person", props=["id"]),
        # the (row, post) pairs that pass, in memory: each distinct post's (creator, forum) joined
        # to every [7] row whose (friend, forum) equals it -- one output per passing pair, so the
        # count is per ROW, as the subquery's (the per-row lists above repeat each post once per
        # member row of its forum; the join is done on the distinct posts to keep it small)
        Step("cpd", "dedup_rows", rows=ref("cp"), field="dst"),
        Step("cpr", "rename", rows=ref("cpd"), names={"src": "forum", "dst": "_post"}),
        Step("pcd", "dedup_rows", rows=ref("pc"), field="src"),
        Step("pcr", "rename", rows=ref("pcd"), names={"src": "_post", "dst": "author"}),
        Step("pfa", "join_multi", left=ref("cpr"), right=ref("pcr"), left_on="_post",
             right_on="_post"),
        Step("pfa_k", "derive", rows=ref("pfa"), into="_pf",
             fn=tuple_(field("author"), field("forum"))),
        Step("memb_k", "derive", rows=ref("memb"), into="_pf",
             fn=tuple_(field("_src"), field("_vid"))),
        Step("own", "join_multi", left=ref("memb_k"), right=ref("pfa_k"), left_on="_pf",
             right_on="_pf"),
        Step("ownv", "pluck", rows=ref("own"), field="author"),
        Step("pcemit", "docs_per_step", vids=ref("ownv"), label="Person", props=["id"]),
        # [31] SubqueryEnd, [18] LENGTH, [19] {postCount}, [20] COLLECT fo = forum INTO members,
        # [27] SORT, [21] SUM: the member-matched post count per forum (in memory)
        Step("own_forum", "pluck", rows=ref("own"), field="forum"),
        Step("forum_count", "group_count", items=ref("own_forum")),
        Step("forums", "dedup", items=ref("mforum")),
        # [22] TO_NUMBER(fo._key), [23] SORT totalPosts DESC, _key ASC (limit 20), [24] LIMIT,
        # [25] fo.title from [7]'s documents: in memory
    ] + _ic5_rank("forum_count", "forums", "fdocs")


ENGINE = {
    "ic5": {"dgraph": ic5_dgraph_engine, "nebula": ic5_nebula_engine,
            "arangodb": ic5_arangodb_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic5": _KNOWS_HINTS}
