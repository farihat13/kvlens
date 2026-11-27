"""LinkBench write operations -- one plan per LinkBench write op (and link type), with the written values taken
from LinkBench's own logged request (python/kvevaluator/datasets/linkbench/gen_linkbench_params.py), so payload sizes follow LinkBench's generators.
Every write is measured against the LOADED graph (the store never changes between measurements).

  addlink_<t>     -- LinkBench addLink/updateLink upsert of a NEW link: insert_edge with its fields.
  updatelink_<t>  -- the same upsert of an EXISTING link: rewrite its fields (update_edge_prop).
  deletelink_<t>  -- LinkBench deleteLink with expunge=false HIDES the link: visibility = 0 (VISIBILITY_HIDDEN).
                     A missing link is still looked up and writes nothing.
  addnode         -- a new node (its id: LinkBench's next id, past every loaded node).
  updatenode      -- LinkBench's updateNode rewrites version, time and data in ONE row write.
  deletenode      -- LinkBench's deleteNode removes the node record only; its links stay.
"""
from kvevaluator.plans.plan import Step, Plan
from kvevaluator.plans.ir.authoring import let, param, compose_vid

LINK_ETYPES = ("link1", "link2")
LINK_FIELDS = {"visibility": "int", "version": "int", "time": "int", "data": "bytes"}
NODE_FIELDS = {"ntype": "int", "version": "int", "time": "int", "data": "bytes"}
HIDDEN = 0                     # LinkStore.VISIBILITY_HIDDEN

_PAIR_PARAM = {"src": {"type": "id"}, "dst": {"type": "id"}}
_PAIR_LET = {"s_vid": compose_vid("Node", param("src")), "d_vid": compose_vid("Node", param("dst"))}
_NODE_PARAM = {"node": {"type": "id"}}
_NODE_LET = {"n_vid": compose_vid("Node", param("node"))}


def _typed(fields):
    return {name: {"type": t} for name, t in fields.items()}


def _addlink(etype):
    def build(args):
        return Plan([Step("out", "insert_edge", etype=etype, src=let("s_vid"), dst=let("d_vid"),
                          props={f: param(f) for f in LINK_FIELDS}, src_label="Node", dst_label="Node")],
                    params={**_PAIR_PARAM, **_typed(LINK_FIELDS)}, let=_PAIR_LET)
    return build


def _updatelink(etype):
    def build(args):
        return Plan([Step("out", "update_edge_prop", etype=etype, src=let("s_vid"), dst=let("d_vid"),
                          props={f: param(f) for f in LINK_FIELDS}, src_label="Node", dst_label="Node")],
                    params={**_PAIR_PARAM, **_typed(LINK_FIELDS)}, let=_PAIR_LET)
    return build


def _deletelink(etype):
    def build(args):
        return Plan([Step("out", "update_edge_prop", etype=etype, src=let("s_vid"), dst=let("d_vid"),
                          props={"visibility": HIDDEN}, src_label="Node", dst_label="Node")],
                    params=_PAIR_PARAM, let=_PAIR_LET)
    return build


def addnode(args):
    return Plan([Step("out", "insert_vertex", vid=let("n_vid"), label="Node",
                      props={f: param(f) for f in NODE_FIELDS})],
                params={**_NODE_PARAM, **_typed(NODE_FIELDS)}, let=_NODE_LET)


def updatenode(args):
    fields = ("version", "time", "data")
    return Plan([Step("out", "update_prop", vid=let("n_vid"), label="Node", props={f: param(f) for f in fields})],
                params={**_NODE_PARAM, **_typed({f: NODE_FIELDS[f] for f in fields})}, let=_NODE_LET)


def deletenode(args):
    return Plan([Step("out", "delete_vertex", vid=let("n_vid"), label="Node", with_edges=False)],
                params=_NODE_PARAM, let=_NODE_LET)


LOGICAL = {**{f"addlink_{t}": _addlink(t) for t in LINK_ETYPES},
           **{f"updatelink_{t}": _updatelink(t) for t in LINK_ETYPES},
           **{f"deletelink_{t}": _deletelink(t) for t in LINK_ETYPES},
           "addnode": addnode, "updatenode": updatenode, "deletenode": deletenode}
