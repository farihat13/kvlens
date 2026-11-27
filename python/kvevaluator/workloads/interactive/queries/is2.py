"""LDBC IS2 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import param, let, compose_vid, notnull, get, field, key_id
from kvevaluator.workloads.interactive._shared import ENGINE_WALK_CAP, MESSAGE_LABELS, person_vid, _anchor_person
from kvevaluator.plans.ir import authoring as E


def _is2_plan(steps):
    """Wrap the built steps as a PARAMETERIZED plan: `person` is a hole, and the anchor
    vid is composed from it at bind time (format-neutral)."""
    return Plan(steps, params={"person": {"type": "id"}},
                let={"me_vid": compose_vid("Person", param("person"))})


def _is2_steps(args, rank_props, fetch_body, ixwalk=False, chase_cap=None):
    """IS2: `person`'s 10 most recent messages; for each, the ROOT post of its
    reply chain (a post roots itself) + that post's author. Sort: creationDate
    desc, message id desc (the one DESC id tie-break in the workload). The root
    walk is `read_chase` over replyOf-out — its cost is the sum of per-hop read_neighbors
    reads, including the final empty read that proves the chain ended. ixwalk:
    rank via the (unbounded) date-index walk, bodies fused."""
    p = let("me_vid")            # the anchor vid, filled at bind time from the person param
    steps = [
        Step("me",      "read_vertex_by_id", vid=p, label="Person"),
        Step("my_msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
    ]
    if ixwalk:
        steps += [
            Step("top10",    "read_top_k_by_index", vids=ref("my_msgs"), prop="creationDate",
                 k=10, labels=["Post", "Comment"], props=["content", "imageFile"],
                 id_order="desc"),                      # IS2 ties: id DESC
            Step("top_vids", "pluck", rows=ref("top10"), field="_vid"),
        ]
        msgs = "top10"
        return _is2_plan(steps + _is2_tail(msgs, chase_cap))
    steps += [
        Step("ranked",   "read_vertex_props", vids=ref("my_msgs"), props=rank_props,
             label=["Post", "Comment"]),
        Step("top10",    "top_k", rows=ref("ranked"),
             keys=[("creationDate", "desc"), ("id", "desc")], k=10),
        Step("top_vids", "pluck", rows=ref("top10"), field="_vid"),
    ]
    if fetch_body:
        steps += [
            Step("bodies",    "read_vertex_props", vids=ref("top_vids"), props=["content", "imageFile"],
                 label=["Post", "Comment"]),
            Step("with_body", "zip_rows", left=ref("top10"), right=ref("bodies")),
        ]
        msgs = "with_body"
    else:
        msgs = "top10"
    return _is2_plan(steps + _is2_tail(msgs, chase_cap))


def _is2_tail(msgs, chase_cap=None):
    # roots can REPEAT (two messages in one thread) -- reads are deduped and the
    # root/author fields come back via joins on the attached root vid, never by
    # position (read_neighbors reads srcs as a set; see formats/base.py).
    # chase_cap: None = correct full walk; 3 = the engines' REPLY_OF*1..3 cap
    # (is2_capped) -- a message deeper than 3 hops resolves to a non-post, so its
    # originalPost comes back wrong/empty, matching the engines' approximation.
    return [
        Step("roots",     "read_chase", srcs=ref("top_vids"), etype="replyOf", dir="out", max_hops=chase_cap),
        Step("msg_root",  "attach", rows=ref(msgs), values=ref("roots"), into="_root"),
        Step("root_uniq", "dedup", items=ref("roots")),
        Step("root_id",   "read_vertex_props", vids=ref("root_uniq"), props=["id"], label="Post"),
        Step("orig_id",   "rename", rows=ref("root_id"), names={"id": "originalPostId"}),
        Step("with_root", "join", left=ref("msg_root"), right=ref("orig_id"),
             left_on="_root", right_on="_vid"),
        Step("root_auth", "read_neighbors", srcs=ref("root_uniq"), etype="hasCreator", dir="out", pairs=True),
        Step("auth_vids", "pluck", rows=ref("root_auth"), field="dst"),
        Step("auth_uniq", "dedup", items=ref("auth_vids")),
        Step("auth_props", "read_vertex_props", vids=ref("auth_uniq"), props=["id", "firstName", "lastName"],
             label="Person"),
        Step("auth_named", "rename", rows=ref("auth_props"),
             names={"id": "originalPostAuthorId",
                    "firstName": "originalPostAuthorFirstName",
                    "lastName": "originalPostAuthorLastName"}),
        Step("auth_join", "join", left=ref("root_auth"), right=ref("auth_named"),
             left_on="dst", right_on="_vid"),
        Step("auth_root", "rename", rows=ref("auth_join"), names={"src": "_root2"}),
        Step("joined",    "join", left=ref("with_root"), right=ref("auth_root"),
             left_on="_root", right_on="_root2"),
        Step("labeled",   "rename", rows=ref("joined"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("out",  "project", rows=ref("labeled"),
             fields=["messageId", "messageContent", "messageImageFile",
                     "messageCreationDate", "originalPostId", "originalPostAuthorId",
                     "originalPostAuthorFirstName", "originalPostAuthorLastName"]),
    ]


def is2_ixwalk(args):
    """IS2, dgraph default: rank via the UNBOUNDED date-index walk; fewer weighted ops than the GET-rank two-phase
    (is2_getrank, recorded loser). The tension: bytes are far higher -- the walk reads the GLOBAL day-token posting
    lists to find one person's 10 newest messages (no maxDate to bound the start)."""
    return _is2_steps(args, rank_props=None, fetch_body=False, ixwalk=True)


def is2_getrank(args):
    """Recorded loser for dgraph (see is2_ixwalk): rank on (id, creationDate)
    GETs, bodies for the top 10 only."""
    return _is2_steps(args, rank_props=["id", "creationDate"], fetch_body=True)


def is2_onephase(args):
    """One-phase (nebula/arango): whole-row rank reads carry the body. The walk loses badly here: one person's messages
    are SPARSE in the global date indexes, so the walk crosses ~123k entries per param."""
    return _is2_steps(args, rank_props=["id", "creationDate", "content", "imageFile"],
                      fetch_body=False)


def is2_ixwalk_capped(args):
    """is2 (dgraph) with the root walk capped at 3 hops; ranking unchanged."""
    return _is2_steps(args, rank_props=None, fetch_body=False, ixwalk=True,
                      chase_cap=ENGINE_WALK_CAP)


def is2_onephase_capped(args):
    """is2 (nebula/arango) with the root walk capped at 3 hops; ranking unchanged."""
    return _is2_steps(args, rank_props=["id", "creationDate", "content", "imageFile"],
                      fetch_body=False, chase_cap=ENGINE_WALK_CAP)


def is2_logical(args):
    """The LOGICAL is2 for the optimizer's search: rank region as one `rank`
    step (unbounded; the one DESC id tie-break), body fetch as a `fetch_props`
    marker; the root-chase tail is shared."""
    return _is2_logical(chase_cap=None)


def is2_capped_logical(args):
    """The LOGICAL is2_capped (the engines' 3-hop root-walk approximation);
    same rank choice point."""
    return _is2_logical(chase_cap=ENGINE_WALK_CAP)


def _is2_logical(chase_cap):
    p = let("me_vid")
    steps = [
        Step("me",      "read_vertex_by_id", vid=p, label="Person"),
        Step("my_msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        Step("top10",   "rank", vids=ref("my_msgs"),
             keys=[("creationDate", "desc"), ("id", "desc")], k=10,
             labels=["Post", "Comment"], read_label=["Post", "Comment"],
             rank_props=["id", "creationDate"], body_props=["content", "imageFile"]),
        Step("top_vids", "pluck", rows=ref("top10"), field="_vid"),
        Step("with_body", "fetch_props", rows=ref("top10"), vids=ref("top_vids"),
             props=["content", "imageFile"], label=["Post", "Comment"]),
    ]
    return _is2_plan(steps + _is2_tail("with_body", chase_cap))


LOGICAL = {"is2": is2_logical, "is2_capped": is2_capped_logical}

SIM = {
    # the hand plans: the date-index walk by default, one-phase on the row stores; the walk is KEPT as a measured body
    # (`_keep`). Every legal body is in the generated plan file, and the meta grid measures each.
    "is2": {"default": is2_ixwalk, "nebula": is2_onephase, "arangodb": is2_onephase,
            "_keep": {"index_walk": ("dgraph", is2_ixwalk)}},
    "is2_capped": {"default": is2_ixwalk_capped, "nebula": is2_onephase_capped, "arangodb": is2_onephase_capped}
}

LABEL_ONLY = {
    "is2": {"default": is2_getrank, "nebula": is2_onephase, "arangodb": is2_onephase}
}


# ── IS2 engine-faithful plans (all three live-validated at sf0.3) ──────────────────────────────
# IS2 ranks one person's 10 newest messages by creationDate with NO date bound, then unrolls the
# replyOf chain to the message's original Post + that Post's author. The three engines differ in
# HOW they rank and how far they chase. Each builder transcribes its captured plan node by node.
#
# Reply-chase depth per engine query (engines/queries/ldbc/<db>/interactive-short-2.*):
#   dgraph  — unrolls replyOf exactly 3 levels (DQL has no @recurse in a query block)  -> cap 3
#   nebula  — REPLY_OF*1..3, AND deviates: top-5 posts + top-5 comments (no ORDER/LIMIT after
#             UNION ALL), so it returns up to 10 rows that are NOT the global top-10  -> cap 3, 5+5
#   arango  — FOR op IN 0..100 OUTBOUND msg replyOf (effectively uncapped), true global top-10
#
# Consequences for the vs-optimal row check (the optimal is2 is uncapped, true top-10):
#   arango  — should MATCH the optimal (true top-10, full chase; only the id-tie direction differs)
#   dgraph  — matches at reply depth <= 3; a comment deeper than 3 hops resolves its original Post
#             wrong/empty (the capped approximation, same class as is6's _CAPPED_CHASE)
#   nebula  — the 5+5 split means the row SET differs from the global top-10 on most params
#             (same class as ic2's _TWO_BRANCHES). Both are EXPECTED divergences, not plan bugs.

def _dg_hop(name, srcs, etype, label):
    """One dgraph child edge with a type filter: `<etype> @filter(type(<label>))` from `srcs`.
    The edge: 1 posting SEEK per distinct src (misses paid). The filter: 1 type-index SEEK when
    the edge returned anything. Returns steps ending in `<name>` = (src, dst) pairs whose dst
    passed the filter, and `<name>t` = the surviving dst vids."""
    return [
        Step(name + "e", "read_neighbors", srcs=srcs, etype=etype, dir="out", pairs=True),
        Step(name + "v", "pluck", rows=ref(name + "e"), field="dst"),
        Step(name + "t", "read_filter_by_label_index", vids=ref(name + "v"), label=label),
        Step(name, "filter_in", rows=ref(name + "e"), field="dst", allowed=ref(name + "t")),
    ]


def _dg_author(name, posts):
    """`originalPoster: hasCreator @filter(type(person)) { authorId: id  authorFirstName:
    firstName  authorLastName: lastName }` from `posts`: the hop, then 3 GETs per distinct author.
    Returns steps ending in `<name>` = (src post, dst author) pairs and `<name>p` = author rows."""
    return _dg_hop(name, posts, "hasCreator", "Person") + [
        Step(name + "p", "read_vertex_props", vids=ref(name + "t"),
             props=["id", "firstName", "lastName"]),
    ]


def is2_dgraph_engine(args):
    """IS2 dgraph, from dgraph/is2/p1/plan.txt (one block; p7/p13/p19 captured, same tree).

    The root `person(func: eq(id), orderdesc: creationDate, orderasc: id, first: 10)` SORTS the
    one-person root by creationDate. creationDate has a sortable (day) index, so detsort runs
    sortWithIndex (sort_topk): it walks the GLOBAL creationDate index newest-first looking for 10
    root uids, finds only the one person, and so never stops (worker/sort.go:697-708: errDone
    only once every list holds `count` uids) -- it crosses the whole index (1 SEEK
    + 1 NEXT + 1 bucket SEEK per day token, + the person's own value), then multiSort reads the
    person's `id`: a constant ~+1000 S / +987 N. microbench topk1 measures the same index walk (987 NEXTs)
    with the same op.

    Then `messages: ~hasCreator @filter(type(post) OR type(comment)) (orderdesc: creationDate,
    orderasc: id, first: 10)` is a second sort_topk over the person's messages, and every child
    of the 10 winners is its own posting read: originalPost / selfAsOriginal / parentComment each
    re-read the winners' replyOf or hasCreator posting (no sharing between sibling children), and
    the unroll stops at 3 levels (greatGrandParentPost), so a deeper comment gets no original post.

    The answer shape follows the query: a Post is its own original post (selfAsOriginal gives the
    author); a Comment takes the first Post found at depth 1, 2 or 3.

    sf0.3 vs live: S and N exact at all 6 params; G is 2-6 low, exactly the split Comment
    type-posting residual (residual_exact YES at all 6). Answer: same 10 messages, dates, original
    posts and authors as the live engine at all 6 params, checked by flattening the raw response
    by hand -- validate --live says UNPARSED because the kvengines parser reads `posts`/`comments`
    blocks this query does not return."""
    p = person_vid(args["person"])
    steps = _anchor_person(p) + [                       # block 0: eq(id) + type(person)
        # block 0 root `order=creationDate desc,id first=10` over the ONE root uid: sortWithIndex
        # walks the whole creationDate index (never reaches 10), multiSort reads the person's id
        Step("mer", "annotate", items=ref("me"), field="creationDate", value=0),
        Step("proot", "sort_topk", rows=ref("mer"), prop="creationDate", k=10,
             second_key="id", id_order="asc"),
        Step("pv", "pluck", rows=ref("proot"), field="_vid"),
        # messages: ~hasCreator -- 1 posting SEEK; @filter(type(post) OR type(comment)) -- one
        # type-index SEEK per branch
        Step("my_msgs", "read_neighbors", srcs=ref("pv"), etype="hasCreator", dir="in"),
        Step("mp", "read_filter_by_label_index", vids=ref("my_msgs"), label="Post"),
        Step("mc", "read_filter_by_label_index", vids=ref("my_msgs"), label="Comment"),
        Step("cand", "concat", a=ref("mp"), b=ref("mc")),
        # messages `order=creationDate desc,id first=10`: sort_topk (index walk + multiSort id)
        Step("candr", "annotate", items=ref("cand"), field="creationDate", value=0),
        Step("top10", "sort_topk", rows=ref("candr"), prop="creationDate", k=10,
             second_key="id", id_order="asc"),
        Step("tv", "pluck", rows=ref("top10"), field="_vid"),
        # messageId / messageCreationDate / imageFile / content (1 GET each), messageType:
        # dgraph.type (a list predicate: 1 SEEK), vals=10
        Step("body", "read_vertex_props", vids=ref("tv"),
             props=["id", "creationDate", "imageFile", "content", "dgraph.type"]),
    ]
    # originalPost: replyOf @filter(type(post)) { postId postCreationDate postContent
    #   originalPoster: hasCreator @filter(type(person)) { authorId authorFirstName authorLastName } }
    steps += _dg_hop("op", ref("tv"), "replyOf", "Post") + [
        Step("opp", "read_vertex_props", vids=ref("opt"), props=["id", "creationDate", "content"]),
    ] + _dg_author("opa", ref("opt"))
    # selfAsOriginal: hasCreator @filter(type(person)) { authorId authorFirstName authorLastName }
    steps += _dg_author("sa", ref("tv"))
    # parentComment: replyOf @filter(type(comment)) { dgraph.type
    #   grandParentPost: replyOf @filter(type(post)) { postId  originalPoster {...} }
    #   grandParentComment: replyOf @filter(type(comment)) {
    #     greatGrandParentPost: replyOf @filter(type(post)) { postId  originalPoster {...} } } }
    steps += _dg_hop("pc", ref("tv"), "replyOf", "Comment") + [
        Step("pcty", "read_vertex_props", vids=ref("pct"), props=["dgraph.type"]),
    ]
    steps += _dg_hop("gpp", ref("pct"), "replyOf", "Post") + [
        Step("gppp", "read_vertex_props", vids=ref("gppt"), props=["id"]),
    ] + _dg_author("gppa", ref("gppt"))
    steps += _dg_hop("gpc", ref("pct"), "replyOf", "Comment")
    steps += _dg_hop("ggp", ref("gpct"), "replyOf", "Post") + [
        Step("ggpp", "read_vertex_props", vids=ref("ggpt"), props=["id"]),
    ] + _dg_author("ggpa", ref("ggpt"))

    # ── in memory: each winner's original post and that post's author (the client's walk) ──
    steps += [
        # a Post is its own original post
        Step("r0", "filter_in", rows=ref("body"), field="_vid", allowed=ref("mp")),
        Step("r0m", "derive", rows=ref("r0"), into="_root", fn=field("_vid")),
        Step("r0p", "project", rows=ref("r0m"), fields=["_vid", "_root"]),
        # depth 1: originalPost
        Step("r1", "rename", rows=ref("op"), names={"src": "_vid", "dst": "_root"}),
        # depth 2: parentComment -> grandParentPost
        Step("pc1", "rename", rows=ref("pc"), names={"src": "_vid", "dst": "_c1"}),
        Step("gpp1", "rename", rows=ref("gpp"), names={"src": "_c1", "dst": "_root"}),
        Step("r2", "join_multi", left=ref("pc1"), right=ref("gpp1"), left_on="_c1",
             right_on="_c1"),
        # depth 3: parentComment -> grandParentComment -> greatGrandParentPost
        Step("gpc1", "rename", rows=ref("gpc"), names={"src": "_c1", "dst": "_c2"}),
        Step("r3a", "join_multi", left=ref("pc1"), right=ref("gpc1"), left_on="_c1",
             right_on="_c1"),
        Step("ggp1", "rename", rows=ref("ggp"), names={"src": "_c2", "dst": "_root"}),
        Step("r3", "join_multi", left=ref("r3a"), right=ref("ggp1"), left_on="_c2",
             right_on="_c2"),
        Step("r01", "concat", a=ref("r0p"), b=ref("r1")),
        Step("r012", "concat", a=ref("r01"), b=ref("r2")),
        Step("r0123", "concat", a=ref("r012"), b=ref("r3")),
        Step("roots", "project", rows=ref("r0123"), fields=["_vid", "_root"]),
        # post -> author, from whichever branch read it
        Step("pa1", "concat", a=ref("opa"), b=ref("sa")),
        Step("pa2", "concat", a=ref("pa1"), b=ref("gppa")),
        Step("pa3", "concat", a=ref("pa2"), b=ref("ggpa")),
        Step("pa", "dedup_rows", rows=ref("pa3"), field="src"),
        Step("ap1", "concat", a=ref("opap"), b=ref("sap")),
        Step("ap2", "concat", a=ref("ap1"), b=ref("gppap")),
        Step("ap3", "concat", a=ref("ap2"), b=ref("ggpap")),
        Step("ap", "dedup_rows", rows=ref("ap3"), field="_vid"),
        Step("apn", "rename", rows=ref("ap"), names=_IS2_AUTHOR_NAMES),
        Step("paj", "join", left=ref("pa"), right=ref("apn"), left_on="dst"),
        Step("pajr", "rename", rows=ref("paj"), names={"src": "_root", "dst": "_auth"}),
        # the winners with a root (depth <= 3), then those without (null originalPost fields)
        Step("wr", "join", left=ref("roots"), right=ref("body"), left_on="_vid"),
        Step("wra", "join", left=ref("wr"), right=ref("pajr"), left_on="_root",
             right_on="_root"),
        Step("wrr", "derive", rows=ref("wra"), into="originalPostId", fn=key_id(field("_root"))),
        Step("rv", "pluck", rows=ref("roots"), field="_vid"),
        Step("nr_v", "exclude", items=ref("tv"), remove=ref("rv")),
        Step("nr", "filter_in", rows=ref("body"), field="_vid", allowed=ref("nr_v")),
        Step("all", "concat", a=ref("wrr"), b=ref("nr")),
        Step("sorted", "top_k", rows=ref("all"), keys=[("creationDate", "desc"), ("id", "asc")],
             k=10),
        Step("labeled", "rename", rows=ref("sorted"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["messageId", "messageContent", "messageImageFile",
                     "messageCreationDate", "originalPostId", "originalPostAuthorId",
                     "originalPostAuthorFirstName", "originalPostAuthorLastName"]),
    ]
    return steps


_IS2_RANK = [("creationDate", "desc"), ("id", "desc")]     # ORDER BY messageCreationDate, messageIdInt
_IS2_AUTHOR_NAMES = {"id": "originalPostAuthorId", "firstName": "originalPostAuthorFirstName",
                     "lastName": "originalPostAuthorLastName"}


def is2_nebula_engine(args):
    """IS2 nebula, from nebula/is2/p1/plan.txt. A UNION ALL of a Post branch and a Comment branch,
    each with its own TopN 5: the top-5 posts + top-5 comments, NOT the global top-10 (the query's
    declared deviation). Every read happens before the TopNs; the output rows are built from those
    reads, nothing is read after.

    Node 23 (REPLY_OF*1..3) follows graph/executor/query/TraverseExecutor.cpp: each step expands a
    frontier of distinct vids; step 2 takes every step-1 dst (step 1 is built against an empty
    adjList_), step 3 skips vids already expanded. Node 42's Post tag filter does not prune its
    hasCreator scan (storage FilterNode drops the edge after reading it).

    sf0.3 vs live: S, G, N exact at p1/p13/p25/p31; +1 each at p7 and p19. That is one extra
    step-3 expansion (1 SEEK + 1 GET + 1 NEXT). Both params have step-3 candidates that step 2
    already expanded with an edge; expand() skips such a vid only if its row came first in the
    step-2 response (hash order), which the sim cannot know, so it skips none."""
    p = person_vid(args["person"])
    steps = [Step("me", "read_vertex_by_id", vid=p)]
    # ── branch 1 (Post) ──
    steps += [
        # node 37 Traverse hasCreator IN_EDGE; `first step filter: Person._tag`
        Step("p_ptag", "tag_probe", vids=[p], label="Person"),
        Step("p_msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        # node 38 AppendVertices Post tag, dedup: 1 (a Comment misses -> storaged prefix scan)
        Step("p_rows", "read_vertex_props", vids=ref("p_msgs"),
             props=["id", "content", "imageFile", "creationDate"], label="Post"),
        Step("posts", "filter", rows=ref("p_rows"), pred=notnull(get("creationDate"))),
        # node 9 Traverse hasCreator OUT_EDGE from each post
        Step("posts_v", "pluck", rows=ref("posts"), field="_vid"),
        Step("p_auth", "read_neighbors", srcs=ref("posts_v"), etype="hasCreator", dir="out",
             pairs=True),
        # node 39 AppendVertices Person tag (firstName, lastName), dedup: 1
        Step("p_auth_v", "pluck", rows=ref("p_auth"), field="dst"),
        Step("p_auth_rows", "read_vertex_props", vids=ref("p_auth_v"),
             props=["id", "firstName", "lastName"], label="Person"),
        # nodes 34/12/13/35 (post AS its own original post, join, Project, TopN 5): in memory
        Step("p_auth_r", "rename", rows=ref("p_auth"), names={"src": "_p", "dst": "_auth"}),
        Step("p_j1", "join", left=ref("posts"), right=ref("p_auth_r"), left_on="_vid", right_on="_p"),
        Step("p_auth_n", "rename", rows=ref("p_auth_rows"), names=_IS2_AUTHOR_NAMES),
        Step("p_j2", "join", left=ref("p_j1"), right=ref("p_auth_n"), left_on="_auth",
             right_on="_vid"),
        Step("p_out", "derive", rows=ref("p_j2"), into="originalPostId", fn=field("id")),
        Step("top5p", "top_k", rows=ref("p_out"), keys=_IS2_RANK, k=5),
    ]
    # ── branch 2 (Comment) ──
    steps += [
        # node 40 Traverse hasCreator IN_EDGE; `first step filter: Person._tag`
        Step("c_ptag", "tag_probe", vids=[p], label="Person"),
        Step("c_msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        # node 41 AppendVertices Comment tag, dedup: 1 (a Post misses -> storaged prefix scan)
        Step("c_rows", "read_vertex_props", vids=ref("c_msgs"),
             props=["id", "content", "creationDate"], label="Comment"),
        Step("coms", "filter", rows=ref("c_rows"), pred=notnull(get("creationDate"))),
        # node 23 Traverse replyOf OUT_EDGE, steps 1..3; vertexProps = the Comment tag only, so
        # each expanded vid pays 1 GET. Step 1: the comments.
        Step("f1", "pluck", rows=ref("coms"), field="_vid"),
        Step("f1_tag", "all_tag_props", vids=ref("f1"), tags=("Comment",)),
        Step("h1", "read_neighbors", srcs=ref("f1"), etype="replyOf", dir="out", pairs=True),
        # node 23 step 2: every distinct step-1 dst
        Step("f2", "pluck", rows=ref("h1"), field="dst"),
        Step("f2_tag", "all_tag_props", vids=ref("f2"), tags=("Comment",)),
        Step("h2", "read_neighbors", srcs=ref("f2"), etype="replyOf", dir="out", pairs=True),
        # node 23 step 3: distinct step-2 dsts not already expanded in step 1
        Step("f3a", "pluck", rows=ref("h2"), field="dst"),
        Step("f3", "exclude", items=ref("f3a"), remove=ref("f1")),
        Step("f3_tag", "all_tag_props", vids=ref("f3"), tags=("Comment",)),
        Step("h3", "read_neighbors", srcs=ref("f3"), etype="replyOf", dir="out", pairs=True),
        # node 23's rows, in memory: every path of 1..3 hops as (comment, end). replyOf has
        # out-degree 1, so one edge per expanded vid.
        Step("h12", "concat", a=ref("h1"), b=ref("h2")),
        Step("h123", "concat", a=ref("h12"), b=ref("h3")),
        Step("adj", "dedup_rows", rows=ref("h123"), field="src"),
        Step("len1", "rename", rows=ref("h1"), names={"src": "_msg", "dst": "_end"}),
        Step("len1m", "rename", rows=ref("len1"), names={"_end": "_mid"}),
        Step("len2j", "join_multi", left=ref("len1m"), right=ref("adj"), left_on="_mid",
             right_on="src"),
        Step("len2", "rename", rows=ref("len2j"), names={"_mid": "_x", "dst": "_end"}),
        Step("len2m", "rename", rows=ref("len2"), names={"_x": "_x0", "_end": "_mid"}),
        Step("len3j", "join_multi", left=ref("len2m"), right=ref("adj"), left_on="_mid",
             right_on="src"),
        Step("len3", "rename", rows=ref("len3j"), names={"_mid": "_x", "dst": "_end"}),
        Step("paths12", "concat", a=ref("len1"), b=ref("len2")),
        Step("paths", "concat", a=ref("paths12"), b=ref("len3")),
        # node 42 Traverse hasCreator OUT_EDGE from each distinct path end; `first step filter:
        # Post._tag` (1 GET per end, no prune)
        Step("ends", "pluck", rows=ref("paths"), field="_end"),
        Step("end_post", "tag_probe", vids=ref("ends"), label="Post"),
        Step("end_auth", "read_neighbors", srcs=ref("ends"), etype="hasCreator", dir="out",
             pairs=True),
        # node 43 AppendVertices origAuthor: 8 tagIds listed, dedup: 1 -> 8 GETs per distinct author
        Step("post_auth", "filter_in", rows=ref("end_auth"), field="src", allowed=ref("end_post")),
        Step("oa_v", "pluck", rows=ref("post_auth"), field="dst"),
        Step("oa_rows", "all_tag_props", vids=ref("oa_v")),
        # nodes 26-30/36 (HashLeftJoin, Dedup, Project, TopN 5): in memory. A comment deeper than
        # 3 hops has no post -> null originalPost fields.
        Step("c_paths", "filter_in", rows=ref("paths"), field="_end", allowed=ref("end_post")),
        Step("post_auth_r", "rename", rows=ref("post_auth"), names={"src": "_p", "dst": "_auth"}),
        Step("c_j1", "join", left=ref("c_paths"), right=ref("post_auth_r"), left_on="_end",
             right_on="_p"),
        Step("oa_p", "project", rows=ref("oa_rows"), fields=["_vid", "id", "firstName", "lastName"]),
        Step("oa_n", "rename", rows=ref("oa_p"), names=_IS2_AUTHOR_NAMES),
        Step("c_j2", "join", left=ref("c_j1"), right=ref("oa_n"), left_on="_auth", right_on="_vid"),
        Step("c_j3", "derive", rows=ref("c_j2"), into="originalPostId", fn=key_id(field("_end"))),
        Step("c_post", "join", left=ref("c_j3"), right=ref("coms"), left_on="_msg",
             right_on="_vid"),
        Step("c_with", "pluck", rows=ref("c_paths"), field="_msg"),
        Step("c_none_v", "exclude", items=ref("f1"), remove=ref("c_with")),
        Step("c_none", "filter_in", rows=ref("coms"), field="_vid", allowed=ref("c_none_v")),
        Step("c_out", "concat", a=ref("c_post"), b=ref("c_none")),
        Step("top5c", "top_k", rows=ref("c_out"), keys=_IS2_RANK, k=5),
    ]
    # node 33 Union
    return steps + [
        Step("union", "concat", a=ref("top5p"), b=ref("top5c")),
        Step("labeled", "rename", rows=ref("union"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["messageId", "messageContent", "messageImageFile",
                     "messageCreationDate", "originalPostId", "originalPostAuthorId",
                     "originalPostAuthorFirstName", "originalPostAuthorLastName"]),
    ]


def is2_arangodb_engine(args):
    """IS2 arango, from arangodb/is2/p1/plan.txt. The true global top-10 and an uncapped chase
    (`0..100 OUTBOUND replyOf`), so the answer should match the optimal plan.

    Both subqueries ([9] the chase, [18] the author) sit after the LIMIT and run once per
    winner, with no dedup across winners. [9]'s postFilter reads each vertex it visits once
    and the survivor again -- the same accounting IS6's [6] (the same query fragment) measures
    exactly at every reply depth. traverse_filtered's own rule (expand + emit + survivor) would
    read each middle chain vertex twice; IS6 and IS2 both measure once, and we do not know why.
    With M messages, W = min(M, 10) winners and d_w = winner w's reply depth:
    S = 2 + sum(d_w + 1) + W, N = M + sum(d_w) + W, G = 2 + 2M + sum(2(d_w + 1) + 2) + 2W."""
    p = person_vid(args["person"])
    return [
        # [2] LET person = DOCUMENT(Person/<id>): pk + doc
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        Step("mev", "pluck", rows=ref("me"), field="_vid"),
        # [3] TraversalNode hasCreator edge(_to), 1..1 -- the first traversal
        Step("open", "traversal_open", items=ref("mev"), etype="hasCreator"),
        Step("msgs", "read_neighbors", srcs=ref("open"), etype="hasCreator", dir="in"),
        # [3] no vertexProjections: every message document, per step
        Step("mdocs", "docs_per_step", vids=ref("msgs"), label=MESSAGE_LABELS,
             props=["id", "content", "imageFile", "creationDate"]),
        # [6]/[7] SORT creationDate DESC, TO_NUMBER(_key) ASC LIMIT 10: in memory
        Step("top10", "top_k", rows=ref("mdocs"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=10),
        Step("top_vids", "pluck", rows=ref("top10"), field="_vid"),
        # [9] TraversalNode replyOf edge(_from), 0..100, once per winner ([27]/[28] subquery):
        # 1 SEEK per visited vertex (the Post too) + 1 NEXT per edge. Rows = (child, parent).
        # traverse_filtered is used only as the walk that returns (child, parent) rows: no
        # pred/props, so it reads no document; the postFilter's reads are the steps below.
        Step("chain", "traverse_filtered", srcs=ref("top_vids"), etype="replyOf", dir="out",
             depth=100, pred=E.lit(True)),
        # [9] postFilter IS_SAME_COLLECTION("Post", op), no vertexProjections: each visited
        # vertex is read (pk + doc) -- the winner itself at depth 0, then every chain step
        Step("v0", "docs_per_step", vids=ref("top_vids"), label=MESSAGE_LABELS, props=["id"]),
        Step("up_v", "pluck", rows=ref("chain"), field="_vid"),
        Step("up", "docs_per_step", vids=ref("up_v"), label=MESSAGE_LABELS, props=["id"]),
        # [9] the survivor (the Post ending each winner's chain) is read again as it is
        # emitted; [10]-[12] FILTER + LIMIT 1 are in memory
        Step("visited", "concat", a=ref("v0"), b=ref("up")),
        Step("surv", "filter", rows=ref("visited"), pred=E.eq(E.field("_label"), "Post")),
        Step("surv_v", "pluck", rows=ref("surv"), field="_vid"),
        Step("surv_d", "docs_per_step", vids=ref("surv_v"), label="Post", props=["id"]),
        # [15]/[16] origPost = FIRST(...), thePost: in memory -- each winner's chain end
        Step("chain_k", "derive", rows=ref("chain"), into="_k", fn=E.lit(0)),
        Step("top_k0", "derive", rows=ref("top10"), into="_k", fn=E.lit(0)),
        Step("with_chain", "nest", rows=ref("top_k0"), children=ref("chain_k"), on="_k",
             child_on="_k", into="_chain", fields=["_src", "_vid"]),
        Step("with_root", "derive", rows=ref("with_chain"), into="_root",
             fn=E.chain_root(E.field("_vid"), E.field("_chain"))),
        Step("with_pid", "derive", rows=ref("with_root"), into="originalPostId",
             fn=key_id(field("_root"))),
        # [18] TraversalNode hasCreator edge(_from), 1..1 from thePost, once per winner
        # ([25]/[26] subquery, no dedup): 1 SEEK + 1 NEXT each; parallel to the winners
        Step("roots", "pluck", rows=ref("with_pid"), field="_root"),
        Step("auth", "read_chase", srcs=ref("roots"), etype="hasCreator", dir="out", max_hops=1),
        # [18] no vertexProjections (RETURN a): the author document per winner
        Step("auth_d", "docs_per_step", vids=ref("auth"), label="Person",
             props=["id", "firstName", "lastName"]),
        # [21]-[23] FIRST(...) + the RETURN projection: in memory
        Step("auth_n", "rename", rows=ref("auth_d"),
             names={"_vid": "_auth", "_label": "_auth_label", **_IS2_AUTHOR_NAMES}),
        Step("joined", "zip_rows", left=ref("with_pid"), right=ref("auth_n")),
        Step("labeled", "rename", rows=ref("joined"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["messageId", "messageContent", "messageImageFile",
                     "messageCreationDate", "originalPostId", "originalPostAuthorId",
                     "originalPostAuthorFirstName", "originalPostAuthorLastName"]),
    ]


ENGINE = {
    "is2": {"nebula": is2_nebula_engine, "arangodb": is2_arangodb_engine,
            "dgraph": is2_dgraph_engine}
}
