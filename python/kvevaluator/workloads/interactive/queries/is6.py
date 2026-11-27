"""LDBC IS6 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (ENGINE_WALK_CAP, MESSAGE_LABELS, _anchor_message,
                                                 _arango_message_anchor, message_vids, MESSAGE_PARAM, MESSAGE_LET)


def is6(args, chase_cap=None):
    """IS6: the forum containing a message (via the root post) + its moderator.
    No anchor step: `read_chase` walks replyOf straight from the one Message vid (the is5 pattern). The message's own
    row and its tag are never needed -- the forum and moderator reads carry their own labels -- so the minimal plan
    has no candidate-tag probe. chase_cap: None = the correct full walk; 3 = the engines' capped approximation
    (is6_capped) -- a message in a thread deeper than 3 yields no root, so containerOf reads empty and the row
    drops, exactly the empty result the engines return."""
    return Plan([
        Step("root_post", "read_chase", srcs=[let("msg_vid")], etype="replyOf", dir="out",
             max_hops=chase_cap),
        Step("forum",     "read_neighbors", srcs=ref("root_post"), etype="containerOf", dir="in"),
        Step("forum_props", "read_vertex_props", vids=ref("forum"), props=["id", "title"], label="Forum"),
        Step("forum_named", "rename", rows=ref("forum_props"), names={"id": "forumId", "title": "forumTitle"}),
        Step("mod_vid",   "read_neighbors", srcs=ref("forum"), etype="hasModerator", dir="out"),
        Step("mod_props", "read_vertex_props", vids=ref("mod_vid"), props=["id", "firstName", "lastName"],
             label="Person"),
        Step("mod_named", "rename", rows=ref("mod_props"),
             names={"id": "moderatorId", "firstName": "moderatorFirstName",
                    "lastName": "moderatorLastName"}),
        Step("joined",    "zip_rows", left=ref("forum_named"), right=ref("mod_named")),
        Step("out",       "project", rows=ref("joined"),
             fields=["forumId", "forumTitle", "moderatorId",
                     "moderatorFirstName", "moderatorLastName"]),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


def is6_capped(args):
    """is6 with the replyOf-to-root walk capped at 3 hops (dgraph's 3-level unroll /
    nebula's REPLY_OF*0..3). Returns EMPTY for threads deeper than 3."""
    return is6(args, chase_cap=ENGINE_WALK_CAP)


def is6_dgraph_engine(args):
    """Message -> forum -> moderator, from dgraph/is6/p1/plan.txt (block 0), over the whole
    reply-depth range.

    Dgraph has no variable-length path, so the query STATICALLY UNROLLS the reply chain into
    typed branches: selfForum (m is a Post), p1 (parent Post), c1 (parent Comment) -> {p2
    (grandparent Post), c2 -> p3 (great-grandparent Post)}. Exactly one terminal branch fires
    for a given message, chosen by its depth; the rest are pruned by a type filter that comes
    back empty.

        depth  0      1       2       3       4       5
        live   9/5    12/5    16/5    18/5    14/0    14/0
        S      3+4+1+1  3+1+6+2  3+1+2+10  3+1+2+2+2+2+6   3+1+2+2+2+2+2

    Two rules make it land at every depth:

    1. **A branch whose input is empty costs NOTHING** -- no posting read, no type-index
       SEEK. That is why a depth-0 Post pays 9 and not 19: the p1/c1 subtrees never run.
    2. **A type filter over a NON-empty input costs exactly 1 type-index SEEK**, whether or
       not anything matches. A depth-2 comment pays for `p1: replyOf` + `type(post)` (2
       SEEKs) even though its parent is a Comment and the branch dies there.

    **Depths 4-5 return NOTHING (G=0), and that is correct.** The unroll only reaches back 3
    levels, so a comment 4+ deep never finds its root Post -- the same 3-hop truncation
    nebula makes explicit as `REPLY_OF*0..3`. Only arango, which walks `0..100`, answers IS6
    correctly for deep comments. S stays at 14 because every branch still runs and fails."""
    steps = _anchor_message(args["message"])

    def forum_branch(src, tag):
        """~containerOf -> forum props -> hasModerator -> moderator props, the leaf every
        one of the four cases ends in. Each type filter is 1 SEEK when its input is
        non-empty, 0 when empty."""
        return [
            # ~containerOf (1 SEEK) @filter(type(forum)) (1 type-index SEEK)
            Step(f"cf_{tag}", "read_neighbors", srcs=ref(src), etype="containerOf", dir="in"),
            Step(f"f_{tag}", "read_filter_by_label_index", vids=ref(f"cf_{tag}"), label="Forum"),
            # forumId: id, forumTitle: title -- 1 GET each
            Step(f"fp_{tag}", "read_vertex_props", vids=ref(f"f_{tag}"), props=["id", "title"]),
            Step(f"fr_{tag}", "rename", rows=ref(f"fp_{tag}"),
                 names={"id": "forumId", "title": "forumTitle"}),
            # hasModerator (1 SEEK) @filter(type(person)) (1 type-index SEEK)
            Step(f"md_{tag}", "read_neighbors", srcs=ref(f"f_{tag}"), etype="hasModerator",
                 dir="out"),
            Step(f"m_{tag}", "read_filter_by_label_index", vids=ref(f"md_{tag}"), label="Person"),
            # moderatorId: id, moderatorFirstName, moderatorLastName -- 1 GET each
            Step(f"mp_{tag}", "read_vertex_props", vids=ref(f"m_{tag}"),
                 props=["id", "firstName", "lastName"]),
            Step(f"mr_{tag}", "rename", rows=ref(f"mp_{tag}"),
                 names={"id": "moderatorId", "firstName": "moderatorFirstName",
                        "lastName": "moderatorLastName"}),
            Step(f"row_{tag}", "zip_rows", left=ref(f"fr_{tag}"), right=ref(f"mr_{tag}")),
        ]

    # block 0 `selfForum: ~containerOf` -- Case 0: the message IS a Post.
    steps += forum_branch("me", "d0")
    # Case 1: parent is a Post. `p1` and `c1` are two aliases on the SAME predicate, so
    # dgraph reads the replyOf posting list TWICE -- both are charged.
    steps += [
        # block 0 `p1: replyOf @filter(type(post))`
        Step("p1_raw", "read_neighbors", srcs=ref("me"), etype="replyOf", dir="out"),
        Step("p1", "read_filter_by_label_index", vids=ref("p1_raw"), label="Post"),
    ] + forum_branch("p1", "d1") + [
        # block 0 `c1: replyOf @filter(type(comment))`
        Step("c1_raw", "read_neighbors", srcs=ref("me"), etype="replyOf", dir="out"),
        Step("c1", "read_filter_by_label_index", vids=ref("c1_raw"), label="Comment"),
    ]
    # Case 2: grandparent is a Post (p2), reached through the Comment parent.
    steps += [
        # block 0 `c1 { p2: replyOf @filter(type(post)) }`
        Step("p2_raw", "read_neighbors", srcs=ref("c1"), etype="replyOf", dir="out"),
        Step("p2", "read_filter_by_label_index", vids=ref("p2_raw"), label="Post"),
    ] + forum_branch("p2", "d2") + [
        # block 0 `c1 { c2: replyOf @filter(type(comment)) }`
        Step("c2_raw", "read_neighbors", srcs=ref("c1"), etype="replyOf", dir="out"),
        Step("c2", "read_filter_by_label_index", vids=ref("c2_raw"), label="Comment"),
    ]
    # Case 3: great-grandparent is a Post (p3). The deepest the unroll reaches.
    steps += [
        # block 0 `c2 { p3: replyOf @filter(type(post)) }`
        Step("p3_raw", "read_neighbors", srcs=ref("c2"), etype="replyOf", dir="out"),
        Step("p3", "read_filter_by_label_index", vids=ref("p3_raw"), label="Post"),
    ] + forum_branch("p3", "d3")
    # @normalize flattens the four cases; at most one produced a row.
    steps += [
        Step("cat01", "concat", a=ref("row_d0"), b=ref("row_d1")),
        Step("cat23", "concat", a=ref("row_d2"), b=ref("row_d3")),
        Step("out", "concat", a=ref("cat01"), b=ref("cat23")),
    ]
    return steps


def is6_nebula_engine(args):
    """Message -> forum -> moderator, from nebula/is6/p1/plan.txt, over the whole reply-depth
    range.

    Exact at all six depths (S/G/N; the query caps its walk at 3 hops):

        depth  0      1      2      3      4      5
        live   3/3/2  5/4/3  7/5/4  8/6/5  7/4/3  7/4/3

    The shape: `read_closure` reaches d+1 vertices (capped at 4), each costs 1 SEEK to expand and
    1 tag probe; containerOf is then tried from ALL of them (1 SEEK each) because the `:Post`
    label does not filter; the forum and moderator add 1 SEEK + 1 GET apiece when found.

    **Depths 4-5 return NOTHING, and that is correct.** The query caps at `REPLY_OF*0..3`
    (an explicit spec deviation, to match dgraph's lack of variable-length paths), so a
    comment 4+ levels deep never reaches its root Post and the engine emits no row. G drops
    6 -> 4 exactly because the forum and moderator reads stop happening. A faithful plan has
    to reproduce the emptiness, not answer correctly."""
    # nebula addresses a message by ONE vid (msg_3) -- post and comment share the vid
    # space and the type lives only in the tag key -- so the anchor is a free key lookup
    # with NO tag probe. The captured plans confirm it: params say messageId=msg_3 and
    # the Start/Dedup pair reads nothing. Passing a candidate vid is fine because the
    # format collapses it idempotently (see NebulaFormat._v).
    m = message_vids(args["message"])[0]
    return [
        Step("me", "read_vertex_by_id", vid=m),
        # node 4: `REPLY_OF*0..3`. This is a VARIABLE-LENGTH walk and it emits the message
        # PLUS every ancestor it reaches -- `read_closure`, not one `read_neighbors` hop and not
        # `read_chase` (which keeps only the last vertex). A depth-0 Post reaches just itself, so
        # modelling it as a single hop looks right at param 1 and is wrong at every other.
        Step("reach", "read_closure", srcs=ref("me"), etype="replyOf", dir="out", max_levels=3),
        # node 11: containerOf runs from EVERY reached vertex, not just the root Post. The
        # pattern is `(m)-[:REPLY_OF*0..3]->(p:Post)<-[:CONTAINER_OF]-(f)`, and in nGQL a tag
        # label on an edge-reached vertex does NOT filter (a documented nebula
        # behaviour). So `(p:Post)` selects nothing; every ancestor is probed
        # and the non-Posts simply find no containerOf edge. That is the whole reason S grows
        # 3/5/7/8 with depth instead of staying flat.
        # node 11's `first step filter: Post._tag IS NOT EMPTY` -- one GET per reached vertex
        # and NO prefix scan on the comments that fail it (see NebulaEngineFormat.tag_probe).
        # Its result is discarded: containerOf is tried from every reached vertex regardless.
        Step("mtag", "tag_probe", vids=ref("reach"), label="Post"),
        Step("forum", "read_neighbors", srcs=ref("reach"), etype="containerOf", dir="in"),
        # node 12 Traverse: vertexProps Forum(title) + `first step filter: Forum._tag` -- the
        # title read IS the filter, one GET, not two
        Step("fprops", "read_vertex_props", vids=ref("forum"), label="Forum", props=["id", "title"]),
        Step("fr", "rename", rows=ref("fprops"), names={"id": "forumId", "title": "forumTitle"}),
        # node 12 Traverse: hasModerator OUT_EDGE
        Step("mod", "read_neighbors", srcs=ref("forum"), etype="hasModerator", dir="out"),
        # node 13 AppendVertices: Person tag (firstName, lastName), dedup: 1
        Step("mprops", "read_vertex_props", vids=ref("mod"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("mr", "rename", rows=ref("mprops"),
             names={"id": "moderatorId", "firstName": "moderatorFirstName",
                    "lastName": "moderatorLastName"}),
        Step("out", "zip_rows", left=ref("fr"), right=ref("mr")),
    ]


def is6_arangodb_engine(args):
    """Message -> forum -> moderator, from arangodb/is6/p1/plan.txt, over the whole reply-depth
    range.

    Exact at all six depths, and cleanly linear because arango's walk is UNCAPPED (0..100),
    so it always reaches the root:

        S = d + 4        G = 2d + 11        N = d + 2

        depth  0        1        2        3        4        5
        live   4/11/2   5/13/3   6/15/4   7/17/5   8/19/6   9/21/7

    Every term is accounted: S = traversal_open + (d+1) walk expansions + containerOf +
    hasModerator. G = 3 (the DOCUMENT anchor pair) + 2(d+1) (each walked vertex read for [6]'s
    postFilter) + 2 (the one survivor, read again as [6] emits it) + 2 (forum) + 2 (moderator).

    Contrast nebula and dgraph, which cap at 3 hops and return NOTHING past depth 3. Same
    query, same data, different answers -- arango is the only one of the three that answers
    IS6 correctly for deep comments."""
    return _arango_message_anchor(args["message"]) + [   # [2]/[3] DOCUMENT(Post/..), DOCUMENT(Comment/..)
        # [6] the first traversal
        Step("open", "traversal_open", items=ref("me"), etype="replyOf"),
        # node 6: `FOR op IN 0..100 OUTBOUND msg replyOf`. Variable-length, and unlike
        # nebula's it is UNCAPPED, so it always reaches the root Post however deep the
        # comment is. It emits the message plus every ancestor -> `read_closure`, 1 SEEK per
        # expansion and 1 NEXT per edge crossed.
        Step("reach", "read_closure", srcs=ref("open"), etype="replyOf", dir="out",
             max_levels=100),
        # [6] postFilter IS_SAME_COLLECTION("Post", op), no vertexProjections: every visited
        # vertex (depth 0 included) is materialized (pk + doc) to evaluate it. Measured:
        # `FOR v IN 0..100 ... RETURN v._key` costs 2 GETs per vertex.
        Step("walked", "docs_per_step", vids=ref("reach"), label=MESSAGE_LABELS,
             props=["id"]),
        # [7]-[9] FILTER + LIMIT 1: in memory (the collection rides on the row)
        Step("root_rows", "filter", rows=ref("walked"),
             pred=E.eq(E.field("_label"), "Post")),
        Step("root", "pluck", rows=ref("root_rows"), field="_vid"),
        # [6] the survivor is read AGAIN as it is emitted -- traverse_filtered's second read
        # (FIRST() [12] itself is in memory)
        Step("root_doc", "docs_per_step", vids=ref("root"), label="Post", props=["id"]),
        # [14] TraversalNode containerOf edge(_to); vertexProjections [_id,_key,title]: forum doc
        Step("forum", "read_neighbors", srcs=ref("root"), etype="containerOf", dir="in"),
        Step("fprops", "docs_per_step", vids=ref("forum"), label="Forum",
             props=["id", "title"]),
        Step("fr", "rename", rows=ref("fprops"), names={"id": "forumId", "title": "forumTitle"}),
        # [15] TraversalNode hasModerator edge(_from); vertexProjections [_key,..]: moderator doc
        Step("mod", "read_neighbors", srcs=ref("forum"), etype="hasModerator", dir="out"),
        Step("mprops", "docs_per_step", vids=ref("mod"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("mr", "rename", rows=ref("mprops"),
             names={"id": "moderatorId", "firstName": "moderatorFirstName",
                    "lastName": "moderatorLastName"}),
        Step("out", "zip_rows", left=ref("fr"), right=ref("mr")),
    ]


# single candidates: passthrough search
LOGICAL = {"is6": is6, "is6_capped": is6_capped}


ENGINE = {
    "is6": {"dgraph": is6_dgraph_engine, "nebula": is6_nebula_engine, "arangodb": is6_arangodb_engine}
}
