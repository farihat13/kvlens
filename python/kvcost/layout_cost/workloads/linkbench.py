"""The LinkBench workloads' request types and texts (suite `linkbench`). The workloads (L01-L04) are files,
inputs/workloads/linkbench/<scale>/<id>.json, written by make_workloads.py from LinkBench's OWN request mix, taken from its
seeded request log (python/kvevaluator/datasets/linkbench/gen_linkbench_params.py -> inputs/frequencies/linkbench/<scale>/mix.json), plus variants of
it. Unlike W01-W15 (ldbc suite) these are not designed to make a layout win: they validate the layout-choice
method on a third-party read/write mix, and are a negative control for mixed layouts.

Each item is (family, query, how often, params): how often = the query's count in the logged requests (per the
log's total), params None = the mean over the query's sampled requests. `expect` was written BEFORE the LinkBench
grid was measured, from the per-layout cost rules; `why` is filled from the measured per-part costs: its numbers are computed per scale by why_numbers.py.
"""
LB, LW = "linkbench", "linkbench_writes"
READS = ["getnode", "getlinklist_link1", "getlinklist_link2", "countlink_link1", "countlink_link2",
         "getlink_link1", "getlink_link2"]
WRITES = ["addlink_link1", "addlink_link2", "updatelink_link1", "updatelink_link2", "deletelink_link1",
          "deletelink_link2", "addnode", "updatenode", "deletenode"]

QUERIES = {
    "getnode":           "getNode: all fields of one node",
    "getlinklist_link1": "getLinkList: the 10,000 newest link1 links of a node, with their data",
    "getlinklist_link2": "getLinkList: the 10,000 newest link2 links of a node, with their data",
    "countlink_link1":   "countLink: the number of link1 links of a node (the list is read and counted)",
    "countlink_link2":   "countLink: the number of link2 links of a node",
    "getlink_link1":     "getLink: given link1 links of a node, with their data (multi-get)",
    "getlink_link2":     "getLink: given link2 links of a node, with their data",
    "addlink_link1":     "addLink / updateLink upsert of a NEW link1 link",
    "addlink_link2":     "addLink / updateLink upsert of a NEW link2 link",
    "updatelink_link1":  "addLink / updateLink upsert of an EXISTING link1 link: rewrite its fields",
    "updatelink_link2":  "addLink / updateLink upsert of an EXISTING link2 link",
    "deletelink_link1":  "deleteLink: hide a link1 link (visibility = 0)",
    "deletelink_link2":  "deleteLink: hide a link2 link",
    "addnode":           "addNode: a new node",
    "updatenode":        "updateNode: rewrite a node's version, time and data (one row write)",
    "deletenode":        "deleteNode: remove a node's record (its links stay)",
}
PARAMS = {
    LB: "a sample of LinkBench's own seeded request stream: its access distributions pick the nodes",
    LW: "the same request stream: each write's node, link and new field values as LinkBench generated them",
}
