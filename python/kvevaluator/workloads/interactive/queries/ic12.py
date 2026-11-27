"""LDBC IC12 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import param, let
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (ENGINE_WALK_CAP, MESSAGE_LABELS, PERSON_RENAME, _anchor_person,
                                                 _friend_set, _scan_lookup, person_vid, PERSON_PARAM, PERSON_LET)


# ----------------------------------------------------------------------- IC12

def ic12(args, closure_cap=None, label_only=False, logical=False):
    """IC12 expert search: friends' comments that reply to POSTS tagged with a
    tag whose class is `tagClassName` or a descendant -> top 20 friends by
    (replyCount desc, personId asc) + the matching tag names.
    replyOf-out doubles as the comment filter (posts pay one empty read);
    the tagclass subtree is a `read_closure` over reversed isSubclassOf (level-BFS
    of read_neighbors calls); parent posts get a read_filter_by_label_probe cut (a reply to a
    COMMENT never counts). closure_cap: None = the full subtree; 3 = the engines'
    capped approximation (ic12_capped; dgraph's 3-level unroll -- misses classes
    deeper than 3, so their tags/messages are undercounted).
    label_only: the TagClass.name lookup becomes read_vertices_by_label(TagClass) + name
    read + filter instead of the read_index_eq SEEK (the label-index-only design)."""
    p = let("me_vid")
    if logical:                                  # the optimizer's entry choice point
        tc_entry = [Step("tagclass", "entry_eq", label="TagClass", prop="name",
                         value=param("tagClassName"))]
    else:
        tc_entry = (_scan_lookup("tagclass", "TagClass", "name", param("tagClassName")) if label_only
                    else [Step("tagclass", "read_index_eq", label="TagClass", prop="name",
                               value=param("tagClassName"))])
    return Plan(_friend_set(p, 1) + [
        Step("fmsgs",       "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in", pairs=True),
        Step("fmsg_v",      "pluck", rows=ref("fmsgs"), field="dst"),
        Step("replies",     "read_neighbors", srcs=ref("fmsg_v"), etype="replyOf", dir="out", pairs=True),
        Step("parentv",     "pluck", rows=ref("replies"), field="dst"),
        Step("parents",     "dedup", items=ref("parentv")),
        Step("posts",       "filter_by_label", vids=ref("parents"), label="Post"),
    ] + tc_entry + [
        Step("tc_sub",      "read_closure", srcs=ref("tagclass"), etype="isSubclassOf", dir="in", max_levels=closure_cap),
        Step("tc_tags",     "read_neighbors", srcs=ref("tc_sub"), etype="hasType", dir="in"),
        Step("tags",        "dedup", items=ref("tc_tags")),
        Step("post_tags",   "read_neighbors", srcs=ref("posts"), etype="hasTag", dir="out", pairs=True),
        Step("matched",     "filter_in", rows=ref("post_tags"), field="dst", allowed=ref("tags")),
        Step("matched_p",   "pluck", rows=ref("matched"), field="src"),
        Step("matched_posts","dedup", items=ref("matched_p")),
        Step("replies_r",   "rename", rows=ref("replies"), names={"src": "_comment", "dst": "_parent"}),
        Step("good_replies","filter_in", rows=ref("replies_r"), field="_parent", allowed=ref("matched_posts")),
        Step("fmsg_r",      "rename", rows=ref("fmsgs"), names={"src": "friend", "dst": "_c2"}),
        Step("friend_reps", "join", left=ref("good_replies"), right=ref("fmsg_r"), left_on="_comment",
             right_on="_c2"),
        Step("friendv",     "pluck", rows=ref("friend_reps"), field="friend"),
        Step("reply_count", "group_count", items=ref("friendv")),
        Step("counted_v",   "pluck", rows=ref("reply_count"), field="item"),
        Step("pid",         "read_vertex_props", vids=ref("counted_v"), props=["id"], label="Person"),
        Step("pid_named",   "rename", rows=ref("pid"), names={"id": "personId"}),
        Step("counted",     "zip_rows", left=ref("reply_count"), right=ref("pid_named")),
        Step("top20",       "top_k", rows=ref("counted"), keys=[("count", "desc"), ("personId", "asc")],
             k=20),
        # firstName/lastName are projection-only (count/personId rank the set): defer to the 20
        # winners instead of reading them for every friend who replied. Mirrors IC5.
        Step("topv",        "pluck", rows=ref("top20"), field="item"),
        Step("names",       "read_vertex_props", vids=ref("topv"), props=["firstName", "lastName"],
             label="Person"),
        Step("names_r",     "rename", rows=ref("names"),
             names={"firstName": "personFirstName", "lastName": "personLastName"}),
        Step("named",       "join", left=ref("top20"), right=ref("names_r"), left_on="item", right_on="_vid"),
        # tag names, only for the winners' matches
        Step("winner_v",    "pluck", rows=ref("named"), field="item"),
        Step("winner_reps", "filter_in", rows=ref("friend_reps"), field="friend", allowed=ref("winner_v")),
        Step("matched_r",   "rename", rows=ref("matched"), names={"src": "_p3", "dst": "_tag"}),
        Step("winner_tags", "join_multi", left=ref("winner_reps"), right=ref("matched_r"), left_on="_parent",
             right_on="_p3"),
        Step("winner_tagv", "pluck", rows=ref("winner_tags"), field="_tag"),
        Step("winner_tagd", "dedup", items=ref("winner_tagv")),
        Step("tag_names",   "read_vertex_props", vids=ref("winner_tagd"), props=["name"], label="Tag"),
        Step("tag_named",   "rename", rows=ref("tag_names"), names={"name": "tagName"}),
        Step("winner_named","join", left=ref("winner_tags"), right=ref("tag_named"), left_on="_tag",
             right_on="_vid"),
        Step("nested",      "nest", rows=ref("named"), children=ref("winner_named"), on="item",
             child_on="friend", into="tagNames", fields=["tagName"]),
        Step("labeled",     "rename", rows=ref("nested"), names={"count": "replyCount"}),
        Step("out",         "project", rows=ref("labeled"),
             fields=["personId", "personFirstName", "personLastName",
                     "tagNames", "replyCount"]),
    ], params={**PERSON_PARAM, "tagClassName": {}}, let=PERSON_LET)


def ic12_logical(args):
    """The LOGICAL ic12 for the optimizer's search: one entry_eq choice point
    (TagClass.name index vs label scan)."""
    return ic12(args, logical=True)


def ic12_capped_logical(args):
    """The LOGICAL ic12_capped (the engines' 3-level subtree approximation);
    same entry choice point."""
    return ic12(args, closure_cap=ENGINE_WALK_CAP, logical=True)


LOGICAL = {"ic12": ic12_logical, "ic12_capped": ic12_capped_logical}


def ic12_capped(args):
    """ic12 with the tagClass isSubclassOf subtree capped at 3 levels (dgraph's unroll).
    Misses descendant classes deeper than 3 (and their tags/messages)."""
    return ic12(args, closure_cap=ENGINE_WALK_CAP)


def ic12_labelonly(args):
    """ic12 with only the label index: the TagClass.name lookup scans TagClass +
    filters instead of the read_index_eq SEEK (the label-index-only design)."""
    return ic12(args, label_only=True)


SIM = {
    "ic12": ic12,
    "ic12_capped": ic12_capped
}

LABEL_ONLY = {
    "ic12": ic12_labelonly
}


# ── IC12 engine-faithful plans ─────────────────────────────────────────────────────────────────
# IC12 = expert search: direct friends' Comments in single-hop reply to Posts tagged with a Tag in
# $tagClassName or a DESCENDANT TagClass -> top 20 friends by (replyCount desc, friendId asc) + the
# matching Tag names. Transcribed from the captured sf0.3 plan
# (ldbc/<db>/ic12/p1/plan.txt); every KV step cites its plan block.

def ic12_dgraph_engine(args):
    """IC12 dgraph, from dgraph/ic12/p1/plan.txt (blocks 0-10 of the DQL).

    The engine enters the tag set from the TAGS, not the classes: it scans every subject of
    hasType and reads each one's hasType posting for uid_in (block 3). P0 is then the posts
    carrying a tag in T (block 4). `uid_in(replyOf, uid(P0))` reads one replyOf posting per
    message, over EVERY message of the friends in block 8 and again over every message of the
    winners in block 10 (both conjuncts of an AND filter see the whole input).

    The subclass walk is the live query's uncapped `@recurse` (the 3-level unroll is commented
    out in the DQL). At sf0.3 every test param's tagClass is a leaf, so read_closure reads one
    empty posting.

    sf0.3 vs live: S and N exact at every param (p1: S = 16080 + 12557 + 10550 + 43 small reads).
    G is 4 low at every param (live = the modelled scalar GETs + 4). Unexplained; the IC7 and IS7
    dgraph cells show a similar +2 G constant. Needs a live /kvtrace of the GET keys."""
    p = person_vid(args["person"])
    tcn = args["tagClassName"]
    steps = [
        # block 0: BaseTC as eq(name, $tagClassName) @filter(type(tagclass))
        Step("tc_ix", "read_index_eq", label="TagClass", prop="name", value=tcn),
        Step("base_tc", "read_filter_by_label_index", vids=ref("tc_ix"), label="TagClass"),
        # blocks 1-2: TCAll = BaseTC + @recurse(loop: false) ~isSubclassOf (uncapped)
        Step("tc_all", "read_closure", srcs=ref("base_tc"), etype="isSubclassOf", dir="in"),
        # block 3: T as has(hasType) @filter(type(tag) AND uid_in(hasType, uid(TCAll))) -- the
        # scan, then both conjuncts over all 16080 scanned subjects
        Step("ht_subj", "has_scan", etype="hasType"),
        Step("ht_tag", "read_filter_by_label_index", vids=ref("ht_subj"), label="Tag"),
        Step("ht_rows", "read_neighbors", srcs=ref("ht_subj"), etype="hasType", dir="out",
             pairs=True),
        Step("ht_in", "filter_in", rows=ref("ht_rows"), field="dst", allowed=ref("tc_all")),
        Step("ht_v", "pluck", rows=ref("ht_in"), field="src"),
        Step("T", "intersect", items=ref("ht_v"), other=ref("ht_tag")),
        # block 4: P0 as ~hasTag @filter(type(post)), from each tag in T
        Step("tagged", "read_neighbors", srcs=ref("T"), etype="hasTag", dir="in"),
        Step("P0", "read_filter_by_label_index", vids=ref("tagged"), label="Post"),
    ] + _anchor_person(p) + [                     # block 5: eq(id) + type(person)
        # block 6: _l1 { a as knows  b as ~knows }
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        # block 7: F as uid(a, b) @filter(type(person) AND NOT uid(P))
        Step("l1d", "dedup", items=ref("l1")),
        Step("l1p", "read_filter_by_label_index", vids=ref("l1d"), label="Person"),
        Step("F", "exclude", items=ref("l1p"), remove=ref("me")),
        # block 8: var(func: uid(F)) @filter(type(person))
        Step("Fp", "read_filter_by_label_index", vids=ref("F"), label="Person"),
        # block 8: rc as count(~hasCreator @filter(type(comment) AND uid_in(replyOf, uid(P0))))
        Step("fm", "read_neighbors", srcs=ref("Fp"), etype="hasCreator", dir="in", pairs=True),
        Step("fm_v", "pluck", rows=ref("fm"), field="dst"),
        Step("fm_c", "read_filter_by_label_index", vids=ref("fm_v"), label="Comment"),
        Step("fm_r", "read_neighbors", srcs=ref("fm_v"), etype="replyOf", dir="out", pairs=True),
        # in memory: keep comments replying to P0, count per friend
        Step("fm_rp", "filter_in", rows=ref("fm_r"), field="dst", allowed=ref("P0")),
        Step("fm_rc", "filter_in", rows=ref("fm_rp"), field="src", allowed=ref("fm_c")),
        Step("fm_rr", "rename", rows=ref("fm_rc"), names={"src": "_comment", "dst": "_parent"}),
        Step("fm_n", "rename", rows=ref("fm"), names={"src": "friend", "dst": "_c2"}),
        Step("reps", "join", left=ref("fm_rr"), right=ref("fm_n"), left_on="_comment",
             right_on="_c2"),
        Step("reps_f", "pluck", rows=ref("reps"), field="friend"),
        Step("rc", "group_count", items=ref("reps_f")),
        # block 9: idv as id, one GET per friend (ord = math(...) is in memory)
        Step("F_id", "read_vertex_props", vids=ref("Fp"), props=["id"]),
        # block 10: friends(func: uid(F), orderdesc: val(ord), first: 20) @filter(gt(val(rc), 0))
        # -- in memory; ord orders by (rc desc, id asc)
        Step("F_idn", "rename", rows=ref("F_id"), names={"id": "personId"}),
        Step("rc_id", "join", left=ref("rc"), right=ref("F_idn"), left_on="item", right_on="_vid"),
        Step("W", "top_k", rows=ref("rc_id"), keys=[("count", "desc"), ("personId", "asc")], k=20),
        Step("W_v", "pluck", rows=ref("W"), field="item"),
        # block 10: friendId: id, friendFirstName: firstName, friendLastName: lastName
        Step("W_props", "read_vertex_props", vids=ref("W_v"), props=["id", "firstName", "lastName"]),
        # block 10: ~hasCreator @filter(type(comment) AND uid_in(replyOf, uid(P0))) -- re-run
        # for the winners
        Step("wm", "read_neighbors", srcs=ref("W_v"), etype="hasCreator", dir="in", pairs=True),
        Step("wm_v", "pluck", rows=ref("wm"), field="dst"),
        Step("wm_c", "read_filter_by_label_index", vids=ref("wm_v"), label="Comment"),
        Step("wm_r", "read_neighbors", srcs=ref("wm_v"), etype="replyOf", dir="out", pairs=True),
        Step("wm_rp", "filter_in", rows=ref("wm_r"), field="dst", allowed=ref("P0")),
        Step("wm_rc", "filter_in", rows=ref("wm_rp"), field="src", allowed=ref("wm_c")),
        # block 10: replyOf @filter(type(post)) of each kept comment
        Step("wk_v", "pluck", rows=ref("wm_rc"), field="src"),
        Step("wk_r", "read_neighbors", srcs=ref("wk_v"), etype="replyOf", dir="out", pairs=True),
        Step("wk_pv", "pluck", rows=ref("wk_r"), field="dst"),
        Step("wk_p", "read_filter_by_label_index", vids=ref("wk_pv"), label="Post"),
        # block 10: hasTag @filter(uid(T)) of each parent post
        Step("wp_t", "read_neighbors", srcs=ref("wk_p"), etype="hasTag", dir="out", pairs=True),
        Step("wp_tT", "filter_in", rows=ref("wp_t"), field="dst", allowed=ref("T")),
        # block 10: tagNames: name
        Step("tag_v", "pluck", rows=ref("wp_tT"), field="dst"),
        Step("tag_names", "read_vertex_props", vids=ref("tag_v"), props=["name"]),
        # in memory: the client's @normalize flatten + tag dedup, and the LDBC projection
        Step("W_pn", "rename", rows=ref("W_props"),
             names={"firstName": "personFirstName", "lastName": "personLastName", "id": "_wid"}),
        Step("named", "join", left=ref("W"), right=ref("W_pn"), left_on="item", right_on="_vid"),
        Step("wk_rr", "rename", rows=ref("wk_r"), names={"src": "_comment", "dst": "_parent"}),
        Step("wm_n", "rename", rows=ref("wm"), names={"src": "friend", "dst": "_c2"}),
        Step("w_reps", "join", left=ref("wk_rr"), right=ref("wm_n"), left_on="_comment",
             right_on="_c2"),
        Step("wp_tr", "rename", rows=ref("wp_tT"), names={"src": "_p3", "dst": "_tag"}),
        Step("w_tags", "join_multi", left=ref("w_reps"), right=ref("wp_tr"), left_on="_parent",
             right_on="_p3"),
        Step("tag_named", "rename", rows=ref("tag_names"), names={"name": "tagName"}),
        Step("w_tagn", "join", left=ref("w_tags"), right=ref("tag_named"), left_on="_tag",
             right_on="_vid"),
        Step("nested", "nest", rows=ref("named"), children=ref("w_tagn"), on="item",
             child_on="friend", into="tagNames", fields=["tagName"]),
        Step("labeled", "rename", rows=ref("nested"), names={"count": "replyCount"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["personId", "personFirstName", "personLastName", "tagNames", "replyCount"]),
    ]
    return steps


def ic12_arangodb_engine(args):
    """IC12 arango, from arangodb/ic12/p1/plan.txt.

    The engine builds the tag set from the TAGS, like dgraph: [17] scans the whole Tag
    collection and [18] walks each tag's hasType edge, with the class test as a postFilter.
    Then per friend, three chained filtered traversals ([28] hasCreator, [31] replyOf, [34]
    hasTag), each with a postFilter and so each paying traverse_filtered's read rule: the
    expanded vertex, every reached vertex, and every survivor again (pk + doc each).
    No `LIMIT` sits on any filtered traversal, so nothing is cut early.

    The subclass walk [11] is uncapped (1..100). At sf0.3 every test param's tagClass is a
    leaf, so it reads one empty edge-index range.

    sf0.3 vs live: S, G and N exact at all 6 params (p1: S=30890 G=203327 N=72055), rows match
    the optimal plan and the live engine (`validate.py --sf 0.3 --db arangodb --query ic12 --live`)."""
    p = person_vid(args["person"])
    tcn = args["tagClassName"]
    return [
        # [64] IndexNode TagClass persistent(name) == @tagClassName, no projections, [62] LIMIT 1:
        # 1 SEEK + 1 NEXT + the document (1 GET, the index holds the docid)
        Step("tc", "read_index_eq", label="TagClass", prop="name", value=tcn),
        # [11] TraversalNode isSubclassOf INBOUND baseClass 1..100, vertexProjections [_id]:
        # the first traversal (+1 open SEEK); each reached class fetched (pk + doc)
        Step("open", "traversal_open", items=ref("tc"), etype="isSubclassOf"),
        Step("subs", "traverse_paths", srcs=ref("open"), etype="isSubclassOf", dir="in",
             depth=100),
        Step("subd", "docs_per_step", vids=ref("subs"), label="TagClass", props=["id"]),
        Step("classes", "concat", a=ref("tc"), b=ref("subs")),        # [2]-ish APPEND: in memory
        # [2] LET person = DOCUMENT(...): pk + doc
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        # [17] EnumerateCollectionNode Tag, no projections: the whole collection, 1 SEEK + 1 NEXT
        # per document (the doc rides in the iterator value, so no GET). read_vertices_by_label
        # walks our pk index instead of the doc range -- same S/N, different bytes.
        Step("tags", "read_vertices_by_label", label="Tag"),
        # [18] TraversalNode hasType OUTBOUND tag, vertexProjections [_id], postFilter
        # tc._id IN allClasses: traverse_filtered (every tag, every class reached, survivors again)
        Step("tt", "traverse_filtered", srcs=ref("tags"), etype="hasType", dir="out", depth=1,
             pred=E.lit(True), allowed=ref("classes"), props=["id"], label="TagClass"),
        # [22] COLLECT tag (RETURN DISTINCT tag), [66] relevantTagIds: in memory
        Step("tt_src", "pluck", rows=ref("tt"), field="_src"),
        Step("rel_tags", "dedup", items=ref("tt_src")),
        # [26] TraversalNode knows IN,OUT from person, vertexProjections [_id,_key,firstName,
        # lastName], no filter: 2 index SEEKs + 1 NEXT per edge, each friend fetched (pk + doc)
        Step("fr", "read_neighbors", srcs=[p], etype="knows", dir="both"),
        Step("frd", "docs_per_step", vids=ref("fr"), label="Person",
             props=["id", "firstName", "lastName"]),
        # [28] TraversalNode hasCreator INBOUND friend, no vertexProjections, postFilter
        # IS_SAME_COLLECTION('Comment', comment) -- one run per friend ([71] subquery)
        Step("cm", "traverse_filtered", srcs=ref("fr"), etype="hasCreator", dir="in", depth=1,
             pred=E.eq(E.get("_label"), "Comment"), props=["id"], label=MESSAGE_LABELS),
        # [31] TraversalNode replyOf OUTBOUND comment, postFilter IS_SAME_COLLECTION('Post', post)
        Step("cm_v", "pluck", rows=ref("cm"), field="_vid"),
        Step("ps", "traverse_filtered", srcs=ref("cm_v"), etype="replyOf", dir="out", depth=1,
             pred=E.eq(E.get("_label"), "Post"), props=["id"], label=MESSAGE_LABELS),
        # [34] TraversalNode hasTag OUTBOUND post, vertexProjections [_id, name], postFilter
        # tag._id IN relevantTagIds -- once per (comment, post) row, no dedup
        Step("ps_v", "pluck", rows=ref("ps"), field="_vid"),
        Step("pt", "traverse_filtered", srcs=ref("ps_v"), etype="hasTag", dir="out", depth=1,
             pred=E.lit(True), allowed=ref("rel_tags"), props=["name"], label="Tag"),
        # [37] {commentId, tagName} per row, [41]-[43] COLLECT commentId -> replyCount,
        # [48] FILTER replyCount > 0, [56]/[57] SORT replyCount DESC, id ASC LIMIT 20,
        # [50]-[52] COLLECT tagName -> tagNames: all in memory
        Step("ps_r", "rename", rows=ref("ps"), names={"_src": "_comment", "_vid": "_post"}),
        Step("ps_p", "project", rows=ref("ps_r"), fields=["_comment", "_post"]),
        Step("pt_r", "rename", rows=ref("pt"),
             names={"_src": "_p3", "_vid": "_tag", "name": "tagName"}),
        Step("pt_p", "project", rows=ref("pt_r"), fields=["_p3", "_tag", "tagName"]),
        Step("ct", "join_multi", left=ref("ps_p"), right=ref("pt_p"), left_on="_post",
             right_on="_p3"),
        Step("cm_r", "rename", rows=ref("cm"), names={"_src": "friend", "_vid": "_c2"}),
        Step("cm_p", "project", rows=ref("cm_r"), fields=["friend", "_c2"]),
        Step("fct", "join", left=ref("ct"), right=ref("cm_p"), left_on="_comment",
             right_on="_c2"),
        Step("fc_k", "derive", rows=ref("fct"), into="_fc",
             fn=E.tuple_(E.field("friend"), E.field("_comment"))),
        Step("fc", "dedup_rows", rows=ref("fc_k"), field="_fc"),
        Step("fc_f", "pluck", rows=ref("fc"), field="friend"),
        Step("rc", "group_count", items=ref("fc_f")),
        Step("frn", "rename", rows=ref("frd"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        Step("named", "join", left=ref("rc"), right=ref("frn"), left_on="item", right_on="_vid"),
        Step("top20", "top_k", rows=ref("named"), keys=[("count", "desc"), ("personId", "asc")],
             k=20),
        Step("nested", "nest", rows=ref("top20"), children=ref("fct"), on="item",
             child_on="friend", into="tagNames", fields=["tagName"]),
        Step("labeled", "rename", rows=ref("nested"), names={"count": "replyCount"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["personId", "personFirstName", "personLastName", "tagNames", "replyCount"]),
    ]


# ── nebula ──────────────────────────────────────────────────────────────────────────────────────
_IC12_NEBULA_HIER_LEVELS = 8    # the upward isSubclassOf walk, unrolled; a level with no frontier reads nothing


def ic12_nebula_engine(args):
    """IC12 Nebula, from nebula/ic12/p1/plan.txt: two MATCH branches joined on the tag (node 25).

    A  node 26 Traverse knows BOTH from n (first-step Person._tag: 1 GET) -> node 27 Traverse
       hasCreator IN per friend (vertexProps Person first/lastName fused with the first-step
       Person._tag: 1 GET per friend) -> node 28 Traverse replyOf OUT per message (first-step
       Comment._tag: 1 GET; it does NOT prune, so a Post pays its replyOf SEEK too) -> node 29
       Traverse hasTag OUT per replied-to message (first-step Post._tag: 1 GET) -> node 38
       AppendVertices Tag name: 1 GET per distinct tag.
    B  node 11 Traverse hasType OUT per tag (vertexProps Tag name: 1 GET per tag) -> node 31 Traverse
       isSubclassOf OUT 0..inf (first-step TagClass._tag: 1 GET per class) -> node 32 AppendVertices
       TagClass name: 1 GET per distinct class reached.
    In memory: node 35 Filter (the tag's class or an ancestor is named $tagClassName), 25 join,
    17 aggregate, 20 TopN 20.

    Node 31's variable-length walk, per graph/executor/query/TraverseExecutor.cpp: the vertexProps
    ride on EVERY step's GetNeighbors request (~line 95), so each step's sources pay 1 GET (step 1's
    is fused with the first-step filter); the next frontier drops only vertices already in adjList_
    (buildAdjList ~180, expand() ~350). Step 1 is built against an empty adjList_, so step 2 expands
    every distinct step-1 destination; after step 1 adjList_ holds every step-1 source (~241), so a
    later step never expands one of them again (`Thing` is a tag's own class at every param: read at
    step 1 only). Unrolled to 8 levels so the match can be derived in memory from the edges read; the
    deepest isSubclassOf chain is 5 edges at sf0.3 and sf3, so 6 levels are read (a deeper hierarchy
    would be cut short: raise the unroll).

    vs live: exact, or below by 1 to 3 on each of S, G and N (sf0.3: exact at p14, -1 at 8 params, -3 at
    p20, p25, p26). The engine's order, not the plan: a class that is a source and a destination of the
    same step (`Person`, `Agent`) enters adjList_ only when the response iteration has passed it
    (expand() ~355), so the engine expands it again at the next step unless its rows came before every
    edge pointing to it; this twin drops it always (a lower bound; each one is 1 SEEK, 1 GET, 1 NEXT)."""
    p = person_vid(args["person"])
    tc = args["tagClassName"]
    s = [
        Step("me", "read_vertex_by_id", vid=p),
        Step("mtag", "tag_probe", vids=[p], label="Person"),                     # node 26 filter
        Step("f0", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),  # node 26
        Step("f", "dedup", items=ref("f0")),
        Step("fp", "tag_probe", vids=ref("f"), label="Person",
             props=["firstName", "lastName"]),                                   # node 27 props+filter
        Step("fm", "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in", pairs=True),
        Step("mv", "pluck", rows=ref("fm"), field="dst"),
        Step("mvd", "dedup", items=ref("mv")),
        Step("comments", "tag_probe", vids=ref("mvd"), label="Comment"),         # node 28 filter
        Step("rep", "read_neighbors", srcs=ref("mvd"), etype="replyOf", dir="out", pairs=True),
        Step("pv", "pluck", rows=ref("rep"), field="dst"),
        Step("pvd", "dedup", items=ref("pv")),
        Step("posts", "tag_probe", vids=ref("pvd"), label="Post"),               # node 29 filter
        Step("pt0", "read_neighbors", srcs=ref("pvd"), etype="hasTag", dir="out", pairs=True),
        # node 29 reads every source's edges (SEEK + NEXTs) but its first-step filter drops the rows
        # of non-Post sources: 17,384 rows out of 30,477 edges read at p1. Downstream sees only these.
        Step("pt", "filter_in", rows=ref("pt0"), field="src", allowed=ref("posts")),
        Step("tv", "pluck", rows=ref("pt"), field="dst"),
        Step("tvd", "dedup", items=ref("tv")),
        Step("tagA", "read_vertex_props", vids=ref("tvd"), props=["name"], label="Tag"),   # node 38
        Step("tagB", "read_vertex_props", vids=ref("tvd"), props=["name"], label="Tag"),   # node 11 props
        Step("ht", "read_neighbors", srcs=ref("tvd"), etype="hasType", dir="out", pairs=True),
        Step("cv", "pluck", rows=ref("ht"), field="dst"),
        Step("c0", "dedup", items=ref("cv")),
        Step("cltag", "tag_probe", vids=ref("c0"), label="TagClass"),            # node 31 filter
    ]
    frontier, seen, edges = "c0", "c0", None
    expanded = "c0"                                  # adjList_ after step 1: its sources; then every later step's
    for lvl in range(1, _IC12_NEBULA_HIER_LEVELS + 1):                                         # node 31, per level
        if lvl >= 2:                                 # vertexProps on EVERY step's sources
            s.append(Step(f"vp{lvl}", "read_vertex_props", vids=ref(frontier), props=["name"],
                          label="TagClass"))
        s += [
            Step(f"up{lvl}", "read_neighbors", srcs=ref(frontier), etype="isSubclassOf", dir="out",
                 pairs=True),
            Step(f"upv{lvl}", "pluck", rows=ref(f"up{lvl}"), field="dst"),
            Step(f"upd{lvl}", "dedup", items=ref(f"upv{lvl}")),
        ]
        if lvl >= 2:
            expanded_now = f"x{lvl}"
            s.append(Step(expanded_now, "concat", a=ref(frontier), b=ref(expanded)))
            expanded = expanded_now
        nxt = (f"upd{lvl}" if lvl == 1 else f"new{lvl}")
        if lvl >= 2:
            s.append(Step(f"new{lvl}", "exclude", items=ref(f"upd{lvl}"), remove=ref(expanded)))
        s.append(Step(f"seen{lvl}", "concat", a=ref(seen), b=ref(nxt)))
        if edges is None:
            edges = f"up{lvl}"
        else:
            s.append(Step(f"e{lvl}", "concat", a=ref(edges), b=ref(f"up{lvl}")))
            edges = f"e{lvl}"
        frontier, seen = nxt, f"seen{lvl}"
    s += [
        Step("classes", "read_vertex_props", vids=ref(seen), props=["name"], label="TagClass"),  # node 32
        Step("hit", "filter", rows=ref("classes"), pred=E.eq(E.get("name"), tc)),
        Step("d0", "pluck", rows=ref("hit"), field="_vid"),
    ]
    desc = "d0"                                    # node 35: descendants-or-self of the named class
    for i in range(1, _IC12_NEBULA_HIER_LEVELS + 1):
        s += [
            Step(f"kid{i}", "filter_in", rows=ref(edges), field="dst", allowed=ref(desc)),
            Step(f"kidv{i}", "pluck", rows=ref(f"kid{i}"), field="src"),
            Step(f"du{i}", "concat", a=ref(desc), b=ref(f"kidv{i}")),
            Step(f"d{i}", "dedup", items=ref(f"du{i}")),
        ]
        desc = f"d{i}"
    s += [
        Step("tmatch", "filter_in", rows=ref("ht"), field="dst", allowed=ref(desc)),
        Step("tmv", "pluck", rows=ref("tmatch"), field="src"),
        Step("tags", "dedup", items=ref("tmv")),
        # node 25 join: Post -> matching tag, Comment -> such a Post, friend -> such a Comment
        Step("matched", "filter_in", rows=ref("pt"), field="dst", allowed=ref("tags")),
        Step("matched_p", "pluck", rows=ref("matched"), field="src"),
        Step("matched_posts", "dedup", items=ref("matched_p")),
        Step("repc", "filter_in", rows=ref("rep"), field="src", allowed=ref("comments")),
        Step("replies_r", "rename", rows=ref("repc"), names={"src": "_comment", "dst": "_parent"}),
        Step("good", "filter_in", rows=ref("replies_r"), field="_parent", allowed=ref("matched_posts")),
        Step("fmsg_r", "rename", rows=ref("fm"), names={"src": "friend", "dst": "_c2"}),
        Step("friend_reps", "join", left=ref("good"), right=ref("fmsg_r"), left_on="_comment",
             right_on="_c2"),
        # node 17 Aggregate: count(DISTINCT comment) per friend; node 20 TopN 20
        Step("friendv", "pluck", rows=ref("friend_reps"), field="friend"),
        Step("reply_count", "group_count", items=ref("friendv")),
        Step("pid", "derive", rows=ref("reply_count"), into="personId",
             fn=E.key_id(E.field("item"))),
        Step("top20", "top_k", rows=ref("pid"), keys=[("count", "desc"), ("personId", "asc")], k=20),
        Step("names_r", "rename", rows=ref("fp"),
             names={"firstName": "personFirstName", "lastName": "personLastName"}),
        Step("named", "join", left=ref("top20"), right=ref("names_r"), left_on="item", right_on="_vid"),
        Step("matched_r", "rename", rows=ref("matched"), names={"src": "_p3", "dst": "_tag"}),
        Step("winner_tags", "join_multi", left=ref("friend_reps"), right=ref("matched_r"),
             left_on="_parent", right_on="_p3"),
        Step("tag_named", "rename", rows=ref("tagA"), names={"name": "tagName"}),
        Step("winner_named", "join", left=ref("winner_tags"), right=ref("tag_named"), left_on="_tag",
             right_on="_vid"),
        Step("nested", "nest", rows=ref("named"), children=ref("winner_named"), on="item",
             child_on="friend", into="tagNames", fields=["tagName"]),
        Step("labeled", "rename", rows=ref("nested"), names={"count": "replyCount"}),
        Step("out", "project", rows=ref("labeled"),
             fields=["personId", "personFirstName", "personLastName", "tagNames", "replyCount"]),
    ]
    return s


ENGINE = {
    "ic12": {"dgraph": ic12_dgraph_engine, "arangodb": ic12_arangodb_engine,
             "nebula": ic12_nebula_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic12": _KNOWS_HINTS, "ic12_capped": _KNOWS_HINTS}
