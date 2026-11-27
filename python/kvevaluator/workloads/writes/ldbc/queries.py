"""LDBC SNB Interactive's insert operations (IU1-IU8) as plans: each a short sequence of the vertex and edge inserts
that writes/microbench prices one at a time, so no new format code is needed.

  IU1  add person         a Person + its city, tags, universities, companies, and friends
  IU2  add post like      one likes edge, Person -> Post
  IU3  add comment like   one likes edge, Person -> Comment
  IU4  add forum          a Forum + its moderator + its tags
  IU5  add forum member   one hasMember edge, Forum -> Person
  IU6  add post           a Post + creator, forum, country, tags
  IU7  add comment        a Comment + creator, country, tags, and its replyOf target
  IU8  add friendship     one knows edge, Person -> Person

The fan-outs (IU1's tags, friends, ...) are plan constants, not params, so a cell is comparable across formats
and runs. The anchor Person seeds the synthetic inserted vertex's vid (let "new_vid") and is an edge endpoint
(let "me_vid"); every other existing endpoint is a param, a TYPICAL target picked by genparams.
"""
from kvevaluator.plans.plan import Step, Plan
from kvevaluator.plans.ir.authoring import let, param, compose_vid
from kvevaluator.workloads.writes._shared import (PERSON_PARAM, PERSON_LET, PAIR_PARAM, PAIR_LET, synthetic_vid_let,
                                                   _SYNTHETIC_BASE, _FULL_PROPS)

# LDBC-typical fan-outs for the composite inserts. Named constants, not magic numbers: the whole
# cost of IU1 is "one vertex plus this many edges", so these ARE the knob.
IU1_TAGS, IU1_UNIS, IU1_COMPANIES, IU1_FRIENDS = 3, 1, 1, 5
IU4_TAGS = 3
IU6_TAGS = 3
IU7_TAGS = 2

_POST_PROPS = {"imageFile": None, "creationDate": 1577836800000, "locationIP": "10.0.0.1",
               "browserUsed": "Chrome", "language": "en", "content": "written by IU6",
               "length": 14}
_COMMENT_PROPS = {"creationDate": 1577836800000, "locationIP": "10.0.0.1",
                  "browserUsed": "Chrome", "content": "written by IU7", "length": 14}
_FORUM_PROPS = {"title": "IU4 forum", "creationDate": 1577836800000}

# The synthetic inserted-vertex vid, same convention as the write microbench (mod 2^40 + a
# per-label salt so IU1/IU4/IU6/IU7 never collide). `new_vid` is composed at bind time.
_MOD_BASE = 1 << 40


def _new_let(label, salt):
    return synthetic_vid_let(label, _SYNTHETIC_BASE, salt=salt, mod_base=_MOD_BASE)


def _edges(prefix, etype, src, dsts, src_label, dst_label, props=None):
    """One insert_edge step per destination. Written out as separate steps rather than folded into
    one bulk operator so the per-edge cost stays visible in the per-step kvstats."""
    return [Step(f"{prefix}{i}", "insert_edge", etype=etype, src=src, dst=d, props=props or {},
                 src_label=src_label, dst_label=dst_label) for i, d in enumerate(dsts)]


# Every existing endpoint other than the anchor is a param: a typical target from the data (genparams._typical),
# since an insert writes into its target's list and the target's size is part of the cost.
_FRIENDS = [f"friend{i}" for i in range(1, IU1_FRIENDS)]   # the params' real friends (me_vid is the 5th)


def _target(name, label):
    """(params, let) for one target param: `name` is its numeric LDBC id, `<name>_vid` the vertex."""
    return {name: {"type": "int"}}, {f"{name}_vid": compose_vid(label, param(name))}


def _targets(*pairs):
    params, lets = {}, {}
    for name, label in pairs:
        p, l = _target(name, label)
        params.update(p)
        lets.update(l)
    return params, lets


def _numbered(name, n):
    """The vids of a numbered target param list: name1_vid .. name<n>_vid."""
    return [let(f"{name}{i}_vid") for i in range(1, n + 1)]


def _numbered_targets(name, label, n):
    return [(f"{name}{i}", label) for i in range(1, n + 1)]


def iu1_add_person(args):
    p = let("new_vid")
    friends = [let("me_vid")] + [let(f"{f}_vid") for f in _FRIENDS]
    tp, tl = _targets(("city", "Place"), *_numbered_targets("interest", "Tag", IU1_TAGS),
                      *_numbered_targets("university", "Organisation", IU1_UNIS),
                      *_numbered_targets("company", "Organisation", IU1_COMPANIES),
                      *[(f, "Person") for f in _FRIENDS])
    return Plan(
        [Step("v", "insert_vertex", vid=p, label="Person", props=dict(_FULL_PROPS))]
        + _edges("loc", "isLocatedIn", p, [let("city_vid")], "Person", "Place")
        + _edges("tag", "hasInterest", p, _numbered("interest", IU1_TAGS), "Person", "Tag")
        + _edges("uni", "studyAt", p, _numbered("university", IU1_UNIS), "Person", "Organisation",
                 {"classYear": 2015})
        + _edges("co", "workAt", p, _numbered("company", IU1_COMPANIES), "Person", "Organisation",
                 {"workFrom": 2018})
        + _edges("kn", "knows", p, friends, "Person", "Person",
                 {"creationDate": 1577836800000}),
        params={**PERSON_PARAM, **tp}, let={**_new_let("Person", 0), **PERSON_LET, **tl})


def iu2_add_post_like(args):
    return Plan([Step("out", "insert_edge", etype="likes", src=let("me_vid"),
                      dst=let("post_vid"), props={"creationDate": 1577836800000},
                      src_label="Person", dst_label="Post")],
                params={**PERSON_PARAM, "post": {"type": "id"}},
                let={**PERSON_LET, "post_vid": compose_vid("Message", param("post"))})


def iu3_add_comment_like(args):
    return Plan([Step("out", "insert_edge", etype="likes", src=let("me_vid"),
                      dst=let("comment_vid"), props={"creationDate": 1577836800000},
                      src_label="Person", dst_label="Comment")],
                params={**PERSON_PARAM, "comment": {"type": "id"}},
                let={**PERSON_LET, "comment_vid": compose_vid("Message", param("comment"))})


def iu4_add_forum(args):
    f = let("new_vid")
    tp, tl = _targets(*_numbered_targets("tag", "Tag", IU4_TAGS))
    return Plan(
        [Step("v", "insert_vertex", vid=f, label="Forum", props=dict(_FORUM_PROPS))]
        + _edges("mod", "hasModerator", f, [let("me_vid")], "Forum", "Person")
        + _edges("tag", "hasTag", f, _numbered("tag", IU4_TAGS), "Forum", "Tag"),
        params={**PERSON_PARAM, **tp}, let={**_new_let("Forum", 1), **PERSON_LET, **tl})


def iu5_add_forum_member(args):
    tp, tl = _targets(("forum", "Forum"))
    return Plan([Step("out", "insert_edge", etype="hasMember", src=let("forum_vid"),
                      dst=let("me_vid"), props={"joinDate": 1577836800000},
                      src_label="Forum", dst_label="Person")],
                params={**PERSON_PARAM, **tp}, let={**PERSON_LET, **tl})


def iu6_add_post(args):
    m = let("new_vid")
    tp, tl = _targets(("forum", "Forum"), ("country", "Place"), *_numbered_targets("tag", "Tag", IU6_TAGS))
    return Plan(
        [Step("v", "insert_vertex", vid=m, label="Post", props=dict(_POST_PROPS))]
        + _edges("cr", "hasCreator", m, [let("me_vid")], "Post", "Person")
        + _edges("ct", "containerOf", let("forum_vid"), [m], "Forum", "Post")
        + _edges("loc", "isLocatedIn", m, [let("country_vid")], "Post", "Place")
        + _edges("tag", "hasTag", m, _numbered("tag", IU6_TAGS), "Post", "Tag"),
        params={**PERSON_PARAM, **tp}, let={**_new_let("Post", 2), **PERSON_LET, **tl})


def iu7_add_comment(args):
    c = let("new_vid")
    # replyOf targets a REAL post (the replyTo param; LDBC's IU7 carries one): the dated
    # adjacency ranks the target by its date, and a ghost target trips the missing-date
    # guard on both evaluators (as it should -- data-model violation).
    tp, tl = _targets(("country", "Place"), *_numbered_targets("tag", "Tag", IU7_TAGS))
    return Plan(
        [Step("v", "insert_vertex", vid=c, label="Comment", props=dict(_COMMENT_PROPS))]
        + _edges("cr", "hasCreator", c, [let("me_vid")], "Comment", "Person")
        + _edges("loc", "isLocatedIn", c, [let("country_vid")], "Comment", "Place")
        + _edges("rep", "replyOf", c, [let("reply_vid")], "Comment", "Post")
        + _edges("tag", "hasTag", c, _numbered("tag", IU7_TAGS), "Comment", "Tag"),
        params={**PERSON_PARAM, "replyTo": {"type": "id"}, **tp},
        let={**_new_let("Comment", 3), **PERSON_LET, **tl,
             "reply_vid": compose_vid("Message", param("replyTo"))})


def iu8_add_friendship(args):
    return Plan([Step("out", "insert_edge", etype="knows", src=let("src_vid"),
                      dst=let("dst_vid"), props={"creationDate": 1577836800000},
                      src_label="Person", dst_label="Person")], params=PAIR_PARAM, let=PAIR_LET)


REGISTRY = {
    "iu1": iu1_add_person, "iu2": iu2_add_post_like, "iu3": iu3_add_comment_like,
    "iu4": iu4_add_forum, "iu5": iu5_add_forum_member, "iu6": iu6_add_post,
    "iu7": iu7_add_comment, "iu8": iu8_add_friendship,
}
