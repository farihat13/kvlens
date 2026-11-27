"""LDBC IS7 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import (_anchor_message, _arango_message_anchor, message_vids, MESSAGE_PARAM,
                                                 MESSAGE_LET)


IS7_FIELDS = ["commentId", "commentContent", "commentCreationDate", "replyAuthorId",
              "replyAuthorFirstName", "replyAuthorLastName", "replyAuthorKnowsOriginalMessageAuthor"]


def is7(args):
    """IS7: direct replies to a message + each reply's author + whether that author knows the message's author.
    Sort: reply date desc, author id asc. Like IS5, NO anchor step: the reverse replyOf / forward hasCreator scans
    run straight from the one Message vid."""
    cands = [let("msg_vid")]
    return Plan([
        Step("replies",     "read_neighbors", srcs=cands, etype="replyOf", dir="in"),
        Step("reply_props", "read_vertex_props", vids=ref("replies"), props=["id", "creationDate", "content"],
             label="Comment"),
        Step("reply_named", "rename", rows=ref("reply_props"),
             names={"id": "commentId", "creationDate": "commentCreationDate",
                    "content": "commentContent"}),
        Step("author_vids", "read_neighbors", srcs=ref("replies"), etype="hasCreator", dir="out"),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"), props=["id", "firstName", "lastName"],
             label="Person"),
        Step("author_named", "rename", rows=ref("author_props"),
             names={"id": "replyAuthorId", "firstName": "replyAuthorFirstName",
                    "lastName": "replyAuthorLastName"}),
        Step("joined",      "zip_rows", left=ref("reply_named"), right=ref("author_named")),   # _vid = author vid
        # LAZY knows flag: nothing is read (not even the message's author) when
        # there are no replies; otherwise per-pair edge probes, not a friend scan
        # (the ≤-engine bound caught the eager whole-friend-list version).
        Step("with_knows", "read_edge_exists", rows=ref("joined"), field="_vid", etype="knows",
             into="replyAuthorKnowsOriginalMessageAuthor",
             via_srcs=cands, via_etype="hasCreator"),
        Step("sorted",     "top_k", rows=ref("with_knows"),
             keys=[("commentCreationDate", "desc"), ("replyAuthorId", "asc")], k=None),
        Step("out",        "project", rows=ref("sorted"), fields=IS7_FIELDS),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


def is7_dgraph_engine(args):
    """Replies to a message, their authors, and the knows check.

        replies  S     G   N
        0        989   1   982      params 1, 7, 31
        1        995   8   982      params 13, 19, 25

    The headline is the sort, and it is the same with or without replies: `~replyOf` with
    `orderdesc creationDate, orderasc id, first: 1000` and NO date bound, so sortWithIndex
    walks the ENTIRE creationDate index -- all 982 day buckets -- before it can know whether
    there is anything to rank. N is exactly the bucket count. Nebula answers the same empty
    question with S=1.

    The `knowsCount`/`knowsCount2` pair is two `count()` subqueries over the reply author's
    knows lists, one per direction: 1 posting-list SEEK each, no GET -- a count reads the
    list length, not the values."""
    steps = _anchor_message(args["message"]) + [
        # var block: o = hasCreator @filter(type(person)) -- the original author
        Step("auth", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="out"),
        Step("author", "read_filter_by_label_index", vids=ref("auth"), label="Person"),
        # the origAuthor block exposes just the id
        Step("aid", "read_vertex_props", vids=ref("author"), props=["id"]),
        # replies block: ~replyOf @filter(type(comment)), ranked
        Step("rep", "read_neighbors", srcs=ref("me"), etype="replyOf", dir="in"),
        Step("reps", "read_filter_by_label_index", vids=ref("rep"), label="Comment"),
        Step("repr", "annotate", items=ref("reps"), field="creationDate", value=0),
        Step("top", "sort_topk", rows=ref("repr"), prop="creationDate", k=1000,
             second_key="id", id_order="asc"),
        Step("topv", "pluck", rows=ref("top"), field="_vid"),
        # each surviving reply's own scalars
        Step("rprops", "read_vertex_props", vids=ref("topv"),
             props=["id", "creationDate", "content"]),
        # ... its author, behind another type filter ...
        Step("ra", "read_neighbors", srcs=ref("topv"), etype="hasCreator", dir="out"),
        Step("rauth", "read_filter_by_label_index", vids=ref("ra"), label="Person"),
        Step("rap", "read_vertex_props", vids=ref("rauth"),
             props=["id", "firstName", "lastName"]),
        # ... and the two count() subqueries, one per knows direction (pairs: output shape only,
        # keeps which reply author each edge belongs to)
        Step("kc1", "read_neighbors", srcs=ref("rauth"), etype="knows", dir="out", pairs=True),
        Step("kc2", "read_neighbors", srcs=ref("rauth"), etype="knows", dir="in", pairs=True),
        Step("cr", "rename", rows=ref("rprops"),
             names={"id": "commentId", "content": "commentContent",
                    "creationDate": "commentCreationDate"}),
        Step("ar", "rename", rows=ref("rap"),
             names={"id": "replyAuthorId", "firstName": "replyAuthorFirstName",
                    "lastName": "replyAuthorLastName"}),
        Step("joined", "zip_rows", left=ref("cr"), right=ref("ar")),
        # LDBC output columns (in memory): knowsCount + knowsCount2 > 0, i.e. the reply author has
        # a knows edge (either direction) to o; the author itself has none, so it reads False
        Step("kc", "concat", a=ref("kc1"), b=ref("kc2")),
        Step("kco", "filter_in", rows=ref("kc"), field="dst", allowed=ref("author")),
        Step("knowers", "pluck", rows=ref("kco"), field="src"),
        Step("flagged", "flag_in", rows=ref("joined"), field="_vid", allowed=ref("knowers"),
             into="replyAuthorKnowsOriginalMessageAuthor"),
        Step("out", "project", rows=ref("flagged"), fields=IS7_FIELDS),
    ]
    return steps


def is7_nebula_engine(args):
    """Replies to a message, their authors, and whether each author knows the original
    author. Exact with and without replies.

        replies  S   G   N        params (== live sf0.1)
        0        1   0   0        p1, p31
        1        5  10  54        p7    (the knows walk is the ORIGINAL author's, walked once)
        3        7  14  32        p13
        7       11  21  41        p19
       10       14  28  84        p25

    Every term: S = replyOf(1) + hasCreator-from-the-reply(1) + hasCreator-from-the-message
    (1) + knows BOTH (2). The knows walk is the ORIGINAL author's, walked ONCE -- not once per
    reply author.
    G = the reply Comment rows + the reply-author Person rows + **8** for node 18: the ORIGINAL
    author's `vertexProps` lists eight tagIds, the same all-tags read as IC1. A final in-memory
    sort reproduces live's ORDER BY commentCreationDate DESC, replyAuthorId ASC (0 KV ops).

    **With no replies the whole query costs ONE SEEK.** Node 4 finds nothing and every
    operator below it -- the author traverse, the knows walk, the HashLeftJoin -- runs on an
    empty input for free. The message author is never read, which is why G is 0 and not 1.
    Dgraph spends S=989 on the identical empty answer, because its sort is driven by the
    creationDate index rather than by the candidates."""
    m = message_vids(args["message"])[0]
    return [
        Step("me", "read_vertex_by_id", vid=m),
        # node 4: the replies. Everything downstream is gated on this being non-empty.
        Step("reps", "read_neighbors", srcs=ref("me"), etype="replyOf", dir="in"),
        # node 16: hasCreator from each reply, carrying the reply's own Comment props
        Step("rprops", "read_vertex_props", vids=ref("reps"), label="Comment",
             props=["id", "content", "creationDate"]),
        Step("rauth", "read_neighbors", srcs=ref("reps"), etype="hasCreator", dir="out"),
        # node 17: AppendVertices reads the reply author's Person row
        Step("rap", "read_vertex_props", vids=ref("rauth"), label="Person",
             props=["id", "firstName", "lastName"]),
        # node 9: the ORIGINAL author. Its `src` is `id($-.m)` fed by the REPLIES branch, so
        # it runs once per reply row and NOT AT ALL when there are none -- which is why an
        # empty IS7 costs 1 SEEK here and not 2. Lifting the anchor through the reply rows
        # reproduces that gating: no replies, no source, no seek. (`read_neighbors` reads its
        # sources as a set, so r replies still cost one seek.)
        Step("gv", "gate", items=[m], when=ref("reps")),
        Step("oa", "read_neighbors", srcs=ref("gv"), etype="hasCreator", dir="out"),
        # node 18: the knows walk that answers "does the reply author know the original
        # author". Live nebula `(m)-[:HAS_CREATOR]->(a)-[:KNOWS]-(p)` walks KNOWS from the
        # SINGLE original author `oa` (the reply authors are the bound join target), so the
        # source is `oa`, not each reply author: ONE walk + `oa`'s 8 tag GETs (the IC1 rule),
        # not |reply-authors| walks + |reply-authors|x8 GETs.
        Step("kn", "read_neighbors", srcs=ref("oa"), etype="knows", dir="both"),
        Step("ktags", "all_tag_props", vids=ref("oa")),
        Step("cr", "rename", rows=ref("rprops"),
             names={"id": "commentId", "content": "commentContent",
                    "creationDate": "commentCreationDate"}),
        Step("ar", "rename", rows=ref("rap"),
             names={"id": "replyAuthorId", "firstName": "replyAuthorFirstName",
                    "lastName": "replyAuthorLastName"}),
        Step("joined", "zip_rows", left=ref("cr"), right=ref("ar")),
        # live nebula ORDER BY commentCreationDate DESC, replyAuthorId ASC. Without this the
        # plan emits reverse-replyOf vid order, which diverges from live once an anchor has
        # enough replies that vid-order != date-order. In-memory sort: 0 KV ops (cost-neutral).
        Step("sorted", "top_k", rows=ref("joined"),
             keys=[("commentCreationDate", "desc"), ("replyAuthorId", "asc")], k=None),
        # LDBC output columns (in memory): node 20 HashLeftJoin -- is the reply author among
        # the original author's knows neighbours (node 18)
        Step("flagged", "flag_in", rows=ref("sorted"), field="_vid", allowed=ref("kn"),
             into="replyAuthorKnowsOriginalMessageAuthor"),
        Step("out", "project", rows=ref("flagged"), fields=IS7_FIELDS),
    ]


def is7_arangodb_engine(args):
    """Replies to a message, their authors, and the knows check, from arangodb/is7/p1/plan.txt.
    S, G, N exact at every param, sf0.3 and sf0.1.

    The G is mostly node 13, `FOR v IN 1..1 ANY replyAuthor knows FILTER v._id ==
    origAuthor._id LIMIT 1`, run once per reply row: arango MATERIALIZES every reached vertex
    (pk + doc) even though the filter only compares `v._id`, which the edge index already
    carries -- pure engine waste, the same materialize-even-for-the-key rule measured in IS1.

    With R replies, K_r = the knows degree of reply r's author, and H = the rows whose author
    knows the original author: S = 3 + 3R, N = 1 + 2R + sum(K_r),
    G = 3 (DOCUMENT pair) + 2 (origAuthor) + 4R (reply + its author) + 2R ([13]'s start
    vertex) + 2 sum(K_r) ([13]'s reached vertices) + 2H ([13]'s survivor, read again)."""
    return _arango_message_anchor(args["message"]) + [   # [2]/[3] DOCUMENT(Post/..), DOCUMENT(Comment/..)
        # [6] TraversalNode hasCreator edge(_from) -- the first traversal. origAuthor runs even
        # with no replies: it is a LET above the FOR. No vertexProjections: the author doc.
        Step("open", "traversal_open", items=ref("me"), etype="hasCreator"),
        Step("oa", "read_neighbors", srcs=ref("open"), etype="hasCreator", dir="out"),
        Step("oad", "docs_per_step", vids=ref("oa"), label="Person", props=["id"]),
        # [10] TraversalNode replyOf edge(_to); vertexProjections [_id,_key,content,creationDate]:
        # the reply doc per step
        Step("reps", "read_neighbors", srcs=ref("me"), etype="replyOf", dir="in"),
        Step("rprops", "docs_per_step", vids=ref("reps"), label="Comment",
             props=["id", "content", "creationDate"]),
        # [11] TraversalNode hasCreator edge(_from), once per reply; vertexProjections: author doc
        Step("rauth", "read_neighbors", srcs=ref("reps"), etype="hasCreator", dir="out"),
        Step("rap", "docs_per_step", vids=ref("rauth"), label="Person",
             props=["id", "firstName", "lastName"]),
        # [13] TraversalNode knows edge(_from)+edge(_to), 1..1, inside FOR reply / FOR
        # replyAuthor: once per reply ROW, no dedup (2 SEEKs per row + 1 NEXT per edge)
        Step("kn_pairs", "traverse_paths", srcs=ref("rauth"), etype="knows", dir="both", depth=1,
             pairs=True),
        Step("kn", "pluck", rows=ref("kn_pairs"), field="dst"),
        # [13] postFilter `v._id == origAuthor._id` (traverse_filtered's rule): the vertex it
        # expands from, and every reached vertex (vertexProjections [_id] still fetches the doc)
        Step("ksrc", "docs_per_step", vids=ref("rauth"), label="Person", props=["id"]),
        Step("knd", "docs_per_step", vids=ref("kn"), label="Person", props=["id"]),
        # [13] the survivor -- the original author, when this reply author knows them -- read
        # again as it is emitted; [16] LIMIT 1 is in memory
        Step("khit", "intersect", items=ref("kn"), other=ref("oa")),
        Step("khitd", "docs_per_step", vids=ref("khit"), label="Person", props=["id"]),
        Step("cr", "rename", rows=ref("rprops"),
             names={"id": "commentId", "content": "commentContent",
                    "creationDate": "commentCreationDate"}),
        Step("ar", "rename", rows=ref("rap"),
             names={"id": "replyAuthorId", "firstName": "replyAuthorFirstName",
                    "lastName": "replyAuthorLastName"}),
        Step("zipped", "zip_rows", left=ref("cr"), right=ref("ar")),
        # live arango SORT reply.creationDate DESC, TO_NUMBER(replyAuthor._key) ASC. Without
        # this the plan emits reverse-replyOf vid order, diverging from live at deeper reply
        # counts (p19/p25). In-memory sort: cost-neutral (0 KV ops).
        Step("sorted", "top_k", rows=ref("zipped"),
             keys=[("commentCreationDate", "desc"), ("replyAuthorId", "asc")], k=None),
        # LDBC output columns (in memory): [20] LENGTH(the [13]/[16] result) > 0 -- did this reply's
        # author reach the original author in [13]
        Step("kn_oa", "filter_in", rows=ref("kn_pairs"), field="dst", allowed=ref("oa")),
        Step("knowers", "pluck", rows=ref("kn_oa"), field="src"),
        Step("flagged", "flag_in", rows=ref("sorted"), field="_vid", allowed=ref("knowers"),
             into="replyAuthorKnowsOriginalMessageAuthor"),
        Step("out", "project", rows=ref("flagged"), fields=IS7_FIELDS),
    ]


# single candidate: the logical plan IS the physical one (passthrough search)
def is7_logical(args):
    """The LOGICAL is7: the knows flag is a membership_flag choice point (the
    VIA form -- the anchor is the message's author, resolved through hasCreator;
    the probe side is the hand plan, the materialize side is the historically
    rejected eager friend-scan, now measured rather than remembered)."""
    cands = [let("msg_vid")]
    return Plan([
        Step("replies",     "read_neighbors", srcs=cands, etype="replyOf", dir="in"),
        Step("reply_props", "read_vertex_props", vids=ref("replies"), props=["id", "creationDate", "content"],
             label="Comment"),
        Step("reply_named", "rename", rows=ref("reply_props"),
             names={"id": "commentId", "creationDate": "commentCreationDate",
                    "content": "commentContent"}),
        Step("author_vids", "read_neighbors", srcs=ref("replies"), etype="hasCreator", dir="out"),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"), props=["id", "firstName", "lastName"],
             label="Person"),
        Step("author_named", "rename", rows=ref("author_props"),
             names={"id": "replyAuthorId", "firstName": "replyAuthorFirstName",
                    "lastName": "replyAuthorLastName"}),
        Step("joined",      "zip_rows", left=ref("reply_named"), right=ref("author_named")),
        Step("with_knows", "membership_flag", rows=ref("joined"), field="_vid",
             etype="knows", dir="both", into="replyAuthorKnowsOriginalMessageAuthor",
             via_srcs=cands, via_etype="hasCreator", via_name="oauthor",
             set_read_name="ofriends0", set_name="ofriends"),
        Step("sorted",     "top_k", rows=ref("with_knows"),
             keys=[("commentCreationDate", "desc"), ("replyAuthorId", "asc")], k=None),
        Step("out",        "project", rows=ref("sorted"), fields=IS7_FIELDS),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


LOGICAL = {"is7": is7_logical}

SIM = {
    "is7": is7
}

ENGINE = {
    "is7": {"dgraph": is7_dgraph_engine, "nebula": is7_nebula_engine, "arangodb": is7_arangodb_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"is7": _KNOWS_HINTS}
