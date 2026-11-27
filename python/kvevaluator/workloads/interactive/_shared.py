"""Cross-query helpers and constants shared by the per-query plan modules."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import eq, lt, notnull, get, field, param, let, compose_vid, add, mul, lit
from kvevaluator.schema.vids import vid, person_vid, message_vids  # vid encoders: neutral infra, re-used here


# --- Reusable parameterization pieces (the anchor id is a hole; the encoded vid is composed
# at bind time, format-neutrally). A person builder wraps its steps in
# Plan(steps, params=PERSON_PARAM, let=PERSON_LET) and reads the anchor as let("me_vid");
# a message builder uses MESSAGE_PARAM/MESSAGE_LET and let("msg_vid"). Merge in extra
# {"param"} declarations (dates, names) with {**PERSON_PARAM, ...}. See is2.py for the
# worked pattern. `{"type": "id"}` is documentation -- compose_vid does the digit/pass-through
# split itself; `{"type": "int"}` is honored by bind (string param rows -> int). ---
PERSON_PARAM = {"person": {"type": "id"}}
PERSON_LET = {"me_vid": compose_vid("Person", param("person"))}
MESSAGE_PARAM = {"message": {"type": "id"}}
MESSAGE_LET = {"msg_vid": compose_vid("Message", param("message"))}

# IC3/IC4's date window: [startDate, startDate + durationDays days). `end` is a derived let.
DATE_WINDOW_PARAM = {"startDate": {"type": "int"}, "durationDays": {"type": "int"}}
DATE_WINDOW_LET = {"end": add(param("startDate"), mul(param("durationDays"), lit(86400000)))}


IC2_FIELDS = ["personId", "personFirstName", "personLastName",
              "messageId", "messageContent", "messageImageFile", "messageCreationDate"]


IC8_FIELDS = ["personId", "personFirstName", "personLastName",
              "commentId", "commentCreationDate", "commentContent"]


PERSON_RENAME = {"id": "personId", "firstName": "personFirstName", "lastName": "personLastName"}


# --- Engine-faithful capped approximations (the "price of correctness") ---
# The real engines cap two transitive walks because of query-language limits (dgraph DQL has
# no variable-length path; nebula matched it for parity): replyOf-to-root (IS2/IS6) and the
# tagClass subtree (IC12), both at 3. kvformat isn't bound by a query language, so it runs
# BOTH the correct and the capped plan -- these builders reproduce the capped engine plan so
# a capped run beside the correct one prices what the engines trade away.
ENGINE_WALK_CAP = 3


# Post and Comment share one id space AND one Message vid. arango learns which collection
# a message is in from the edge-index value it followed ("collection/id"): the hop puts it on the
# row (C++: Value::label; the Python oracle's _coll map stands in for it), so a traversal-reached
# message read never probes the wrong one; a vid that arrives with no label (the bare-id anchor,
# no edge to learn from) pays a per-collection probe.
MESSAGE_LABELS = ("Post", "Comment")


def _scan_lookup(out, label, prop, value):
    """The label-index-only replacement for read_index_eq(label, prop, value) -> vids:
    with no value index, enter through the type-enumeration index (read_vertices_by_label),
    read `prop` off every vertex of the type, and keep the equality matches. The
    per-vertex prop read here IS the price of the missing value index. The output
    keeps the ORIGINAL step's name (`out`), so downstream refs are unchanged --
    the only edit a label-only builder makes is swapping this block in for the
    read_index_eq step."""
    return [
        Step(out + "_scan", "read_vertices_by_label", label=label),
        Step(out + "_pr",   "read_vertex_props", vids=ref(out + "_scan"), props=[prop], label=label),
        Step(out + "_fx",   "filter", rows=ref(out + "_pr"),
             pred=eq(field(prop), value)),
        Step(out,           "pluck", rows=ref(out + "_fx"), field="_vid"),
    ]


# --------------------------------------------------------- shared step blocks

def _friend_set(p, hops):
    """Anchor `a` + the distinct persons within `hops` KNOWS steps (undirected),
    out name `f`. hops=2 (the IC friends+fof set) excludes the start person."""
    steps = [
        Step("a",  "read_vertex_by_id", vid=p, label="Person"),
        Step("f0", "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
    ]
    if hops == 1:
        steps.append(Step("f", "dedup", items=ref("f0")))
    else:
        steps += [
            Step("f1",  "dedup", items=ref("f0")),
            Step("f2r", "read_neighbors", srcs=ref("f1"), etype="knows", dir="both"),
            Step("f12", "concat", a=ref("f1"), b=ref("f2r")),
            Step("fd",  "dedup", items=ref("f12")),
            Step("f",   "exclude", items=ref("fd"), remove=ref("a")),
        ]
    return steps


# ------------------------------------------------------------------- IC2, IC9

def _recent_messages(args, rank_props, fetch_body, hops, ixwalk=False, k=20):
    """IC2 (hops=1) / IC9 (hops=2): the `k` most recent messages (creationDate <
    maxDate) by the friend set, with each message's creator. Sort: creationDate
    desc, message id asc. ixwalk (dgraph, rank_props unused): ranking AND the
    maxDate filter go through one bounded creationDate index walk (hi=maxDate --
    what real dgraph's fused le + orderdesc does); only the ~k boundary
    candidates pay exact-ms/id GETs, no per-message reads.

    k defaults to LDBC's 20; k=None keeps ALL qualifying messages (the ic2_nolimit
    ablation probe -- forces the non-ixwalk rank path, since a no-limit top-k index
    walk is not a top-k)."""
    if k is None and ixwalk:
        raise ValueError("_recent_messages: k=None (no limit) needs ixwalk=False (rank path)")
    p = let("me_vid")                       # anchor vid, filled at bind time from the person param
    steps = _friend_set(p, hops) + [
        # friends' messages; pairs keep the msg -> creator-friend association
        Step("fmsgs",    "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in", pairs=True),
        Step("msg_vids", "pluck", rows=ref("fmsgs"), field="dst"),
    ]
    if ixwalk:
        # ALL formats: ranking + maxDate filter + winner bodies in one bounded
        # walk (props fused: dgraph pays the same per-winner GETs a separate
        # fetch would; nebula 1 known-tag row GET each; arango 0 -- membership
        # already read the doc).
        steps += [
            Step("topk", "read_top_k_by_index", vids=ref("msg_vids"), prop="creationDate",
                 k=k, hi=param("maxDate"), labels=["Post", "Comment"],
                 props=["content", "imageFile"]),
        ]
    else:
        steps += [
            Step("ranked",     "read_vertex_props", vids=ref("msg_vids"), props=rank_props,
                 label=["Post", "Comment"]),
            Step("before_max", "filter", rows=ref("ranked"),
                 pred=lt(field("creationDate"), param("maxDate"))),
            Step("topk",       "top_k", rows=ref("before_max"),
                 keys=[("creationDate", "desc"), ("id", "asc")], k=k),
        ]
    steps += [
        Step("with_authv", "join", left=ref("topk"), right=ref("fmsgs"),
             left_on="_vid", right_on="dst"),           # adds src = creator friend
    ]
    if fetch_body and not ixwalk:                       # ixwalk fuses the body
        steps += [
            Step("top_vids",  "pluck", rows=ref("with_authv"), field="_vid"),
            Step("bodies",    "read_vertex_props", vids=ref("top_vids"),
                 props=["content", "imageFile"], label=["Post", "Comment"]),
            Step("with_body", "zip_rows", left=ref("with_authv"), right=ref("bodies")),
        ]
        merged = "with_body"
    else:
        merged = "with_authv"
    steps += [
        Step("author_v0",    "pluck", rows=ref(merged), field="src"),
        Step("author_vids",  "dedup", items=ref("author_v0")),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("authors_named", "rename", rows=ref("author_props"), names=PERSON_RENAME),
        Step("with_author",  "join", left=ref(merged), right=ref("authors_named"),
             left_on="src", right_on="_vid"),
        Step("labeled",      "rename", rows=ref("with_author"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("out", "project", rows=ref("labeled"), fields=IC2_FIELDS),
    ]
    return Plan(steps, params={**PERSON_PARAM, "maxDate": {"type": "int"}}, let=PERSON_LET)


def _recent_messages_logical(args, hops, k=20):
    """The LOGICAL IC2 (hops=1) / IC9 (hops=2): _recent_messages with the rank
    region as one `rank` step and the body fetch as a `fetch_props` marker; the
    optimizer's rule catalog lowers them (optimizer/rules.py) into exactly the
    ixwalk / getrank / onephase physical shapes above."""
    p = let("me_vid")
    steps = _friend_set(p, hops) + [
        Step("fmsgs",    "read_neighbors", srcs=ref("f"), etype="hasCreator", dir="in", pairs=True),
        Step("msg_vids", "pluck", rows=ref("fmsgs"), field="dst"),
        Step("topk", "rank", vids=ref("msg_vids"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=k, hi=param("maxDate"),
             labels=["Post", "Comment"], read_label=["Post", "Comment"],
             rank_props=["id", "creationDate"], body_props=["content", "imageFile"]),
        Step("with_authv", "join", left=ref("topk"), right=ref("fmsgs"),
             left_on="_vid", right_on="dst"),
        Step("with_body", "fetch_props", rows=ref("with_authv"), pluck="top_vids",
             props=["content", "imageFile"], label=["Post", "Comment"]),
        Step("author_v0",    "pluck", rows=ref("with_body"), field="src"),
        Step("author_vids",  "dedup", items=ref("author_v0")),
        Step("author_props", "read_vertex_props", vids=ref("author_vids"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("authors_named", "rename", rows=ref("author_props"), names=PERSON_RENAME),
        Step("with_author",  "join", left=ref("with_body"), right=ref("authors_named"),
             left_on="src", right_on="_vid"),
        Step("labeled",      "rename", rows=ref("with_author"),
             names={"id": "messageId", "creationDate": "messageCreationDate",
                    "content": "messageContent", "imageFile": "messageImageFile"}),
        Step("out", "project", rows=ref("labeled"), fields=IC2_FIELDS),
    ]
    return Plan(steps, params={**PERSON_PARAM, "maxDate": {"type": "int"}}, let=PERSON_LET)


# ── IS1-IS7 (dgraph) ─────────────────────────────────────────────────────────
# The short queries are small enough that the whole cost is visible in the plan, which makes
# them a good check on the ANCHOR and TYPE-FILTER modelling that the IC plans mostly hide.
#
# Two conventions the captured plans make explicit and every IS plan below follows:
#   - the anchor is `eq(id, $x)` + a type filter, i.e. ONE index SEEK plus one type-index SEEK
#     per candidate type. A MESSAGE anchor tests both Post and Comment (`@filter[or]`), so it
#     pays two type SEEKs, not one.
#   - `type(...)` on a traversal's output is likewise ONE type-index SEEK (typefilterpath=
#     index), never per-vertex.
def _anchor_person(p):
    """eq(id)+type(person): 1 index SEEK + 1 type-index SEEK."""
    return [Step("anchor", "read_vertex_by_id", vid=p),
            Step("atype", "read_vertices_by_label", label="Person"),
            Step("me", "intersect", items=ref("anchor"), other=ref("atype"))]


def _anchor_message(m):
    """`eq(id, $messageId) @filter(type(post) OR type(comment))`: exactly 3 SEEKs, whatever the message's type -- 1 for
    the id lookup and 1 per type-index posting list (the OR pays for each); measured, IS4 costs S=3 at every reply
    depth. The type scans come first and then the one lookup, so no step charges a probe the engine skips."""
    return [Step("tpost", "read_vertices_by_label", label="Post"),
            Step("tcomment", "read_vertices_by_label", label="Comment"),
            Step("tmsg", "concat", a=ref("tpost"), b=ref("tcomment")),
            # the candidate that the type index says exists -- picked with no store read
            Step("mv", "intersect", items=message_vids(m), other=ref("tmsg")),
            Step("me", "read_vertex_by_id", vid=ref("mv"))]


# ── IS1-IS7, arangodb ────────────────────────────────────────────────────────
# Arango's plan text does not show where its SEEKs go, so the per-construct costs below were
# MEASURED, one AQL probe per construct, /kvstats delta as the answer:
#
#   DOCUMENT('Post/3')            hit   S=0 G=2   pk + document
#   DOCUMENT('Comment/3')         miss  S=0 G=1   pk only -- nothing to address on a miss
#   FOR p IN Person FILTER p._key==X  RETURN p._key       S=0 G=1
#   FOR p IN Person FILTER p._key==X  RETURN p.firstName  S=0 G=2
#   FOR v IN 1..1 OUTBOUND <p> isLocatedIn RETURN v._key  S=2 G=2 N=1
#   ...                                    RETURN v.name  S=2 G=2 N=1
#   ...                              FOR v,e ... RETURN e._key  S=2 G=1 N=1
#   FOR v IN 1..1 ANY <p> knows RETURN v._key             S=3 G=18 N=9
#
# Four things fall out, and two of them are engine waste worth naming:
#
#   1. A traversal MATERIALIZES its destination vertex -- pk + doc, 2 GETs -- even when the
#      query asks only for `v._key`, which the edge index already carries. `_key`-only and
#      `.name` cost the same. That is why the arango IS cells cost more GETs than nebula's.
#   2. The edge document is read (1 GET) only when the query wants an edge attribute; with
#      `edgeProjections=[_from,_to]` the index is covering and the edge doc is never touched.
#   3. Primary-index lookup is a GET, not a SEEK. An `IndexNode primary(_key)` costs S=0.
#   4. The first traversal in a query pays one extra SEEK (see
#      ArangodbEngineFormat.traversal_open). Every later traversal costs 1 SEEK per start
#      vertex per edge index.
def _arango_message_anchor(m):
    """DOCUMENT('Post/<id>') then DOCUMENT('Comment/<id>'), which is what every message-anchored IS query compiles to:
    two CalculationNodes, one of which misses. Hit = pk+doc, miss = pk only, so the pair costs 3 GETs and no SEEK
    whichever type it is. The SAME Message vid is probed in both collections."""
    v, = message_vids(m)
    return [
        Step("as_post", "read_vertex_props", vids=[v], label="Post",
             props=["id", "content", "imageFile", "creationDate"]),
        Step("as_comment", "read_vertex_props", vids=[v], label="Comment",
             props=["id", "content", "creationDate"]),
        Step("both", "concat", a=ref("as_post"), b=ref("as_comment")),
        # the ternary `post != null ? post : comment` keeps whichever DOCUMENT found a row
        Step("me_rows", "filter", rows=ref("both"),
             pred=notnull(get("creationDate"))),
        Step("me", "pluck", rows=ref("me_rows"), field="_vid"),
    ]

