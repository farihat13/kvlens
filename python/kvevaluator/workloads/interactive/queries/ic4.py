"""LDBC IC4 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import and_, le, lt, not_, eq, notnull, get, field, param, let
from kvevaluator.workloads.interactive._shared import (MESSAGE_LABELS, _anchor_person, _friend_set, person_vid, PERSON_PARAM,
                                                 PERSON_LET, DATE_WINDOW_PARAM, DATE_WINDOW_LET)


# ------------------------------------------------------------------------ IC4

def ic4(args):
    """IC4: tags on friends' POSTS created in the window that were NEVER used by friends' posts before it -> top 10 by
    (postCount desc, tagName asc). filter_by_label prices the posts-only cut (comments carry hasTag too).
    Recorded alternative, priced symmetrically (every format materializes the type-enumeration structures):
    enumerate ALL posts, then a free intersect. Per-vid checks win for the small candidate sets IC plans produce.
    """
    p = let("me_vid")
    return Plan(_friend_set(p, 1) + [
        Step("fmsgs",      "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in"),
        Step("posts",      "filter_by_label", vids=ref("fmsgs"), label="Post"),
        Step("post_date",  "read_vertex_props", vids=ref("posts"), props=["creationDate"], label="Post"),
        Step("in_win",     "filter", rows=ref("post_date"),
             pred=and_(le(param("startDate"), field("creationDate")), lt(field("creationDate"), let("end")))),
        Step("pre_win",    "filter", rows=ref("post_date"),
             pred=lt(field("creationDate"), param("startDate"))),
        Step("winv",       "pluck", rows=ref("in_win"), field="_vid"),
        Step("oldv",       "pluck", rows=ref("pre_win"), field="_vid"),
        Step("win_tags",   "read_neighbors", srcs=ref("winv"), etype="hasTag", dir="out"),
        Step("old_tags",   "read_neighbors", srcs=ref("oldv"), etype="hasTag", dir="out"),
        Step("old_tagd",   "dedup", items=ref("old_tags")),
        Step("tag_count",  "group_count", items=ref("win_tags")),
        Step("tag_ids",    "pluck", rows=ref("tag_count"), field="item"),
        # keep only tags never used by a friend's post before the window
        Step("fresh",      "exclude", items=ref("tag_ids"), remove=ref("old_tagd")),
        Step("fresh_count","filter_in", rows=ref("tag_count"), field="item", allowed=ref("fresh")),
        Step("fresh_ids",  "pluck", rows=ref("fresh_count"), field="item"),
        Step("tag_names",  "read_vertex_props", vids=ref("fresh_ids"), props=["name"], label="Tag"),
        Step("named",      "zip_rows", left=ref("fresh_count"), right=ref("tag_names")),
        Step("top10",      "top_k", rows=ref("named"), keys=[("count", "desc"), ("name", "asc")], k=10),
        Step("labeled",    "rename", rows=ref("top10"), names={"name": "tagName", "count": "postCount"}),
        Step("out",        "project", rows=ref("labeled"), fields=["tagName", "postCount"]),
    ], params={**PERSON_PARAM, **DATE_WINDOW_PARAM}, let={**PERSON_LET, **DATE_WINDOW_LET})


# ── IC4 ──────────────────────────────────────────────────────────────────────
# "Tags on a person's friends' posts in a date window, that were NOT used before it."
#
# Nebula runs the friend expansion TWICE, once per branch, and the second time reads every
# message with all eight tags. From the captured plan (p1):
#
#   branch A (postCount, in-window)      branch B (oldCount, the correlated subplan)
#     node 28  knows BOTH      S=2         node 12  knows BOTH      S=2
#     node 29  hasCreator IN   S=2         node 32  hasCreator IN   S=2   (the SAME 751 msgs)
#     node 30  hasTag OUT      S=751       node 33  hasTag OUT      S=751
#              vertexProps tagId 3                  vertexProps: EIGHT tagIds
#              -> 751 GETs (creationDate)           -> 751 x 8 = 6008 GETs
#     node 35  AppendVertices Tag.name, deduped
#
#   S = 2 + 2 + 751 + 2 + 2 + 751 = 1510      live 1510, exact
#   G = 6008 + 751 + the small reads          live 6789
#
# Branch B is the cost: re-reading the same 751 messages under all eight tags is 88% of the
# GETs, and it happens only because the subplan lost the type information branch A had.
def ic4_nebula_engine(args):
    p = person_vid(args["person"])
    lo = int(args["startDate"])
    hi = lo + int(args["durationDays"]) * 86400000
    return [
        Step("me", "read_vertex_by_id", vid=p),
        # ---- branch A: the in-window postCount ----
        # node 28 Traverse knows BOTH from start: first-step Person._tag (1 GET) + 2 SEEK + NEXT
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("fr_a", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("frd_a", "exclude", items=ref("fr_a"), remove=[p]),
        # node 29 Traverse hasCreator IN per friend: first-step Person._tag (1 GET) + SEEK + NEXT
        Step("fp_a", "tag_probe", vids=ref("frd_a"), label="Person"),
        Step("msg_a", "read_neighbors", srcs=ref("frd_a"), etype="hasCreator", dir="in"),
        # node 30 Traverse hasTag OUT per message: vertexProps Post [creationDate] fused with the
        # first-step Post._tag filter (1 GET, no prefix scan), + 1 SEEK per message + NEXT
        Step("dated", "tag_probe", vids=ref("msg_a"), label="Post", props=["creationDate"]),
        Step("datedv", "pluck", rows=ref("dated"), field="_vid"),
        Step("tags_a", "read_neighbors", srcs=ref("msg_a"), etype="hasTag", dir="out", pairs=True),
        # tag_probe keeps only the messages that ARE Posts, so restrict before joining dates
        Step("tags_p", "filter_in", rows=ref("tags_a"), field="src", allowed=ref("datedv")),
        Step("mt", "join", left=ref("tags_p"), right=ref("dated"),
             left_on="src", right_on="_vid"),
        Step("inwin", "filter", rows=ref("mt"),
             pred=and_(notnull(get("creationDate")), le(lo, field("creationDate")), lt(field("creationDate"), hi))),
        # ---- branch B: the oldCount subplan, GATED on branch A having produced something ----
        # Node 38 is a HashLeftJoin fed by the Argument at node 11. With no in-window tag to
        # correlate against, the subplan never fires and its four operators cost NOTHING.
        # Measured: param 3 has an empty in-window set and its live S/N are exactly HALF what
        # running both branches costs (611 vs 1222, 1298 vs 2596).
        Step("gate", "gate", items=ref("me"), when=ref("inwin")),
        # node 12 Traverse knows BOTH from start (no first-step filter): 2 SEEK + NEXT
        Step("fr_b", "read_neighbors", srcs=ref("gate"), etype="knows", dir="both"),
        Step("frd_b", "exclude", items=ref("fr_b"), remove=[p]),
        # node 32 Traverse hasCreator IN per friend2: first-step Person._tag (1 GET) + SEEK + NEXT
        Step("fp_b", "tag_probe", vids=ref("frd_b"), label="Person"),
        Step("msg_b", "read_neighbors", srcs=ref("frd_b"), etype="hasCreator", dir="in"),
        # node 33 Traverse hasTag OUT per message: vertexProps = all 8 tags (8 GETs per source),
        # + 1 SEEK per message + NEXT. Branch A already knew which are Posts.
        Step("btags", "all_tag_props", vids=ref("msg_b")),
        Step("tags_b", "read_neighbors", srcs=ref("msg_b"), etype="hasTag", dir="out", pairs=True),
        # node 35 AppendVertices Tag [name], dedup: 1. Its input is node 30's OUTPUT, which the
        # first-step `Post._tag` filter has already pruned to the POSTS' tags -- not the tags
        # of all 751 messages. Naming every message's tags reads 558 where the engine reads
        # 67, and that gap is the entire GET residual (S is unaffected: node 30 still SEEKS
        # from all 751, it just emits fewer rows).
        Step("tv", "pluck", rows=ref("tags_p"), field="dst"),
        Step("tvd", "dedup", items=ref("tv")),
        Step("tnames", "read_vertex_props", vids=ref("tvd"), label="Tag", props=["name"]),
        # ---- nodes 36, 27, 10, 37, 38, 18-20, 23: the answer, in memory ----
        Step("old", "filter", rows=ref("mt"),
             pred=and_(notnull(get("creationDate")), lt(field("creationDate"), lo))),
        Step("inwv", "pluck", rows=ref("inwin"), field="dst"),
        Step("oldv", "pluck", rows=ref("old"), field="dst"),
        Step("cnt", "group_count", items=ref("inwv")),
        Step("noold", "flag_in", rows=ref("cnt"), field="item", allowed=ref("oldv"),
             into="used_before"),
        Step("keep", "filter", rows=ref("noold"), pred=not_(field("used_before"))),
        Step("named", "join", left=ref("keep"), right=ref("tnames"),
             left_on="item", right_on="_vid"),
        Step("out", "top_k", rows=ref("named"),
             keys=[("count", "desc"), ("name", "asc")], k=10),
    ]


# Dgraph's IC4 is six blocks, and blocks 4 and 5 are the interesting ones: having found the
# 4 candidate tags, it walks ~hasTag BACK to every message carrying them (563) and re-applies
# the whole type+date filter -- twice, once per block -- rather than reusing block 3's work.
#
#   block 0  anchor person                                   S=2
#   block 1  knows + ~knows                          -> 2    S=2
#   block 2  type(person) AND NOT uid(P)                      S=1
#   block 3  ~hasCreator -> 751 msgs                          S=2
#              type(post)                                     S=1
#              creationDate ge   vals=751                      G=751
#              creationDate lt   vals=751                      G=751
#              hasTag -> 18 -> 4 tags                          S=18 + 1
#   block 4  ~hasTag COUNT-ONLY from the 4 tags -> 563 msgs   S=4 + 1
#              creationDate ge/lt  vals=563 each               G=563 + 563
#              hasCreator uid_in(<the 2 friends>)              S=563
#   block 5  same again, but only the `lt` half                S=4 + 1 + 563, G=563
#
#   S = 27 + 568 + 568 = 1163      live 1163, exact
#   G = 1502 + 1126 + 563 + 3 = 3194   live 3194, exact
#
# Note the asymmetry: block 4 reads creationDate TWICE per message (ge and lt are separate
# filter nodes) while block 5 needs only `lt` and reads it once. Modelling both blocks the
# same way over-counts by 563.
def ic4_dgraph_engine(args):
    p = person_vid(args["person"])
    lo = int(args["startDate"])
    hi = lo + int(args["durationDays"]) * 86400000
    return _anchor_person(p) + [
        # block 1: knows + ~knows from P; block 2: type(person) (1 SEEK) AND NOT uid(P)
        Step("fr", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("frt", "read_filter_by_label_index", vids=ref("fr"), label="Person"),
        Step("friends", "exclude", items=ref("frt"), remove=[p]),
        # block 3: ~hasCreator per friend (1 SEEK each) @filter(type(post) (1 SEEK) AND
        # ge(creationDate) AND lt(creationDate)) -- two filter nodes, each reads the date
        # (ineqfilterpath=per_vertex, the live config); then hasTag per surviving post (1 SEEK
        # each) @filter(type(tag)) (1 SEEK)
        Step("msgs", "read_neighbors", srcs=ref("friends"), etype="hasCreator", dir="in"),
        Step("posts", "read_filter_by_label_index", vids=ref("msgs"), label="Post"),
        Step("d1", "read_vertex_props", vids=ref("msgs"), props=["creationDate"]),
        Step("d2", "read_vertex_props", vids=ref("msgs"), props=["creationDate"]),
        Step("win", "filter", rows=ref("d2"),
             pred=and_(notnull(field("creationDate")), le(lo, field("creationDate")), lt(field("creationDate"), hi))),
        Step("winv", "pluck", rows=ref("win"), field="_vid"),
        Step("winp", "intersect", items=ref("winv"), other=ref("posts")),
        Step("tags", "read_neighbors", srcs=ref("winp"), etype="hasTag", dir="out"),
        Step("tagt", "read_filter_by_label_index", vids=ref("tags"), label="Tag"),
        Step("tagd", "dedup", items=ref("tagt")),
    ] + [
        # blocks 4 and 5: COUNT-ONLY ~hasTag per tag (1 SEEK each) back to EVERY message carrying
        # it, @filter type(post) (1 SEEK), the date read(s), and uid_in(hasCreator, F) -- one
        # hasCreator posting read per candidate. Block 4: pc (ge + lt: 2 date reads); block 5:
        # ic (lt only: 1 date read); a tag survives only if ic == 0.
        Step("back4", "read_neighbors", srcs=ref("tagd"), etype="hasTag", dir="in", pairs=True),
        Step("b4v", "pluck", rows=ref("back4"), field="dst"),
        Step("b4t", "read_filter_by_label_index", vids=ref("b4v"), label="Post"),
        Step("b4d1", "read_vertex_props", vids=ref("b4v"), props=["creationDate"]),
        Step("b4d2", "read_vertex_props", vids=ref("b4v"), props=["creationDate"]),
        # hasCreator uid_in(<friends>): one posting read PER candidate message
        Step("b4a", "read_neighbors", srcs=ref("b4v"), etype="hasCreator", dir="out", pairs=True),
        Step("b4mine", "filter_in", rows=ref("b4a"), field="dst", allowed=ref("friends")),
        Step("b4minev", "pluck", rows=ref("b4mine"), field="src"),
        Step("b4win", "filter", rows=ref("b4d2"),
             pred=and_(notnull(field("creationDate")), le(lo, field("creationDate")), lt(field("creationDate"), hi))),
        Step("b4winv", "pluck", rows=ref("b4win"), field="_vid"),
        Step("b4ok1", "intersect", items=ref("b4winv"), other=ref("b4t")),
        Step("b4ok", "intersect", items=ref("b4ok1"), other=ref("b4minev")),
        Step("pc_rows", "filter_in", rows=ref("back4"), field="dst", allowed=ref("b4ok")),
        Step("pc_src", "pluck", rows=ref("pc_rows"), field="src"),
        Step("pc", "group_count", items=ref("pc_src")),
        # block 5: the same walk, but only the `lt` half of the date test is read
        Step("back5", "read_neighbors", srcs=ref("tagd"), etype="hasTag", dir="in", pairs=True),
        Step("b5v", "pluck", rows=ref("back5"), field="dst"),
        Step("b5t", "read_filter_by_label_index", vids=ref("b5v"), label="Post"),
        Step("b5d", "read_vertex_props", vids=ref("b5v"), props=["creationDate"]),
        Step("b5a", "read_neighbors", srcs=ref("b5v"), etype="hasCreator", dir="out", pairs=True),
        Step("b5mine", "filter_in", rows=ref("b5a"), field="dst", allowed=ref("friends")),
        Step("b5minev", "pluck", rows=ref("b5mine"), field="src"),
        Step("b5old", "filter", rows=ref("b5d"),
             pred=and_(notnull(field("creationDate")), lt(field("creationDate"), lo))),
        Step("b5oldv", "pluck", rows=ref("b5old"), field="_vid"),
        Step("b5ok1", "intersect", items=ref("b5oldv"), other=ref("b5t")),
        Step("b5ok", "intersect", items=ref("b5ok1"), other=ref("b5minev")),
        Step("old_rows", "filter_in", rows=ref("back5"), field="dst", allowed=ref("b5ok")),
        Step("old_src", "pluck", rows=ref("old_rows"), field="src"),
        # block 6: pc > 0 and oldc == 0, ranked
        Step("fresh", "flag_in", rows=ref("pc"), field="item", allowed=ref("old_src"),
             into="used_before"),
        Step("keep", "filter", rows=ref("fresh"), pred=not_(field("used_before"))),
        # block 6: orderdesc val(pc), every tag that passes (no `first`) -- the value-variable sort is in memory
        Step("top", "top_k", rows=ref("keep"), keys=[("count", "desc"), ("item", "asc")], k=None),
        Step("topv", "pluck", rows=ref("top"), field="item"),
        # block 6: tagName: name, for every one (p1: vals=134)
        Step("tnames", "read_vertex_props", vids=ref("topv"), props=["name"]),
        Step("named", "join", left=ref("top"), right=ref("tnames"),
             left_on="item", right_on="_vid"),
        # the LDBC client's (postCount desc, tagName asc) LIMIT 10: in memory
        Step("out", "top_k", rows=ref("named"),
             keys=[("count", "desc"), ("name", "asc")], k=10),
    ]


# Arango's IC4 builds `friendPosts` once and then scans that LIST twice -- once for the
# in-window tags, once for the tags used before. Unlike nebula and dgraph it does NOT re-walk
# the graph for the second pass: the re-read is over an in-memory array, so only the hasTag
# traversals are paid twice, not the friend/message expansion.
#
#   LET person = DOCUMENT(...)                          G=2
#   friendPosts: knows ANY -> f ; hasCreator INBOUND f  S = open + 2 + |friends|
#                RETURN { post }  -- the WHOLE document, so every message is materialized
#   windowTags:  hasTag OUTBOUND each in-window post    S = |in-window posts|
#   oldTagIds:   hasTag OUTBOUND each older post        S = |older posts|
#
# STATUS: S, G and N exact at every param (sf0.3 and sf0.1); G needs [5]'s friend documents
# (vertexProjections [_id]) and [6]'s postFilter reads (traverse_filtered).
def ic4_arangodb_engine(args):
    p = person_vid(args["person"])
    lo = int(args["startDate"])
    hi = lo + int(args["durationDays"]) * 86400000
    return [
        # [2] LET person = DOCUMENT(...): pk + doc (docs_per_step, because read_vertex_props would
        # charge nothing for the key-derived `id` -- the engine fetches the document anyway)
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        Step("mev", "pluck", rows=ref("me"), field="_vid"),
        # [5] TraversalNode knows IN,OUT 1..1, vertexProjections [_id]: the first traversal's
        # open SEEK, 2 SEEK + 1 NEXT per edge, and each friend's document per step
        Step("open", "traversal_open", items=ref("mev"), etype="knows"),
        Step("fr", "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),
        Step("frdocs", "docs_per_step", vids=ref("fr"), label="Person", props=["id"]),
        # [6] TraversalNode hasCreator INBOUND f, no vertexProjections (RETURN post), postFilter
        # IS_SAME_COLLECTION("Post", post): a filtered traversal (traverse_filtered)
        Step("posts", "traverse_filtered", srcs=ref("fr"), etype="hasCreator", dir="in",
             depth=1, props=["id", "creationDate"], label=MESSAGE_LABELS,
             pred=eq(get("_label"), "Post")),
        # [13] EnumerateList friendPosts FILTER in-window: in memory
        Step("win", "filter", rows=ref("posts"),
             pred=and_(notnull(get("creationDate")), le(lo, field("creationDate")), lt(field("creationDate"), hi))),
        Step("winv", "pluck", rows=ref("win"), field="_vid"),
        # [17] TraversalNode hasTag OUTBOUND fp.post, no vertexProjections (COLLECT t = tag
        # groups on the document): 1 SEEK + 1 NEXT, and the tag document per step, no dedup
        Step("wtags", "read_neighbors", srcs=ref("winv"), etype="hasTag", dir="out", pairs=True),
        Step("wtv", "pluck", rows=ref("wtags"), field="dst"),
        Step("tnames", "docs_per_step", vids=ref("wtv"), label="Tag", props=["id", "name"]),
        # [23] EnumerateList friendPosts FILTER before-window: in memory
        Step("old", "filter", rows=ref("posts"),
             pred=and_(notnull(get("creationDate")), lt(field("creationDate"), lo))),
        Step("oldv", "pluck", rows=ref("old"), field="_vid"),
        # [27] TraversalNode hasTag OUTBOUND fp.post, vertexProjections [_id]: 1 SEEK + 1 NEXT,
        # and the tag document per step
        Step("otags", "read_neighbors", srcs=ref("oldv"), etype="hasTag", dir="out", pairs=True),
        Step("otv", "pluck", rows=ref("otags"), field="dst"),
        # (`RETURN DISTINCT tag._id` dedups the result, not the reads)
        Step("otd", "docs_per_step", vids=ref("otv"), label="Tag", props=["id"]),
        # [18] COLLECT WITH COUNT, [29] DISTINCT, [33]-[40] FILTER, SORT, LIMIT 10: in memory
        Step("cnt", "group_count", items=ref("wtv")),
        Step("fresh", "flag_in", rows=ref("cnt"), field="item", allowed=ref("otv"),
             into="used_before"),
        Step("keep", "filter", rows=ref("fresh"), pred=not_(field("used_before"))),
        Step("tnamed", "dedup_rows", rows=ref("tnames"), field="_vid"),
        Step("named", "join", left=ref("keep"), right=ref("tnamed"),
             left_on="item", right_on="_vid"),
        Step("out", "top_k", rows=ref("named"),
             keys=[("count", "desc"), ("name", "asc")], k=10),
    ]


# single candidate: the logical plan IS the physical one (passthrough search)
LOGICAL = {"ic4": ic4}


ENGINE = {
    "ic4": {"nebula": ic4_nebula_engine, "dgraph": ic4_dgraph_engine, "arangodb": ic4_arangodb_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic4": _KNOWS_HINTS}
