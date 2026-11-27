"""LDBC IC7 plans (sim + engine)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import not_, floordiv, sub, field, let
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.interactive._shared import (MESSAGE_LABELS, PERSON_RENAME, person_vid, PERSON_PARAM, PERSON_LET,
                                                 _anchor_person)


# ------------------------------------------------------------------------ IC7

def _ic7_winner_tail(cand, logical=False):
    """IC7 tail shared by the label-only and the facet-sorted feature plan. Input `cand` = the ref
    name of the ~20 candidate distinct likers (rows: _vid=liker, _msg=represented message,
    likeCreationDate). Reads liker id+name and message id+body for the WINNERS ONLY, does the final
    top-20 by (likeCreationDate desc, personId asc), then isNew (liker not among P's friends) and
    latencyMinutes. Both callers define `a` (the person vertex)."""
    return [
        Step("liker_v",     "pluck", rows=ref(cand), field="_vid"),
        Step("liker_props", "read_vertex_props", vids=ref("liker_v"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("liker_named", "rename", rows=ref("liker_props"), names=PERSON_RENAME),
        Step("liker_join",  "join", left=ref(cand), right=ref("liker_named"), left_on="_vid", right_on="_vid"),
        Step("top20",       "top_k", rows=ref("liker_join"),
             keys=[("likeCreationDate", "desc"), ("personId", "asc")], k=20),
        Step("msg_v",       "pluck", rows=ref("top20"), field="_msg"),
        Step("msg_props",   "read_vertex_props", vids=ref("msg_v"),
             props=["id", "content", "imageFile", "creationDate"], label=MESSAGE_LABELS),
        Step("msg_named",   "rename", rows=ref("msg_props"),
             names={"id": "messageId", "content": "messageContent",
                    "imageFile": "messageImageFile", "creationDate": "_msgDate"}),
        Step("with_msg",    "join", left=ref("top20"), right=ref("msg_named"), left_on="_msg", right_on="_vid"),
    ] + ([
        # the optimizer's membership choice point: materialize the friend set vs
        # probe the knows edge per winner (read_edge_exists)
        Step("flagged", "membership_flag", rows=ref("with_msg"), field="_vid",
             etype="knows", dir="both", into="_knows", anchors=ref("a"),
             set_read_name="friends0", set_name="friends"),
    ] if logical else [
        Step("friends0",    "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("friends",     "dedup", items=ref("friends0")),
        Step("flagged",     "flag_in", rows=ref("with_msg"), field="_vid", allowed=ref("friends"), into="_knows"),
    ]) + [
        Step("with_new",    "derive", rows=ref("flagged"), into="isNew", fn=not_(field("_knows"))),
        Step("with_lat",    "derive", rows=ref("with_new"), into="latencyMinutes",
             fn=floordiv(sub(field("likeCreationDate"), field("_msgDate")), 60000)),
        Step("out",         "project", rows=ref("with_lat"),
             fields=["personId", "personFirstName", "personLastName", "likeCreationDate",
                     "messageId", "messageContent", "messageImageFile",
                     "latencyMinutes", "isNew"]),
    ]


def ic7(args):
    """IC7 for ALL formats: newest like per liker of `person`'s messages -> top 20 by
    (likeCreationDate desc, personId asc), with the liked message + like latency + isNew.

    Ranks on the like edge's creationDate -- a FREE edge facet already read by `lk` -- so per-liker recency needs NO
    property GETs; id/name/body are fetched for the ~20 winners only. group_top ranks on date alone (the inner
    smallest-message-id tie-break never fires in the data); top_k keeps boundary-date ties so the deferred
    personId-asc re-sort stays exact.
    """
    p = let("me_vid")
    return Plan([
        Step("a",          "read_vertex_by_id", vid=p, label="Person"),
        Step("msgs",       "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
        Step("like_edges", "read_neighbors", srcs=ref("msgs"), etype="likes", dir="in",
             edge_props=["creationDate"], pairs=True),
        Step("liker_rows", "rename", rows=ref("like_edges"),
             names={"src": "_msg", "dst": "_vid", "creationDate": "likeCreationDate"}),
        Step("newest",     "group_top", rows=ref("liker_rows"), by="_vid",
             keys=[("likeCreationDate", "desc")]),      # newest like per liker (0 GET -- free facet)
        Step("cand",       "top_k", rows=ref("newest"),
             keys=[("likeCreationDate", "desc")], k=20, keep_ties=True),
    ] + _ic7_winner_tail("cand"), params=PERSON_PARAM, let=PERSON_LET)


# single candidate: the logical plan IS the physical one (passthrough search)
def ic7_logical(args):
    """The LOGICAL ic7: the friend check is a membership_flag choice point."""
    p = let("me_vid")
    return Plan([
        Step("a",          "read_vertex_by_id", vid=p, label="Person"),
        Step("msgs",       "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
        Step("like_edges", "read_neighbors", srcs=ref("msgs"), etype="likes", dir="in",
             edge_props=["creationDate"], pairs=True),
        Step("liker_rows", "rename", rows=ref("like_edges"),
             names={"src": "_msg", "dst": "_vid", "creationDate": "likeCreationDate"}),
        Step("newest",     "group_top", rows=ref("liker_rows"), by="_vid",
             keys=[("likeCreationDate", "desc")]),
        Step("cand",       "top_k", rows=ref("newest"),
             keys=[("likeCreationDate", "desc")], k=20, keep_ties=True),
    ] + _ic7_winner_tail("cand", logical=True), params=PERSON_PARAM, let=PERSON_LET)


LOGICAL = {"ic7": ic7_logical}

SIM = {
    "ic7": ic7
}

# ── IC7 engine-faithful plans ──────────────────────────────────────────────────────────────────
# Each builder transcribes the engine's captured sf0.3 plan
# (ldbc/<db>/ic7/p1/plan.txt).
# Every KV step cites the plan node it models; in-memory steps cost nothing and are grouped.
# Status vs live /kvstats at sf0.3 (6 params) is in each builder's docstring.
#
# The recency key is the like edge's creationDate, which the likes traversal already carries
# (dgraph posting facet, nebula edge value, arango edge doc). So no engine walks a date index.

def _ic7_engine_finish(src):
    """Shared IC7 tail once every liker row carries personId/name, messageId/content/imageFile,
    _msgDate, likeCreationDate and the _knows flag: isNew + latencyMinutes, then the client/engine
    sort by (likeCreationDate desc, personId asc) and LIMIT 20, then the LDBC projection. In memory."""
    return [
        Step("with_new", "derive", rows=ref(src), into="isNew", fn=not_(field("_knows"))),
        Step("with_lat", "derive", rows=ref("with_new"), into="latencyMinutes",
             fn=floordiv(sub(field("likeCreationDate"), field("_msgDate")), 60000)),
        Step("top20", "top_k", rows=ref("with_lat"),
             keys=[("likeCreationDate", "desc"), ("personId", "asc")], k=20),
        Step("out", "project", rows=ref("top20"),
             fields=["personId", "personFirstName", "personLastName", "likeCreationDate",
                     "messageId", "messageContent", "messageImageFile",
                     "latencyMinutes", "isNew"]),
    ]


_IC7_MSG_NAMES = {"id": "messageId", "content": "messageContent",
                  "imageFile": "messageImageFile", "creationDate": "_msgDate"}
_IC7_LIKE_NAMES = {"src": "_msg", "dst": "_vid", "creationDate": "likeCreationDate"}


def ic7_dgraph_engine(args):
    """IC7 dgraph, from dgraph/ic7/p1/plan.txt (blocks 0-4 of the DQL).

    The result block is `result(func: uid(L), first: 200)`: it reads names and the liked message
    for the FIRST 200 likers in uid order only, not for every liker. The loader assigns uids, so
    that order is the live Dgraph's own: first_k_by_uid takes it from the uid map captured with the
    live counts. (Before the map, the sim took the first 200 by vid, a different 200: at sf0.3 p1
    it read 38 liked messages where live read 48, G 752 vs 794.) The +2 G left is Dgraph's split
    Comment type index (engine read, not modelled). S is exact at every param."""
    p = person_vid(args["person"])
    return _anchor_person(p) + [                  # block 0: eq(id) + type(person)
        # block 1: M as ~hasCreator @filter(type(post) OR type(comment)) -- the reverse posting +
        # one type-index SEEK per type
        Step("my_msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        Step("mp", "read_filter_by_label_index", vids=ref("my_msgs"), label="Post"),
        Step("mc", "read_filter_by_label_index", vids=ref("my_msgs"), label="Comment"),
        Step("M", "concat", a=ref("mp"), b=ref("mc")),
        # block 2: ~likes { L as uid } -- one reverse-likes posting per message
        Step("like_edges", "read_neighbors", srcs=ref("M"), etype="likes", dir="in", pairs=True),
        Step("L0", "pluck", rows=ref("like_edges"), field="dst"),
        Step("L", "dedup", items=ref("L0")),
        # block 3: count(knows) @filter(uid(P)) + count(~knows) @filter(uid(P)) -- both postings
        # of every liker (count_vals=1248 each at p1)
        Step("lk", "read_neighbors", srcs=ref("L"), etype="knows", dir="both", pairs=True),
        Step("lk_p", "filter", rows=ref("lk"), pred=E.eq(E.get("dst"), p)),
        Step("fl0", "pluck", rows=ref("lk_p"), field="src"),
        Step("friends", "dedup", items=ref("fl0")),
        # block 4: result(func: uid(L), first: 200) -- the first 200 likers in the live Dgraph's
        # uid order (in memory; the uid map is captured with the live counts)
        Step("res_v", "first_k_by_uid", items=ref("L"), k=200),
        # block 4: friendId: id, friendFirstName, friendLastName (vals=200 each at p1)
        Step("res_props", "read_vertex_props", vids=ref("res_v"),
             props=["id", "firstName", "lastName"], label="Person"),
        # block 4: likes @facets(orderdesc: creationDate) @filter(uid(M)) (first: 1) -- each result
        # liker's forward likes posting (the facet rides in it), kept to M, newest first
        Step("res_likes", "read_neighbors", srcs=ref("res_v"), etype="likes", dir="out",
             edge_props=["creationDate"], pairs=True),
        Step("res_likes_m", "filter_in", rows=ref("res_likes"), field="dst", allowed=ref("M")),
        Step("liker_rows", "rename", rows=ref("res_likes_m"),
             names={"src": "_vid", "dst": "_msg", "creationDate": "likeCreationDate"}),
        Step("newest", "group_top", rows=ref("liker_rows"), by="_vid",
             keys=[("likeCreationDate", "desc")]),
        # block 4: messageId/messageContent/messageImageFile/messageCreationDate of each kept message
        Step("msg_v", "pluck", rows=ref("newest"), field="_msg"),
        Step("msg_props", "read_vertex_props", vids=ref("msg_v"),
             props=["id", "content", "imageFile", "creationDate"], label=MESSAGE_LABELS),
        # in memory: the client joins, flags isNew, sorts, LIMIT 20
        Step("liker_named", "rename", rows=ref("res_props"), names=PERSON_RENAME),
        Step("with_liker", "join", left=ref("newest"), right=ref("liker_named"),
             left_on="_vid", right_on="_vid"),
        Step("msg_named", "rename", rows=ref("msg_props"), names=_IC7_MSG_NAMES),
        Step("with_msg", "join", left=ref("with_liker"), right=ref("msg_named"),
             left_on="_msg", right_on="_vid"),
        Step("flagged", "flag_in", rows=ref("with_msg"), field="_vid",
             allowed=ref("friends"), into="_knows"),
    ] + _ic7_engine_finish("flagged")


def ic7_nebula_engine(args):
    """IC7 nebula, from nebula/ic7/p1/plan.txt.

    Two nodes read ALL 8 tags of their SOURCE vertices (their vertexProps list 8 tagIds): node 5
    (the likes Traverse, sources = the person's messages) and node 14 (the knows Traverse,
    sources = the likers). Node 15 then reads all 8 tags of every distinct knows-neighbour
    (`dedup: 1`), because the isNew pattern `not((liker)-[:KNOWS]-(person))` is materialized
    before it is matched. Messages' bodies come from node 5's read; no later read.

    sf0.3 vs live: S, G and N all exact at every param. With M messages, L like edges, D distinct
    likers, K knows edges and DN distinct knows-neighbours of the likers:
    S = 1 + M + 2D, G = 1 + 8M + D + 8D + 8DN, N = M + L + K."""
    p = person_vid(args["person"])
    return [
        Step("me", "read_vertex_by_id", vid=p),
        # node 22 Traverse: hasCreator IN_EDGE from the person, `first step filter: Person._tag`
        Step("mtag", "tag_probe", vids=[p], label="Person"),
        Step("my_msgs", "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),
        # node 5 Traverse: likes IN_EDGE (edgeProps creationDate) from each message; vertexProps =
        # all 8 tags, so each source message is read with 8 GETs (dedup: 1)
        Step("msg_mat", "all_tag_props", vids=ref("my_msgs")),
        Step("like_edges", "read_neighbors", srcs=ref("my_msgs"), etype="likes", dir="in",
             edge_props=["creationDate"], pairs=True),
        # node 23 AppendVertices: the liker's Person tag only (firstName, lastName), dedup: 1
        Step("liker_all", "pluck", rows=ref("like_edges"), field="dst"),
        Step("liker_props", "read_vertex_props", vids=ref("liker_all"),
             props=["id", "firstName", "lastName"], label="Person"),
        # nodes 20/9/10/11 (Project, Sort, Aggregate by liker, head(collect)): in memory
        Step("liker_rows", "rename", rows=ref("like_edges"), names=_IC7_LIKE_NAMES),
        Step("newest", "group_top", rows=ref("liker_rows"), by="_vid",
             keys=[("likeCreationDate", "desc")]),
        # node 14 Traverse (Argument = the grouped likers): knows BOTH; vertexProps = all 8 tags,
        # so each liker source is read again with 8 GETs (dedup: 1)
        Step("liker_v", "pluck", rows=ref("newest"), field="_vid"),
        Step("liker_mat", "all_tag_props", vids=ref("liker_v")),
        Step("lk", "read_neighbors", srcs=ref("liker_v"), etype="knows", dir="both", pairs=True),
        # node 15 AppendVertices: all 8 tags of every distinct knows-neighbour (dedup: 1)
        Step("lk_nbr", "pluck", rows=ref("lk"), field="dst"),
        Step("nbr_mat", "all_tag_props", vids=ref("lk_nbr")),
        # nodes 16/17 (PathBuild, RollUpApply on liker+person): in memory
        Step("lk_p", "filter", rows=ref("lk"), pred=E.eq(E.get("dst"), p)),
        Step("fl0", "pluck", rows=ref("lk_p"), field="src"),
        Step("friends", "dedup", items=ref("fl0")),
        # node 12 Project: names from node 23's rows, the body from node 5's rows -- no read
        Step("liker_named", "rename", rows=ref("liker_props"), names=PERSON_RENAME),
        Step("with_liker", "join", left=ref("newest"), right=ref("liker_named"),
             left_on="_vid", right_on="_vid"),
        Step("msg_rows", "project", rows=ref("msg_mat"),
             fields=["_vid", "id", "content", "imageFile", "creationDate"]),
        Step("msg_named", "rename", rows=ref("msg_rows"), names=_IC7_MSG_NAMES),
        Step("with_msg", "join", left=ref("with_liker"), right=ref("msg_named"),
             left_on="_msg", right_on="_vid"),
        Step("flagged", "flag_in", rows=ref("with_msg"), field="_vid",
             allowed=ref("friends"), into="_knows"),
    ] + _ic7_engine_finish("flagged")                 # node 21 TopN 20


def ic7_arangodb_engine(args):
    """IC7 arango, from arangodb/ic7/p1/plan.txt.

    Neither traversal projects its vertex, so both materialize a document per step: [4] every
    message, [8] the liker once per like EDGE (COLLECT groups on the liker document). Names and
    bodies come from those documents; nothing is re-read. The engine's optimizer places the isFriend
    subquery AFTER `SORT ... LIMIT 20` ([32]/[33] before [38]), so it runs for the 20 winners
    only: both knows indexes of each winner and the whole list (N).

    [22]'s `LIMIT 1` sits directly on the traversal (no FilterNode between), so it stops the
    vertex fetches at the match while the edge index is still read in full
    (traverse_filtered_limit: the rule and its probes).

    sf0.3 vs live: S, G and N exact at every param (S = M + 42, N = M + L + sum of the 20 winners'
    knows degrees; `validate.py --sf 0.3 --db arangodb --query ic7`)."""
    p = person_vid(args["person"])
    return [
        # [2] LET person = DOCUMENT(...): pk + doc, fetched even though only _id is used
        Step("me", "docs_per_step", vids=[p], label="Person", props=["id"]),
        Step("mev", "pluck", rows=ref("me"), field="_vid"),
        # [4] TraversalNode hasCreator edge(_to), 1..1 INBOUND person -- the first traversal
        Step("open", "traversal_open", items=ref("mev"), etype="hasCreator"),
        Step("my_msgs", "read_neighbors", srcs=ref("open"), etype="hasCreator", dir="in"),
        # [4] no vertexProjections (myMessages RETURNs msg): every message document, per step
        Step("msg_docs", "docs_per_step", vids=ref("my_msgs"), label=MESSAGE_LABELS,
             props=["id", "content", "imageFile", "creationDate"]),
        # [8] TraversalNode likes edge(_to) from each message; edgeProjections include creationDate,
        # which the index does not hold -> one edge-doc GET per edge
        Step("like_edges", "read_neighbors", srcs=ref("my_msgs"), etype="likes", dir="in",
             edge_props=["creationDate"], pairs=True),
        # [8] no vertexProjections: the liker document per like EDGE (no dedup)
        Step("liker_all", "pluck", rows=ref("like_edges"), field="dst"),
        Step("liker_docs", "docs_per_step", vids=ref("liker_all"), label="Person",
             props=["id", "firstName", "lastName"]),
        # [10] COLLECT l = liker, [40]-[41] per-group SORT likeDate DESC LIMIT 1: in memory
        Step("liker_rows", "rename", rows=ref("like_edges"), names=_IC7_LIKE_NAMES),
        Step("newest", "group_top", rows=ref("liker_rows"), by="_vid",
             keys=[("likeCreationDate", "desc")]),
        Step("liker_named", "rename", rows=ref("liker_docs"), names=PERSON_RENAME),
        Step("with_liker", "join", left=ref("newest"), right=ref("liker_named"),
             left_on="_vid", right_on="_vid"),
        Step("msg_named", "rename", rows=ref("msg_docs"), names=_IC7_MSG_NAMES),
        Step("with_msg", "join", left=ref("with_liker"), right=ref("msg_named"),
             left_on="_msg", right_on="_vid"),
        # [32]/[33] SORT likeDate DESC, TO_NUMBER(l._key) ASC LIMIT 20: in memory, BEFORE isFriend
        Step("winners", "top_k", rows=ref("with_msg"),
             keys=[("likeCreationDate", "desc"), ("personId", "asc")], k=20),
        # [38]/[22] isFriend, once per winner: TraversalNode knows dir=IN,OUT on edge(_from) and
        # edge(_to) -- 2 index SEEKs per winner + one NEXT per knows edge (the whole batch).
        # [22] vertexProjections [_id] + postFilter v._id == person._id, and [25] LimitNode 1
        # takes [22]'s rows directly (no FilterNode left: remove-filter-covered-by-traversal),
        # so the LIMIT cuts the vertex fetches at the match, per winner (traverse_filtered_limit's
        # rule, measured). Default DFS order: the last edge read is evaluated first.
        Step("win_v", "pluck", rows=ref("winners"), field="_vid"),
        Step("fr", "traverse_filtered_limit", srcs=ref("win_v"), etype="knows", dir="both",
             limit=1, order="dfs", props=["id"], label="Person",
             pred=E.eq(E.get("_vid"), p)),
        Step("fl0", "pluck", rows=ref("fr"), field="_src"),
        Step("friends", "dedup", items=ref("fl0")),
        Step("flagged", "flag_in", rows=ref("winners"), field="_vid",
             allowed=ref("friends"), into="_knows"),
    ] + _ic7_engine_finish("flagged")                 # [34]/[35] RETURN


ENGINE = {
    "ic7": {"dgraph": ic7_dgraph_engine, "nebula": ic7_nebula_engine,
            "arangodb": ic7_arangodb_engine}
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"ic7": _KNOWS_HINTS}
