"""Write PRIMITIVES as plans (mb2 MB81-88 lineage): one write op each -- what a SINGLE write
costs. The write analog of the microbench read primitives. The LDBC insert OPERATIONS
(writes/ldbc) compose these, as the Interactive queries compose the microbench read shapes.
query = plan.

Each is a SINGLE step: a write primitive is the unit being priced, so composing it with anything
else would fold another operator's cost into the number.

Synthetic ids. Every insert makes up an id from the param row deterministically and far outside
the LDBC id space, so a cell is repeatable and two cells never collide (_shared.synthetic_vid_let).
Repeatability is not automatic here the way it is for reads -- a write changes what the next run
would see -- which is also why write cells run against a WriteShadowKV overlay (kvstore/shadow.py).
"""
from kvevaluator.plans.plan import Step, Plan
from kvevaluator.plans.ir.authoring import let
from kvevaluator.workloads.writes._shared import (PERSON_PARAM, PERSON_LET, PAIR_PARAM, PAIR_LET, synthetic_vid_let,
                                                   _SYNTHETIC_BASE, _FULL_PROPS)

# The property sets MB83/MB84 contrast: a minimal Person carries only non-indexed scalars, a full
# one (_shared._FULL_PROPS) carries every column including the indexed firstName and the
# day-indexed creationDate. The gap between them is what "cost per property" means for a layout.
_MIN_PROPS = {"lastName": "WMin", "gender": "other", "locationIP": "10.0.0.1"}


# ---------------------------------------------------------------- property updates

def w_upd_plain(args):
    """MB81: write one NON-indexed property. The cleanest read of the layout's update cost --
    nothing but the property itself moves. A predicate store writes it blind (no read-back); a
    row or document store must read the packed record first and rewrite it whole."""
    return Plan([Step("out", "update_prop", vid=let("me_vid"), label="Person",
                      prop="lastName", value="WUpd")], params=PERSON_PARAM, let=PERSON_LET)


def w_upd_indexed(args):
    """MB82: write one INDEXED property. w_upd_indexed - w_upd_plain isolates index maintenance:
    the touched column's old entry out, its new entry in. (mb2 measured 82 == 81 on the real nebula
    AND arango, because both engines rewrite every secondary index on any update -- an engine
    choice the *_engine formats model, not a cost of the layout.)"""
    return Plan([Step("out", "update_prop", vid=let("me_vid"), label="Person",
                      prop="firstName", value="WUpd")], params=PERSON_PARAM, let=PERSON_LET)


def w_upd_dated(args):
    """Write one DATE-indexed property (creationDate). The kv-amp write-inversion cell:
    dgraph's day-token postings hold many co-tenants, so moving one uid between two
    buckets rewrites them all (write amp ~50x at sf0.1) -- while the row/doc stores pay
    only their flat RMW tax. The order of formats FLIPS vs w_upd_indexed."""
    return Plan([Step("out", "update_prop", vid=let("me_vid"), label="Person",
                      prop="creationDate", value=1300000000000)], params=PERSON_PARAM, let=PERSON_LET)


def w_upd_multi(args):
    """Write THREE columns of one vertex in ONE update: an indexed one (firstName) and two plain
    ones. Against three w_upd_plain-style writes, it shows what a layout saves by writing them
    together: a row or document store reads and rewrites the record once (and re-maintains its
    index set once), while a predicate store still writes one record per column."""
    return Plan([Step("out", "update_prop", vid=let("me_vid"), label="Person",
                      props={"firstName": "WMulti", "lastName": "WMulti", "browserUsed": "Firefox"})],
                params=PERSON_PARAM, let=PERSON_LET)


def w_upd_edge(args):
    """Rewrite the creationDate of one EXISTING knows edge (a pair the param file records as
    adjacent). Every stored copy of the edge changes: the forward record and the reverse one. The
    layouts differ in how they reach the edge's props: an edge record is read back and rewritten, a
    posting list is rewritten whole, a chunked layout rewrites one edge-prop chunk, and a document
    store first scans its edge index to find the edge document."""
    return Plan([Step("out", "update_edge_prop", etype="knows", src=let("src_vid"),
                      dst=let("dst_vid"), props={"creationDate": 1300000000000},
                      src_label="Person", dst_label="Person")], params=PAIR_PARAM, let=PAIR_LET)


# ---------------------------------------------------------------- vertex inserts

def w_ins_v_min(args):
    return Plan([Step("out", "insert_vertex", vid=let("new_vid"), label="Person",
                      props=dict(_MIN_PROPS))],
                params=PERSON_PARAM, let=synthetic_vid_let("Person", _SYNTHETIC_BASE))


def w_ins_v_full(args):
    return Plan([Step("out", "insert_vertex", vid=let("new_vid"), label="Person",
                      props=dict(_FULL_PROPS))],
                params=PERSON_PARAM, let=synthetic_vid_let("Person", _SYNTHETIC_BASE))


# ---------------------------------------------------------------- edge inserts

def w_ins_e_plain(args):
    """MB85: insert a knows edge carrying no properties, between two persons the param file
    records as NOT already adjacent -- so this is a true insert, not an overwrite."""
    return Plan([Step("out", "insert_edge", etype="knows", src=let("src_vid"),
                      dst=let("dst_vid"), props={},
                      src_label="Person", dst_label="Person")], params=PAIR_PARAM, let=PAIR_LET)


def w_ins_e_facet(args):
    """MB86: the same edge carrying its creationDate. Minus w_ins_e_plain, that is the price of an
    edge property -- which the layouts answer very differently: it rides inside the posting entry
    on dgraph and inside the edge value on nebula (no extra key either way), while arango stores it
    in the edge document it was already writing."""
    return Plan([Step("out", "insert_edge", etype="knows", src=let("src_vid"),
                      dst=let("dst_vid"), props={"creationDate": 1577836800000},
                      src_label="Person", dst_label="Person")], params=PAIR_PARAM, let=PAIR_LET)


# ---------------------------------------------------------------- deletes

def w_del_v_record(args):
    """MB87 as the engines actually run it: remove the vertex record and its index entries, leave
    the edges. Comparable to mb2's row; NOT comparable across formats as a graph operation, since
    it leaves each layout in a different state."""
    return Plan([Step("out", "delete_vertex", vid=let("me_vid"), label="Person",
                      with_edges=False)], params=PERSON_PARAM, let=PERSON_LET)


def w_del_v(args):
    """The fair delete: the vertex AND every incident edge, so all three formats end in the same
    state and the numbers answer the same question. This is where connectivity enters -- half of
    every incident edge is stored at the NEIGHBOUR, and reaching it costs whatever that layout
    charges to reach a neighbour."""
    return Plan([Step("out", "delete_vertex", vid=let("me_vid"), label="Person",
                      with_edges=True)], params=PERSON_PARAM, let=PERSON_LET)


def w_del_e(args):
    """MB88: remove one knows edge, between a pair the param file records as adjacent."""
    return Plan([Step("out", "delete_edge", etype="knows", src=let("src_vid"),
                      dst=let("dst_vid"))], params=PAIR_PARAM, let=PAIR_LET)


REGISTRY = {
    "w_upd_plain": w_upd_plain, "w_upd_indexed": w_upd_indexed, "w_upd_dated": w_upd_dated,
    "w_upd_multi": w_upd_multi, "w_upd_edge": w_upd_edge,
    "w_ins_v_min": w_ins_v_min, "w_ins_v_full": w_ins_v_full,
    "w_ins_e_plain": w_ins_e_plain, "w_ins_e_facet": w_ins_e_facet,
    "w_del_v_record": w_del_v_record, "w_del_v": w_del_v, "w_del_e": w_del_e,
}
