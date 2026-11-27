"""LDBC IC6 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import param, let
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (MESSAGE_LABELS, _friend_set, _scan_lookup, person_vid, PERSON_PARAM,
                                                 PERSON_LET, _anchor_person)


# ------------------------------------------------------------------------ IC6

def _ic6_steps(args, label_only=False, logical=False):
    """IC6: tags co-occurring with `tagName` on friends+fof posts -> top 10 by
    (postCount desc, tagName asc).

    Entry: read_index_eq(Tag.name) then INTERSECT (posts-with-tag x friend-messages)
    before the type check, so read_filter_by_label_probe (dgraph: a dgraph.type SEEK per
    vid; nebula: a tag-probe GET per vid; arango: FREE, collection-qualified
    ids) runs on the small intersection only — the enumerate-all-posts
    alternative is priced symmetrically in ic4's docstring and loses the same
    way here. Recorded candidate not run: skip the tag->posts reverse scan and
    instead scan each friend post's own hasTag list -- cheaper only when the
    tag is very popular relative to the friend set's post count.

    label_only: the Tag.name lookup becomes read_vertices_by_label(Tag) + name read +
    filter instead of the read_index_eq SEEK (the label-index-only design)."""
    p = let("me_vid")
    if logical:                                  # the optimizer's entry choice point
        tag_entry = [Step("tag", "entry_eq", label="Tag", prop="name", value=param("tagName"))]
    else:
        tag_entry = (_scan_lookup("tag", "Tag", "name", param("tagName")) if label_only
                     else [Step("tag", "read_index_eq", label="Tag", prop="name", value=param("tagName"))])
    return Plan(_friend_set(p, 2) + [
        # narrow to friend/fof messages that carry the query tag, keep only the Posts
        Step("fmsgs",     "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in"),
    ] + tag_entry + [
        Step("tag_msgs",  "read_neighbors", srcs=ref("tag"), etype="hasTag", dir="in"),
        Step("fm_tagged", "intersect", items=ref("fmsgs"), other=ref("tag_msgs")),
        Step("posts",     "filter_by_label", vids=ref("fm_tagged"), label="Post"),
        # tags co-occurring on those posts (the query tag itself removed), counted
        Step("alltags",   "read_neighbors", srcs=ref("posts"), etype="hasTag", dir="out"),
        Step("cotags",    "exclude", items=ref("alltags"), remove=ref("tag")),
        Step("counts",    "group_count", items=ref("cotags")),
        # name is the SECONDARY sort key (tagName asc), so it can't defer past the limit -- but it
        # CAN defer past count: keep only tags whose count ties the 10th (keep_ties), read names for
        # that narrowed set, then break count-ties by name. Reads ~10-20 names, not every tag.
        Step("cand_tags", "top_k", rows=ref("counts"), keys=[("count", "desc")], k=10, keep_ties=True),
        Step("cand_vids", "pluck", rows=ref("cand_tags"), field="item"),
        Step("tag_names", "read_vertex_props", vids=ref("cand_vids"), props=["name"], label="Tag"),
        Step("named",     "join", left=ref("cand_tags"), right=ref("tag_names"), left_on="item", right_on="_vid"),
        Step("top10",     "top_k", rows=ref("named"), keys=[("count", "desc"), ("name", "asc")],
             k=10),
        Step("labeled",   "rename", rows=ref("top10"), names={"name": "tagName", "count": "postCount"}),
        Step("out",       "project", rows=ref("labeled"), fields=["tagName", "postCount"]),
    ], params={**PERSON_PARAM, "tagName": {}}, let=PERSON_LET)


def ic6_logical(args):
    """The LOGICAL ic6 for the optimizer's search: one entry_eq choice point
    (Tag.name index vs label scan)."""
    return _ic6_steps(args, logical=True)


LOGICAL = {"ic6": ic6_logical}


def ic6(args):
    return _ic6_steps(args)


def ic6_labelonly(args):
    return _ic6_steps(args, label_only=True)


SIM = {
    "ic6": ic6
}

LABEL_ONLY = {
    "ic6": ic6_labelonly
}


# ── IC6 engine-faithful plans ──────────────────────────────────────────────────────────────────
# Each builder transcribes the engine's captured sf0.3 plan
# (ldbc/<db>/ic6/p1/plan.txt). Every KV step cites the plan
# node it models; in-memory steps cost nothing and are grouped.

def ic6_dgraph_engine(args):
    """IC6 dgraph, from dgraph/ic6/p1/plan.txt (blocks 0-10; p7/p13/p19 captured, same tree).

    The cost is block 7: `PP as ~hasCreator @filter(type(post) AND uid_in(hasTag, uid(K)))`.
    Both conjuncts of the AND see the whole input (the plan: uid_in src=429229 at p1), so the
    engine reads the hasTag posting of EVERY message of every friend and friend-of-friend --
    comments too -- to find the ~44 posts carrying the tag. The optimal plan enters from the tag
    instead (its reverse hasTag posting) and intersects.

    Block 10 returns EVERY tag with a count (no `first`) and reads each one's name; the client ranks them by
    (postCount desc, tagName asc) and keeps 10 -- LDBC's answer.
    """
    p = person_vid(args["person"])
    return [
        # block 0: K as eq(name, $tagName) @filter(type(tag)) -- name index SEEK + type SEEK
        Step("tag_ix", "read_index_eq", label="Tag", prop="name", value=args["tagName"]),
        Step("K", "read_filter_by_label_index", vids=ref("tag_ix"), label="Tag"),
    ] + _anchor_person(p) + [                          # block 1: eq(id) + type(person)
        # block 2: _l1 { knows  ~knows } -- 1 SEEK per direction
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("l1d", "dedup", items=ref("l1")),
        # block 3: F1 as uid(l1a, l1b) @filter(type(person) AND NOT uid(P))
        Step("l1p", "read_filter_by_label_index", vids=ref("l1d"), label="Person"),
        Step("F1", "exclude", items=ref("l1p"), remove=ref("me")),
        # block 4: _l2(func: uid(l1a, l1b)) { knows  ~knows } -- from ALL of level 1
        Step("l2", "read_neighbors", srcs=ref("l1d"), etype="knows", dir="both"),
        Step("l2d", "dedup", items=ref("l2")),
        # block 5: F2 as uid(l2a, l2b) @filter(type(person) AND NOT uid(P, F1))
        Step("l2p", "read_filter_by_label_index", vids=ref("l2d"), label="Person"),
        Step("l2x", "exclude", items=ref("l2p"), remove=ref("me")),
        Step("F2", "exclude", items=ref("l2x"), remove=ref("F1")),
        # block 6: F as uid(F1, F2) -- in memory
        Step("F12", "concat", a=ref("F1"), b=ref("F2")),
        Step("F", "dedup", items=ref("F12")),
        # block 7: ~hasCreator from F -- 1 SEEK per person
        Step("fmsgs", "read_neighbors", srcs=ref("F"), etype="hasCreator", dir="in"),
        # block 7 filter: type(post) -- 1 type-index SEEK; uid_in(hasTag, uid(K)) -- the hasTag
        # posting of EVERY message (1 SEEK each)
        Step("fpost", "read_filter_by_label_index", vids=ref("fmsgs"), label="Post"),
        Step("fht", "read_neighbors", srcs=ref("fmsgs"), etype="hasTag", dir="out", pairs=True),
        Step("fhtK", "filter_in", rows=ref("fht"), field="dst", allowed=ref("K")),
        Step("withK", "pluck", rows=ref("fhtK"), field="src"),
        Step("PP", "intersect", items=ref("fpost"), other=ref("withK")),
        # block 8: TT as hasTag @filter(type(tag) AND NOT uid(K)), from PP -- 1 SEEK per post
        # (the posting is read again: block 7's read is not kept), + 1 type-index SEEK
        Step("tt", "read_neighbors", srcs=ref("PP"), etype="hasTag", dir="out"),
        Step("ttd", "dedup", items=ref("tt")),
        Step("ttt", "read_filter_by_label_index", vids=ref("ttd"), label="Tag"),
        Step("TT", "exclude", items=ref("ttt"), remove=ref("K")),
        # block 9: pc as count(~hasTag @filter(uid(PP))), per tag of TT -- 1 SEEK per tag (the
        # whole reverse posting is read to intersect it with PP), count in memory
        Step("rev", "read_neighbors", srcs=ref("TT"), etype="hasTag", dir="in", pairs=True),
        Step("revPP", "filter_in", rows=ref("rev"), field="dst", allowed=ref("PP")),
        Step("revt", "pluck", rows=ref("revPP"), field="src"),
        Step("pc", "group_count", items=ref("revt")),
        # block 10: tags(func: uid(TT), orderdesc: val(pc)) @filter(gt(val(pc), 0)): every tag with
        # a count; the value-variable sort is in memory (query/query.go sortAndPaginateUsingVar)
        Step("ranked", "top_k", rows=ref("pc"), keys=[("count", "desc"), ("item", "asc")], k=None),
        Step("rankedv", "pluck", rows=ref("ranked"), field="item"),
        # block 10: tagName: name [vals = every tag] -- 1 GET each
        Step("names", "read_vertex_props", vids=ref("rankedv"), props=["name"]),
        Step("named", "join", left=ref("ranked"), right=ref("names"), left_on="item"),
        # the client (kvengines.ldbc.parsers.dgraph DgraphLDBCParser._parse_ic6): sort (postCount desc, tagName asc),
        # keep 10 -- in memory
        Step("top10", "top_k", rows=ref("named"), keys=[("count", "desc"), ("name", "asc")],
             k=10),
        Step("labeled", "rename", rows=ref("top10"), names={"name": "tagName", "count": "postCount"}),
        Step("out", "project", rows=ref("labeled"), fields=["tagName", "postCount"]),
    ]


def ic6_arangodb_engine(args):
    """IC6 arango, from arangodb/ic6/p1/plan.txt.

    The optimizer inlined the `friends` subquery: [11] walks knows 1..2 (one row per path,
    postFilter f._id != start), [15] COLLECT dedups to distinct friends, [19] DOCUMENT(f)
    fetches each one. Then every friend's messages are fetched ([20], postFilter Post), every
    post's tags are fetched ([23], postFilter tag == target), and the matching posts' tags
    are fetched again ([26]). The target tag's own hasTag list (the cheap entry) is never used.

    sf0.3 vs live: S, G and N exact at all 6 params (p1: S=129094 G=1596409 N=538088), rows
    match the optimal plan and the live engine (`validate.py --sf 0.3 --db arangodb --query ic6
    --live`)."""
    p = person_vid(args["person"])
    tn = args["tagName"]
    return [
        # [37] IndexNode Tag persistent(name) == @tagName, no projections, [36] LIMIT 1:
        # 1 SEEK + 1 NEXT + the document
        Step("tag", "read_index_eq", label="Tag", prop="name", value=tn),
        # [11] TraversalNode knows IN,OUT 1..2 from the start string, vertexProjections [_id],
        # postFilter f._id != start: the first traversal (+1 open SEEK), traverse_filtered
        Step("open", "traversal_open", items=[p], etype="knows"),
        Step("fr", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both", depth=2,
             props=["id"], label="Person", pred=E.ne(E.field("_vid"), p)),
        # [14]/[15] COLLECT f = f._id (RETURN DISTINCT): in memory
        Step("frv", "pluck", rows=ref("fr"), field="_vid"),
        Step("f", "dedup", items=ref("frv")),
        # [19] DOCUMENT(f) per distinct friend: pk + doc
        Step("fdoc", "docs_per_step", vids=ref("f"), label="Person", props=["id"]),
        # [20] TraversalNode hasCreator INBOUND DOCUMENT(f), no vertexProjections, postFilter
        # IS_SAME_COLLECTION('Post', post): traverse_filtered
        Step("ps", "traverse_filtered", srcs=ref("f"), etype="hasCreator", dir="in", depth=1,
             props=["id"], label=MESSAGE_LABELS, pred=E.eq(E.get("_label"), "Post")),
        # [23] TraversalNode hasTag OUTBOUND post, vertexProjections [_id], postFilter
        # tag._id == targetTag._id: traverse_filtered over every post
        Step("psv", "pluck", rows=ref("ps"), field="_vid"),
        Step("hit", "traverse_filtered", srcs=ref("psv"), etype="hasTag", dir="out", depth=1,
             props=["id"], label="Tag", pred=E.lit(True), allowed=ref("tag")),
        # [26] TraversalNode hasTag OUTBOUND post, once per matching post row, no
        # vertexProjections, postFilter otherTag._id != targetTag._id: traverse_filtered
        Step("hitp", "pluck", rows=ref("hit"), field="_src"),
        Step("ot", "traverse_filtered", srcs=ref("hitp"), etype="hasTag", dir="out", depth=1,
             props=["name"], label="Tag", pred=E.lit(True), excluded=ref("tag")),
        # [29] COLLECT ot = otherTag WITH COUNT, [30]-[32] SORT postCount DESC,
        # TO_STRING(ot.name) ASC LIMIT 10: in memory
        Step("otv", "pluck", rows=ref("ot"), field="_vid"),
        Step("counts", "group_count", items=ref("otv")),
        Step("names", "dedup_rows", rows=ref("ot"), field="_vid"),
        Step("named", "join", left=ref("counts"), right=ref("names"), left_on="item",
             right_on="_vid"),
        Step("named_s", "derive", rows=ref("named"), into="tagName",
             fn=E.field("name")),   # TO_STRING(ot.name): a tag name is already a string
        Step("top10", "top_k", rows=ref("named_s"), keys=[("count", "desc"), ("tagName", "asc")],
             k=10),
        Step("labeled", "rename", rows=ref("top10"), names={"count": "postCount"}),
        Step("out", "project", rows=ref("labeled"), fields=["tagName", "postCount"]),
    ]


# ── nebula ──────────────────────────────────────────────────────────────────────────────────────

def ic6_nebula_engine(args):
    """IC6 Nebula, from nebula/ic6/p1/plan.txt. Three MATCH clauses, joined in memory:

    node 33 Traverse knows BOTH 1..2 (dedup: 1, vertexProps = all 8 tags): per step, 8 GETs per
      vertex it expands FROM (the start's Person row answers the first-step filter), 1 SEEK per
      direction + 1 NEXT per edge -- the IC1 shape, two steps. node 39 AppendVertices: all 8 tags
      of every distinct path end; 40 Filter person != friend; 9 Dedup.
    node 11 Traverse hasCreator IN per friend (vertexProps = all 8 tags): 8 GETs + 1 SEEK per
      friend, 1 NEXT per message.
    node 35 Traverse hasTag OUT per message (first-step Post._tag: 1 GET per message; the edges of
      every message are read, the filter drops the Comment rows -- the IC12 node-29 rule).
    node 36 AppendVertices: all 8 tags of every distinct tag on those Posts; 42 Filter name.
    node 18 Traverse hasTag OUT per matching Post (no filter); node 37 AppendVertices: all 8 tags of
      every distinct other tag. In memory: 21 join, 22 other != known, 23 count(DISTINCT post) per
      tag name, 26 TopN 10 (postCount desc, tagName asc)."""
    p = person_vid(args["person"])
    tn = args["tagName"]
    return [
        Step("me", "read_vertex_by_id", vid=p),
        Step("src0", "all_tag_props", vids=ref("me")),                             # node 33 step 1
        Step("e1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("L1", "dedup", items=ref("e1")),
        Step("src1", "all_tag_props", vids=ref("L1")),                             # node 33 step 2
        Step("e2", "read_neighbors", srcs=ref("L1"), etype="knows", dir="both"),
        Step("L2d", "dedup", items=ref("e2")),
        Step("L2", "exclude", items=ref("L2d"), remove=ref("me")),                 # trail rule (IC1)
        Step("ends0", "concat", a=ref("L1"), b=ref("L2")),
        Step("ends", "dedup", items=ref("ends0")),
        Step("endp", "all_tag_props", vids=ref("ends")),                           # node 39
        Step("friends", "exclude", items=ref("ends"), remove=ref("me")),           # nodes 40, 9
        Step("fsrc", "all_tag_props", vids=ref("friends")),                        # node 11 vertexProps
        Step("fm", "read_neighbors", srcs=ref("friends"), etype="hasCreator", dir="in"),   # node 11
        Step("msgs", "dedup", items=ref("fm")),
        Step("posts", "tag_probe", vids=ref("msgs"), label="Post"),                # node 35 filter
        Step("pt0", "read_neighbors", srcs=ref("msgs"), etype="hasTag", dir="out", pairs=True),
        Step("pt", "filter_in", rows=ref("pt0"), field="src", allowed=ref("posts")),   # Post rows only
        Step("tv", "pluck", rows=ref("pt"), field="dst"),
        Step("tvd", "dedup", items=ref("tv")),
        Step("tagp", "all_tag_props", vids=ref("tvd")),                            # node 36
        Step("known", "filter", rows=ref("tagp"), pred=E.eq(E.get("name"), tn)),  # node 42
        Step("knownv", "pluck", rows=ref("known"), field="_vid"),
        Step("kp", "filter_in", rows=ref("pt"), field="dst", allowed=ref("knownv")),
        Step("kpv", "pluck", rows=ref("kp"), field="src"),
        Step("kposts", "dedup", items=ref("kpv")),
        Step("ot", "read_neighbors", srcs=ref("kposts"), etype="hasTag", dir="out", pairs=True),  # node 18
        Step("otv", "pluck", rows=ref("ot"), field="dst"),
        Step("otd", "dedup", items=ref("otv")),
        Step("otp", "all_tag_props", vids=ref("otd")),                             # node 37
        Step("other", "exclude", items=ref("otv"), remove=ref("knownv")),          # node 22
        Step("counts", "group_count", items=ref("other")),                         # node 23
        Step("named", "join", left=ref("counts"), right=ref("otp"), left_on="item", right_on="_vid"),
        Step("top10", "top_k", rows=ref("named"), keys=[("count", "desc"), ("name", "asc")], k=10),
        Step("labeled", "rename", rows=ref("top10"), names={"name": "tagName", "count": "postCount"}),
        Step("out", "project", rows=ref("labeled"), fields=["tagName", "postCount"]),
    ]


ENGINE = {
    "ic6": {"arangodb": ic6_arangodb_engine, "nebula": ic6_nebula_engine, "dgraph": ic6_dgraph_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic6": _KNOWS_HINTS}
