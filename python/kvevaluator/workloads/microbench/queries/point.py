"""Microbench point access-path shapes."""

from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.microbench._shared import (PERSON_PROPS, PERSON_PARAM, PERSON_LET, POST_PROPS,
                                                       MESSAGE_PARAM, MESSAGE_LET)


# ---------------------------------------------------------------- point reads

def _point(args, nprops):
    """NS1 'get n properties of a vertex', anchor-free: the vid is known, the
    only cost is the prop read. dgraph pays 1 GET per prop (columnar); nebula 1
    GET total (whole row); arango 2 GETs (pk + doc)."""
    p = let("anchor_vid")
    props = PERSON_PROPS[:nprops]
    return Plan([
        Step("pr",  "read_vertex_props", vids=[p], props=props, label="Person"),
        Step("out", "project", rows=ref("pr"), fields=props),
    ], params=PERSON_PARAM, let=PERSON_LET)


def point1(args):
    return _point(args, 1)


def point4(args):
    return _point(args, 4)


def point7(args):
    return _point(args, 7)


def point_post(args):
    """NS1 on a Post: all props of one known post. The post's row is dominated by `content`,
    so the param family strata are posts by content length (post_lengths). dgraph pays 1 GET per
    prop; a row store 1 GET for the whole row, content included."""
    p = let("anchor_vid")
    return Plan([
        Step("pr",  "read_vertex_props", vids=[p], props=POST_PROPS, label="Post"),
        Step("out", "project", rows=ref("pr"), fields=POST_PROPS),
    ], params=MESSAGE_PARAM, let=MESSAGE_LET)


# ================================================================ engine plans
# Transcribed from the engines' captured sf0.3 plans (microbench/<db>/<shape>/p<train>/plan.txt), at the training
# param only (blind holdout: live_workloads.train_param).
# One section per engine; each cites the plan node behind every KV step.


# ------------------------------------------------------------ dgraph engine plans

from kvevaluator.workloads.microbench._shared import dgraph_engine_anchor


def point1_dgraph_engine(args):
    """From dgraph/point1/p3/plan.txt. Block 0: the id anchor (`eq(id)` + `type(person)`), then
    `firstName` [vals=1]: 1 GET."""
    return Plan(dgraph_engine_anchor("Person") + [                    # block 0: eq(id) + type(person)
        Step("pr",  "read_vertex_props", vids=ref("me"), props=["firstName"]),   # block 0: firstName
        Step("out", "project", rows=ref("pr"), fields=["firstName"]),
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_DGRAPH = {"point1": point1_dgraph_engine}
ENGINE_DRAFT_DGRAPH = {}


# ------------------------------------------------------------ nebula engine plans

def point1_nebula_engine(args):
    """From nebula/point1/p3/plan.txt. Node 4 GetVertices: tagId 2 (Person), props [firstName],
    1 GET of the tag row. Node 3 Project is in memory; the YIELD that feeds the vid reads nothing."""
    return Plan([
        Step("pr",  "read_vertex_props", vids=[let("anchor_vid")], props=["firstName"],
             label="Person"),                                          # node 4 GetVertices
        Step("out", "project", rows=ref("pr"), fields=["firstName"]),  # node 3 Project
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_NEBULA = {"point1": point1_nebula_engine}
ENGINE_DRAFT_NEBULA = {}


# ------------------------------------------------------------ arangodb engine plans

def point1_arangodb_engine(args):
    """From arangodb/point1/p3/plan.txt. Node [2] CalculationNode `DOCUMENT("Person/<id>")`:
    a pk GET + the document GET (docs_per_step, as the LDBC plans transcribe DOCUMENT).
    Node [3] builds {firstName} in memory. No traversal, so no traversal_open."""
    return Plan([
        Step("pr",  "docs_per_step", vids=[let("anchor_vid")], label="Person",
             props=["firstName"]),                                     # node 2: DOCUMENT
        Step("out", "project", rows=ref("pr"), fields=["firstName"]),  # node 3: in memory
    ], params=PERSON_PARAM, let=PERSON_LET)


ENGINE_ARANGODB = {"point1": point1_arangodb_engine}
ENGINE_DRAFT_ARANGODB = {}
