#!/usr/bin/env python3
"""A second opinion on Per-type CSR's chunk-probe counts under the label rule: every simulated step's S / S_miss /
scan bytes, computed here from the store's chunk keys and the record stream, must equal what the binary measured.

The C++-against-Python differential compares rows and order on Per-type CSR, never counts, and the configurable
format's corner reads other chunks (its runs are per vid group). So this is the count check for that format with
label tags on. It shares no code with the format: Python, the chunk keys from `kvevaluator dump`, the labels and
neighbours from the record stream (Truth, below), its own replay of the chunk cache.

Per (plan, param) the binary under test runs once with `trace_args`: each KV step's resolved arguments, the label
each argument value carries on its row (`arg_tags`), and its measured counts. Three checks:
  1. COUNTS. From a step's inputs and the labels they carry, the rule gives the families probed: a carried label
     opens ONE family (none when the schema says that label holds no such list, or the plan does not ask for it); a
     vid with no label tries the schema's owners of (edge type, direction) in schema order, a props read the plan's
     labels in order. The replay charges them as exec/replay.cc does (free inside a chunk already read by the call,
     else one SEEK + the landed key; the chunk's value once per call). Simulated == measured, per lane, or FAIL.
  2. THE LABELS ARE TRUE. Every label an input value carries equals that vertex's label in the record stream.
  3. THE SOURCES TAG. Where an input comes straight from a KV step (through `dedup` / `pluck` only), the rule says
     which of its values must carry a label (a per-label index or scan: all; a label filter's output: all; a hop's
     far end: all when the schema gives that direction one label, none otherwise unless --stored; a pairs row's src:
     all) and the trace must agree. Inputs that pass other flow steps are not predicted here (their labels are
     still checked by 2); the count of values checked is printed.
A hop that reads edge props adds one lookup per found edge in the edge-prop chunks of the edge's source label, at
the edge's ordinal (its rank among that label's sorted out-edges of the type, as the ingest numbers them).
Not simulated (counted per op in the verdict line, never silently skipped): read_edge_exists, the index reads,
by-id reads, writes.
How labels pass through flow steps (dedup, join, concat, ...) is NOT re-derived: each step is replayed from the
labels its inputs carry in the trace. Check 2 says those labels are true, check 3 covers the direct KV sources.
What it does not see: a flow step that DROPS a true label. That only raises counts (the binary and this replay then
probe the same owners), and the rows are covered by the C++-against-Python gate.

  python3 tests/gates/check_probe_counts.py --sf 0.1 --store <pertype_csr store dir> [--nparam 5]
        [--families interactive,microbench,bi] [--queries a,b] [--stored] --out <dir>
--stored: the store keeps the far end's label in its adjacency entries (after the stored-label step).
Controls (the check must FAIL under each; run them after any change to this file):
  --wrong-rule ignore-carried | owners-reversed    this replay applies a wrong rule -> counts differ
  --allow-tag-setting with KVEVAL_LABEL_TAGS=hops=off    the binary drops the hop tags -> check 3 fails
Writes <out>/steps.csv (one row per KV step: measured and simulated lanes, covered or why not). Exit 1 on a count
that differs, a carried label that is not the vertex's, a source that did not tag, or 0 simulated steps.
"""
import argparse
import bisect
import csv
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import records, server, stores
from kvevaluator.capture.binary import binary_path
from kvevaluator.schema import vids as vids_mod
import check_cpp_vs_python as differential  # (params_for, expand_fragments: the gate's own plan handling)

# the data side (the record stream's truth, a plan's calls)
# the gate owns it; predict_probe_savings imports it from here
SCHEMA = schema_path("ldbc")
LANES = ("S", "S_miss", "scan_key", "scan_value")


class Truth:
    """the schema, each vid's label and each owner's neighbours (the record stream), the store's chunks"""

    def __init__(self, sf, store, binary):
        s = json.load(open(SCHEMA))
        self.labels = [v["label"] for v in s["vertices"]]
        self.label_id = {v["label"]: v["id"] for v in s["vertices"]}
        self.etype_id = {e["etype"]: e["id"] for e in s["edges"]}
        self.endpoints = {e["etype"]: [tuple(p) for p in e["endpoints"]] for e in s["edges"]}
        self.tag_labels = defaultdict(list)          # the vid's group -> its labels, declaration order (labels_of_tag)
        for lab in self.labels:
            self.tag_labels[vids_mod._TAG[vids_mod.group_of(lab)]].append(lab)
        self.label_of, self.nbrs = {}, defaultdict(dict)
        members = defaultdict(list)                   # label -> uids
        with open(records.records_file("ldbc", sf)) as f:
            for line in f:
                r = json.loads(line)
                if r[0] == "v":
                    self.label_of[r[1]] = r[2]
                    members[r[2]].append(int(r[1], 16))
                elif r[0] == "e":
                    s_, d_ = int(r[2], 16), int(r[3], 16)
                    self.nbrs[(r[1], "out")].setdefault(s_, []).append(d_)
                    self.nbrs[(r[1], "in")].setdefault(d_, []).append(s_)
        for fam in self.nbrs.values():
            for v in fam.values():
                v.sort()
        # owners per family: props:<label> = the label's vertices; adj:<label>:<etype>:<dir> = its owners with a list
        owners = {("p", lab): sorted(u) for lab, u in members.items()}
        for (etype, d), fam in self.nbrs.items():
            for u in fam:
                lab = self.label_of.get(f"{u:016x}")
                owners.setdefault(("a", lab, etype, d), []).append(u)
        self.owners = {k: sorted(v) for k, v in owners.items()}
        self.owner_set = {k: set(v) for k, v in self.owners.items()}
        self.chunks = self._chunks(store, binary)

    def _chunks(self, store, binary):
        """{family: (firsts, his, value sizes, key bytes)}: the chunk keys under 't', the last owner from the records"""
        by_lid = {i: lab for lab, i in self.label_id.items()}
        by_eid = {i: e for e, i in self.etype_id.items()}
        raw = defaultdict(list)
        pat = re.compile(r'^\{"key":"([0-9A-Fa-f]+)","vsize":(\d+)')
        p = subprocess.Popen([binary, "dump", store, "--prefix", "74", "--limit", "1000000000"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
        for line in p.stdout:
            m = pat.match(line)
            if not m:
                continue
            k, vsize = bytes.fromhex(m.group(1)), int(m.group(2))
            lab, kind = by_lid[int.from_bytes(k[1:3], "big")], chr(k[3])
            first = int.from_bytes(k[-8:], "big")
            if kind == "p":
                raw[("p", lab)].append((first, vsize, len(k)))
            elif kind == "a":
                d = {"o": "out", "i": "in"}[chr(k[6])]
                raw[("a", lab, by_eid[int.from_bytes(k[4:6], "big")], d)].append((first, vsize, len(k)))
            elif kind == "e":                         # edge-prop chunks, keyed by the out-edge's ordinal
                raw[("e", lab, by_eid[int.from_bytes(k[4:6], "big")])].append((first, vsize, len(k)))
        if p.wait() != 0 or not raw:
            raise SystemExit(f"dump --prefix 74 on {store}: rc {p.returncode}, {len(raw)} families")
        out = {}
        for fam, cs in raw.items():
            cs.sort()
            firsts = [c[0] for c in cs]
            own = self.owners.get(fam, [])
            if fam[0] == "e":                         # ordinals are dense: a chunk ends where the next one starts
                out[fam] = (firsts, [f - 1 for f in firsts[1:]] + [float("inf")], [c[1] for c in cs], cs[0][2])
                continue
            his = []
            for i, f0 in enumerate(firsts):
                nxt = firsts[i + 1] if i + 1 < len(firsts) else float("inf")
                j = bisect.bisect_left(own, nxt) - 1
                his.append(own[j] if j >= 0 and own[j] >= f0 else f0)
            out[fam] = (firsts, his, [c[1] for c in cs], cs[0][2])
        return out

    def owns(self, lab, etype, d):
        return any((s if d == "out" else t) == lab for s, t in self.endpoints[etype])

    def owner_order(self, etype, d):
        """the schema's owner labels of (etype, dir), endpoint order: what an untagged source is probed under"""
        seen = []
        for s, t in self.endpoints[etype]:
            lab = s if d == "out" else t
            if lab not in seen:
                seen.append(lab)
        return seen

    def far_k(self, etype, d):
        return len({(t if d == "out" else s) for s, t in self.endpoints[etype]})

    def group_labels(self, vid):
        return self.tag_labels[int(vid, 16) >> vids_mod.SHIFT]


class Call:
    """one op call's (or one replay group's) chunk cache per family, and what it was charged"""

    def __init__(self, truth):
        self.t, self.read, self.c = truth, defaultdict(set), Counter()

    def probe(self, fam, u):
        """-> the probe was charged; replay.cc: free inside a chunk already read, else S (+ key, + value once)"""
        firsts, his, sizes, keylen = self.t.chunks.get(fam, ([], [], [], 0))
        i = bisect.bisect_right(firsts, u) - 1
        if i >= 0 and i in self.read[fam] and u <= his[i]:
            return
        self.c["S"] += 1
        if i < 0:
            self.c["S_miss"] += 1
            return
        self.c["scan_key"] += keylen
        if i not in self.read[fam]:
            self.c["scan_value"] += sizes[i]
            self.read[fam].add(i)


def dirs_of(d):
    return ["out", "in"] if d == "both" else [d]


def distinct(xs):
    return list(dict.fromkeys(xs))


def as_list(v):
    return [v] if isinstance(v, str) else list(v or [])


INPUT_KEY = {"read_neighbors": "srcs", "read_chase": "srcs", "read_closure": "srcs", "read_vertex_props": "vids",
             "read_filter_by_label_probe": "vids", "read_anchor_props": "cands"}
INDEX_OPS = ("read_index_eq", "read_index_range", "read_vertices_by_label", "read_top_k_by_index")


WRONG_RULE = None      # a control: "ignore-carried" or "owners-reversed"


def owners_in_schema_order(t, etype, d):
    """the labels that can hold a list of (etype, d), in the schema's vertex order: what an unlabelled vid probes"""
    labs = [lab for lab in t.labels if t.owns(lab, etype, d)]
    return labs[::-1] if WRONG_RULE == "owners-reversed" else labs


_ORDINALS = {}


def ordinal(t, lab, etype, src, dst):
    """the out-edge's edge-prop ordinal: its rank among the (src, dst) pairs of the sources of label `lab`, sorted
    (the format's ingest numbers them so, per (source label, edge type))"""
    if (lab, etype) not in _ORDINALS:
        own = t.owners.get(("a", lab, etype, "out"), [])
        out = t.nbrs[(etype, "out")]
        n, m = 0, {}
        for s in own:
            for dd in out[s]:
                m[(s, dd)] = n
                n += 1
        _ORDINALS[(lab, etype)] = m
    return _ORDINALS[(lab, etype)].get((src, dst))


def hop(t, call, vid, etype, dirs, tag, eprops=False, stored=False):
    """eprops: the hop also reads each found edge's props, one lookup per edge in the edge-prop chunks of the edge's
    SOURCE label (an out hop: the family that held the list; an in hop: the far end's, known from the stored label
    or from the schema's one label -- the binary refuses the read otherwise)"""
    u = int(vid, 16)
    for d in dirs:
        cands = ([tag] if t.owns(tag, etype, d) else []) if tag else owners_in_schema_order(t, etype, d)
        for lab in cands:
            fam = ("a", lab, etype, d)
            call.probe(fam, u)
            if u in t.owner_set.get(fam, ()):
                if eprops:
                    for n in t.nbrs[(etype, d)].get(u, []):
                        src, dst = (u, n) if d == "out" else (n, u)
                        slab = lab if d == "out" else t.label_of.get(f"{n:016x}")
                        if d == "in" and not (stored or t.far_k(etype, d) == 1):
                            raise SystemExit(f"edge props of {etype} on an in hop with no known far label: the binary refuses this")
                        o = ordinal(t, slab, etype, src, dst)
                        if o is None:
                            raise SystemExit(f"no ordinal for {src:x} -{etype}-> {dst:x} under {slab}")
                        call.probe(("e", slab, etype), o)
                break


def lookup(t, call, vid, labels, tag):
    if tag:
        labels = [tag] if tag in labels else []
    u = int(vid, 16)
    for lab in labels:
        call.probe(("p", lab), u)
        if t.label_of.get(vid) == lab:
            return lab
    return None


def far_tag(t, etype, d, vid, stored):
    """the label a hop gives its far end: stored with the entry, or the schema's one label for that direction"""
    return t.label_of.get(vid) if (stored or t.far_k(etype, d) == 1) else None


def simulate(t, op, args, tag_of, stored):
    """-> (Counter of the lanes, None) for one step, or (None, why) when this check does not cover it"""
    if op == "read_neighbors":
        eprops = bool(args.get("edge_props")) and args["etype"] in t.etypes_with_props
        call = Call(t)
        for s in distinct(args["srcs"]):
            hop(t, call, s, args["etype"], dirs_of(args.get("dir", "out")), tag_of(s), eprops, stored)
        return call.c, None
    if op == "read_chase":
        etype, dirs, mx = args["etype"], dirs_of(args.get("dir", "out")), args.get("max_hops")
        total = Counter()
        # level by level: ONE batch = one hop, one cache per hop (the once-per-batch rule); a hop reads
        # the distinct heads not read before, in chain order, each with its first chain's tag: a node is read once
        # per call (chase_over)
        chains, hops, next_of = [(s, tag_of(s)) for s in args["srcs"]], 0, {}
        while chains and (mx is None or hops < mx):
            call, frontier = Call(t), {}
            for cur, tag in chains:
                if cur not in next_of and cur not in frontier:
                    frontier[cur] = tag
            for cur, tag in frontier.items():
                hop(t, call, cur, etype, dirs, tag)
                next_of[cur] = None
                for d in dirs:
                    if tag and not t.owns(tag, etype, d):
                        continue                       # a label that holds no such list: no read, no neighbour
                    lst = t.nbrs[(etype, d)].get(int(cur, 16))
                    if lst:
                        next_of[cur] = (f"{lst[0]:016x}", far_tag(t, etype, d, f"{lst[0]:016x}", stored))
                        break
            total.update(call.c)
            chains, hops = [next_of[cur] for cur, _ in chains if next_of[cur]], hops + 1
        return total, None
    if op == "read_closure":
        etype, dirs, mx = args["etype"], dirs_of(args.get("dir", "out")), args.get("max_levels")
        total, seen, level = Counter(), set(), 0
        frontier = [(s, tag_of(s)) for s in distinct(args["srcs"])]
        seen.update(s for s, _ in frontier)
        while frontier and (mx is None or level < mx):
            call = Call(t)                             # one level = one hop call with its own cache
            nxt = {}
            for s, tag in frontier:
                hop(t, call, s, etype, dirs, tag)
                for d in dirs:
                    for n in t.nbrs[(etype, d)].get(int(s, 16), []):
                        v = f"{n:016x}"
                        if v not in seen and v not in nxt:
                            nxt[v] = far_tag(t, etype, d, v, stored)
            total.update(call.c)
            seen.update(nxt)
            frontier, level = list(nxt.items()), level + 1
        return total, None
    if op == "read_vertex_props":
        if not [p for p in args.get("props") or [] if p != "id"]:
            return Counter(), None
        call = Call(t)
        for v in distinct(args["vids"]):
            lookup(t, call, v, as_list(args.get("label")), tag_of(v))
        return call.c, None
    if op == "read_filter_by_label_probe":
        call = Call(t)
        for v in distinct(args["vids"]):
            if tag_of(v):
                continue                               # a carried label decides it at 0 ops
            lookup(t, call, v, as_list(args.get("label") or args.get("labels")), None)
        return call.c, None
    if op == "read_anchor_props":
        call = Call(t)
        for v in args["cands"]:
            if lookup(t, call, v, as_list(args.get("labels") or args.get("label")), tag_of(v)):
                break
        return call.c, None
    return None, op                                    # not simulated: listed by op


def expected_tagged(t, body, ref, field, stored):
    """what the rule says about the values of step `ref` (its `field`, or its items): True = every value carries a
    label, False = none does, None = not predicted here. Follows dedup / pluck only."""
    step = next((s for s in body["steps"] if s["name"] == ref), None)
    if step is None:
        return None
    op = step["op"]
    if op == "dedup" and set(step.get("items", {})) == {"ref"}:
        return expected_tagged(t, body, step["items"]["ref"], field, stored)
    if op == "pluck" and set(step.get("rows", {})) == {"ref"}:
        return expected_tagged(t, body, step["rows"]["ref"], step["field"], stored)
    if op in INDEX_OPS and field in (None, "_vid"):
        return True
    if op in ("read_filter_by_label_probe", "read_filter_by_label_index") and field is None:
        return True
    if op == "read_neighbors":
        pairs, dirs = step.get("pairs"), dirs_of(step.get("dir", "out"))
        if pairs and field == "src":
            return True                                # the family that held its list names it
        if (not pairs and field is None) or (pairs and field == "dst"):
            ks = {stored or t.far_k(step["etype"], d) == 1 for d in dirs}
            return ks.pop() if len(ks) == 1 else None
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--store", required=True, help="the pertype_csr store dir name under output/stores/ldbc/sf<SF>/, or a path")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--families", dest="workloads", default="interactive,microbench,bi")
    ap.add_argument("--queries", default=None)
    ap.add_argument("--stored", action="store_true", help="the store keeps the far end's label per adjacency entry")
    ap.add_argument("--wrong-rule", choices=["ignore-carried", "owners-reversed"], help="control: must FAIL")
    ap.add_argument("--allow-tag-setting", action="store_true", help="control: run with KVEVAL_LABEL_TAGS set")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    global WRONG_RULE
    WRONG_RULE = a.wrong_rule
    if a.wrong_rule or a.allow_tag_setting:
        print(f"=== CONTROL: wrong rule {a.wrong_rule}, KVEVAL_LABEL_TAGS={os.environ.get('KVEVAL_LABEL_TAGS')}: a FAIL is the expected verdict")
    if os.environ.get("KVEVAL_LABEL_TAGS") and not a.allow_tag_setting:
        raise SystemExit("check_probe_counts: KVEVAL_LABEL_TAGS is set; this check is for the default (all tags on)")
    store = a.store if os.path.isdir(a.store) else stores.store_dir("ldbc", a.sf, a.store)
    binary = binary_path()
    prov = json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout)
    print(f"=== binary {binary} sha {prov['binary_git_sha']} dirty {prov['binary_dirty']}; store {store}")
    t = Truth(a.sf, store, binary)
    t.etypes_with_props = {e["etype"] for e in json.load(open(schema_path("ldbc")))["edges"] if e.get("props")}
    name_of = {i: lab for lab, i in t.label_id.items()}
    want = set(a.queries.split(",")) if a.queries else None
    rows, bad, not_covered = [], [], Counter()
    ncell = nsim = nagree = nskip = 0
    labels_checked = labels_wrong = src_checked = src_wrong = 0
    fmt_name = json.load(open(os.path.join(store, "manifest.json")))["format"]
    if fmt_name != "pertype_csr":
        sys.exit(f"{store}: format {fmt_name!r}; this check replays Per-type CSR's chunk probes only")
    with server.CppServer(store, fmt_name) as srv:
        for wl in a.workloads.split(","):
            for path in sorted(glob.glob(os.path.join(differential._PLANS[wl], "*.json"))):
                q = os.path.basename(path)[:-5]
                if want and q not in want:
                    continue
                ir = json.load(open(path))
                try:
                    plist = differential.params_for(wl, q, a.sf, a.nparam)
                except Exception as e:
                    print(f"  no params {wl}/{q}: {type(e).__name__}: {e}")
                    nskip += 1
                    continue
                for idx, params in enumerate(plist, 1):
                    r = srv.run(ir, params, run={"P": 1, "trace_args": True})
                    if r.get("error"):
                        print(f"  not run {wl}/{q} p{idx}: {str(r['error'])[:120]}")
                        nskip += 1
                        break
                    variant = r["provenance"].get("body")
                    plan = ir if "steps" in ir else ir["plans"][variant]
                    body = differential.expand_fragments(plan, ir.get("fragments", []))
                    by_name = {s["name"]: s for s in body["steps"]}
                    ncell += 1
                    for st in r["steps"]:
                        if st.get("kind") != "kv":
                            continue
                        op, args, key = st["op"], st.get("args") or {}, INPUT_KEY.get(st["op"])
                        meas = {"S": st["S"], "S_miss": st["S_miss"], "scan_key": st["bytes"]["scan_key"],
                                "scan_value": st["bytes"]["scan_value"]}
                        carried = {}                       # vid -> the label of its first tagged occurrence in the input
                        if key:
                            ins = as_list(args.get(key))
                            ins = [x["_vid"] if isinstance(x, dict) else x for x in ins]
                            tags = (st.get("arg_tags") or {}).get(key)
                            tags = tags if isinstance(tags, list) else [tags] * len(ins) if tags is not None else [None] * len(ins)
                            for v, lab in zip(ins, tags):
                                if lab is None:
                                    continue
                                labels_checked += 1
                                if name_of.get(lab) != t.label_of.get(v):
                                    labels_wrong += 1
                                    bad.append(f"{wl}/{q} p{idx} {st['name']}: {v} carries {name_of.get(lab)}, the records say {t.label_of.get(v)}")
                                carried.setdefault(v, name_of.get(lab))
                            # check 3: what the producing KV step must have tagged
                            src = by_name.get(st["name"], {}).get(key)
                            if isinstance(src, dict) and set(src) == {"ref"}:
                                exp = expected_tagged(t, body, src["ref"], None, a.stored)
                                if exp is not None:
                                    for v, lab in zip(ins, tags):
                                        src_checked += 1
                                        if (lab is not None) != exp:
                                            src_wrong += 1
                                            if src_wrong <= 20:
                                                bad.append(f"{wl}/{q} p{idx} {st['name']}.{key}: {v} should {'carry a label' if exp else 'carry none'} "
                                                           f"(from {src['ref']}), the trace says {'one' if lab is not None else 'none'}")
                        sim, why = simulate(t, op, args, (lambda v: None) if WRONG_RULE == "ignore-carried" else carried.get, a.stored)
                        row = {"workload": wl, "query": q, "body": variant or "", "param_idx": idx, "step": st["name"], "op": op,
                               "n_inputs": len(distinct(as_list(args.get(key)) if key and not isinstance(as_list(args.get(key))[:1] and as_list(args.get(key))[0], dict) else [])),
                               "n_carried": len(carried), "simulated": int(sim is not None), "not_covered": why or ""}
                        for lane in LANES:
                            row[f"measured_{lane}"] = meas[lane]
                            row[f"sim_{lane}"] = sim[lane] if sim is not None else ""
                        if sim is None:
                            not_covered[why] += 1
                            row["agree"] = ""
                        else:
                            nsim += 1
                            ok = all(sim[lane] == meas[lane] for lane in LANES)
                            nagree += ok
                            row["agree"] = int(ok)
                            if not ok:
                                bad.append(f"{wl}/{q} p{idx} {st['name']} ({op}): measured {[meas[x] for x in LANES]} vs "
                                           f"simulated {[sim[x] for x in LANES]}")
                        rows.append(row)
    os.makedirs(a.out, exist_ok=True)
    if rows:
        with open(os.path.join(a.out, "steps.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    json.dump({"binary": binary, "sha": prov["binary_git_sha"], "dirty": prov["binary_dirty"], "store": store, "sf": a.sf,
               "nparam": a.nparam, "workloads": a.workloads, "stored": a.stored}, open(os.path.join(a.out, "meta.json"), "w"), indent=1)
    for b in bad[:25]:
        print("  FAIL", b)
    ok = nsim > 0 and nagree == nsim and not labels_wrong and not src_wrong
    print(f"[probe-counts] sf{a.sf} {os.path.basename(store)}: {ncell} (plan, param) cells, {nskip} plans not run; "
          f"{nsim} KV steps simulated, {nagree} equal the measured S / S_miss / scan key / scan value, {nsim - nagree} differ; "
          f"not covered: {dict(not_covered)}; carried labels checked against the records: {labels_checked}, wrong {labels_wrong}; "
          f"values whose producing step's tagging was predicted: {src_checked}, wrong {src_wrong} -> {'OK' if ok else 'FAIL'}  ({a.out})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
