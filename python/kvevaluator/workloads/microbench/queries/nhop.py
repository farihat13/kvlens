"""Microbench nhop access-path shapes."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.microbench._shared import (PERSON_PARAM, PERSON_LET, MESSAGE_PARAM, MESSAGE_LET,
                                                       TAGCLASS_PARAM, TAGCLASS_LET)


# ----------------------------------------------------------------- n-hop (BFS)

def _nhop(k, props=False, from_person_run=False):
    """TM1 generalized: the distinct-new frontier after exactly K undirected
    KNOWS hops (BFS, global visited set). The live queries do the same walk: dgraph
    unrolls one block per hop; nebula runs one GO per hop with MINUS/UNION for the
    visited set (GO K STEPS alone keeps no visited set) --
    the live microbench measurements. Per hop: dgraph pays 2 SEEK per frontier
    vertex; nebula/arango pay 2 SEEK + (its degree) NEXT. The final is the
    frontier itself (person vids agree raw), so `rows` = frontier size and hop
    K's cost = nhopK - nhop(K-1) row totals. props=True adds a 3-prop read of
    the final frontier (nhop2p/nhop4p) -- the composition check against point."""
    def build(args):
        # from_person_run: the final frontier's props by ONE scan of the Person label's key run kept to the frontier
        pr = (Step("pr", "read_vertices_by_label", label="Person", props=["id", "firstName", "lastName"],
                   keep=ref(f"f{k}")) if from_person_run else
              Step("pr", "read_vertex_props", vids=ref(f"f{k}"), props=["id", "firstName", "lastName"], label="Person"))
        p = let("anchor_vid")
        steps = [Step("a", "read_vertex_by_id", vid=p, label="Person"),
                 Step("s0", "dedup", items=ref("a"))]
        for i in range(1, k + 1):
            steps += [
                Step(f"f{i}r", "read_neighbors", srcs=ref(f"f{i-1}" if i > 1 else "s0"),
                     etype="knows", dir="both"),
                Step(f"f{i}d", "dedup", items=ref(f"f{i}r")),
                Step(f"f{i}",  "exclude", items=ref(f"f{i}d"), remove=ref(f"s{i-1}")),
                Step(f"s{i}",  "concat", a=ref(f"s{i-1}"), b=ref(f"f{i}")),
            ]
        if not props:
            steps.append(Step("out", "dedup", items=ref(f"f{k}")))
            return Plan(steps, params=PERSON_PARAM, let=PERSON_LET)
        steps += [
            pr,
            Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
            Step("out", "project", rows=ref("srt"), fields=["id", "firstName", "lastName"]),
        ]
        return Plan(steps, params=PERSON_PARAM, let=PERSON_LET)
    return build


def nhop2_hetero(args):
    """TM1's heterogeneous chain (MB43 shape): person <-hasCreator- messages
    -hasTag-> tags, distinct tags. Fan-out over non-social edges; the dedup
    ratio (raw tag pairs -> distinct tags) differs from the knows BFS. Tag vids
    agree raw across formats, so the final is the tag set itself."""
    p = let("anchor_vid")
    return Plan([
        Step("a",   "read_vertex_by_id", vid=p, label="Person"),
        Step("ms",  "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
        Step("md",  "dedup", items=ref("ms")),
        Step("tg",  "read_neighbors", srcs=ref("md"), etype="hasTag", dir="out"),
        Step("out", "dedup", items=ref("tg")),
    ], params=PERSON_PARAM, let=PERSON_LET)


# ------------------------------------------------------------ closure (hierarchy)

def closure(args):
    """The whole subtree under one TagClass: follow isSubclassOf backwards (the edge points
    child -> parent, so read it from the parent side) level by level until no new class
    appears. Unlike a chain it fans out like a tree, and unlike nhop it has no fixed depth.
    The final is the class set, root included (tagclass vids agree raw across formats)."""
    return Plan([
        Step("sub", "read_closure", srcs=[let("anchor_vid")], etype="isSubclassOf", dir="in"),
        Step("out", "dedup", items=ref("sub")),
    ], params=TAGCLASS_PARAM, let=TAGCLASS_LET)


# ------------------------------------------------------- variable-depth chains

def _chain(cap):
    """TM2: read_chase replyOf from a message to its root post. cap=None is the correct unbounded walk (pays the
    terminal empty read); cap=3 is the engines' truncation -- on chains deeper than 3 it returns a NON-root comment,
    and the unbounded twin beside it prices that correctness. No anchor step: the anchor's tag is never used, so a
    candidate-tag probe is a resolution step the minimal plan drops.
    """
    def build(args):
        return Plan([
            Step("root", "read_chase", srcs=[let("anchor_vid")], etype="replyOf", dir="out",
                 max_hops=cap),
            Step("pr",   "read_vertex_props", vids=ref("root"), props=["id"],
                 label=["Post", "Comment"]),
            Step("out",  "project", rows=ref("pr"), fields=["id"]),
        ], params=MESSAGE_PARAM, let=MESSAGE_LET)
    return build


# ================================================================ engine plans
# Transcribed from the engines' captured sf0.3 plans (microbench/<db>/<shape>/p<train>/plan.txt), at the training
# param only (blind holdout: live_workloads.train_param).
# One section per engine; each cites the plan node behind every KV step.


# ------------------------------------------------------------ dgraph engine plans

from kvevaluator.workloads.microbench._shared import dgraph_engine_anchor


def nhop2_dgraph_engine(args):
    """From dgraph/nhop2/p3/plan.txt, the IC1-style unroll, one block per level.
    Block 0: h0 = the id anchor; `knows`, `~knows` (1 posting read each).
    Block 1: h1 = uid(f1, r1) @filter(NOT uid(h0)) -- in memory; then `knows`, `~knows` for
        every h1 vertex (2 posting reads each).
    Block 2: h2 = uid(f2, r2) @filter(NOT uid(h0, h1)) -- in memory. Block 3: `uid` is free."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f1r", "read_neighbors", srcs=ref("me"), etype="knows", dir="out"),   # block 0: knows
        Step("r1r", "read_neighbors", srcs=ref("me"), etype="knows", dir="in"),    # block 0: ~knows
        Step("u1",  "concat", a=ref("f1r"), b=ref("r1r")),             # block 1: uid(f1, r1)
        Step("u1d", "dedup", items=ref("u1")),
        Step("h1",  "exclude", items=ref("u1d"), remove=ref("me")),    # block 1: NOT uid(h0)
        Step("f2r", "read_neighbors", srcs=ref("h1"), etype="knows", dir="out"),   # block 1: knows
        Step("r2r", "read_neighbors", srcs=ref("h1"), etype="knows", dir="in"),    # block 1: ~knows
        Step("u2",  "concat", a=ref("f2r"), b=ref("r2r")),             # block 2: uid(f2, r2)
        Step("u2d", "dedup", items=ref("u2")),
        Step("h01", "concat", a=ref("me"), b=ref("h1")),
        Step("h2",  "exclude", items=ref("u2d"), remove=ref("h01")),   # block 2: NOT uid(h0, h1)
        Step("out", "dedup", items=ref("h2")),                         # block 3: uid(h2)
    ], params=PERSON_PARAM, let=PERSON_LET)


def closure_dgraph_engine(args):
    """From dgraph/closure/p3/plan.txt.
    Block 0: root = the id anchor with `@filter(type(tagclass))` (1 SEEK + 1 type-index SEEK).
    Block 1: `@recurse(loop: false) { ~isSubclassOf }` -- level by level, one posting read per
        frontier class, the empty leaf postings included, until a level adds nothing. That is
        read_closure (the frontier of each level is read once; seen classes are not re-read).
    Block 2: uid(root, sub) -- in memory; `uid` is free."""
    return Plan(dgraph_engine_anchor("TagClass") + [                  # block 0: eq(id) + type(tagclass)
        Step("sub", "read_closure", srcs=ref("me"), etype="isSubclassOf", dir="in"),  # block 1: @recurse
        Step("out", "dedup", items=ref("sub")),                        # block 2: uid(root, sub)
    ], params=TAGCLASS_PARAM, let=TAGCLASS_LET)


def chain_dgraph_engine(args):
    """From dgraph/chain/p4/plan.txt.
    Block 0: m = `eq(id)` (1 SEEK) @filter(type(post) OR type(comment)) -- one type-index SEEK
        per branch. `id` is shared by every type, so the lookup can return several uids.
    Block 1: `@recurse(loop: false) { up as replyOf }` -- one replyOf posting read per message
        on the chain, the root post's empty one included (read_closure: level by level).
    Block 2: uid(m, up) @filter(type(post)) -- 1 type-index SEEK; `id` of the root, 1 GET.

    Known miss (kept in the suite, like LDBC is4-is7): S and N are exact (8/0 at the training
    param) but G is 1 against live 3. The missing
    2 GETs are the known sf0.3 residual on every type(comment) read: the Comment dgraph.type
    posting is large enough that dgraph splits it into parts, and reading a split list GETs the
    parts; the store keeps one posting per key. LDBC ic2/ic3/ic8/ic9 carry the same -2 G per
    type(comment) read. Not closed here, because modelling it would change the shared
    read_vertices_by_label, not this plan."""
    return Plan([
        Step("anchor", "read_vertex_by_id", vid=let("anchor_vid")),    # block 0: eq(id)
        Step("tpost",  "read_vertices_by_label", label="Post"),        # block 0: type(post)
        Step("tcomm",  "read_vertices_by_label", label="Comment"),     # block 0: type(comment)
        Step("tmsg",   "concat", a=ref("tpost"), b=ref("tcomm")),
        Step("m",      "intersect", items=ref("anchor"), other=ref("tmsg")),
        Step("up",     "read_closure", srcs=ref("m"), etype="replyOf", dir="out"),  # block 1: @recurse
        Step("root",   "read_filter_by_label_index", vids=ref("up"), label="Post"), # block 2: type(post)
        Step("pr",     "read_vertex_props", vids=ref("root"), props=["id"]),        # block 2: id
        Step("out",    "project", rows=ref("pr"), fields=["id"]),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


# chain is transcribed from its plan and stays in the suite; its G misses by the split Comment type
# posting (-2 G at sf0.3), the same known residual LDBC's is4-is7 carry (see its docstring)
ENGINE_DGRAPH = {"nhop2": nhop2_dgraph_engine,
                 "closure": closure_dgraph_engine,
                 "chain": chain_dgraph_engine}
ENGINE_DRAFT_DGRAPH = {}


# ------------------------------------------------------------ nebula engine plans

def nhop2_nebula_engine(args):
    """From nebula/nhop2/p3/plan.txt: one GO per hop, the visited set in graphd memory. Node 3
    Expand: KNOWS both directions from the anchor ($h1). Node 16 Expand: the same from $f1, the
    new hop-1 vertices only (the query's `GO FROM $f1.id`; node 16's depends_on is execution
    order, not its input). Nodes 4/17 Dedup, 8/21 Minus, 12 Union, 13 Dedup are in memory."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: $v0
        Step("h1r", "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),   # node 3 Expand
        Step("h1",  "dedup", items=ref("h1r")),                        # node 4 Dedup
        Step("f1",  "exclude", items=ref("h1"), remove=ref("a")),      # node 8 Minus: $f1
        Step("v1c", "concat", a=ref("a"), b=ref("f1")),                # node 12 Union
        Step("v1",  "dedup", items=ref("v1c")),                        # node 13 Dedup: $v1
        Step("h2r", "read_neighbors", srcs=ref("f1"), etype="knows", dir="both"),  # node 16 Expand
        Step("h2",  "dedup", items=ref("h2r")),                        # node 17 Dedup
        Step("out", "exclude", items=ref("h2"), remove=ref("v1")),     # node 21 Minus
    ], params=PERSON_PARAM, let=PERSON_LET)


def chain_nebula_engine(args):
    """From nebula/chain/p4/plan.txt. Node 4 Traverse: REPLY_OF out (type 21), steps
    0..unbounded, dedup 1, no vertexProps -- 1 edge scan per message on the chain, the root's
    empty scan included (read_chase, unbounded). Node 11 AppendVertices lists no props, so it
    makes no storage call (--optimize_appendvertices=true, AppendVerticesExecutor.cpp:33); the
    id is the vid, which the `id`-only read below charges nothing for. Nodes 7 Project and 12
    TopN (the deepest hop) are in memory: read_chase already returns the chain's last message."""
    return Plan([
        Step("root", "read_chase", srcs=[let("anchor_vid")], etype="replyOf", dir="out",
             max_hops=None),                                           # node 4 Traverse
        Step("pr",   "read_vertex_props", vids=ref("root"), props=["id"],
             label=["Post", "Comment"]),                               # node 11 AppendVertices
        Step("out",  "project", rows=ref("pr"), fields=["id"]),        # node 10 Project id($r)
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


def closure_nebula_engine(args):
    """From nebula/closure/p3/plan.txt. Node 10 Traverse: IS_SUBCLASS_OF in (type -24), steps
    0..unbounded, dedup 1. Its `first step filter: TagClass._tag IS NOT EMPTY` is 1 GET on the
    root with no prefix scan (tag_probe, the IS6 rule); then 1 edge scan per class reached,
    leaves included (read_closure). Its `tag filter` and node 11 AppendVertices (props [_tag]
    only) make no storage call: AppendVertices' _tag-only read is pruned under
    --optimize_appendvertices=true. Nodes 9 Project and
    8 Dedup are in memory."""
    return Plan([
        Step("root", "tag_probe", vids=[let("anchor_vid")], label="TagClass"),   # node 10 first step filter
        Step("sub",  "read_closure", srcs=ref("root"), etype="isSubclassOf",
             dir="in"),                                                # node 10 Traverse
        Step("out",  "dedup", items=ref("sub")),                       # node 8 Dedup
    ], params=TAGCLASS_PARAM, let=TAGCLASS_LET)


ENGINE_NEBULA = {"nhop2": nhop2_nebula_engine,
                 "chain": chain_nebula_engine,
                 "closure": closure_nebula_engine}
ENGINE_DRAFT_NEBULA = {}


# ------------------------------------------------------------ arangodb engine plans

from kvevaluator.plans.ir.authoring import lit as _lit  # (arangodb section only)

# a message vid's collection is resolved per vertex (the engine read it off the edge's _id)
_ARANGO_MESSAGE_COLLECTIONS = ["Post", "Comment"]


def nhop2_arangodb_engine(args):
    """From arangodb/nhop2/p3/plan.txt. Node 2 TraversalNode: knows ANY (edge(_from) +
    edge(_to)), depth 2..2, order bfs, uniqueVertices global, vertexProjections [_key].
    BFS with a global visited set expands the start and each distinct depth-1 person once
    (2 SEEK each + 1 NEXT per edge), plus the first traversal's extra SEEK. Only depth-2
    vertices are emitted (min depth 2), and only an emitted vertex's document is fetched:
    a vertex dropped by the uniqueness check is never read."""
    p = let("anchor_vid")
    return Plan([
        Step("open", "traversal_open", items=[p], etype="knows"),      # node 2: first traversal
        Step("f1r",  "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),  # node 2: depth 1
        Step("f1d",  "dedup", items=ref("f1r")),
        Step("f1",   "exclude", items=ref("f1d"), remove=ref("open")), # uniqueVertices global
        Step("s1",   "concat", a=ref("open"), b=ref("f1")),
        Step("f2r",  "read_neighbors", srcs=ref("f1"), etype="knows", dir="both"),    # node 2: depth 2
        Step("f2d",  "dedup", items=ref("f2r")),
        Step("f2",   "exclude", items=ref("f2d"), remove=ref("s1")),   # uniqueVertices global
        Step("v",    "docs_per_step", vids=ref("f2"), props=[], label="Person"),  # node 2: emitted vertex fetch
        Step("vv",   "pluck", rows=ref("v"), field="_vid"),
        Step("out",  "dedup", items=ref("vv")),                        # node 4: the keys
    ], params=PERSON_PARAM, let=PERSON_LET)


def chain_arangodb_engine(args):
    """From arangodb/chain/p4/plan.txt. [24] IndexNode primary(_key) on Post and [25] on
    Comment, each covering the projected _id: one pk GET each, hit or miss (both subqueries
    always run). [16] FIRST(UNION(...)) is in memory. [18] TraversalNode replyOf OUTBOUND
    (edge(_from)), 0..100, order bfs, default uniqueness, vertexProjections [_key]: the first
    traversal's extra SEEK, 1 SEEK per visited vertex (the root too, which finds no edge),
    1 NEXT per edge, and every emitted vertex's document fetched -- depth 0, the message
    itself, included. [22] LAST(walk) is in memory: BFS emits in depth order and replyOf has
    out-degree <= 1, so the last vertex is the one no edge leaves from.

    The walk is traverse_filtered used only for its (child, parent) rows, as the LDBC IS2
    plan does: no props, a pass-all predicate, so it reads no document; the fetches are the
    docs_per_step below."""
    m = let("anchor_vid")
    return Plan([
        Step("pp",   "read_vertex_by_id", vid=m, label="Post"),        # [24] primary(_key) on Post
        Step("pc",   "read_vertex_by_id", vid=m, label="Comment"),     # [25] primary(_key) on Comment
        Step("msg",  "concat", a=ref("pp"), b=ref("pc")),              # [16] FIRST(UNION(...))
        Step("open", "traversal_open", items=ref("msg"), etype="replyOf"),  # [18] first traversal
        Step("walk", "traverse_filtered", srcs=ref("open"), etype="replyOf", dir="out",
             depth=100, pred=_lit(True)),                              # [18] 0..100 walk
        Step("up",   "pluck", rows=ref("walk"), field="_vid"),
        Step("vis",  "concat", a=ref("msg"), b=ref("up")),             # depth 0 + every step
        Step("docs", "docs_per_step", vids=ref("vis"), label=_ARANGO_MESSAGE_COLLECTIONS,
             props=["id"]),                                            # [18] vertex fetch per step
        Step("par",  "pluck", rows=ref("walk"), field="_src"),
        Step("last", "exclude", items=ref("vis"), remove=ref("par")),  # [22] LAST(walk)
        Step("root", "filter_in", rows=ref("docs"), field="_vid", allowed=ref("last")),
        Step("out",  "project", rows=ref("root"), fields=["id"]),      # [22] {id: TO_NUMBER(...)}
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


def closure_arangodb_engine(args):
    """From arangodb/closure/p3/plan.txt. Node 2 TraversalNode: isSubclassOf INBOUND (the
    explain prints dir=OUT; the index is edge(_to), the inbound one), depth 0..100, order bfs,
    uniqueVertices global, vertexProjections [_key]. A BFS with a global visited set is
    read_closure: each class in the subtree expanded once (1 SEEK + 1 NEXT per edge), the root
    included, plus the first traversal's extra SEEK. Every emitted class -- the root at depth 0
    too -- has its document fetched (docs_per_step)."""
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="isSubclassOf"),  # node 2: first traversal
        Step("sub",  "read_closure", srcs=ref("open"), etype="isSubclassOf", dir="in"),     # node 2: BFS, global
        Step("docs", "docs_per_step", vids=ref("sub"), props=[], label="TagClass"),        # node 2: vertex fetch
        Step("v",    "pluck", rows=ref("docs"), field="_vid"),
        Step("out",  "dedup", items=ref("v")),                         # node 4: the keys
    ], params=TAGCLASS_PARAM, let=TAGCLASS_LET)


ENGINE_ARANGODB = {"nhop2": nhop2_arangodb_engine,
                   "chain": chain_arangodb_engine,
                   "closure": closure_arangodb_engine}
ENGINE_DRAFT_ARANGODB = {}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"nhop1": _KNOWS_HINTS, "nhop2": _KNOWS_HINTS, "nhop2p": _KNOWS_HINTS, "nhop3": _KNOWS_HINTS, "nhop4": _KNOWS_HINTS, "nhop4p": _KNOWS_HINTS, "nhop5": _KNOWS_HINTS, "nhop6": _KNOWS_HINTS}
