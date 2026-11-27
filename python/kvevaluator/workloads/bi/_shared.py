"""BI constants shared by the per-query modules (this workload's own; never imported from ldbc)."""

DAY = 86400000

# Post and Comment share one message id space; a read over "Messages" names both labels.
MESSAGE_LABELS = ["Post", "Comment"]

# A message's content is non-empty exactly when its `length` is > 0: on the LDBC data every Comment has
# content, and a Post has either content or an imageFile (then content is empty and length 0; sf0.1:
# 120,256 of 135,701 Posts, all with length 0). The reference Cypher tests `content IS NOT NULL`; reading
# `length` (needed anyway) instead of the fat `content` column is the cheapest equivalent test on this
# data, and an independent CSV check tests content itself.
HAS_CONTENT_FIELD = "length"


def message_vids_steps(src, typed, out):
    """Steps that keep the MESSAGES of the vid list `src`, which may hold Forums too (hasTag runs from Post,
    Comment AND Forum to Tag): the vids named `out`. A vid is opaque at query time (python/kvevaluator/schema/vids.py), so the
    type is never read from the id; two forms:
    "none"    -- no test: the entries flow on as they are (0 KV) and a Forum drops where its message-only reads
                 miss (hasCreator-out, likes-in, replyOf-in, containerOf-in on a root: one miss SEEK each). The
                 cheapest form wherever the plan's next reads are message-only and the result cannot change.
    "posting" -- dgraph: the two type postings restrict `src` (one SEEK each, the intersect free) -- a storage
                 read of the type, cheaper than the misses when the list holds many Forums."""
    from kvevaluator.plans.plan import Step, ref
    if typed == "none":
        return [Step(out, "dedup", items=ref(src))]                       # the entries as they are: 0 KV
    assert typed == "posting", typed
    return [Step(f"{out}_posts", "filter_by_label", vids=ref(src), label="Post"),
            Step(f"{out}_comments", "filter_by_label", vids=ref(src), label="Comment"),
            Step(out, "concat", a=ref(f"{out}_posts"), b=ref(f"{out}_comments"))]


def message_prop_steps(src, props, typed, out="msg_d"):
    """`props` of the MESSAGES of the vid list `src` (which may hold Forums): rows (_vid, props...) named `out`.
    The type test, by what the layout offers (never the id):
    "null"    -- a row store keyed by (vid, label): the props read under the two message labels returns a null row
                 for a Forum (its GETs miss), dropped by a notnull filter -- the labelled read IS the test.
    "posting" -- dgraph: the type postings first (message_vids_steps), then the props read.
    "probe"   -- a per-vid label test (filter_by_label, lowered per format) before the props read: for a layout
                 whose props read is not label-scoped (arangodb resolves any vid's collection) the plan must ask."""
    from kvevaluator.plans.plan import Step, ref
    from kvevaluator.plans.ir import authoring as E
    if typed == "null":
        return [Step(f"{out}_all", "read_vertex_props", vids=ref(src), props=list(props), label=list(MESSAGE_LABELS)),
                Step(out, "filter", rows=ref(f"{out}_all"), pred=E.notnull(E.field(props[0])))]
    if typed == "probe":
        return [Step(f"{out}_posts", "filter_by_label", vids=ref(src), label="Post"),
                Step(f"{out}_comments", "filter_by_label", vids=ref(src), label="Comment"),
                Step(f"{out}_v", "concat", a=ref(f"{out}_posts"), b=ref(f"{out}_comments")),
                Step(out, "read_vertex_props", vids=ref(f"{out}_v"), props=list(props), label=list(MESSAGE_LABELS))]
    assert typed == "posting", typed
    return message_vids_steps(src, typed, f"{out}_v") + [
        Step(out, "read_vertex_props", vids=ref(f"{out}_v"), props=list(props), label=list(MESSAGE_LABELS))]


def message_window_steps(src, props, typed, out):
    """Steps that turn a creationDate-window vid list `src` (both message labels) into rows (_vid, props..., _isPost)
    of the MESSAGES in it, three bodies by what the layout offers (bi1's docstrings):
    "scoped"  -- label_scoped_props: the Post-label props read misses on a Comment (null row), so the hits are the
                 Posts; the rest read under the Comment label. One props read per message, no type probe.
    "posting" -- dgraph: the window holds every label with the predicate and a uid carries no type: the two type
                 postings restrict it, then one props read under the message labels; the Post posting flags the type.
    "probe"   -- the generic body: a per-vid type probe (filter_by_label lowered per format) beside the props read."""
    from kvevaluator.plans.plan import Step, ref
    from kvevaluator.plans.ir import authoring as E
    if typed == "scoped":
        return [Step(f"{out}_p0", "read_vertex_props", vids=ref(src), props=list(props), label="Post"),
                Step(f"{out}_p", "filter", rows=ref(f"{out}_p0"), pred=E.notnull(E.field(props[0]))),
                Step(f"{out}_pv", "pluck", rows=ref(f"{out}_p"), field="_vid"),
                Step(f"{out}_rest", "exclude", items=ref(src), remove=ref(f"{out}_pv")),
                Step(f"{out}_c", "read_vertex_props", vids=ref(f"{out}_rest"), props=list(props), label="Comment"),
                Step(f"{out}_pt", "derive", rows=ref(f"{out}_p"), into="_isPost", fn=E.lit(True)),
                Step(f"{out}_ct", "derive", rows=ref(f"{out}_c"), into="_isPost", fn=E.lit(False)),
                Step(out, "concat", a=ref(f"{out}_pt"), b=ref(f"{out}_ct"))]
    if typed == "posting":
        return [Step(f"{out}_posts", "filter_by_label", vids=ref(src), label="Post"),
                Step(f"{out}_comments", "filter_by_label", vids=ref(src), label="Comment"),
                Step(f"{out}_msgs", "concat", a=ref(f"{out}_posts"), b=ref(f"{out}_comments")),
                Step(f"{out}_all", "read_vertex_props", vids=ref(f"{out}_msgs"), props=list(props), label=list(MESSAGE_LABELS)),
                Step(out, "flag_in", rows=ref(f"{out}_all"), field="_vid", allowed=ref(f"{out}_posts"), into="_isPost")]
    return [Step(f"{out}_all", "read_vertex_props", vids=ref(src), props=list(props), label=list(MESSAGE_LABELS)),
            Step(f"{out}_posts", "filter_by_label", vids=ref(src), label="Post"),
            Step(out, "flag_in", rows=ref(f"{out}_all"), field="_vid", allowed=ref(f"{out}_posts"), into="_isPost")]


def message_scan_steps(props, out):
    """The type-pass entry: enumerate Post and Comment by the type index and read `props` per vertex, the label known
    from the enumeration (rows _vid, props..., _isPost). On a layout whose type scan carries the rows the two steps
    per label fuse into one (kvevaluator.plans.ir.lowering.fuse_label_scan: the registry's _FUSED formats)."""
    from kvevaluator.plans.plan import Step, ref
    from kvevaluator.plans.ir import authoring as E
    return [Step(f"{out}_pv", "read_vertices_by_label", label="Post"),
            Step(f"{out}_p", "read_vertex_props", vids=ref(f"{out}_pv"), props=list(props), label="Post"),
            Step(f"{out}_cv", "read_vertices_by_label", label="Comment"),
            Step(f"{out}_c", "read_vertex_props", vids=ref(f"{out}_cv"), props=list(props), label="Comment"),
            Step(f"{out}_pt", "derive", rows=ref(f"{out}_p"), into="_isPost", fn=E.lit(True)),
            Step(f"{out}_ct", "derive", rows=ref(f"{out}_c"), into="_isPost", fn=E.lit(False)),
            Step(out, "concat", a=ref(f"{out}_pt"), b=ref(f"{out}_ct"))]


def with_label_run(plan, names):
    """The LABEL-RUN twin of a plan (as ic10_fof_from_person_run): each step named in `names`, a read_vertex_props of
    many vertices (one point read each), becomes ONE scan of its label's key run kept to the same vids
    (read_vertices_by_label with keep: one SEEK, then one NEXT per key of the run, the keep test in memory). A step read
    under several labels (the message labels) becomes one kept scan per label, then concat. Valid where the label's
    records are one key run (label_rows_one_run: every props layout of meta, pertype_csr, arangodb); a candidate beside
    today's bodies, picked by cost. Rows come one per kept vertex, in vid order (every input here is unique)."""
    from kvevaluator.plans.plan import Plan, Step, ref

    def twin(steps):
        out, done = [], set()
        for s in steps:
            if s.out not in names:
                out.append(Step(s.out, s.op, **dict(s.args)))
                continue
            assert s.op == "read_vertex_props", s
            labels = s.args["label"] if isinstance(s.args["label"], list) else [s.args["label"]]
            props, keep = s.args["props"], s.args["vids"]
            if len(labels) == 1:
                out.append(Step(s.out, "read_vertices_by_label", label=labels[0], props=props, keep=keep))
            else:
                assert len(labels) == 2, s           # concat takes two inputs
                runs = [f"{s.out}_run_{lab}" for lab in labels]
                out += [Step(r, "read_vertices_by_label", label=lab, props=props, keep=keep) for r, lab in zip(runs, labels)]
                out.append(Step(s.out, "concat", a=ref(runs[0]), b=ref(runs[1])))
            done.add(s.out)
        assert done == set(names), (sorted(names), sorted(done))     # every named step found
        return out

    if isinstance(plan, Plan):
        return Plan(twin(plan.steps), params=plan.params, let=plan.let)
    return twin(plan)


# The reply chains' depth on the LDBC data (a comment to its root Post): 7 at sf0.1, 8 at sf0.3, 9 at sf3 (the CSVs).
# CHASE_LEVELS = the deepest chain + 1, so the last level's frontier is empty at every scale: the plan
# has no loop, so the level-wise chase is unrolled to this fixed, data-derived depth (a deeper chain at a larger scale
# would leave a non-root in `_root`; the cross-store and CSV checks would show it).
CHASE_LEVELS = 10


def chase_levelwise_steps(rows, etype, out, levels=CHASE_LEVELS, cur="_vid", into="_root"):
    """Rows `rows` (each with the start vertex in field `cur`) -> the same rows with `into` = the end of each start's
    parent chain through `etype` (out), named `out` -- the LEVEL-WISE chase, each chain node read exactly once:
      forward: per level one read_neighbors over the frontier (a chunk landed on once per level, shared by every
               source in it); the frontier = the distinct parents just found that were never expanded before (a
               visited set); the nodes read at a level that have no parent are that level's ROOTS;
      backward: the roots resolve bottom-up -- from the deepest level, each level's (child, parent) pairs join the
               roots known so far on the parent (one small join per level, no copy of a growing map); a pair whose
               parent is a node of a shallower level stays pending and resolves when that level is reached;
               `levels` extra passes settle chains that run through the start level itself.
    Memory is O(N) rows in total: the executor keeps every step's output until the plan ends, so a per-level concat of
    the whole parent map would hold tens of GB at sf3. read_chase also reads each node once; the two differ in how a
    level is read (one read_neighbors over the frontier here, a first-neighbour read per node there), and the pick
    chooses between them. `levels` = the data's deepest chain + 1 (CHASE_LEVELS): unrolled, the plan IR has no loop;
    a chain deeper than that would leave a start without a root (the final join drops it)."""
    from kvevaluator.plans.plan import Step, ref
    from kvevaluator.plans.ir import authoring as E
    o = out
    steps = [Step(f"{o}_f0v", "pluck", rows=ref(rows), field=cur),
             Step(f"{o}_f0", "dedup", items=ref(f"{o}_f0v"))]
    f, seen = f"{o}_f0", f"{o}_f0"
    for k in range(levels):                                   # forward: each node read once
        steps += [Step(f"{o}_p{k}", "read_neighbors", srcs=ref(f), etype=etype, dir="out", pairs=True),
                  Step(f"{o}_n{k}", "rename", rows=ref(f"{o}_p{k}"), names={"src": "_c", "dst": "_p"}),
                  Step(f"{o}_pv{k}", "pluck", rows=ref(f"{o}_p{k}"), field="src"),
                  Step(f"{o}_t{k}", "exclude", items=ref(f), remove=ref(f"{o}_pv{k}")),          # this level's roots
                  Step(f"{o}_fv{k + 1}", "pluck", rows=ref(f"{o}_p{k}"), field="dst"),
                  Step(f"{o}_fd{k + 1}", "dedup", items=ref(f"{o}_fv{k + 1}")),
                  Step(f"{o}_f{k + 1}", "exclude", items=ref(f"{o}_fd{k + 1}"), remove=ref(seen)),
                  Step(f"{o}_s{k + 1}", "concat", a=ref(seen), b=ref(f"{o}_f{k + 1}"))]
        f, seen = f"{o}_f{k + 1}", f"{o}_s{k + 1}"
    known, pending = None, None

    def resolve(tag, pend, kn):
        """pend (rows _c, _p) against kn (rows _vid, _root): the resolved (_vid, _root) rows and the still-pending pairs"""
        nonlocal steps
        steps += [Step(f"{o}_jn{tag}", "left_join", left=ref(pend), right=ref(kn), left_on="_p", right_on="_vid",
                       default={"_root": None}),
                  Step(f"{o}_rs{tag}", "filter", rows=ref(f"{o}_jn{tag}"), pred=E.notnull(E.field("_root"))),
                  Step(f"{o}_rn{tag}", "rename", rows=ref(f"{o}_rs{tag}"), names={"_c": "_vid"}),
                  Step(f"{o}_rr{tag}", "project", rows=ref(f"{o}_rn{tag}"), fields=["_vid", "_root"]),
                  Step(f"{o}_pn{tag}", "filter", rows=ref(f"{o}_jn{tag}"), pred=E.isnull(E.field("_root"))),
                  Step(f"{o}_pp{tag}", "project", rows=ref(f"{o}_pn{tag}"), fields=["_c", "_p"])]
        return f"{o}_rr{tag}", f"{o}_pp{tag}"

    for k in reversed(range(levels)):                         # backward: roots bottom-up
        steps += [Step(f"{o}_tr{k}", "annotate", items=ref(f"{o}_t{k}"), field="_k", value=1),
                  Step(f"{o}_tk{k}", "derive", rows=ref(f"{o}_tr{k}"), into="_root", fn=E.field("_vid")),
                  Step(f"{o}_tq{k}", "project", rows=ref(f"{o}_tk{k}"), fields=["_vid", "_root"])]
        if known is None:
            kn = f"{o}_tq{k}"
        else:
            steps.append(Step(f"{o}_kn{k}", "concat", a=ref(known), b=ref(f"{o}_tq{k}")))
            kn = f"{o}_kn{k}"
        if pending is None:
            pend = f"{o}_n{k}"
        else:
            steps.append(Step(f"{o}_pe{k}", "concat", a=ref(pending), b=ref(f"{o}_n{k}")))
            pend = f"{o}_pe{k}"
        res, pending = resolve(f"b{k}", pend, kn)
        steps.append(Step(f"{o}_k{k}", "concat", a=ref(kn), b=ref(res)))
        known = f"{o}_k{k}"
    for i in range(levels):                                   # the chains through the start level itself
        res, pending = resolve(f"x{i}", pending, known)
        steps.append(Step(f"{o}_kx{i}", "concat", a=ref(known), b=ref(res)))
        known = f"{o}_kx{i}"
    steps.append(Step(out, "join", left=ref(rows), right=ref(known), left_on=cur, right_on="_vid"))
    return steps
