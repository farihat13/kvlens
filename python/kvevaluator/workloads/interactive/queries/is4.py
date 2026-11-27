"""LDBC IS4 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import (_anchor_message, _arango_message_anchor, message_vids, MESSAGE_PARAM,
                                                 MESSAGE_LET)
from kvevaluator.plans.ir import authoring as E


IS4_FIELDS = ["creationDate", "content", "imageFile"]


def is4(args):
    """IS4: a message's creationDate + content (imageFile for content-less image
    posts). Anchor of unknown type -> the fused read_anchor_props (formats pay their
    own resolution: dgraph fqid SEEK per miss, nebula tag GET per miss, arango pk
    GET per miss + one doc read)."""
    return Plan([
        Step("msg", "read_anchor_props", cands=[let("msg_vid")],
             props=["creationDate", "content", "imageFile"], labels=["Post", "Comment"]),
        Step("out", "project", rows=ref("msg"), fields=IS4_FIELDS),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


def is4_dgraph_engine(args):
    """Message content, from dgraph/is4/p1/plan.txt (block 0). live S=3 G=4."""
    return _anchor_message(args["message"]) + [      # block 0: eq(id) + type(post) OR type(comment)
        # block 0: id, content, imageFile, creationDate -- 1 GET each
        Step("out", "read_vertex_props", vids=ref("me"),
             props=["id", "content", "imageFile", "creationDate"]),
    ]


def is4_nebula_engine(args):
    """Message content, from nebula/is4/p1/plan.txt. live S=1 G=2 N=0 -- exact, and it is the cleanest confirmation of
    the missed-tag rule (NebulaGraph follows a missed tag GET with a prefix scan of the vid) anywhere in the suite.

    The captured plan is a Union of two independent AppendVertices, one per tag: node 16
    reads tagId 3 (Post), node 17 reads tagId 4 (Comment). This param's message is a Post,
    so node 16 HITS and node 17 MISSES. Two GETs, one per branch -- and the single SEEK is
    the miss, where storaged prefix-scans the vid to discover which tags exist. Take the
    prefix scan away and this cell reads S=0 against a live S=1 with nothing else in the
    query to absorb it."""
    # nebula addresses a message by ONE vid (msg_3) -- post and comment share the vid
    # space and the type lives only in the tag key -- so the anchor is a free key lookup
    # with NO tag probe. The captured plans confirm it: params say messageId=msg_3 and
    # the Start/Dedup pair reads nothing. Passing a candidate vid is fine because the
    # format collapses it idempotently (see NebulaFormat._v).
    m = message_vids(args["message"])[0]
    return [
        Step("me", "read_vertex_by_id", vid=m),
        # node 16 AppendVertices: Post tag, dedup: 1 (a miss -> storaged prefix scan)
        Step("post", "read_vertex_props", vids=ref("me"), label="Post",
             props=["id", "content", "imageFile", "creationDate"]),
        # node 17 AppendVertices: Comment tag, dedup: 1 (same)
        Step("comment", "read_vertex_props", vids=ref("me"), label="Comment",
             props=["id", "content", "creationDate"]),
        # node 13 Union keeps whichever branch found a row
        Step("both", "concat", a=ref("post"), b=ref("comment")),
        Step("found", "filter", rows=ref("both"),
             pred=E.notnull(E.get("creationDate"))),
        # LDBC output columns (in memory); the Comment branch has no imageFile -> None
        Step("out", "project", rows=ref("found"), fields=IS4_FIELDS),
    ]


def is4_arangodb_engine(args):
    """Message content, from arangodb/is4/p1/plan.txt. live S=0 G=3 N=0 -- exact.

    The whole query is the two DOCUMENT calls ([2]/[3], see _arango_message_anchor), so it is the isolated measurement of the
    anchor: 2 GETs for the hit, 1 for the miss, and no SEEK anywhere because a primary-key
    lookup is a hash GET, not an index scan (fact 3)."""
    return _arango_message_anchor(args["message"]) + [   # [2]/[3] DOCUMENT(Post/..), DOCUMENT(Comment/..)
        Step("found", "filter", rows=ref("me_rows"), pred=E.lit(True)),   # [4]-[6] in memory
        # LDBC output columns (in memory); the Comment document has no imageFile -> None
        Step("out", "project", rows=ref("found"), fields=IS4_FIELDS),
    ]


# single candidate: the logical plan IS the physical one (passthrough search)
LOGICAL = {"is4": is4}


ENGINE = {
    "is4": {"dgraph": is4_dgraph_engine, "nebula": is4_nebula_engine, "arangodb": is4_arangodb_engine}
}
