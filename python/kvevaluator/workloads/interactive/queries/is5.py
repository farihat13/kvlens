"""LDBC IS5 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import (PERSON_RENAME, _anchor_message, _arango_message_anchor, message_vids,
                                                 MESSAGE_PARAM, MESSAGE_LET)


def is5(args):
    """IS5: a message's creator. No anchor step: read_neighbors() straight from the one Message vid over hasCreator."""
    return Plan([
        Step("author_vid",   "read_neighbors", srcs=[let("msg_vid")],
             etype="hasCreator", dir="out"),
        Step("author_props", "read_vertex_props", vids=ref("author_vid"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("author_named", "rename", rows=ref("author_props"), names=PERSON_RENAME),
        Step("out",          "project", rows=ref("author_named"),
             fields=["personId", "personFirstName", "personLastName"]),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


def is5_dgraph_engine(args):
    """Message author, from dgraph/is5/p1/plan.txt (block 0). live S=5 G=3."""
    return _anchor_message(args["message"]) + [      # block 0: eq(id) + type(post) OR type(comment)
        # block 0: hasCreator -- the forward posting (1 SEEK)
        Step("auth", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="out"),
        # block 0: hasCreator @filter(type(person)) -- 1 type-index SEEK
        Step("author", "read_filter_by_label_index", vids=ref("auth"), label="Person"),
        # block 0: authorId: id, authorFirstName, authorLastName -- 1 GET each
        Step("out", "read_vertex_props", vids=ref("author"), props=["id", "firstName", "lastName"]),
    ]


def is5_nebula_engine(args):
    """Message author, from nebula/is5/p1/plan.txt. live S=1 G=1 N=1 -- exact.

    Node 4's Traverse has NO `first step filter` line, so unlike IS3 the anchor message is
    never read: the query only needs the hasCreator edge, whose destination is in the key.
    The single GET is the author's Person row."""
    # nebula addresses a message by ONE vid (msg_3) -- post and comment share the vid
    # space and the type lives only in the tag key -- so the anchor is a free key lookup
    # with NO tag probe. The captured plans confirm it: params say messageId=msg_3 and
    # the Start/Dedup pair reads nothing. Passing a candidate vid is fine because the
    # format collapses it idempotently (see NebulaFormat._v).
    m = message_vids(args["message"])[0]
    return [
        Step("me", "read_vertex_by_id", vid=m),
        # node 4 Traverse: hasCreator OUT_EDGE, no first-step filter -> no source read
        Step("auth", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="out"),
        # node 9 AppendVertices: Person tag (firstName, lastName), dedup: 1
        Step("out", "read_vertex_props", vids=ref("auth"), label="Person",
             props=["id", "firstName", "lastName"]),
    ]


def is5_arangodb_engine(args):
    """Message author, from arangodb/is5/p1/plan.txt. live S=2 G=5 N=1 -- exact.
    3 GETs for the anchor pair + 2 for materializing the author."""
    return _arango_message_anchor(args["message"]) + [   # [2]/[3] DOCUMENT(Post/..), DOCUMENT(Comment/..)
        # [5] TraversalNode hasCreator edge(_from), 1..1 -- the first traversal
        Step("open", "traversal_open", items=ref("me"), etype="hasCreator"),
        Step("auth", "read_neighbors", srcs=ref("open"), etype="hasCreator", dir="out"),
        # [5] vertexProjections [_key, firstName, lastName]: the author document
        Step("out", "docs_per_step", vids=ref("auth"), label="Person",
             props=["id", "firstName", "lastName"]),
    ]


# single candidate: the logical plan IS the physical one (passthrough search)
LOGICAL = {"is5": is5}


ENGINE = {
    "is5": {"dgraph": is5_dgraph_engine, "nebula": is5_nebula_engine, "arangodb": is5_arangodb_engine}
}
