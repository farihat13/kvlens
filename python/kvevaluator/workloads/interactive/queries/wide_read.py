"""Wide-read queries: six realistic queries built around the ONE access pattern a row store (nebula) prices cheaper
than a posting store (dgraph), a WIDE read -- several properties of a modest set of vertices.

nebula reads a whole row in 1 GET; dgraph pays 1 GET per (predicate, vertex). Each query avoids the three
dgraph-favoring traps: (1) no rank/filter on an indexed vertex prop that dgraph can short-circuit with an index
walk; (2) shallow reach with an unambiguous target type; (3) a real property projection, not a count/id-list final.

One logical shape per query (LOGICAL only, also its hand plan): every format runs the SAME plan and prices it its
own way.
"""
from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let, param, compose_vid
from kvevaluator.workloads.interactive._shared import (PERSON_PARAM, PERSON_LET, PERSON_RENAME, MESSAGE_PARAM, MESSAGE_LET,
                                                 MESSAGE_LABELS)


# A forum anchor (no shared constant -- only this file anchors on a forum). The id hole is filled
# at bind time; compose_vid maps it to the forum_ vid, format-neutrally.
FORUM_PARAM = {"forum": {"type": "id"}}
FORUM_LET = {"forum_vid": compose_vid("Forum", param("forum"))}

# The full Person profile (id is key-derived -> free on both formats; the other 7 are scalar).
PERSON_PROFILE = ["id", "firstName", "lastName", "gender", "birthday",
                  "creationDate", "locationIP", "browserUsed"]


# --------------------------------------------------------------- A1: profile multiget

def profile_multiget(args):
    """A1 (control) -- full profile of every direct friend, unsorted. Isolates the pure wide read:
    dgraph pays |friends| x 7 GETs, nebula |friends| x 1. Byte-neutral because it projects ~the
    whole Person record (is1's mechanism, batched over the friend set). Robust -- everyone has
    friends, so every param projects."""
    p = let("me_vid")
    return Plan([
        Step("me",    "read_vertex_by_id", vid=p, label="Person"),
        Step("fr",    "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("fvids", "dedup", items=ref("fr")),
        Step("props", "read_vertex_props", vids=ref("fvids"), props=PERSON_PROFILE, label="Person"),
        Step("sorted", "top_k", rows=ref("props"), keys=[("id", "asc")], k=None),  # deterministic order
        Step("out",   "project", rows=ref("sorted"), fields=PERSON_PROFILE),
    ], params=PERSON_PARAM, let=PERSON_LET)


# ----------------------------------------------------- A2: network profile digest

def network_profile_digest(args):
    """A2 -- friend-list page. Every friend's profile + the friendship date, sorted by
    (friendshipCreationDate desc, personId asc). This is is3 (measured nebula 1.80x) widened from
    4 to 7 projected fields -- the wider projection grows the ops win and shrinks the byte ratio.
    Robust (every param projects)."""
    p = let("me_vid")
    return Plan([
        Step("me",      "read_vertex_by_id", vid=p, label="Person"),
        Step("fr",      "read_neighbors", srcs=ref("me"), etype="knows", dir="both",
             edge_props=["creationDate"], pairs=True),
        Step("fvids",   "pluck", rows=ref("fr"), field="dst"),
        # person.creationDate is left out so it does not collide with the friendship (edge) date
        Step("props",   "read_vertex_props", vids=ref("fvids"),
             props=["id", "firstName", "lastName", "gender", "birthday", "locationIP", "browserUsed"],
             label="Person"),
        Step("rows",    "zip_rows", left=ref("fr"), right=ref("props")),
        Step("sorted",  "top_k", rows=ref("rows"),
             keys=[("creationDate", "desc"), ("id", "asc")], k=None),
        Step("labeled", "rename", rows=ref("sorted"),
             names={**PERSON_RENAME, "creationDate": "friendshipCreationDate"}),
        Step("out",     "project", rows=ref("labeled"),
             fields=["personId", "personFirstName", "personLastName", "gender", "birthday",
                     "locationIP", "browserUsed", "friendshipCreationDate"]),
    ], params=PERSON_PARAM, let=PERSON_LET)


# ---------------------------------------------------------- A4: full profile page

def _org_block(kind, srcs_ref, etype, eprop, name_field):
    """A compact, self-contained org sub-plan for the profile page: friend -[etype {eprop}]-> org,
    returning `<kind>_full` rows {src: friend, <name_field>, <eprop>}. (Deliberately simpler than
    ic1's version -- names + the edge attribute, no place hop -- and kept local so this file does
    not import another query's internals.)"""
    k = kind
    return [
        Step(k + "_e",   "read_neighbors", srcs=srcs_ref, etype=etype, dir="out",
             edge_props=[eprop], pairs=True),
        Step(k + "_v",   "pluck", rows=ref(k + "_e"), field="dst"),
        Step(k + "_d",   "dedup", items=ref(k + "_v")),
        Step(k + "_nm",  "read_vertex_props", vids=ref(k + "_d"), props=["name"], label="Organisation"),
        Step(k + "_nm2", "rename", rows=ref(k + "_nm"), names={"name": name_field}),
        Step(k + "_full", "join", left=ref(k + "_e"), right=ref(k + "_nm2"),
             left_on="dst", right_on="_vid"),
    ]


def full_profile_page(args):
    """A4 -- profile page. Each friend's full profile + city name + universities + companies
    (= IC1's spec return minus the dgraph-favoring BFS + firstName index filter). The wide profile
    read dominates the cost; the org/place names are the page's secondary detail. Robust."""
    p = let("me_vid")
    steps = [
        Step("me",       "read_vertex_by_id", vid=p, label="Person"),
        Step("fr",       "read_neighbors", srcs=ref("me"), etype="knows", dir="both"),
        Step("fvids",    "dedup", items=ref("fr")),
        Step("props",    "read_vertex_props", vids=ref("fvids"), props=PERSON_PROFILE, label="Person"),
        # city: one per person, aligned with `props` order (LDBC Person.isLocatedIn is mandatory)
        Step("city",     "read_neighbors", srcs=ref("fvids"), etype="isLocatedIn", dir="out"),
        Step("city_n",   "read_vertex_props", vids=ref("city"), props=["name"], label="Place"),
        Step("city_r",   "rename", rows=ref("city_n"), names={"name": "cityName"}),
        Step("withcity", "zip_rows", left=ref("props"), right=ref("city_r")),
    ]
    steps += _org_block("u", ref("fvids"), "studyAt", "classYear", "universityName")
    steps += _org_block("w", ref("fvids"), "workAt", "workFrom", "companyName")
    steps += [
        Step("withuni", "nest", rows=ref("withcity"), children=ref("u_full"), on="_vid",
             child_on="src", into="universities", fields=["universityName", "classYear"]),
        Step("withco",  "nest", rows=ref("withuni"), children=ref("w_full"), on="_vid",
             child_on="src", into="companies", fields=["companyName", "workFrom"]),
        Step("labeled", "rename", rows=ref("withco"), names=PERSON_RENAME),
        Step("sorted",  "top_k", rows=ref("labeled"), keys=[("personId", "asc")], k=None),  # deterministic order
        Step("out",     "project", rows=ref("sorted"),
             fields=["personId", "personFirstName", "personLastName", "gender", "birthday",
                     "creationDate", "locationIP", "browserUsed",
                     "cityName", "universities", "companies"]),
    ]
    return Plan(steps, params=PERSON_PARAM, let=PERSON_LET)


# ------------------------------------------------------------- B1: liker digest (ic7-wide)

def liker_digest(args):
    """B1 -- ic7 widened. Recent likers of a person's messages (ranked on the FREE likeCreationDate
    edge facet, so no dgraph index short-circuit), top 20, each with the FULL liker profile + the
    FULL liked message. ic7's mechanism (nebula 1.42x on the one param that has likers) with a
    wider winner projection. PARAM-FRAGILE: needs like-bearing anchors (curate params, is7
    reply-anchor discipline) -- else, like stock ic7, most params have no likers and the win
    vanishes. The tail is inlined (not ic7's shared narrow tail) so ic7.py is untouched."""
    p = let("me_vid")
    return Plan([
        Step("a",          "read_vertex_by_id", vid=p, label="Person"),
        Step("msgs",       "read_neighbors", srcs=ref("a"), etype="hasCreator", dir="in"),
        Step("like_edges", "read_neighbors", srcs=ref("msgs"), etype="likes", dir="in",
             edge_props=["creationDate"], pairs=True),
        Step("liker_rows", "rename", rows=ref("like_edges"),
             names={"src": "_msg", "dst": "_vid", "creationDate": "likeCreationDate"}),
        Step("newest",     "group_top", rows=ref("liker_rows"), by="_vid",
             keys=[("likeCreationDate", "desc")]),            # newest like per liker (0 GET -- free facet)
        Step("cand",       "top_k", rows=ref("newest"),
             keys=[("likeCreationDate", "desc")], k=20, keep_ties=True),
        # WIDE winner tail: full liker profile
        Step("liker_v",    "pluck", rows=ref("cand"), field="_vid"),
        Step("liker_props", "read_vertex_props", vids=ref("liker_v"), props=PERSON_PROFILE, label="Person"),
        Step("liker_join", "join", left=ref("cand"), right=ref("liker_props"),
             left_on="_vid", right_on="_vid"),
        Step("top20",      "top_k", rows=ref("liker_join"),
             keys=[("likeCreationDate", "desc"), ("id", "asc")], k=20),
        # + the full liked message
        Step("msg_v",      "pluck", rows=ref("top20"), field="_msg"),
        Step("msg_props",  "read_vertex_props", vids=ref("msg_v"),
             props=["id", "content", "imageFile", "creationDate", "locationIP", "browserUsed", "length"],
             label=MESSAGE_LABELS),
        Step("msg_named",  "rename", rows=ref("msg_props"),
             names={"id": "messageId", "content": "messageContent", "imageFile": "messageImageFile",
                    "creationDate": "messageCreationDate", "locationIP": "messageLocationIP",
                    "browserUsed": "messageBrowserUsed", "length": "messageLength"}),
        Step("with_msg",   "join", left=ref("top20"), right=ref("msg_named"),
             left_on="_msg", right_on="_vid"),
        Step("out",        "project", rows=ref("with_msg"),
             fields=["id", "firstName", "lastName", "gender", "birthday", "creationDate",
                     "locationIP", "browserUsed", "likeCreationDate",
                     "messageId", "messageContent", "messageImageFile", "messageCreationDate",
                     "messageLocationIP", "messageBrowserUsed", "messageLength"]),
    ], params=PERSON_PARAM, let=PERSON_LET)


# --------------------------------------------------------- D3: reply-thread render

def reply_thread_render(args):
    """D3 -- reply-thread view. A message + all its direct replies (replyOf-in), each reply's full
    content + its author's name. Message-anchored on the is7 reply-stratified params so the reply
    set is non-empty (else, like stock ic7, the projection is empty and the win vanishes)."""
    m = let("msg_vid")
    return Plan([
        Step("replies",      "read_neighbors", srcs=[m], etype="replyOf", dir="in"),
        Step("rvids",        "dedup", items=ref("replies")),
        Step("rprops",       "read_vertex_props", vids=ref("rvids"),
             props=["id", "content", "creationDate", "locationIP", "browserUsed", "length"],
             label=MESSAGE_LABELS),
        # each reply's author (name only -- the reply bodies are the wide read here)
        Step("auth_e",       "read_neighbors", srcs=ref("rvids"), etype="hasCreator", dir="out", pairs=True),
        Step("auth_v",       "pluck", rows=ref("auth_e"), field="dst"),
        Step("auth_p",       "read_vertex_props", vids=ref("auth_v"),
             props=["id", "firstName", "lastName"], label="Person"),
        Step("auth_named",   "rename", rows=ref("auth_p"), names=PERSON_RENAME),
        Step("auth_join",    "join", left=ref("auth_e"), right=ref("auth_named"),
             left_on="dst", right_on="_vid"),
        Step("auth_by_reply", "rename", rows=ref("auth_join"), names={"src": "_reply"}),
        Step("rmsg_named",   "rename", rows=ref("rprops"),
             names={"id": "messageId", "content": "messageContent", "creationDate": "messageCreationDate",
                    "locationIP": "messageLocationIP", "browserUsed": "messageBrowserUsed",
                    "length": "messageLength"}),
        Step("with_auth",    "join", left=ref("rmsg_named"), right=ref("auth_by_reply"),
             left_on="_vid", right_on="_reply"),
        Step("sorted",       "top_k", rows=ref("with_auth"), keys=[("messageId", "asc")], k=None),  # deterministic order
        Step("out",          "project", rows=ref("sorted"),
             fields=["messageId", "messageContent", "messageCreationDate", "messageLocationIP",
                     "messageBrowserUsed", "messageLength",
                     "personId", "personFirstName", "personLastName"]),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


# --------------------------------------------------------- D2: forum board render

def forum_board_render(args):
    """D2 -- forum board. A forum's posts (containerOf, Post-only so no type probe), each with full
    content + the author's full profile. Shallow, so a robust wide read over the forum's posts and
    their authors. Forum-anchored (see gen_forum_params for the anchor set)."""
    f = let("forum_vid")
    return Plan([
        Step("posts",        "read_neighbors", srcs=[f], etype="containerOf", dir="out"),
        Step("pvids",        "dedup", items=ref("posts")),
        Step("pprops",       "read_vertex_props", vids=ref("pvids"),
             props=["id", "content", "imageFile", "creationDate", "locationIP", "browserUsed",
                    "language", "length"], label="Post"),
        Step("auth_e",       "read_neighbors", srcs=ref("pvids"), etype="hasCreator", dir="out", pairs=True),
        Step("auth_v",       "pluck", rows=ref("auth_e"), field="dst"),
        Step("auth_d",       "dedup", items=ref("auth_v")),
        Step("auth_p",       "read_vertex_props", vids=ref("auth_d"), props=PERSON_PROFILE, label="Person"),
        Step("auth_named",   "rename", rows=ref("auth_p"), names=PERSON_RENAME),
        Step("auth_join",    "join", left=ref("auth_e"), right=ref("auth_named"),
             left_on="dst", right_on="_vid"),
        Step("auth_by_post", "rename", rows=ref("auth_join"), names={"src": "_post"}),
        Step("pmsg_named",   "rename", rows=ref("pprops"),
             names={"id": "messageId", "content": "messageContent", "imageFile": "messageImageFile",
                    "creationDate": "messageCreationDate", "locationIP": "messageLocationIP",
                    "browserUsed": "messageBrowserUsed", "language": "messageLanguage",
                    "length": "messageLength"}),
        Step("with_auth",    "join", left=ref("pmsg_named"), right=ref("auth_by_post"),
             left_on="_vid", right_on="_post"),
        Step("sorted",       "top_k", rows=ref("with_auth"), keys=[("messageId", "asc")], k=None),  # deterministic order
        Step("out",          "project", rows=ref("sorted"),
             fields=["messageId", "messageContent", "messageImageFile", "messageCreationDate",
                     "messageLocationIP", "messageBrowserUsed", "messageLanguage", "messageLength",
                     "personId", "personFirstName", "personLastName", "gender", "birthday",
                     "creationDate", "locationIP", "browserUsed"]),
    ], params=FORUM_PARAM, let=FORUM_LET)


# single candidate each: the logical plan IS the physical one (passthrough search); every format
# runs the same shape and prices it its own way.
_QUERIES = {
    "profile_multiget": profile_multiget,
    "network_profile_digest": network_profile_digest,
    "full_profile_page": full_profile_page,
    "liker_digest": liker_digest,
    "reply_thread_render": reply_thread_render,
    "forum_board_render": forum_board_render,
}
LOGICAL = dict(_QUERIES)


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"full_profile_page": _KNOWS_HINTS, "network_profile_digest": _KNOWS_HINTS, "profile_multiget": _KNOWS_HINTS}
