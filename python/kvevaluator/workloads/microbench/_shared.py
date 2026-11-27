"""Microbench constants shared across the family modules."""

from kvevaluator.plans.plan import Step, ref
from kvevaluator.plans.ir.authoring import param, let, compose_vid, add, mul, lit


DAY = 86400000


# ~2011-01-01 UTC: mid-range for LDBC creationDates (data spans ~2010-2012), so
# the 1-hop filter shapes keep a non-trivial pass/drop split at every anchor.
DATE_CUTOFF = 1293840000000


PERSON_PROPS = ["firstName", "lastName", "gender", "birthday",
                "creationDate", "locationIP", "browserUsed"]

# A Post's scalar props, in schema order. `content` is the fat column (null on image posts).
POST_PROPS = ["imageFile", "creationDate", "locationIP", "browserUsed", "language", "content", "length"]

# Post and Comment share one message id space; a read over a person's messages names both labels.
MESSAGE_LABELS = ("Post", "Comment")


# --- Parameterization pieces (this workload's own; the compose_vid FUNCTION is infra, but the
# param/let constants are per-workload -- never imported from ldbc, so the workloads stay
# independent). A builder wraps its steps in Plan(steps, params=..., let=...) and reads the
# anchor as let("anchor_vid"); see the ldbc queries for the pattern. `anchor_vid` is the one
# convention so the generic _hop1 (Person/Forum/Tag) needs no per-label name. ---
def anchor_param(key):
    return {key: {"type": "id"}}


def anchor_let(label, key):
    return {"anchor_vid": compose_vid(label, param(key))}


PERSON_PARAM = anchor_param("person")
PERSON_LET = anchor_let("Person", "person")
MESSAGE_PARAM = anchor_param("message")
MESSAGE_LET = anchor_let("Message", "message")
TAGCLASS_PARAM = anchor_param("tagclass")
TAGCLASS_LET = anchor_let("TagClass", "tagclass")

# entry_range's window: [startDate, startDate + days days). `end` is a derived let.
DATE_WINDOW_PARAM = {"startDate": {"type": "int"}, "days": {"type": "int"}}
DATE_WINDOW_LET = {"end": add(param("startDate"), mul(param("days"), lit(DAY)))}


# --- Engine-plan pieces ---
def dgraph_engine_anchor(label):
    """Dgraph's anchor as the live queries write it: `eq(id, $x) @filter(type(<label>))`. Dgraph
    has no external-key lookup, so it finds the vertex through the `id` index (1 SEEK), and `id`
    is shared by every LDBC type, so `type()` picks the right one (1 SEEK on the dgraph.type
    index). The optimal plan addresses the vertex directly. Output step: `me`."""
    return [Step("anchor", "read_vertex_by_id", vid=let("anchor_vid")),
            Step("atype", "read_vertices_by_label", label=label),
            Step("me", "intersect", items=ref("anchor"), other=ref("atype"))]
