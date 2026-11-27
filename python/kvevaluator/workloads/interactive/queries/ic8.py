"""LDBC IC8 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import field, key_id, let
from kvevaluator.workloads.interactive._shared import (IC8_FIELDS, MESSAGE_LABELS, PERSON_RENAME, _anchor_person, person_vid,
                                                 PERSON_PARAM, PERSON_LET)


# ------------------------------------------------------------------------ IC8

def _ic8_steps(args, rank_props, fetch_body):
    """IC8: the 20 most recent replies to any of `person`'s messages, with each
    reply's author. Sort: reply creationDate desc, reply id asc."""
    p = let("me_vid")
    steps = [
        Step("me",       "read_vertex_by_id", vid=p, label="Person"),
        Step("my_msgs",  "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        Step("replies",  "read_neighbors", srcs=ref("my_msgs"), etype="replyOf", dir="in"),
        Step("ranked",   "read_vertex_props", vids=ref("replies"), props=rank_props, label="Comment"),
        Step("top20",    "top_k", rows=ref("ranked"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=20),
        Step("top_vids", "pluck", rows=ref("top20"), field="_vid"),
    ]
    if fetch_body:
        steps += [
            Step("bodies",    "read_vertex_props", vids=ref("top_vids"), props=["content"], label="Comment"),
            Step("with_body", "zip_rows", left=ref("top20"), right=ref("bodies")),
        ]
        zip_src = "with_body"
    else:
        zip_src = "top20"
    steps += [
        Step("author_vids",  "read_neighbors", srcs=ref("top_vids"), etype="hasCreator", dir="out"),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("authors_named", "rename", rows=ref("author_props"), names=PERSON_RENAME),
        Step("joined",   "zip_rows", left=ref(zip_src), right=ref("authors_named")),
        Step("labeled",  "rename", rows=ref("joined"),
             names={"id": "commentId", "creationDate": "commentCreationDate",
                    "content": "commentContent"}),
        Step("out",      "project", rows=ref("labeled"), fields=IC8_FIELDS),
    ]
    return Plan(steps, params=PERSON_PARAM, let=PERSON_LET)


def ic8_ixwalk(args):
    """IC8, dgraph default: rank replies via the creationDate index walk (unbounded -- IC8 has no maxDate; replies are
    Comments, so ONE index), as the real engine does (`orderdesc + first:20`); the GET-rank two-phase (ic8_getrank,
    recorded loser) is dearer on every param. Day tokens keep the unbounded walk cheap (1 NEXT per DAY). For
    nebula/arango the per-ROW walk loses (ic8_onephase): the one date-ranked query where the walk stays dgraph-only.
    """
    p = let("me_vid")
    return Plan([
        Step("me",       "read_vertex_by_id", vid=p, label="Person"),
        Step("my_msgs",  "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        Step("replies",  "read_neighbors", srcs=ref("my_msgs"), etype="replyOf", dir="in"),
        # replies are always Comments -> single index; winner bodies fused
        Step("top20",    "read_top_k_by_index", vids=ref("replies"), prop="creationDate", k=20,
             labels=["Comment"], props=["content"]),
        Step("top_vids", "pluck", rows=ref("top20"), field="_vid"),
        Step("author_vids",  "read_neighbors", srcs=ref("top_vids"), etype="hasCreator", dir="out"),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("authors_named", "rename", rows=ref("author_props"), names=PERSON_RENAME),
        Step("joined",   "zip_rows", left=ref("top20"), right=ref("authors_named")),
        Step("labeled",  "rename", rows=ref("joined"),
             names={"id": "commentId", "creationDate": "commentCreationDate",
                    "content": "commentContent"}),
        Step("out",      "project", rows=ref("labeled"), fields=IC8_FIELDS),
    ], params=PERSON_PARAM, let=PERSON_LET)


def ic8_getrank(args):
    """Recorded loser for dgraph (see ic8_ixwalk): rank replies on 2 GETs each,
    content for the top 20."""
    return _ic8_steps(args, rank_props=["creationDate", "id"], fetch_body=True)


def ic8_onephase(args):
    """IC8 nebula/arango override: rank by reading every reply's whole row/doc. The per-row date walk loses for both:
    arango's index key holds the docid, so the walk pays a doc GET per crossed entry just for membership; for nebula
    the unbounded walk's cost varies ~7x with how recent the person's replies are, and one-phase wins the aggregate
    over all 15 params. One plan per (query, format): one-phase stays.
    """
    return _ic8_steps(args, rank_props=["creationDate", "id", "content"], fetch_body=False)


# ── IC8 ──────────────────────────────────────────────────────────────────────
# "The 20 most recent replies to any of a person's messages."
#
# Nebula (p1): S = 1 + 2652 + 2114 = 4767, N = 2652 + 2114 + 2114 = 6880 -- both exact.
#   node 14  hasCreator IN from the person   -> 2652 messages   S=1     N=2652
#   node 5   replyOf IN from those           -> 2114 replies    S=2652  N=2114
#   node 15  hasCreator OUT from the replies                    S=2114  N=2114
#              vertexProps tagId 4 rides in the first-step Comment tag filter -> 2114 GETs
#   node 16  AppendVertices, the reply authors' Person rows, deduped
def ic8_nebula_engine(args):
    p = person_vid(args["person"])
    return [
        Step("me", "read_vertex_by_id", vid=p),
        # node 14 Traverse hasCreator IN from start: first-step Person._tag (1 GET) + SEEK + NEXT
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        # node 5 Traverse replyOf IN per message, no vertexProps: 1 SEEK + 1 NEXT per edge
        Step("reps", "read_neighbors", srcs=ref("msgs"), etype="replyOf", dir="in"),
        # node 15 Traverse hasCreator OUT per reply: vertexProps Comment [creationDate,content]
        # fused with the first-step Comment._tag filter (1 GET), + 1 SEEK + 1 NEXT
        Step("rprops", "tag_probe", vids=ref("reps"), label="Comment",
             props=["creationDate", "content"]),
        Step("auth", "read_neighbors", srcs=ref("reps"), etype="hasCreator", dir="out",
             pairs=True),
        Step("av", "pluck", rows=ref("auth"), field="dst"),
        # node 16 AppendVertices Person [firstName,lastName], dedup: 1
        Step("aprops", "read_vertex_props", vids=ref("av"), label="Person",
             props=["id", "firstName", "lastName"]),
        # node 12 Project, node 13 TopN 20: in memory, after everything above is read
        Step("ra", "join", left=ref("auth"), right=ref("aprops"),
             left_on="dst", right_on="_vid"),
        Step("full", "join", left=ref("ra"), right=ref("rprops"),
             left_on="src", right_on="_vid"),
        Step("cr0", "rename", rows=ref("full"),
             names={"src": "_cvid", "content": "commentContent",
                    "creationDate": "commentCreationDate"}),
        # id(comment) is the vid; the LDBC id is key-derived, in memory (0 KV)
        Step("cr", "derive", rows=ref("cr0"), into="commentId", fn=key_id(field("_cvid"))),
        Step("top20", "top_k", rows=ref("cr"),
             keys=[("commentCreationDate", "desc"), ("commentId", "asc")], k=20),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("top20"), names=PERSON_RENAME),
        Step("out", "project", rows=ref("labeled"), fields=IC8_FIELDS),
    ]


# Dgraph (p1): G = 2114 + 120 = 2234, exact. The 2114 is the multiSort second-key read --
# `orderdesc: creationDate, orderasc: id` fetches `id` for every candidate in the first
# sort's matrix, which is the whole 2114 (dgraph's captured plan). The 120 is the projection:
# 3 scalars x 20 winners, twice (the comment and its author). At sf0.3: S and N exact, G -4 at
# every param (the split Comment type posting, read twice -- see ic2.py).
def ic8_dgraph_engine(args):
    p = person_vid(args["person"])
    return _anchor_person(p) + [
        # block 1: ~hasCreator from P (1 SEEK) @filter(type(post) OR type(comment)) (2 SEEKs)
        Step("msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        Step("tp", "read_filter_by_label_index", vids=ref("msgs"), label="Post"),
        Step("tc", "read_filter_by_label_index", vids=ref("msgs"), label="Comment"),
        # block 2: ~replyOf per message (1 SEEK each) @filter(type(comment)) (1 SEEK)
        Step("reps", "read_neighbors", srcs=ref("msgs"), etype="replyOf", dir="in"),
        Step("repc", "read_filter_by_label_index", vids=ref("reps"), label="Comment"),
        # block 3: orderdesc creationDate, orderasc id, first 20 (sort_topk); id, creationDate,
        # content per winner; hasCreator per winner (1 SEEK) @filter(type(person)) (1 SEEK);
        # the author's id, firstName, lastName
        Step("repr", "annotate", items=ref("repc"), field="creationDate", value=0),
        Step("top", "sort_topk", rows=ref("repr"), prop="creationDate", k=20,
             second_key="id", id_order="asc"),
        Step("topv", "pluck", rows=ref("top"), field="_vid"),
        Step("cprops", "read_vertex_props", vids=ref("topv"),
             props=["id", "creationDate", "content"]),
        Step("cr", "rename", rows=ref("cprops"),
             names={"id": "commentId", "content": "commentContent",
                    "creationDate": "commentCreationDate"}),
        Step("auth", "read_neighbors", srcs=ref("topv"), etype="hasCreator", dir="out"),
        Step("authp", "read_filter_by_label_index", vids=ref("auth"), label="Person"),
        Step("ap", "read_vertex_props", vids=ref("authp"), props=["id", "firstName", "lastName"]),
        Step("ar", "rename", rows=ref("ap"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        Step("out", "zip_rows", left=ref("cr"), right=ref("ar")),
    ]


# Arango (p1): the three traversals are the SAME shape as nebula's, and the numbers say so --
# S = 4768 = nebula's 4767 + the one traversal_open seek, and N = 6880 is identical. The whole
# difference is G: nebula reads one tag row per vertex, arango reads a document.
#
#   [2]  DOCUMENT(person)                                       2 GETs   (pk + doc)
#   [3]  hasCreator OUT  -> 2652 messages   vertexProj [_id]     5304    (2 per step)
#   [4]  replyOf    OUT  -> 2114 replies    vertexProj [_id,_key,content,creationDate]  4228
#   [5]  hasCreator IN   -> 2114 authors    vertexProj [_key,firstName,lastName]        4228
#                                                              ------
#                                                               13762 = live G, exact
#
# Node [3] is the one worth pausing on. It projects `_id` and NOTHING else -- the message is
# never referenced again except as the replyOf traversal's start vertex, and the edge index
# already carries its id. Arango materializes all 2652 documents anyway (the IS1 rule, which
# `docs_per_step` exists to charge). That single unnecessary read is 5304 of this query's
# 13762 GETs: 39% of the cost buys nothing.
def ic8_arangodb_engine(args):
    p = person_vid(args["person"])
    return [
        # [2] LET person = DOCUMENT(...): pk + doc
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        Step("mev", "pluck", rows=ref("me"), field="_vid"),
        # [3] TraversalNode hasCreator INBOUND person, vertexProjections [_id]: the first
        # traversal's open SEEK, 1 SEEK + 1 NEXT per edge, and each message document per step
        Step("open", "traversal_open", items=ref("mev"), etype="hasCreator"),
        Step("msgs", "read_neighbors", srcs=ref("open"), etype="hasCreator", dir="in"),
        Step("mdocs", "docs_per_step", vids=ref("msgs"), label=MESSAGE_LABELS, props=["id"]),
        # [4] TraversalNode replyOf INBOUND msg, vertexProjections [_id,_key,content,creationDate]:
        # 1 SEEK per message + 1 NEXT per edge, and each reply document per step
        Step("reps", "read_neighbors", srcs=ref("msgs"), etype="replyOf", dir="in"),
        Step("rdocs", "docs_per_step", vids=ref("reps"), label="Comment",
             props=["id", "content", "creationDate"]),
        Step("rr", "rename", rows=ref("rdocs"),
             names={"id": "commentId", "content": "commentContent",
                    "creationDate": "commentCreationDate"}),
        # [5] TraversalNode hasCreator OUTBOUND reply, vertexProjections [_key,firstName,lastName]:
        # 1 SEEK per reply + 1 NEXT, and the author document per step (no dedup)
        Step("auth", "read_neighbors", srcs=ref("reps"), etype="hasCreator", dir="out",
             pairs=True),
        Step("av", "pluck", rows=ref("auth"), field="dst"),
        Step("adocs", "docs_per_step", vids=ref("av"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("ar", "rename", rows=ref("adocs"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        # dedup BEFORE the join: docs_per_step deliberately keeps one row per path step, so
        # joining on it directly would multiply a reply by however many times its author was
        # re-read. The reads are real; the extra rows are not.
        Step("ard", "dedup_rows", rows=ref("ar"), field="_vid"),
        Step("ra", "join", left=ref("auth"), right=ref("ard"),
             left_on="dst", right_on="_vid"),
        Step("full", "join", left=ref("ra"), right=ref("rr"),
             left_on="src", right_on="_vid"),
        # [8] SORT, [9] LIMIT 20, [10] RETURN: in memory, after all three traversals
        Step("out", "top_k", rows=ref("full"),
             keys=[("commentCreationDate", "desc"), ("commentId", "asc")], k=20),
    ]


def ic8_logical(args):
    """The LOGICAL ic8 for the optimizer's search: rank region as one `rank`
    step (unbounded -- IC8 has no maxDate; replies are Comments, ONE index),
    the body fetch as a `fetch_props` marker."""
    p = let("me_vid")
    return Plan([
        Step("me",       "read_vertex_by_id", vid=p, label="Person"),
        Step("my_msgs",  "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        Step("replies",  "read_neighbors", srcs=ref("my_msgs"), etype="replyOf", dir="in"),
        Step("top20",    "rank", vids=ref("replies"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=20,
             labels=["Comment"], read_label="Comment",
             rank_props=["creationDate", "id"], body_props=["content"]),
        Step("top_vids", "pluck", rows=ref("top20"), field="_vid"),
        Step("with_body", "fetch_props", rows=ref("top20"), vids=ref("top_vids"),
             props=["content"], label="Comment"),
        Step("author_vids",  "read_neighbors", srcs=ref("top_vids"), etype="hasCreator", dir="out"),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("authors_named", "rename", rows=ref("author_props"), names=PERSON_RENAME),
        Step("joined",   "zip_rows", left=ref("with_body"), right=ref("authors_named")),
        Step("labeled",  "rename", rows=ref("joined"),
             names={"id": "commentId", "creationDate": "commentCreationDate",
                    "content": "commentContent"}),
        Step("out",      "project", rows=ref("labeled"), fields=IC8_FIELDS),
    ], params=PERSON_PARAM, let=PERSON_LET)


LOGICAL = {"ic8": ic8_logical}

SIM = {
    "ic8": {"default": ic8_ixwalk, "nebula": ic8_onephase, "arangodb": ic8_onephase},
    # the k-way merge alias: every format runs the normal ic8 plan.
    "ic8_merge": {"default": ic8_ixwalk, "nebula": ic8_onephase, "arangodb": ic8_onephase},
}

LABEL_ONLY = {
    "ic8": {"default": ic8_getrank, "nebula": ic8_onephase, "arangodb": ic8_onephase},
    # ic8_merge on a label-only design: normal ic8.
    "ic8_merge": {"default": ic8_getrank, "nebula": ic8_onephase, "arangodb": ic8_onephase},
}

ENGINE = {
    "ic8": {"nebula": ic8_nebula_engine, "dgraph": ic8_dgraph_engine, "arangodb": ic8_arangodb_engine}
}
