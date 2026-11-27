"""BI17 Information propagation analysis (spec 8.4 BI/read/17; reference neo4j/queries/bi-17.cypher)."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir import authoring as E
from kvevaluator.workloads.bi._shared import MESSAGE_LABELS, chase_levelwise_steps

HOUR = 3_600_000


def bi17(args):
    """Per person1 who wrote a message1 tagged $tag in forum1: the number of distinct message2 tagged $tag, in another
    forum2, created more than $delta hours after message1, that a member of forum1 (person2) replied to with a
    Comment tagged $tag, where message2's author (person3, a different Person than person2) is a member of forum1 and
    person1 is not a member of forum2. Top 10 by messageCount desc, person1.id asc.

    One pass over the tag's messages M: Tag.name index -> hasTag in -> one props read each under the message labels
    (creationDate; a Forum in the list gives a null row on a row store and drops), hasCreator out per message, the
    level-wise replyOf chase to the root post (its first level is also the comment -> parent pairs), containerOf in
    per root (the forum), hasMember out per forum of M. Rows A = (message1, person1, forum1, cd1); rows B = the
    comments of M whose parent is in M, with person2, message2, person3, forum2, cd2. B joins the members on person2
    (forum1 candidates), A joins on forum1; the rest are tuple-key tests and flow. Ids read for the 10 winners.

    The two big joins test each pair as they make it (join_multi's `where` and `where_key`), so the pairs a test drops
    are never held: at SF30 they ran to ~370 GB. A is cut to one row per (forum1, person1), its oldest cd1: some message1
    of person1 in forum1 is older than cd2 - $delta exactly when the oldest one is, so the (person1, message2) pairs
    are the same."""
    cd = E.field("creationDate")
    return Plan([
        Step("tag",      "read_index_eq", label="Tag", prop="name", value=E.param("tag")),
        Step("m0",       "read_neighbors", srcs=ref("tag"), etype="hasTag", dir="in"),
        Step("M",        "dedup", items=ref("m0")),
        Step("mp0",      "read_vertex_props", vids=ref("M"), props=["creationDate"], label=list(MESSAGE_LABELS)),
        Step("mp",       "filter", rows=ref("mp0"), pred=E.notnull(cd)),
        Step("cr",       "read_neighbors", srcs=ref("M"), etype="hasCreator", dir="out", pairs=True),         # src = message, dst = author
        Step("cr_n",     "rename", rows=ref("cr"), names={"src": "_vid", "dst": "_author"}),
    ] + chase_levelwise_steps("mp", "replyOf", "rooted") + [                                                  # rows _vid, creationDate, _root; rooted_p0 = (child, parent)
        Step("roots0",   "pluck", rows=ref("rooted"), field="_root"),
        Step("roots",    "dedup", items=ref("roots0")),
        Step("fr",       "read_neighbors", srcs=ref("roots"), etype="containerOf", dir="in", pairs=True),     # src = post, dst = forum
        Step("fr_n",     "rename", rows=ref("fr"), names={"src": "_root", "dst": "_forum"}),
        Step("mf",       "join_multi", left=ref("rooted"), right=ref("fr_n"), left_on="_root", right_on="_root"),   # _vid, creationDate, _root, _forum (inner: a Forum in M has no post)
        Step("mfa",      "join_multi", left=ref("mf"), right=ref("cr_n"), left_on="_vid", right_on="_vid"),         # + _author
        Step("fv0",      "pluck", rows=ref("fr_n"), field="_forum"),
        Step("fv",       "dedup", items=ref("fv0")),
        Step("mem",      "read_neighbors", srcs=ref("fv"), etype="hasMember", dir="out", pairs=True),         # src = forum, dst = member
        Step("mem_n",    "rename", rows=ref("mem"), names={"src": "_f1", "dst": "_p2"}),
        Step("mem_k",    "derive", rows=ref("mem_n"), into="_k", fn=E.tuple_(E.field("_f1"), E.field("_p2"))),
        Step("mem_keys", "pluck", rows=ref("mem_k"), field="_k"),
        # A: message1 rows
        Step("A",        "rename", rows=ref("mfa"), names={"_vid": "_m1", "creationDate": "_cd1", "_forum": "_f1", "_author": "_p1"}),
        Step("A_p",      "project", rows=ref("A"), fields=["_m1", "_cd1", "_f1", "_p1"]),
        Step("A_min",    "group_agg", rows=ref("A_p"), by=["_f1", "_p1"], aggs={"_cd1": ["min", "_cd1"]}),   # the oldest message1 per (forum1, person1)
        # B: comment -> message2 rows, both in M
        Step("par",      "rename", rows=ref("rooted_p0"), names={"src": "_c", "dst": "_m2"}),
        Step("par_m",    "filter_in", rows=ref("par"), field="_m2", allowed=ref("M")),
        Step("cr_c",     "rename", rows=ref("cr"), names={"src": "_c", "dst": "_p2"}),
        Step("B1",       "join_multi", left=ref("par_m"), right=ref("cr_c"), left_on="_c", right_on="_c"),    # + _p2
        Step("m2info",   "rename", rows=ref("mfa"), names={"_vid": "_m2", "creationDate": "_cd2", "_forum": "_f2", "_author": "_p3"}),
        Step("m2i_p",    "project", rows=ref("m2info"), fields=["_m2", "_cd2", "_f2", "_p3"]),
        Step("B",        "join_multi", left=ref("B1"), right=ref("m2i_p"), left_on="_m2", right_on="_m2"),    # _c, _m2, _p2, _cd2, _f2, _p3
        # forum1 candidates: the forums person2 is a member of; person3 a member too; then message1 rows of forum1
        Step("Bf",       "join_multi", left=ref("B"), right=ref("mem_n"), left_on="_p2", right_on="_p2",      # + _f1
             where=E.and_(E.ne(E.field("_f1"), E.field("_f2")), E.ne(E.field("_p2"), E.field("_p3"))),          # the spec: person2 and person3 differ
             where_key=E.tuple_(E.field("_f1"), E.field("_p3")), where_in=ref("mem_keys")),                   # person3 a member of forum1
        Step("Bf_p",     "project", rows=ref("Bf"), fields=["_f1", "_f2", "_m2", "_cd2"]),
        Step("AB",       "join_multi", left=ref("Bf_p"), right=ref("A_min"), left_on="_f1", right_on="_f1",  # + _p1, _cd1
             where=E.gt(E.field("_cd2"), E.add(E.field("_cd1"), E.mul(E.param("delta"), E.lit(HOUR)))),
             where_key=E.tuple_(E.field("_f2"), E.field("_p1")), where_not_in=ref("mem_keys")),               # person1 not a member of forum2
        Step("AB_d",     "group_agg", rows=ref("AB"), by=["_p1", "_m2"], aggs={"_n": ["count", None]}),                           # distinct message2 per person1
        Step("cnt",      "group_agg", rows=ref("AB_d"), by=["_p1"], aggs={"messageCount": ["count", None]}),
        Step("top",      "top_k", rows=ref("cnt"), keys=[("messageCount", "desc"), ("_p1", "asc")], k=10),
        Step("top_v",    "pluck", rows=ref("top"), field="_p1"),
        Step("ids",      "read_vertex_props", vids=ref("top_v"), props=["id"], label="Person"),
        Step("ids_n",    "rename", rows=ref("ids"), names={"id": "personId"}),
        Step("with_id",  "join", left=ref("top"), right=ref("ids_n"), left_on="_p1", right_on="_vid"),
        Step("out",      "project", rows=ref("with_id"), fields=["personId", "messageCount"]),
    ], params={"tag": {"type": "str"}, "delta": {"type": "int"}})


SIM = {"bi17": bi17}
