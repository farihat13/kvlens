"""The example queries a reader adds (README "Add your own workload"): a person's friends with their names, and a
person's first name. Both take IC1's person ids (params_like), so they need no param file of their own."""
from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.interactive._shared import PERSON_PARAM, PERSON_LET


def example_friends(args):
    """A person's friends, each with id and name, sorted by id."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("me_vid"), label="Person"),
        Step("nb",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both"),
        Step("d",   "dedup", items=ref("nb")),
        Step("pr",  "read_vertex_props", vids=ref("d"), props=["id", "firstName", "lastName"], label="Person"),
        Step("srt", "top_k", rows=ref("pr"), keys=[("id", "asc")], k=None),
        Step("out", "project", rows=ref("srt"), fields=["id", "firstName", "lastName"]),
    ], params=PERSON_PARAM, let=PERSON_LET,
        question="Given a person, list their friends with each friend's id and name, sorted by id.", params_like="ic1")


def example_first_name(args):
    """A person's first name."""
    return Plan([
        Step("a",   "read_vertex_by_id", vid=let("me_vid"), label="Person"),
        Step("pr",  "read_vertex_props", vids=ref("a"), props=["firstName"], label="Person"),
        Step("out", "project", rows=ref("pr"), fields=["firstName"]),
    ], params=PERSON_PARAM, let=PERSON_LET, question="Given a person, read their first name.", params_like="ic1")


LOGICAL = {"example_friends": example_friends, "example_first_name": example_first_name}
