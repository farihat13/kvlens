"""An in-memory model of the LinkBench graph under an ordered request file: the loaded graph (only the nodes and link
lists the file touches), then every request in order, with the semantics of OUR plans (queries/wide.py, writes.py and
the meta format's write operators). A read's answer comes back in canonical form (canonical_rows.py), so it compares
by sha256 with our responses.

  addlink_<t>     insert the link with its fields (the file runs an add of an existing pair as updatelink:
                  params.sequence_meaning; an add of an existing pair here is a model error, raised)
  updatelink_<t>  set every field of an existing link; a missing link: nothing
  deletelink_<t>  hide: visibility 0 on an existing link, which stays listed and counted; a missing link: nothing
  addnode         the node with its four fields
  updatenode      version, time and data set; a node that does not exist gets just those three (every meta layout
                  writes the updated columns without the row: an upsert, unlike LinkBench's updateNode)
  deletenode      the node goes; its links stay
  getnode         [the node's row] or [] (no row for a missing node); a field the node lacks is None
  getlinklist_<t> the type's links of the node, time desc then dst asc, at most 10,000 (hidden ones included)
  countlink_<t>   [{count}] of that list (hidden ones included)
  getlink_<t>     the links to the request's id2s that exist, dst asc
"""
import base64
import json
import os

from kvevaluator.workloads.linkbench.canonical_rows import canonical_rows
from kvevaluator.datasets.linkbench.loader import LINK_TYPES

RANGE_LIMIT = 10000
_TYPE_OF = {v: k for k, v in LINK_TYPES.items()}


def _blob(b64):
    return base64.b64decode(b64)


class LogModel:
    def __init__(self):
        self.nodes = {}      # id -> {ntype, version, time, data} (a revived node has only the updated three)
        self.links = {}      # (etype, src) -> {dst: {visibility, version, time, data}}

    @staticmethod
    def touched(requests):
        """(node ids whose record a request reads or writes, (etype, src) lists a request reads or writes)"""
        nodes, lists = set(), set()
        for r in requests:
            q, a = r["ran_as"], r["args"]
            if q in ("getnode", "addnode", "updatenode", "deletenode"):
                nodes.add(a["node"])
            elif q.startswith(("getlinklist_", "countlink_", "getlink_")):
                lists.add((q.rsplit("_", 1)[1], a["node"]))
            else:
                lists.add((q.rsplit("_", 1)[1], a["src"]))
        return nodes, lists

    def load(self, data_dir, requests):
        """the loaded graph's part the requests touch, from nodes.jsonl / links.jsonl"""
        nodes, lists = self.touched(requests)
        with open(os.path.join(data_dir, "nodes.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                if r["id"] in nodes:
                    self.nodes[r["id"]] = {"ntype": r["type"], "version": r["version"], "time": r["time"],
                                           "data": _blob(r["data"])}
        for key in lists:
            self.links[key] = {}
        with open(os.path.join(data_dir, "links.jsonl")) as f:
            for line in f:
                r = json.loads(line)
                key = (LINK_TYPES[r["link_type"]], r["id1"])
                if key in self.links:
                    self.links[key][r["id2"]] = {"visibility": r["visibility"], "version": r["version"],
                                                 "time": r["time"], "data": _blob(r["data"])}
        return self

    def apply(self, q, a):
        """one write request (its ran_as and args)"""
        if q.startswith("addlink_"):
            lst = self.links.setdefault((q.rsplit("_", 1)[1], a["src"]), {})
            if a["dst"] in lst:
                raise ValueError(f"model: addlink of an existing link {q} {a['src']}->{a['dst']} (the file should run "
                                 f"it as updatelink)")
            lst[a["dst"]] = {f: a[f] for f in ("visibility", "version", "time", "data")}
        elif q.startswith("updatelink_"):
            link = self.links.setdefault((q.rsplit("_", 1)[1], a["src"]), {}).get(a["dst"])
            if link is not None:
                link.update({f: a[f] for f in ("visibility", "version", "time", "data")})
        elif q.startswith("deletelink_"):
            link = self.links.setdefault((q.rsplit("_", 1)[1], a["src"]), {}).get(a["dst"])
            if link is not None:
                link["visibility"] = 0
        elif q == "addnode":
            self.nodes[a["node"]] = {"ntype": a["ntype"], "version": a["version"], "time": a["time"], "data": a["data"]}
        elif q == "updatenode":
            self.nodes.setdefault(a["node"], {}).update({f: a[f] for f in ("version", "time", "data")})
        elif q == "deletenode":
            self.nodes.pop(a["node"], None)
        else:
            raise ValueError(f"model: {q} is not a LinkBench write")

    def read(self, q, a):
        """one read request -> its answer rows (dicts with raw int ids, as canonical_rows takes them)"""
        if q == "getnode":
            n = self.nodes.get(a["node"])
            if n is None:
                return []
            return [{"_vid": a["node"], **{f: n.get(f) for f in ("ntype", "version", "time", "data")}}]
        t, src = q.rsplit("_", 1)[1], a["node"]
        lst = self.links.get((t, src), {})
        if q.startswith("countlink_"):
            return [{"count": len(lst)}]
        if q.startswith("getlinklist_"):
            picked = sorted(lst.items(), key=lambda kv: (-kv[1]["time"], kv[0]))[:RANGE_LIMIT]
        elif q.startswith("getlink_"):
            if "id2s" not in a:
                raise ValueError(f"model: {q} args {sorted(a)} have no id2s: a request log made before the getlink list "
                                 "param; regenerate the params (python/kvevaluator/datasets/linkbench/gen_linkbench_params.py)")
            picked = sorted((d, lst[d]) for d in set(a["id2s"]) if d in lst)
        else:
            raise ValueError(f"model: {q} is not a LinkBench read")
        return [{"src": src, "dst": d, **p} for d, p in picked]

    def replay(self, requests, is_write):
        """every request in order: {seq: canonical answer rows} for the reads"""
        answers = {}
        for r in requests:
            if is_write(r["ran_as"]):
                self.apply(r["ran_as"], r["args"])
            else:
                answers[r["seq"]] = canonical_rows(self.read(r["ran_as"], r["args"]))
        return answers
