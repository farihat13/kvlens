"""Basic traversal plans: knows / fof / friends."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import PERSON_PARAM, PERSON_LET


# ---------------------------------------------------------------- demo queries

def knows(args):
    """Who does `person` know? -> their names."""
    p = let("me_vid")
    return Plan([
        Step("a",       "read_vertex_by_id", vid=p, label="Person"),
        Step("friends", "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("names",   "read_vertex_props", vids=ref("friends"), props=["name"], label="Person"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def fof(args):
    """Friends-of-friends of `person` (2 hops) -> names."""
    p = let("me_vid")
    return Plan([
        Step("a",     "read_vertex_by_id", vid=p, label="Person"),
        Step("f1",    "read_neighbors", srcs=ref("a"),  etype="knows", dir="both"),
        Step("f2",    "read_neighbors", srcs=ref("f1"), etype="knows", dir="both"),
        Step("names", "read_vertex_props", vids=ref("f2"), props=["name"], label="Person"),
    ], params=PERSON_PARAM, let=PERSON_LET)


def friends(args):
    """A person's friends (knows, undirected) -> their names."""
    p = let("me_vid")
    return Plan([
        Step("a",       "read_vertex_by_id", vid=p, label="Person"),
        Step("friends", "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("names",   "read_vertex_props", vids=ref("friends"),
             props=["firstName", "lastName"], label="Person"),
    ], params=PERSON_PARAM, let=PERSON_LET)


SIM = {
    "knows": knows,
    "fof": fof,
    "friends": friends
}


# what predict's cardinality rules may assume for these queries (plan file field estimate_hints, read by
# predict/cardinality.cc): a hop over knows from a set of persons reached through knows prices each person's degree as
# a neighbour's (sum d^2 / sum d), except the anchor's own first hop
_KNOWS_HINTS = {"size_biased": ["knows"]}
ESTIMATE_HINTS = {"fof": _KNOWS_HINTS, "friends": _KNOWS_HINTS, "knows": _KNOWS_HINTS}
