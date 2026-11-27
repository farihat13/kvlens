"""LinkBench wide-read queries -- one shared logical shape per query; every format prices it (the
format comparison). Parameterized on one node-id anchor.

  getnode          -- the whole record of a node (getNode). 1 GET/row (nebula) vs 1 GET/field (dgraph);
                      run over the node sample, this is the getNode multiget.
  composed_getnode -- a node's link1 neighbors' FULL records (getLinkList-then-getNode): the wide
                      node-read over the neighbor set vs the edge-walk fan-out.
  getlinklist_<t>  -- the 10,000 NEWEST links of one type of a node, with their data: LinkBench's
                      getLinkList(id1, link_type) (LinkStore.DEFAULT_LIMIT = 10000, newest first). No measured
                      layout keeps lists time-sorted, so the whole list is read and ranked -- the cheapest plan
                      those layouts allow; LinkBench's history paging (an older window) costs the same.
  countlink_<t>    -- the number of links of one type (LinkBench keeps a count table; we read the list).
  getlink_<t>      -- given links id1 -> id2s of one type, with their data (LinkBench's multiget). Modelled as
                      id1's list, filtered to the id2s: exact for Condensed and Chunked; a Split layout could
                      point-read each link instead (a directed read_edges op, not built -- getlink is 0.5% of
                      the mix). The id2s only filter: all of them are carried as one list param (`id2s`), turned
                      into vids by a `map` let; the list's length moves the answer, not the KV ops.
Requests pick the link type uniformly (half each).
Each LinkBench link_type is its own edge type (link1 / link2, see linkbench.schema).
"""
from kvevaluator.plans.plan import Step, ref, Plan
from kvevaluator.plans.ir.authoring import let, param, compose_vid, field, in_, map_, item

NODE_PARAM = {"node": {"type": "id"}}
NODE_LET = {"n_vid": compose_vid("Node", param("node"))}
NODE_PROPS = ["ntype", "version", "time", "data"]
LINK_PROPS = ["visibility", "version", "time", "data"]
_NODE_OUT = ["_vid"] + NODE_PROPS


def getnode(args):
    return Plan([
        Step("nv",  "read_vertex_props", vids=[let("n_vid")], props=NODE_PROPS, label="Node"),
        Step("out", "project", rows=ref("nv"), fields=_NODE_OUT),
    ], params=NODE_PARAM, let=NODE_LET)


def composed_getnode(args):
    return Plan([
        Step("ll",  "read_neighbors", srcs=[let("n_vid")], etype="link1", dir="out"),
        Step("nb",  "dedup", items=ref("ll")),
        Step("nv",  "read_vertex_props", vids=ref("nb"), props=NODE_PROPS, label="Node"),
        Step("srt", "top_k", rows=ref("nv"), keys=[("_vid", "asc")], k=None),  # deterministic order
        Step("out", "project", rows=ref("srt"), fields=_NODE_OUT),
    ], params=NODE_PARAM, let=NODE_LET)


RANGE_LIMIT = 10000     # LinkStore.DEFAULT_LIMIT: a getLinkList returns at most this many links, newest first


def _getlinklist(etype):
    def build(args):
        return Plan([
            Step("ll",  "read_neighbors", srcs=[let("n_vid")], etype=etype, dir="out",
                 edge_props=LINK_PROPS, pairs=True),
            Step("srt", "top_k", rows=ref("ll"), keys=[("time", "desc"), ("dst", "asc")], k=RANGE_LIMIT),
            Step("out", "project", rows=ref("srt"), fields=["src", "dst"] + LINK_PROPS),
        ], params=NODE_PARAM, let=NODE_LET)
    return build


def _countlink(etype):
    def build(args):
        return Plan([
            Step("ll",  "read_neighbors", srcs=[let("n_vid")], etype=etype, dir="out"),
            Step("n",   "count", items=ref("ll")),
            Step("out", "as_row", value=ref("n"), field="count"),
        ], params=NODE_PARAM, let=NODE_LET)
    return build


GETLINK_PARAM = {**NODE_PARAM, "id2s": {"type": "list"}}      # every id the request lists (1 to 17 at n1m)
GETLINK_LET = {**NODE_LET, "ids_vid": map_(param("id2s"), compose_vid("Node", item()))}


def _getlink(etype):
    def build(args):
        return Plan([
            Step("ll",  "read_neighbors", srcs=[let("n_vid")], etype=etype, dir="out",
                 edge_props=LINK_PROPS, pairs=True),
            Step("hit", "filter", rows=ref("ll"), pred=in_(field("dst"), let("ids_vid"))),
            Step("srt", "top_k", rows=ref("hit"), keys=[("dst", "asc")], k=None),
            Step("out", "project", rows=ref("srt"), fields=["src", "dst"] + LINK_PROPS),
        ], params=GETLINK_PARAM, let=GETLINK_LET)
    return build


LINK_ETYPES = ("link1", "link2")
LOGICAL = {"getnode": getnode, "composed_getnode": composed_getnode,
           **{f"getlinklist_{t}": _getlinklist(t) for t in LINK_ETYPES},
           **{f"countlink_{t}": _countlink(t) for t in LINK_ETYPES},
           **{f"getlink_{t}": _getlink(t) for t in LINK_ETYPES}}
