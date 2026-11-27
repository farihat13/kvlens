"""LDBC IC2 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (IC2_FIELDS, MESSAGE_LABELS, PERSON_RENAME, _anchor_person,
                                                 _recent_messages, _recent_messages_logical,
                                                 person_vid)


# LDBC's message column names (the engine plans emit the predicate names)
_IC2_MESSAGE_RENAME = {"id": "messageId", "creationDate": "messageCreationDate",
                       "content": "messageContent", "imageFile": "messageImageFile"}


def ic2_ixwalk(args):
    """IC2, ALL formats: the bounded creationDate index walk (hi=maxDate), winner-props fused. Cheaper on weighted ops
    than the recorded losers (ic2_getrank for dgraph, ic2_onephase for nebula/arango), same results.
    """
    return _recent_messages(args, rank_props=None, fetch_body=False, hops=1,
                            ixwalk=True)


def ic2_getrank(args):
    """Recorded loser for dgraph (see ic2_ixwalk): rank all friend messages on
    (creationDate, id) = 2 GETs each, then fetch content/imageFile for the top
    20 only (+40 GETs). Beats one-phase (4 GETs per message) but not the walk."""
    return _recent_messages(args, rank_props=["id", "creationDate"], fetch_body=True,
                            hops=1)


def ic2_onephase(args):
    """Recorded loser for nebula/arango (see ic2_ixwalk): rank by reading every friend message's whole row/doc; the
    dense candidate set makes the bounded index walk cheaper.
    """
    return _recent_messages(args, rank_props=["id", "creationDate", "content", "imageFile"],
                            fetch_body=False, hops=1)


def ic2_nolimit(args):
    """IC2 with the LIMIT removed (k=None): return ALL of the friend set's messages
    before maxDate, and read firstName/lastName for EVERY distinct friend-author (not
    just the top-20's authors). An ablation probe for property grouping (several predicates in one key) -- it
    widens the person-prop read from the ~5 authors of the top 20 to the whole authoring
    friend set, so the grouping saving scales with the friend set, not the limit."""
    return _recent_messages(args, rank_props=["id", "creationDate"], fetch_body=True,
                            hops=1, k=None)


# ── IC2 engine-faithful plans ──────────────────────────────────────────────────────────────────
# Each builder transcribes the engine's captured sf0.3 plan
# (ldbc/<db>/ic2/p1/plan.txt). Every KV step cites the plan
# node it models; in-memory steps cost nothing and are grouped.


def ic2_dgraph_engine(args):
    """IC2 dgraph, from dgraph/ic2/p1/plan.txt (blocks 0-6).

    The `lt(creationDate, $maxDate)` filters are read per vertex: the live baseline runs at
    ineqfilterpath=per_vertex (live_kvstats.csv `ineqpath`). The sf0.3 capture pinned `index`;
    the plan tree is the same, only that filter's access path differs.

    The engine returns both blocks' top-20 (40 rows); the LDBC client merges them to 20, so the
    plan stops at the two blocks.

    sf0.3 vs live: S and N exact at every param; G -2 at every param. The same -2 per
    type(comment) read shows in ic3/ic8/ic9: at sf0.3 the Comment dgraph.type posting is big
    enough to be split into parts, which the store does not model."""
    p = person_vid(args["person"])
    maxdate = int(args["maxDate"])      # param rows are strings; creationDate is epoch ms
    steps = _anchor_person(p) + [      # block 0: eq(id) + type(person)
        # block 1: knows + ~knows from P
        Step("fr_raw", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("fr_dedup", "dedup", items=ref("fr_raw")),
        # block 2: F = type(person) (1 type-index SEEK) AND NOT uid(P) (in memory)
        Step("fr_t", "read_filter_by_label_index", vids=ref("fr_dedup"), label="Person"),
        Step("friends", "exclude", items=ref("fr_t"), remove=[p]),
    ]
    for label, tag, props in (("Post", "posts", ["id", "content", "imageFile", "creationDate"]),
                              ("Comment", "comments", ["id", "content", "creationDate"])):
        steps += [
            # block 3 / 5: ~hasCreator from each friend (1 posting SEEK each), @filter
            # type(<label>) (1 type-index SEEK) AND lt(creationDate) (1 read per message)
            Step(f"msgs_{tag}", "read_neighbors", srcs=ref("friends"), etype="hasCreator", dir="in"),
            Step(f"typed_{tag}", "read_filter_by_label_index", vids=ref(f"msgs_{tag}"), label=label),
            Step(f"dated_{tag}", "read_vertex_props", vids=ref(f"msgs_{tag}"), props=["creationDate"]),
            Step(f"cand_{tag}", "filter_in", rows=ref(f"dated_{tag}"), field="_vid",
                 allowed=ref(f"typed_{tag}")),
            Step(f"win_{tag}", "filter", rows=ref(f"cand_{tag}"),
                 pred=E.lt(E.field("creationDate"), maxdate)),
            # block 4 / 6: orderdesc creationDate, orderasc id, first 20 (sort_topk)
            Step(f"top_{tag}", "sort_topk", rows=ref(f"win_{tag}"), prop="creationDate",
                 k=20, second_key="id", id_order="asc"),
            Step(f"topv_{tag}", "pluck", rows=ref(f"top_{tag}"), field="_vid"),
            # block 4 / 6: the message's scalars, as listed in the block
            Step(f"props_{tag}", "read_vertex_props", vids=ref(f"topv_{tag}"), props=props),
            # block 4 / 6: hasCreator @filter(type(person)) { id firstName lastName }
            Step(f"auth_{tag}", "read_neighbors", srcs=ref(f"topv_{tag}"), etype="hasCreator",
                 dir="out"),
            Step(f"autht_{tag}", "read_filter_by_label_index", vids=ref(f"auth_{tag}"),
                 label="Person"),
            Step(f"authp_{tag}", "read_vertex_props", vids=ref(f"autht_{tag}"),
                 props=["id", "firstName", "lastName"]),
            # in memory: each message has one creator, so author row i is message i's
            Step(f"authn_{tag}", "rename", rows=ref(f"authp_{tag}"),
                 names={**PERSON_RENAME, "_vid": "_author"}),
            Step(f"named_{tag}", "zip_rows", left=ref(f"props_{tag}"), right=ref(f"authn_{tag}")),
        ]
    steps += [
        Step("both", "concat", a=ref("named_posts"), b=ref("named_comments")),
        # LDBC output columns (in memory); a Comment has no imageFile -> None
        Step("labeled", "rename", rows=ref("both"), names=_IC2_MESSAGE_RENAME),
        Step("out", "project", rows=ref("labeled"), fields=IC2_FIELDS),
    ]
    return steps


def ic2_nebula_engine(args):
    """IC2 nebula, from nebula/ic2/p1/plan.txt: a UNION ALL of a Post branch (nodes 30, 31, 37)
    and a Comment branch (33, 34, 40). Each branch walks knows and hasCreator on its own, and
    AppendVertices reads its tag for every message, a miss included, before TopN takes 20.

    The engine returns both branches' top-20 (40 rows); the client merges them.
    sf0.3 vs live: S, G and N exact at every param."""
    p = person_vid(args["person"])
    maxdate = int(args["maxDate"])
    steps = [Step("me", "read_vertex_by_id", vid=p)]
    for label, tag, props in (("Post", "posts", ["id", "creationDate", "content", "imageFile"]),
                              ("Comment", "comments", ["id", "creationDate", "content"])):
        steps += [
            # node 30 / 33 Traverse knows BOTH from p: first-step filter Person._tag (1 GET on
            # p), then 1 SEEK per direction + 1 NEXT per edge; dedup: 1
            Step(f"ptag_{tag}", "tag_probe", vids=[p], label="Person"),
            Step(f"fr_{tag}", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
            Step(f"friends_{tag}", "dedup", items=ref(f"fr_{tag}")),
            # node 31 / 34 Traverse hasCreator IN from each friend: vertexProps Person
            # [firstName,lastName] fused with the first-step Person._tag filter (1 GET), then
            # 1 SEEK + 1 NEXT per edge
            Step(f"fnames_{tag}", "tag_probe", vids=ref(f"friends_{tag}"), label="Person",
                 props=["firstName", "lastName"]),
            # (pairs: output shape only -- keeps each message's creator friend for the answer)
            Step(f"msgs_{tag}", "read_neighbors", srcs=ref(f"friends_{tag}"), etype="hasCreator",
                 dir="in", pairs=True),
            Step(f"msgv_{tag}", "pluck", rows=ref(f"msgs_{tag}"), field="dst"),
            # node 37 / 40 AppendVertices Post [imageFile,creationDate,content] / Comment
            # [creationDate,content], filter <label>._tag, dedup: 1 -- 1 GET per message; a
            # miss adds storaged's prefix scan
            Step(f"rows_{tag}", "read_vertex_props", vids=ref(f"msgv_{tag}"), label=label,
                 props=props),
            # node 38 / 41 Filter creationDate, node 24 / 25 TopN 20: in memory
            Step(f"real_{tag}", "filter", rows=ref(f"rows_{tag}"),
                 pred=E.notnull(E.get("creationDate"))),
            Step(f"win_{tag}", "filter", rows=ref(f"real_{tag}"),
                 pred=E.lt(E.field("creationDate"), maxdate)),
            Step(f"top_{tag}", "top_k", rows=ref(f"win_{tag}"),
                 keys=[("creationDate", "desc"), ("id", "asc")], k=20),
            # in memory: the creator friend (node 31 / 34's src) and its names (its vertexProps)
            Step(f"creator_{tag}", "join", left=ref(f"top_{tag}"), right=ref(f"msgs_{tag}"),
                 left_on="_vid", right_on="dst"),
            Step(f"fnamed_{tag}", "rename", rows=ref(f"fnames_{tag}"),
                 names={"firstName": "personFirstName", "lastName": "personLastName"}),
            Step(f"named_{tag}", "join", left=ref(f"creator_{tag}"), right=ref(f"fnamed_{tag}"),
                 left_on="src", right_on="_vid"),
        ]
    # node 23 Union: 40 rows leave graphd
    steps += [
        Step("both", "concat", a=ref("named_posts"), b=ref("named_comments")),
        # LDBC output columns (in memory): id(friend) is the vid; a Comment has no imageFile
        Step("pid", "derive", rows=ref("both"), into="personId", fn=E.key_id(E.field("src"))),
        Step("labeled", "rename", rows=ref("pid"), names=_IC2_MESSAGE_RENAME),
        Step("out", "project", rows=ref("labeled"), fields=IC2_FIELDS),
    ]
    return steps


def ic2_arangodb_engine(args):
    """IC2 arango, from arangodb/ic2/p1/plan.txt: two subqueries, each re-walking knows and then
    a hasCreator traversal whose postFilter (collection + date) reads every message document.
    Arango merges server-side ([32]-[36]), so 20 rows come back, not 40.
    sf0.3 vs live: S, G and N exact at every param."""
    p = person_vid(args["person"])
    maxdate = int(args["maxDate"])
    steps = [
        # [2] LET person = DOCUMENT(...): pk + doc, once
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        Step("mev", "pluck", rows=ref("me"), field="_vid"),
        # the first traversal's one extra SEEK, once per query
        Step("open", "traversal_open", items=ref("mev"), etype="knows"),
    ]
    for label, tag in (("Post", "posts"), ("Comment", "comments")):
        steps += [
            # [4] / [18] TraversalNode knows IN,OUT 1..1, vertexProjections
            # [_id,_key,firstName,lastName], edgeProjections [_from,_to] (covering): 2 SEEK +
            # 1 NEXT per edge, and each friend's document per step
            Step(f"fr_{tag}", "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),
            Step(f"frdocs_{tag}", "docs_per_step", vids=ref(f"fr_{tag}"), label="Person",
                 props=["id", "firstName", "lastName"]),
            # [5] / [19] TraversalNode hasCreator INBOUND friend, no vertexProjections, postFilter
            # IS_SAME_COLLECTION(<label>, msg) AND msg.creationDate < @maxDate
            Step(f"kept_{tag}", "traverse_filtered", srcs=ref(f"fr_{tag}"), etype="hasCreator",
                 dir="in", depth=1, props=["id", "creationDate", "content", "imageFile"],
                 label=MESSAGE_LABELS,
                 pred=E.and_(E.eq(E.get("_label"), label), E.notnull(E.get("creationDate")),
                             E.lt(E.field("creationDate"), maxdate))),
            # [12] / [26] SORT creationDate DESC, key ASC + LIMIT 20: in memory
            Step(f"top_{tag}", "top_k", rows=ref(f"kept_{tag}"),
                 keys=[("creationDate", "desc"), ("id", "asc")], k=20),
            # in memory: each message's friend (the traversal's start, `_src`) from its projection
            Step(f"frn_{tag}", "rename", rows=ref(f"frdocs_{tag}"), names=PERSON_RENAME),
            Step(f"frp_{tag}", "project", rows=ref(f"frn_{tag}"),
                 fields=["_vid", "personId", "personFirstName", "personLastName"]),
            Step(f"named_{tag}", "join", left=ref(f"top_{tag}"), right=ref(f"frp_{tag}"),
                 left_on="_src", right_on="_vid"),
        ]
    # [31]-[36] APPEND, SORT, LIMIT 20: in memory
    steps += [
        Step("merged", "concat", a=ref("named_posts"), b=ref("named_comments")),
        Step("top20", "top_k", rows=ref("merged"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=20),
        # LDBC output columns (in memory)
        Step("labeled", "rename", rows=ref("top20"), names=_IC2_MESSAGE_RENAME),
        Step("out", "project", rows=ref("labeled"), fields=IC2_FIELDS),
    ]
    return steps


def ic2_logical(args):
    """The LOGICAL ic2 for the optimizer's search: the rank region is one `rank`
    step; the catalog's rules lower it to ixwalk / getrank / onephase."""
    return _recent_messages_logical(args, hops=1)


LOGICAL = {"ic2": ic2_logical,
           "ic2_nolimit": ic2_nolimit}   # ablation probe (no LIMIT): single candidate (passthrough), its own hand plan

SIM = {
    "ic2": ic2_ixwalk,
    "ic2_merge": ic2_ixwalk,        # the k-way merge alias: every format runs the normal ic2
}

LABEL_ONLY = {
    "ic2": {"default": ic2_getrank, "nebula": ic2_onephase, "arangodb": ic2_onephase},
    # ic2_merge on a label-only design (dgraph baseline): the same label-only ic2 plan.
    "ic2_merge": {"default": ic2_getrank, "nebula": ic2_onephase, "arangodb": ic2_onephase},
}

ENGINE = {
    "ic2": {"dgraph": ic2_dgraph_engine, "nebula": ic2_nebula_engine, "arangodb": ic2_arangodb_engine}
}

# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic2": _KNOWS_HINTS, "ic2_merge": _KNOWS_HINTS, "ic2_nolimit": _KNOWS_HINTS}
