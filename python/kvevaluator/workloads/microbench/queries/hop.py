"""Microbench hop access-path shapes."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let, param, and_, lt, coalesce, get, field, lit, compose_vid
from kvevaluator.workloads.microbench._shared import (dgraph_engine_anchor, DATE_CUTOFF, MESSAGE_LABELS, PERSON_PROPS,
                                                       PERSON_PARAM, PERSON_LET, POST_PROPS, anchor_param, anchor_let)


# ------------------------------------------------------------ 1-hop traversal

def _hop1(etype, dir, label, anchor_key="person"):
    """T1 'get 1-hop neighbor ids': the degree-axis workhorse. dgraph = 1 SEEK
    per direction (whole posting, FLAT in degree); nebula/arango = 1 SEEK + d
    NEXT per direction (LINEAR in degree). Final = count row (message/neighbor
    vid spellings differ across formats; the count and the ops are the data).
    Anchored on `anchor_key` (person/forum/tag); its vid is composed from `label`."""
    def build(args):
        v = let("anchor_vid")
        return Plan([
            Step("a",   "read_vertex_by_id", vid=v, label=label),
            Step("nb",  "read_neighbors", srcs=ref("a"), etype=etype, dir=dir),
            Step("d",   "dedup", items=ref("nb")),
            Step("n",   "count", items=ref("d")),
            Step("out", "as_row", value=ref("n"), field="count"),
        ], params=anchor_param(anchor_key), let=anchor_let(label, anchor_key))
    return build


def hop1p_knows(args):
    """T1+T2 with neighbor props: hop1_knows + 3 props per distinct friend.
    Composition check: this minus hop1_knows == |friends| x point-style reads."""
    p = let("anchor_vid")
    return Plan([
        Step("a",   "read_vertex_by_id", vid=p, label="Person"),
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("d",   "dedup", items=ref("nb")),
        Step("pr",  "read_vertex_props", vids=ref("d"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id", "firstName", "lastName"]),
    ], params=PERSON_PARAM, let=PERSON_LET)


def _hop1p(nprops):
    """hop1_knows + read n props off each distinct friend (PERSON_PROPS[:n]): the width x fan-out cross. Per friend:
    dgraph pays n GET (one key per predicate), nebula 1 GET (whole tag row), arango 2 GET (pk + doc).
    Sorts by the first prop and reads NO id, so n props = exactly n dgraph GETs (ties leave the row order unspecified;
    the result SET still agrees across formats).
    """
    props = PERSON_PROPS[:nprops]

    def build(args):
        p = let("anchor_vid")
        return Plan([
            Step("a",   "read_vertex_by_id", vid=p, label="Person"),
            Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
            Step("d",   "dedup", items=ref("nb")),
            Step("pr",  "read_vertex_props", vids=ref("d"), props=props, label="Person"),
            Step("srt", "top_k", rows=ref("pr"), keys=[(props[0], "asc")], k=None),
            Step("out", "project", rows=ref("srt"), fields=props),
        ], params=PERSON_PARAM, let=PERSON_LET)
    return build


def hop1_label(args):
    """T3: heterogeneous 1-hop (a person's messages) filtered to one type: the PER-CANDIDATE PROBE fork of the type
    filter (dgraph 1 SEEK, nebula 1 GET, arango free, per candidate). It names the physical probe op
    `read_filter_by_label_probe` directly, not the logical `filter_by_label`, so the canonical schema's per-type index
    does not lower dgraph to the index-once path and the measurement stays the probe.
    """
    p = let("anchor_vid")
    return Plan([
        Step("a",   "read_vertex_by_id", vid=p, label="Person"),
        Step("ms",  "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
        Step("d",   "dedup", items=ref("ms")),
        Step("po",  "read_filter_by_label_probe", vids=ref("d"), label="Post"),
        Step("n",   "count", items=ref("po")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_propfilter(args):
    """T4: 1-hop + neighbor-property range filter -- the filter costs 1 prop
    read per distinct neighbor (dgraph 1 GET; nebula 1 GET whole-row; arango 2
    GET pk+doc), then drops rows for free."""
    p = let("anchor_vid")
    return Plan([
        Step("a",   "read_vertex_by_id", vid=p, label="Person"),
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("d",   "dedup", items=ref("nb")),
        Step("pp",  "read_vertex_props", vids=ref("d"), props=["creationDate"], label="Person"),
        Step("fx",  "filter", rows=ref("pp"),
             pred=lt(field("creationDate"), lit(DATE_CUTOFF))),
        Step("n",   "count", items=ref("fx")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_edgefilter(args):
    """T5: 1-hop + EDGE-property range filter. The edge prop rides the walk for
    dgraph (facet in the posting blob) and nebula (edge value) -- 0 extra ops,
    bytes only; arango pays 1 GET per edge (covering index holds just the
    neighbor id, the prop lives in the edge doc)."""
    p = let("anchor_vid")
    return Plan([
        Step("a",   "read_vertex_by_id", vid=p, label="Person"),
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both",
             edge_props=["creationDate"]),
        Step("fx",  "filter", rows=ref("nb"),
             pred=lt(coalesce(get("creationDate"), 0), lit(DATE_CUTOFF))),
        Step("n",   "count", items=ref("fx")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


# ------------------------------------------------- a person's messages (read / write mixes)

def _person_msgs():
    """The shared prefix: a person's messages (Posts and Comments) through the reverse hasCreator."""
    return [Step("a",  "read_vertex_by_id", vid=let("anchor_vid"), label="Person"),
            Step("ms", "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
            Step("md", "dedup", items=ref("ms"))]


def hop1_msgs_date(args):
    """A NARROW read over many vertices: one small prop (creationDate) of each of a person's
    messages, then a date filter. A column layout reads just that column; a row layout reads
    whole rows, `content` included. The pair with hop1_msgs_content isolates the fat column."""
    return Plan(_person_msgs() + [
        Step("pp",  "read_vertex_props", vids=ref("md"), props=["creationDate"], label=MESSAGE_LABELS),
        Step("fx",  "filter", rows=ref("pp"), pred=lt(field("creationDate"), lit(DATE_CUTOFF))),
        Step("n",   "count", items=ref("fx")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_msgs_content(args):
    """The FAT read over the same messages: each message's content (the widest column)."""
    return Plan(_person_msgs() + [
        Step("pp",  "read_vertex_props", vids=ref("md"), props=["content"], label=MESSAGE_LABELS),
        Step("n",   "count", items=ref("pp")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_posts_props(args):
    """Read a person's posts: their messages, filtered to Posts (the logical type filter,
    lowered by capability), then every prop of each post -- a list read plus a wide read of many
    vertices. The read side of the like-vs-read ratio sweep."""
    return Plan(_person_msgs() + [
        Step("po",  "filter_by_label", vids=ref("md"), label="Post"),
        Step("pr",  "read_vertex_props", vids=ref("po"), props=POST_PROPS, label="Post"),
        Step("n",   "count", items=ref("pr")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop2_post_likers(args):
    """Who liked a person's posts: their posts, then each post's likers (the reverse likes
    list), distinct. Many small lists read, one per post -- an analytical read on the Post side."""
    return Plan(_person_msgs() + [
        Step("po",  "filter_by_label", vids=ref("md"), label="Post"),
        Step("lk",  "read_neighbors", srcs=ref("po"), etype="likes", dir="in"),
        Step("ld",  "dedup", items=ref("lk")),
        Step("n",   "count", items=ref("ld")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


# ------------------------------------------------------------------ edge probe

def edge_probe(args):
    """E1 / primitive 14: does a knows edge exist between two given persons
    (undirected)? The layout decides the probe: dgraph reads the src's fwd+rev
    postings (2 SEEK, degree-INDEPENDENT ops, whole-posting bytes); nebula
    point-GETs exact edge keys (<=2 GET, degree-independent, tiny bytes);
    arango scans the src's from+to edge index ranges (2 SEEK + degree NEXT,
    degree-DEPENDENT). Params carry positive and negative pairs."""
    return Plan([
        Step("dr",  "annotate", items=[let("dst_vid")], field="probe", value=1),
        Step("fl",  "read_edge_exists", rows=ref("dr"), field="_vid", etype="knows",
             into="knows", others=[let("src_vid")]),
        Step("out", "project", rows=ref("fl"), fields=["knows"]),
    ], params={**anchor_param("src"), **anchor_param("dst")},
        let={"src_vid": compose_vid("Person", param("src")),
             "dst_vid": compose_vid("Person", param("dst"))})



# ------------------------------------------------------------ logical plans (the optimizer's)
# The generator expands these into the shape's candidate bodies (plan_choice/generate.py); the pick record names the
# CHEAPEST (cheapest_plan_for); the hand builders above stay the shapes' primitive measurements. Each logical plan
# expands, under one legal choice, byte-for-byte to its hand plan (tests/unit/check_optimizer_rules.py checks).

def hop1_label_logical(args):
    """hop1_label with the type filter as a choice point: probe each candidate (the hand plan,
    which pins it to isolate that primitive) or read the type index once."""
    p = let("anchor_vid")
    return Plan([
        Step("a",   "read_vertex_by_id", vid=p, label="Person"),
        Step("ms",  "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
        Step("d",   "dedup", items=ref("ms")),
        Step("po",  "filter_by_label", vids=ref("d"), label="Post"),
        Step("n",   "count", items=ref("po")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def edge_probe_logical(args):
    """edge_probe with the membership test as a choice point: probe the edge (the hand plan) or
    read the src's knows adjacency once and test membership in memory."""
    return Plan([
        Step("dr",  "annotate", items=[let("dst_vid")], field="probe", value=1),
        Step("fl",  "membership_flag", rows=ref("dr"), field="_vid", etype="knows",
             into="knows", anchors=[let("src_vid")], set_read_name="src_nb0",
             set_name="src_nb"),
        Step("out", "project", rows=ref("fl"), fields=["knows"]),
    ], params={**anchor_param("src"), **anchor_param("dst")},
        let={"src_vid": compose_vid("Person", param("src")),
             "dst_vid": compose_vid("Person", param("dst"))})


LOGICAL = {"hop1_label": hop1_label_logical, "edge_probe": edge_probe_logical}


# ================================================================ engine plans
# Transcribed from the engines' captured sf0.3 plans (microbench/<db>/<shape>/p<train>/plan.txt), at the training
# param only (blind holdout: live_workloads.train_param).
# One section per engine; each cites the plan node behind every KV step.


# ------------------------------------------------------------ dgraph engine plans

def hop1_knows_dgraph_engine(args):
    """From dgraph/hop1_knows/p3/plan.txt. Block 0: the id anchor, then `knows` and `~knows`
    (1 posting read each). Block 1: `uid(f, r)` unions the two uid sets in memory, no read."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f",   "read_neighbors", srcs=ref("me"), etype="knows", dir="out"),   # block 0: knows
        Step("r",   "read_neighbors", srcs=ref("me"), etype="knows", dir="in"),    # block 0: ~knows
        Step("fr",  "concat", a=ref("f"), b=ref("r")),                 # block 1: uid(f, r)
        Step("d",   "dedup", items=ref("fr")),
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_knows_out_dgraph_engine(args):
    """From dgraph/hop1_knows_out/p3/plan.txt. Block 0: the id anchor, then `friends: knows`
    (1 posting read). `uid` [vals=n] is free."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f",   "read_neighbors", srcs=ref("me"), etype="knows", dir="out"),   # block 0: knows
        Step("d",   "dedup", items=ref("f")),
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_label_dgraph_engine(args):
    """From dgraph/hop1_label/p3/plan.txt. Block 0: the id anchor, `posts: ~hasCreator` (1 posting
    read), then `@filter(type(post))` over all the messages -- at typefilterpath=index that is
    ONE read of the post type posting, not the per-candidate probe the optimal plan names.
    `uid` is free."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("ms",  "read_neighbors", srcs=ref("me"), etype="hasCreator", dir="in"),  # ~hasCreator
        Step("po",  "read_filter_by_label_index", vids=ref("ms"), label="Post"),      # type(post)
        Step("n",   "count", items=ref("po")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_propfilter_dgraph_engine(args):
    """From dgraph/hop1_propfilter/p3/plan.txt. Block 0: the id anchor, `knows`, `~knows`.
    Block 1: `uid(f, r)` (in memory), then `@filter(creationDate lt(...))` [vals=n]: at
    ineqfilterpath=per_vertex (plan header) the value is read per friend, 1 GET each."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f",   "read_neighbors", srcs=ref("me"), etype="knows", dir="out"),   # block 0: knows
        Step("r",   "read_neighbors", srcs=ref("me"), etype="knows", dir="in"),    # block 0: ~knows
        Step("fr",  "concat", a=ref("f"), b=ref("r")),                 # block 1: uid(f, r)
        Step("d",   "dedup", items=ref("fr")),
        Step("pp",  "read_vertex_props", vids=ref("d"), props=["creationDate"]),   # block 1: lt filter
        Step("fx",  "filter", rows=ref("pp"),
             pred=lt(field("creationDate"), lit(DATE_CUTOFF))),
        Step("n",   "count", items=ref("fx")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_edgefilter_dgraph_engine(args):
    """From dgraph/hop1_edgefilter/p3/plan.txt. Block 0: the id anchor, `knows` and `~knows`,
    each with `@facets(lt(creationDate))`: the facet is inside the posting, so the filter reads
    nothing extra (1 posting read per direction). Block 1: `uid(f, r)` (in memory), `uid` free.
    A missing facet fails dgraph's facet filter, so a None date is dropped (coalesce to the
    cutoff itself); every LDBC knows edge has a date."""
    date_ok = lt(coalesce(get("creationDate"), lit(DATE_CUTOFF)), lit(DATE_CUTOFF))
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f",   "read_neighbors", srcs=ref("me"), etype="knows", dir="out",
             edge_props=["creationDate"]),                             # block 0: knows @facets
        Step("r",   "read_neighbors", srcs=ref("me"), etype="knows", dir="in",
             edge_props=["creationDate"]),                             # block 0: ~knows @facets
        Step("ff",  "filter", rows=ref("f"), pred=date_ok),
        Step("rf",  "filter", rows=ref("r"), pred=date_ok),
        Step("fv",  "pluck", rows=ref("ff"), field="dst"),
        Step("rv",  "pluck", rows=ref("rf"), field="dst"),
        Step("fr",  "concat", a=ref("fv"), b=ref("rv")),               # block 1: uid(f, r)
        Step("d",   "dedup", items=ref("fr")),
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def edge_probe_dgraph_engine(args):
    """From dgraph/edge_probe/p3/plan.txt.
    Block 0: `dst as var(eq(id, person2) @filter(type(person)))` -- its own id lookup (1 SEEK) and
        type-index read (1 SEEK). `id` is shared by every type, so the lookup can return several
        uids and the type filter picks the person.
    Block 1: the anchor for person1 (the same 2 SEEKs again), then `out: count(knows
        @filter(uid(dst)))` and `in: count(~knows @filter(uid(dst)))`: one posting read each on
        person1; the uid(dst) filter and the counts are in memory. That is read_edge_exists's
        dgraph path exactly (the anchor's fwd + rev postings, membership in memory)."""
    return Plan([
        Step("danchor", "read_vertex_by_id", vid=let("dst_vid")),      # block 0: eq(id, person2)
        Step("dtype",   "read_vertices_by_label", label="Person"),     # block 0: type(person)
        Step("dst",     "intersect", items=ref("danchor"), other=ref("dtype")),
        Step("anchor",  "read_vertex_by_id", vid=let("src_vid")),      # block 1: eq(id, person1)
        Step("atype",   "read_vertices_by_label", label="Person"),     # block 1: type(person)
        Step("me",      "intersect", items=ref("anchor"), other=ref("atype")),
        Step("dr",      "annotate", items=ref("dst"), field="probe", value=1),
        Step("fl",      "read_edge_exists", rows=ref("dr"), field="_vid", etype="knows",
             into="knows", others=ref("me"),                           # block 1: knows + ~knows: person1's forward and
             reads=[{"dir": "out", "at": "anchor"}, {"dir": "in", "at": "anchor"}]),   # reverse posting, both always (pinned: the engine's reads)
        Step("out",     "project", rows=ref("fl"), fields=["knows"]),
    ], params={**anchor_param("src"), **anchor_param("dst")},
        let={"src_vid": compose_vid("Person", param("src")),
             "dst_vid": compose_vid("Person", param("dst"))})


def hop1p7_knows_dgraph_engine(args):
    """From dgraph/hop1p7_knows/p3/plan.txt. Block 0: the id anchor, then `knows` and `~knows` (1 posting read each).
    Block 1: `result(func: uid(f, r))` unions the two uid sets in memory, then reads the 7 predicates of every friend
    (vals=63 each: 1 GET per predicate per friend, LocalCache off). The sort is the client's, in memory."""
    props = PERSON_PROPS[:7]
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("f",   "read_neighbors", srcs=ref("me"), etype="knows", dir="out"),   # block 0: knows
        Step("r",   "read_neighbors", srcs=ref("me"), etype="knows", dir="in"),    # block 0: ~knows
        Step("fr",  "concat", a=ref("f"), b=ref("r")),                 # block 1: uid(f, r)
        Step("d",   "dedup", items=ref("fr")),
        Step("pr",  "read_vertex_props", vids=ref("d"), props=props),  # block 1: the 7 predicates
        Step("srt", "top_k", rows=ref("pr"), keys=[(props[0], "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=props),
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_DGRAPH = {"hop1_knows": hop1_knows_dgraph_engine,
                 "hop1_knows_out": hop1_knows_out_dgraph_engine,
                 "hop1_label": hop1_label_dgraph_engine,
                 "hop1_propfilter": hop1_propfilter_dgraph_engine,
                 "hop1_edgefilter": hop1_edgefilter_dgraph_engine,
                 "edge_probe": edge_probe_dgraph_engine}
ENGINE_DRAFT_DGRAPH = {}


# ------------------------------------------------------------ nebula engine plans

def hop1_knows_nebula_engine(args):
    """From nebula/hop1_knows/p3/plan.txt. Node 3 Expand: KNOWS both directions (edge types
    -12 and 12), `_dst` only. Node 4 Dedup is in memory. The start vid is the key: no read."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: the vid
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),   # node 3 Expand
        Step("d",   "dedup", items=ref("nb")),                         # node 4 Dedup
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


# The GO shapes below plan as Expand -> ExpandAll. For a 1-step GO the Expand node has
# maxSteps 0: it only turns the start vid into a row, with no storage call
# (graph/executor/query/ExpandExecutor.cpp:50). The ExpandAll does the one edge walk.
from kvevaluator.plans.ir.authoring import notnull  # (nebula section only)


def hop1_knows_out_nebula_engine(args):
    """From nebula/hop1_knows_out/p3/plan.txt. Node 4 Expand: maxSteps 0, no read. Node 5
    ExpandAll: KNOWS out only (type 12), edgeProps _dst/_rank/_src/_type/creationDate (the edge
    value is read on every edge either way). Nodes 6 HashInnerJoin, 7 Project dst(EDGE), 8 Dedup
    are in memory."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: the vid
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="out",
             edge_props=["creationDate"]),                             # node 5 ExpandAll
        Step("dst", "pluck", rows=ref("nb"), field="dst"),             # node 7 Project dst(EDGE)
        Step("d",   "dedup", items=ref("dst")),                        # node 8 Dedup
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_label_nebula_engine(args):
    """From nebula/hop1_label/p3/plan.txt. Node 5 ExpandAll: HAS_CREATOR reverse (type -20).
    Node 7 GetVertices: tagId 3 (Post), props [creationDate], dedup 1 -- 1 GET per message, and
    for a message that is not a Post, storaged's follow-up prefix scan of the vid
    (NebulaEngineFormat.read_vertex_props; storage/exec/GetPropNode.h:47-68). Node 15 Filter
    `$__COL_0 IS NOT EMPTY` keeps the Posts; nodes 12 Project src(EDGE) and 13 Dedup are in
    memory."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: the vid
        Step("ms",  "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),  # node 5 ExpandAll
        Step("pv",  "read_vertex_props", vids=ref("ms"), props=["creationDate"],
             label="Post"),                                            # node 7 GetVertices
        Step("po",  "filter", rows=ref("pv"), pred=notnull(field("creationDate"))),  # node 15 Filter
        Step("ids", "pluck", rows=ref("po"), field="_vid"),            # node 12 Project src(EDGE)
        Step("d",   "dedup", items=ref("ids")),                        # node 13 Dedup
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_propfilter_nebula_engine(args):
    """From nebula/hop1_propfilter/p3/plan.txt. Node 5 ExpandAll: KNOWS both directions (types
    -12 and 12), `_dst` only. Node 7 GetVertices: tagId 2 (Person), props [creationDate],
    dedup 1 -- 1 GET per distinct friend. Node 15 Filter creationDate < 1293840000000; nodes 12
    Project and 13 Dedup are in memory."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: the vid
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),   # node 5 ExpandAll
        Step("pp",  "read_vertex_props", vids=ref("nb"), props=["creationDate"],
             label="Person"),                                          # node 7 GetVertices
        Step("fx",  "filter", rows=ref("pp"),
             pred=lt(field("creationDate"), lit(DATE_CUTOFF))),        # node 15 Filter
        Step("ids", "pluck", rows=ref("fx"), field="_vid"),            # node 12 Project
        Step("d",   "dedup", items=ref("ids")),                        # node 13 Dedup
        Step("n",   "count", items=ref("d")),
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_edgefilter_nebula_engine(args):
    """From nebula/hop1_edgefilter/p3/plan.txt. Node 12 ExpandAll: KNOWS both directions,
    edgeProps [_dst, creationDate], filter KNOWS.creationDate < 1293840000000 pushed into the
    walk (the prop is in the edge value, so the filter reads nothing extra). Node 11
    HashInnerJoin and node 8 Project are in memory; no Dedup (the count is over edges)."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: the vid
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both",
             edge_props=["creationDate"]),                             # node 12 ExpandAll
        Step("fx",  "filter", rows=ref("nb"),
             pred=lt(coalesce(get("creationDate"), 0), lit(DATE_CUTOFF))),   # node 12 filter
        Step("n",   "count", items=ref("fx")),                         # node 8 Project
        Step("out", "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def edge_probe_nebula_engine(args):
    """From nebula/edge_probe/p3/plan.txt. Nodes 2 and 6 GetEdges: KNOWS (type 12) src -> dst,
    then dst -> src, one edge key each, in the two branches of a UNION ALL -- so both are
    always fetched, with no stop on the first hit (NebulaEngineFormat.get_edge). Nodes 3/7
    Filter drop the empty row of a missing edge; 9 Union, 11 Aggregate count(*) and 12 Project
    `count > 0` are in memory."""
    return Plan([
        Step("ab",  "get_edge", src=let("src_vid"), dst=let("dst_vid"), etype="knows"),  # node 2
        Step("ba",  "get_edge", src=let("dst_vid"), dst=let("src_vid"), etype="knows"),  # node 6
        Step("abd", "pluck", rows=ref("ab"), field="dst"),             # the far end is dst_vid
        Step("bas", "pluck", rows=ref("ba"), field="src"),
        Step("un",  "concat", a=ref("abd"), b=ref("bas")),             # node 9 Union
        Step("dr",  "annotate", items=[let("dst_vid")], field="probe", value=1),
        Step("fl",  "flag_in", rows=ref("dr"), field="_vid", allowed=ref("un"),
             into="knows"),                                            # nodes 11-12 count(*) > 0
        Step("out", "project", rows=ref("fl"), fields=["knows"]),
    ], params={**anchor_param("src"), **anchor_param("dst")},
        let={"src_vid": compose_vid("Person", param("src")),
             "dst_vid": compose_vid("Person", param("dst"))})


def hop1p7_knows_nebula_engine(args):
    """From nebula/hop1p7_knows/p3/plan.txt. Node 4 Expand: maxSteps 0, no read. Node 5 ExpandAll: KNOWS both
    directions (types -12 and 12), `_dst` only. Node 7 GetVertices: tagId 2 (Person), the 7 props, dedup 1 -- 1 GET per
    distinct friend (the whole tag row). Nodes 8-12 (Project, the two hash joins, Dedup) are in memory; the statement
    does not sort, so the sort is in memory too."""
    props = PERSON_PROPS[:7]
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("anchor_vid")),       # node 1 Project: the vid
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),   # node 5 ExpandAll
        Step("pr",  "read_vertex_props", vids=ref("nb"), props=props,
             label="Person"),                                          # node 7 GetVertices
        Step("ids", "dedup_rows", rows=ref("pr")),                     # node 12 Dedup
        Step("srt", "top_k", rows=ref("ids"), keys=[(props[0], "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=props),
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_NEBULA = {"hop1_knows": hop1_knows_nebula_engine,
                 "hop1_knows_out": hop1_knows_out_nebula_engine,
                 "hop1_label": hop1_label_nebula_engine,
                 "hop1_propfilter": hop1_propfilter_nebula_engine,
                 "hop1_edgefilter": hop1_edgefilter_nebula_engine,
                 "edge_probe": edge_probe_nebula_engine}
ENGINE_DRAFT_NEBULA = {}


# ------------------------------------------------------------ arangodb engine plans

def hop1_knows_arangodb_engine(args):
    """From arangodb/hop1_knows/p3/plan.txt. Node 2 TraversalNode: knows ANY (edge(_from) +
    edge(_to)), 1..1, uniqueVertices global, vertexProjections [_key]. The traversal fetches
    each returned vertex's document even though only `_key` is used (docs_per_step). No
    IndexNode: the start is the literal `Person/<id>`, so the anchor costs nothing."""
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="knows"),  # node 2: first traversal
        Step("nb",   "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),   # node 2: both edge indexes
        Step("d",    "dedup", items=ref("nb")),                        # node 2: uniqueVertices global
        Step("v",    "docs_per_step", vids=ref("d"), props=[], label="Person"),    # node 2: vertex fetch
        Step("n",    "count", items=ref("v")),
        Step("out",  "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


from kvevaluator.plans.ir.authoring import eq as _eq, gt as _gt  # (arangodb section only)

# a message vid's collection is resolved per vertex (the engine read it off the edge's _id)
_ARANGO_MESSAGE_COLLECTIONS = ["Post", "Comment"]


def hop1_knows_out_arangodb_engine(args):
    """From arangodb/hop1_knows_out/p3/plan.txt. Node 2 TraversalNode: knows OUTBOUND (the
    explain prints dir=IN; the index it uses, edge(_from), is the outbound one), 1..1,
    uniqueVertices global, vertexProjections [_key]. As in hop1_knows: the first traversal's
    extra SEEK, 1 SEEK + 1 NEXT per edge, and each returned vertex's document fetched although
    only `_key` is used (docs_per_step). The start is the literal `Person/<id>`: no anchor read."""
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="knows"),  # node 2: first traversal
        Step("nb",   "read_neighbors", srcs=ref("open"), etype="knows", dir="out"),    # node 2: edge(_from)
        Step("d",    "dedup", items=ref("nb")),                        # node 2: uniqueVertices global
        Step("v",    "docs_per_step", vids=ref("d"), props=[], label="Person"),    # node 2: vertex fetch
        Step("n",    "count", items=ref("v")),
        Step("out",  "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_label_arangodb_engine(args):
    """From arangodb/hop1_label/p3/plan.txt. Node 2 TraversalNode: hasCreator INBOUND (index
    edge(_to)), 1..1, uniqueVertices global, vertexProjections [_id, _key], postFilter
    IS_SAME_COLLECTION("Post", m._id). A traversal with a vertex postFilter is
    traverse_filtered: the start vertex and every reached message are fetched (pk + doc) to
    evaluate the filter -- although the collection is already in the _id -- and every Post
    that passes is fetched again as it is emitted. Nodes [3]/[4] (the same test as a
    FilterNode) filter nothing: the condition was pushed into node 2."""
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="hasCreator"),  # node 2: first traversal
        Step("po",   "traverse_filtered", srcs=ref("open"), etype="hasCreator", dir="in", depth=1,
             props=["id"], label=_ARANGO_MESSAGE_COLLECTIONS,
             pred=_eq(get("_label"), lit("Post"))),                    # node 2: postFilter
        Step("pv",   "pluck", rows=ref("po"), field="_vid"),
        Step("d",    "dedup", items=ref("pv")),                        # node 2: uniqueVertices global
        Step("n",    "count", items=ref("d")),                         # node 6: the keys, counted
        Step("out",  "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_propfilter_arangodb_engine(args):
    """From arangodb/hop1_propfilter/p3/plan.txt. Node 2 TraversalNode: knows ANY (edge(_from)
    + edge(_to)), 1..1, uniqueVertices global, vertexProjections [_key, creationDate],
    postFilter v.creationDate < 1293840000000: traverse_filtered (the start vertex and every
    reached friend fetched for the filter, every survivor fetched again)."""
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="knows"),  # node 2: first traversal
        Step("kept", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both", depth=1,
             props=["creationDate"], label="Person",
             pred=lt(field("creationDate"), lit(DATE_CUTOFF))),        # node 2: postFilter
        Step("kv",   "pluck", rows=ref("kept"), field="_vid"),
        Step("d",    "dedup", items=ref("kv")),                        # node 2: uniqueVertices global
        Step("n",    "count", items=ref("d")),                         # node 6: the keys, counted
        Step("out",  "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def hop1_edgefilter_arangodb_engine(args):
    """From arangodb/hop1_edgefilter/p3/plan.txt. Node 2 TraversalNode: knows ANY (edge(_from)
    + edge(_to)), 1..1, NO vertexProjections (the vertex is never used), edgeProjections
    [_from, _key, _to, creationDate], postFilter e.creationDate < 1293840000000. creationDate
    is not in the edge index, so each edge document is read for the filter (1 GET) and each
    surviving edge read again as it is emitted: traverse_filtered with edge_props only (the
    IC11 [7] shape). No uniqueness option, so every edge counts, as in the optimal plan."""
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="knows"),  # node 2: first traversal
        Step("kept", "traverse_filtered", srcs=ref("open"), etype="knows", dir="both", depth=1,
             pred=lt(coalesce(get("creationDate"), 0), lit(DATE_CUTOFF)),
             edge_props=["creationDate"]),                             # node 2: edge postFilter
        Step("n",    "count", items=ref("kept")),                      # node 6: the edge keys, counted
        Step("out",  "as_row", value=ref("n"), field="count"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def edge_probe_arangodb_engine(args):
    """From arangodb/edge_probe/p3/plan.txt. Subquery [15]-[16] around node [5]
    TraversalNode: knows ANY from person1 (edge(_from) + edge(_to)), 1..1, default order (DFS),
    vertexProjections [_id], postFilter v._id == "Person/<person2>", then [8] LimitNode 1.
    traverse_filtered_limit: every edge of person1 is read (the traversal pulls the whole
    edge batch before it evaluates any vertex), but vertices are fetched for the filter only
    until the first match, popped last-edge-first (DFS); the start vertex is fetched too and
    the match is fetched again as it is emitted. [12] LENGTH(...) > 0 is in memory."""
    return Plan([
        Step("open", "traversal_open", items=[let("src_vid")], etype="knows"),  # node 5: first traversal
        Step("hit",  "traverse_filtered_limit", srcs=ref("open"), etype="knows", dir="both",
             limit=1, order="dfs", props=["id"], label="Person",
             pred=_eq(field("_vid"), let("dst_vid"))),                 # node 5 + node 8 LIMIT 1
        Step("n",    "count", items=ref("hit")),                       # node 12: LENGTH(...)
        Step("nr",   "as_row", value=ref("n"), field="n"),
        Step("fl",   "derive", rows=ref("nr"), into="knows", fn=_gt(field("n"), lit(0))),  # node 12: > 0
        Step("out",  "project", rows=ref("fl"), fields=["knows"]),
    ], params={**anchor_param("src"), **anchor_param("dst")},
        let={"src_vid": compose_vid("Person", param("src")),
             "dst_vid": compose_vid("Person", param("dst"))})


def hop1p7_knows_arangodb_engine(args):
    """From arangodb/hop1p7_knows/p3/plan.txt. Node 2 TraversalNode: knows ANY (edge(_from) + edge(_to)), 1..1,
    uniqueVertices global: the first traversal's extra SEEK, 1 SEEK + 1 NEXT per edge, and each returned vertex's
    document (pk + doc, docs_per_step) -- the 7 attributes come from that document. Nodes 3-5 (the sort key, the
    SortNode, the object) are in memory: no second fetch. The start is the literal `Person/<id>`: no anchor read."""
    props = PERSON_PROPS[:7]
    return Plan([
        Step("open", "traversal_open", items=[let("anchor_vid")], etype="knows"),  # node 2: first traversal
        Step("nb",   "read_neighbors", srcs=ref("open"), etype="knows", dir="both"),   # node 2: both edge indexes
        Step("d",    "dedup", items=ref("nb")),                        # node 2: uniqueVertices global
        Step("v",    "docs_per_step", vids=ref("d"), props=props, label="Person"),  # node 2: vertex fetch
        Step("srt",  "top_k", rows=ref("v"), keys=[(props[0], "asc")], k=None),     # node 4 SortNode
        Step("out",  "project", rows=ref("srt"), fields=props),        # node 5: the object
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_ARANGODB = {"hop1_knows": hop1_knows_arangodb_engine,
                   "hop1_knows_out": hop1_knows_out_arangodb_engine,
                   "hop1_label": hop1_label_arangodb_engine,
                   "hop1_propfilter": hop1_propfilter_arangodb_engine,
                   "hop1_edgefilter": hop1_edgefilter_arangodb_engine,
                   "edge_probe": edge_probe_arangodb_engine}
ENGINE_DRAFT_ARANGODB = {}


# The complex-kv-reads figure's Q1 engine plans, transcribed from the same captures with every param's live counts in
# view. Not in the engine-fidelity check's plan set (the ENGINE_<DB> dicts above): experiments/engine_fidelity/
# time_queries.py runs them and checks their counts against the live engines' counts of its own run.
COMPLEX_KV_READS_ENGINE = {"hop1p7_knows": {"dgraph": hop1p7_knows_dgraph_engine, "nebula": hop1p7_knows_nebula_engine,
                                   "arangodb": hop1p7_knows_arangodb_engine}}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"edge_probe": _KNOWS_HINTS, "hop1_edgefilter": _KNOWS_HINTS, "hop1_knows": _KNOWS_HINTS, "hop1_knows_out": _KNOWS_HINTS, "hop1_propfilter": _KNOWS_HINTS, "hop1p1_knows": _KNOWS_HINTS, "hop1p3_knows": _KNOWS_HINTS, "hop1p7_knows": _KNOWS_HINTS, "hop1p_knows": _KNOWS_HINTS}
