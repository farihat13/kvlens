"""LDBC IC9 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (IC2_FIELDS, MESSAGE_LABELS, PERSON_RENAME, _anchor_person,
                                                 _recent_messages, _recent_messages_logical,
                                                 person_vid)


def ic9_ixwalk(args):
    """IC9, ALL formats: IC2's shape over the friends+fof set, ranked via the BOUNDED creationDate index walk
    (hi=maxDate, which folds the maxDate filter into the walk, like the engine's fused le + orderdesc). The
    friends+fof message set is DENSE, so the walk crosses few entries per param: cheaper than the GET-rank two-phase
    (ic9_getrank) and one-phase (ic9_onephase), the recorded losers."""
    return _recent_messages(args, rank_props=None, fetch_body=False, hops=2, ixwalk=True)


def ic9_getrank(args):
    """Recorded loser for dgraph (see ic9_ixwalk): rank on (id, creationDate)
    GETs per message."""
    return _recent_messages(args, rank_props=["id", "creationDate"], fetch_body=True,
                            hops=2)


def ic9_onephase(args):
    """Recorded loser for nebula/arango (see ic9_ixwalk, ic2_onephase)."""
    return _recent_messages(args, rank_props=["id", "creationDate", "content", "imageFile"],
                            fetch_body=False, hops=2)


# ── IC9 ──────────────────────────────────────────────────────────────────────
# "The 20 most recent messages by a person's friends and friends-of-friends, before maxDate."
#
# Nebula (p1). Every component falls out of the captured plan, exactly:
#
#   node 16  Traverse knows BOTH steps 1..2, dedup      S = 2*(1 + d1) = 2*(1+10) = 22
#              first step filter: Person._tag           G = 1     (the anchor only)
#                                                       N = e_1 + e_2
#   node 17  Traverse hasCreator IN from the friends    S = 451   (1 prefix each)
#              first step filter: Person._tag           G = 451   fused with vertexProps
#              vertexProps tagId 2 [firstName,lastName]           tagId 2 -- IS1's rule
#                                                       N = 119970 messages
#   node 19  AppendVertices over the messages           G = 2 * 119970
#              props: tagId 4 AND tagId 3
#   node 20  Filter creationDate < maxDate
#   node 13  TopN 20 -- after all of the above
#
#   S = 22 + 451 = 473        G = 1 + 451 + 239940 = 240392        both exact vs live.
#
# Two things this pins down that no other query has:
#
# 1. **AppendVertices reads BOTH listed tags.** A message is a Post or a Comment, so one of
#    the two rows cannot exist -- yet the node lists both tagIds and pays for both. That is
#    half this query's cost: 119970 of the 239940 GETs read a row that is not there.
#    `all_tag_props(tags=("Post","Comment"))` is that operator; see its docstring for why the
#    misses cost no prefix scan here while IS4's single-tag probe does.
#
# 2. **The anchor is not a path end.** Node 18 (`id($-.friend) != $personId`) sits after
#    node 17, but a MATCH path may not use an edge twice, so a 2-step path back to the anchor
#    (p-f-p re-uses the p-f edge) does not exist: node 17's sources are the ends minus p.
#    A plain neighbour-of-neighbour set contains p, so the plan removes it before node 17.
def ic9_nebula_engine(args):
    p = person_vid(args["person"])
    maxdate = int(args["maxDate"])
    return [
        Step("me", "read_vertex_by_id", vid=p),
        Step("mtag", "tag_probe", vids=[p], label="Person"),       # node 16 first-step filter
        # node 16: steps 1..2, BOTH, dedup:1 -- the frontier is deduped between hops
        Step("h1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("h1d", "dedup", items=ref("h1")),
        Step("h2", "read_neighbors", srcs=ref("h1d"), etype="knows", dir="both"),
        Step("fu", "concat", a=ref("h1d"), b=ref("h2")),
        Step("fd", "dedup", items=ref("fu")),
        Step("fr", "exclude", items=ref("fd"), remove=ref("me")),   # trail rule (note 2 above)
        # node 17 Traverse hasCreator IN per friend: vertexProps Person [firstName,lastName]
        # fused with the first-step Person._tag filter (1 GET), + 1 SEEK + 1 NEXT per edge
        Step("fp", "tag_probe", vids=ref("fr"), label="Person",
             props=["firstName", "lastName"]),
        Step("frn", "rename", rows=ref("fp"),
             names={"firstName": "personFirstName", "lastName": "personLastName"}),
        Step("fm", "read_neighbors", srcs=ref("fr"), etype="hasCreator", dir="in", pairs=True),
        Step("mv", "pluck", rows=ref("fm"), field="dst"),
        # node 19 AppendVertices, props list tagId 4 AND tagId 3, dedup: 1: both rows read
        Step("mp", "all_tag_props", vids=ref("mv"), tags=MESSAGE_LABELS),
        # nodes 20 Filter, 21 Project, 10 Dedup, 13 TopN 20: in memory
        Step("old", "filter", rows=ref("mp"),
             pred=E.and_(E.notnull(E.get("creationDate")), E.lt(E.field("creationDate"), maxdate))),
        Step("mr", "rename", rows=ref("old"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("tm", "join", left=ref("mr"), right=ref("fm"),
             left_on="_vid", right_on="dst"),                # adds src = the creator friend
        Step("named", "join", left=ref("tm"), right=ref("frn"),
             left_on="src", right_on="_vid"),
        Step("pid", "derive", rows=ref("named"), into="personId",
             fn=E.key_id(E.field("src"))),
        Step("top20", "top_k", rows=ref("pid"),
             keys=[("messageCreationDate", "desc"), ("messageId", "asc")], k=20),
        # LDBC output columns (in memory); a Comment has no imageFile -> None
        Step("out", "project", rows=ref("top20"), fields=IC2_FIELDS),
    ]


# Dgraph (p1). Seven var blocks, and the two that matter both read `creationDate` over the
# WHOLE message set before anything is limited:
#
#   block 6  ~hasCreator from 451 friends        -> 119970 messages   S = 451
#              @filter type(post) OR type(comment)                    S = 2   (type index)
#              @filter lt(creationDate, maxDate)  vals=119970         G = 119970
#   block 7  orderdesc creationDate, orderasc id, first 20            G = 117061 (one per
#              -- 117061 survived the date filter                         candidate, 2.3)
#            projections: 4 scalars x 20 winners + hasCreator + 3 x 20 authors  G = 140
#
#   G = 119970 + 117061 + 140 = 237171, exact vs live.
#
# The two reads of the same predicate are the engine's filter-then-sort split: the date
# filter has no idea the sort will keep 20, and the sort has no idea the filter already read
# every value. Cache is off, so neither read helps the other. 237031 of this query's 237171
# GETs -- 99.94% -- are spent on messages that never appear in the answer. At sf0.3: S and N
# exact, G -2 at every param (the split Comment type posting -- see ic2.py).
def ic9_dgraph_engine(args):
    p = person_vid(args["person"])
    maxdate = int(args["maxDate"])
    return _anchor_person(p) + [
        # block 1: knows + ~knows from the anchor
        Step("l1", "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        # block 2: type(person) AND NOT uid(P)
        Step("l1p", "read_filter_by_label_index", vids=ref("l1"), label="Person"),
        Step("f1", "exclude", items=ref("l1p"), remove=ref("me")),
        # block 3: the second hop, from the level-1 frontier
        Step("l2", "read_neighbors", srcs=ref("f1"), etype="knows", dir="both"),
        # block 4: type(person) AND NOT uid(P, F1)
        Step("l2p", "read_filter_by_label_index", vids=ref("l2"), label="Person"),
        Step("f2a", "exclude", items=ref("l2p"), remove=ref("me")),
        Step("f2", "exclude", items=ref("f2a"), remove=ref("f1")),
        # block 5: F = F1 + F2
        Step("fu", "concat", a=ref("f1"), b=ref("f2")),
        Step("fr", "dedup", items=ref("fu")),
        # block 6: every message by every friend, then the date read for every one of them
        Step("fm", "read_neighbors", srcs=ref("fr"), etype="hasCreator", dir="in", pairs=True),
        Step("mv", "pluck", rows=ref("fm"), field="dst"),
        Step("mp", "read_filter_by_label_index", vids=ref("mv"), label="Post"),
        Step("mc", "read_filter_by_label_index", vids=ref("mv"), label="Comment"),
        Step("msgs", "concat", a=ref("mp"), b=ref("mc")),
        Step("dates", "read_vertex_props", vids=ref("msgs"), props=["creationDate"]),
        Step("old", "filter", rows=ref("dates"),
             pred=E.lt(E.field("creationDate"), maxdate)),
        # block 7: the multi-key sort, then the projections on the 20 winners only
        Step("top", "sort_topk", rows=ref("old"), prop="creationDate", k=20,
             second_key="id", id_order="asc"),
        Step("topv", "pluck", rows=ref("top"), field="_vid"),
        Step("mprops", "read_vertex_props", vids=ref("topv"),
             props=["id", "creationDate", "content", "imageFile"]),
        Step("mr", "rename", rows=ref("mprops"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("auth", "read_neighbors", srcs=ref("topv"), etype="hasCreator", dir="out"),
        Step("authp", "read_filter_by_label_index", vids=ref("auth"), label="Person"),
        Step("ap", "read_vertex_props", vids=ref("authp"), props=["id", "firstName", "lastName"]),
        Step("ar", "rename", rows=ref("ap"), names=PERSON_RENAME),
        Step("out", "zip_rows", left=ref("mr"), right=ref("ar")),
    ]


# Arango (p1): S = 474 and N = 120638 are nebula's numbers plus the traversal_open seek, but
# G is 477618 against nebula's 240392. The whole gap is ONE engine rule, measured here for
# the first time:
#
#   A TraversalNode with a postFilter materializes EVERY step to evaluate the filter, and
#   then materializes every SURVIVING step AGAIN as it emits it.
#
# It explains both traversals at once, exactly:
#
#   node [3]  knows 1..2 ANY, postFilter friend._id != startPerson
#             11 expansions + 658 steps, 658 survive -> 2 x (11+658+658) =    2654
#   node [7]  hasCreator, postFilter msg.creationDate < maxDate
#             451 expansions + 119970 steps, 117061 survive
#                                       -> 2 x (451+119970+117061)      =  474964
#                                                                        --------
#                                                                          477618  = live G
#
# The measurements that pin it down (cumulative AQL prefixes, /kvstats deltas):
#
#   walk, nothing referenced          G = 0        <- traversals are demand-driven
#   walk referenced, NO filter        G = 1316     <- 2 x 658: one read per emitted step
#   walk + a filter dropping ALL      G = 1338     <- 2 x (11 + 658): no survivors to re-read
#   walk + a filter dropping NONE     G = 2654     <- 2 x (11 + 658 + 658)
#   the same at depth 1..1            G =   42     <- 2 x ( 1 +  10 +  10)
#   messages referenced, NO filter    G = +239940  <- 2 x 119970
#   msg filter keeping nothing        G = +902     <- 2 x 451 expansions, 0 survivors
#   msg filter keeping everything     G = +240842  <- 2 x (451 + 119970)... plus 0 re-reads?
#                                                     no: 2 x (451 + 119970) = 240842, exact
#   msg filter keeping 117061         G = +235024  <- 2 x (117061 + 451), exact
#
# The 11 at depth 1..2 against 1 at depth 1..1 is what identifies the extra term as
# per-EXPANSION (the start vertex, plus each depth-1 vertex the walk expands from) rather
# than per-step. It also re-explains node [7]'s 451, which is not "one
# materialization per COLLECT group" -- varying the group count with a LIMIT gave
# 1/5/20/100/451 groups -> 2/10/40/200/902 GETs, equally consistent with expansion events,
# and only the expansion reading also accounts for the 11. One rule, not two.
#
# The second read is NOT the projection: adding the query's real `RETURN {...}` on top of the
# filtered traversal costs zero more, and the doubling shows up even when the body is
# `RETURN 1`. It is the filtered traversal emitting its survivors.
#
# What this costs the query: 235024 of 477618 GETs -- 49% -- are a second read of a document
# the engine had in hand a moment earlier.
def ic9_arangodb_engine(args):
    p = person_vid(args["person"])
    maxdate = int(args["maxDate"])
    return [
        Step("open", "traversal_open", items=[p], etype="knows"),
        # node [3]: 1..2 ANY with the `friend._id != startPerson` postFilter
        Step("wkeep", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both",
             depth=2, props=["id"], label="Person",
             pred=E.ne(E.field("_vid"), p)),
        Step("wv", "pluck", rows=ref("wkeep"), field="_vid"),
        # COLLECT f = friend: the distinct friends, one group each
        Step("fr", "dedup", items=ref("wv")),
        # node [7] expands from each of these, and a filtered traversal materializes every
        # vertex it expands FROM as well as every step it emits
        Step("fdocs", "docs_per_step", vids=ref("fr"), label="Person",
             props=["id", "firstName", "lastName"]),
        Step("frn", "rename", rows=ref("fdocs"),
             names={"id": "personId", "firstName": "personFirstName",
                    "lastName": "personLastName"}),
        # node [7]: hasCreator IN, per group
        Step("msgs", "read_neighbors", srcs=ref("fr"), etype="hasCreator", dir="in", pairs=True),
        Step("mv", "pluck", rows=ref("msgs"), field="dst"),
        Step("mdocs", "docs_per_step", vids=ref("mv"), label=MESSAGE_LABELS,
             props=["id", "creationDate", "content", "imageFile"]),
        Step("old", "filter", rows=ref("mdocs"),
             pred=E.and_(E.notnull(E.get("creationDate")), E.lt(E.field("creationDate"), maxdate))),
        Step("ov", "pluck", rows=ref("old"), field="_vid"),
        # the surviving messages, read again as the filtered traversal emits them
        Step("oemit", "docs_per_step", vids=ref("ov"), label=MESSAGE_LABELS,
             props=["id", "creationDate", "content", "imageFile"]),
        Step("mr", "rename", rows=ref("oemit"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("md", "dedup_rows", rows=ref("mr"), field="_vid"),
        Step("tm", "join", left=ref("md"), right=ref("msgs"),
             left_on="_vid", right_on="dst"),               # adds src = the creator friend
        Step("named", "join", left=ref("tm"), right=ref("frn"),
             left_on="src", right_on="_vid"),
        Step("out", "top_k", rows=ref("named"),
             keys=[("messageCreationDate", "desc"), ("messageId", "asc")], k=20),
    ]


def ic9_logical(args):
    """The LOGICAL ic9 (the 2-hop ic2) for the optimizer's search."""
    return _recent_messages_logical(args, hops=2)


LOGICAL = {"ic9": ic9_logical}

SIM = {
    "ic9": ic9_ixwalk,
    "ic9_merge": ic9_ixwalk,        # the k-way merge alias: every format runs the normal ic9
}

LABEL_ONLY = {
    "ic9": {"default": ic9_getrank, "nebula": ic9_onephase, "arangodb": ic9_onephase},
    "ic9_merge": {"default": ic9_getrank, "nebula": ic9_onephase, "arangodb": ic9_onephase},
}

ENGINE = {
    "ic9": {"nebula": ic9_nebula_engine, "dgraph": ic9_dgraph_engine, "arangodb": ic9_arangodb_engine}
}

# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic9": _KNOWS_HINTS, "ic9_merge": _KNOWS_HINTS}
