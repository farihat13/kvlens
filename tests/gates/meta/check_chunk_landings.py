#!/usr/bin/env python3
"""An independent oracle for Chunked READ counts: the SEEKs a read_neighbors / read_chase / read_closure step pays on a
Chunked-adjacency cell, computed from the store's chunk boundaries, the step's sources and the record stream -- never
through the C++ replay.

The rule (Python's reference _ChunkReader, formats/pertype_csr.py; the C++ replay reproduces it): per BATCH (a set of
probes whose keys are known before the first is read), an id inside a chunk already read ([first owner, last owner])
costs 0; anything else is 1 SEEK -- a landing on a new chunk (read once), a re-landing on a chunk already read (an id
between two chunks), or a miss (no chunk at or below the id). A read_neighbors call is one batch; a chase or a closure
is one batch per level (the once-per-batch rule).
The probes go in task order:
  read_neighbors: the op's distinct sources in first-seen order; per source, out then in; a direction is skipped only
    when the step names its sources' labels and none of them owns it (the schema's endpoints; the vid is opaque);
  read_chase: level by level over the distinct unread chain heads; a head probes its directions in
    order until one holds a neighbour, and moves to that list's FIRST (smallest) neighbour (from the records);
  read_closure: level by level, each level a read_neighbors over the new frontier (distinct, unseen);
the cache is the level's.
--props checks the Chunked PROPS reads instead (read_vertex_props on the 3 Chunked-props cells, every candidate body of
every query): per op call, the distinct vids in first-seen order, each with its first tagged occurrence's label (the
trace's arg_tags); per vid its candidate labels -- the carried label if one of the step's, none if not, else the step's
labels in order -- and per label one probe of that label's chunk run under the same rule (a chunk already read in this
call costs 0, else 1 SEEK), until the label that holds the vid (the records); no wanted prop but `id`: no read. The
props chunk boundaries come from census --chunk-keys (families props:<label>).

Inputs: `kvevaluator census --chunk-keys <store>` (each Chunked adjacency family's chunks as [first, last] owner,
read from the keys and the chunk headers), the step's sources from a traced run (run.trace_args), and, for a chase or
a closure, the neighbour lists from the record stream. Compared per (step, adjacency part) with the run's own per-part S.

  python3 tests/gates/meta/check_chunk_landings.py [--sf 0.1] [--queries ic5,ic12,ic9,is2,is6] [--cells ...]
  python3 tests/gates/meta/check_chunk_landings.py --props [--sf 0.1] [--family microbench] [--control]
        [--params <capture with slice_params.json>]
Exit 0 = every compared step's S equals the oracle's; prints the steps compared (0 = FAIL).
"""
import argparse
import bisect
import json
import os
import subprocess
import sys
from collections import defaultdict
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.capture import binary
from kvevaluator.measure import plan_runs, records, server
from kvevaluator.measure import grid_capture
from kvevaluator.plan_choice import cells

SCHEMA = schema_path("ldbc")
CELLS = ("props_split_adj_chunked", "props_condensed_adj_chunked", "props_chunked_adj_chunked")
PROPS_CELLS = ("props_chunked_adj_split", "props_chunked_adj_condensed", "props_chunked_adj_chunked")


class Schema:
    def __init__(self):
        s = json.load(open(SCHEMA))
        self.owns = set()
        for e in s["edges"]:
            for a, b in e["endpoints"]:
                self.owns.add((a, e["etype"], "out"))
                self.owns.add((b, e["etype"], "in"))

    def probed(self, labels, etype, d):
        """is (etype, d) probed for sources the step names `labels` for? (no labels: always)"""
        return not labels or any((lab, etype, d) in self.owns for lab in labels)


def neighbour_lists(sf, etypes):
    """{(etype, dir): {owner: sorted neighbours}} from the record stream, for the etypes a chase / closure walks"""
    lists = defaultdict(lambda: defaultdict(list))
    if not etypes:
        return lists
    with open(records.records_file("ldbc", sf)) as f:
        for line in f:
            if not line.startswith('["e"'):
                continue
            r = json.loads(line)
            if r[1] not in etypes:
                continue
            s, d = int(r[2], 16), int(r[3], 16)
            lists[(r[1], "out")][s].append(d)
            lists[(r[1], "in")][d].append(s)
    for fam in lists.values():
        for v in fam.values():
            v.sort()
    return lists


def chunked_family(spec, etype, d):
    """is the spec's (edge type, direction) adjacency Chunked: the direction's override, else the etype's, else the default"""
    c = (spec.get("adjacency_by_etype_dir", {}).get(etype, {}).get(d) or spec.get("adjacency_by_etype", {}).get(etype)
         or spec["adjacency"])
    return isinstance(c.get("owners"), dict)


def merged(ranges):
    """the control: every pair of adjacent chunks merged into one -- the oracle must then disagree with the replay"""
    out = {}
    for fam, chunks in ranges.items():
        m = []
        for i in range(0, len(chunks), 2):
            pair = chunks[i:i + 2]
            m.append((pair[0][0], pair[-1][1]))
        out[fam] = m
    return out


def chunk_ranges(store, props=False):
    """{(etype, dir): sorted [(first, last)]} from census --chunk-keys; props=True: {(label, "props"): ...} of the
    props chunks (families props:<label>)"""
    out = json.loads(subprocess.run([binary.binary_path(), "census", "--chunk-keys", store], capture_output=True,
                                    text=True, check=True).stdout)
    ranges = {}
    for fam, chunks in out["chunk_ranges"].items():
        a, b = fam.split(":")
        if (a == "props") != props:
            continue
        rg = sorted((int(x, 16), int(y, 16)) for x, y in chunks)
        for (lo1, hi1), (lo2, hi2) in zip(rg, rg[1:]):   # Call.probe finds a vid's chunk by bisect: needs disjoint chunks
            if lo2 <= hi1:
                raise SystemExit(f"{store}: chunks of {fam} overlap ({lo1:x}-{hi1:x}, {lo2:x}-{hi2:x})")
        ranges[(b, "props") if props else (a, b)] = rg
    return ranges


class Call:
    """one op call's chunk cache and its SEEKs per dir"""

    def __init__(self, etype, ranges):
        self.etype, self.ranges = etype, ranges
        self.cache = defaultdict(set)     # dir -> indexes into the family's chunks read in this call
        self.seeks = defaultdict(int)

    def probe(self, u, d):
        # the family's chunks are sorted and disjoint (chunk_ranges checks it), so the one chunk at or below u is the
        # only one that can hold it: found by bisect, not by a scan of the chunks already read
        fam = self.ranges.get((self.etype, d), [])
        i = bisect.bisect_right(fam, (u, float("inf"))) - 1
        if i >= 0 and i in self.cache[d] and u <= fam[i][1]:
            return                        # inside a chunk already read: 0 ops
        self.seeks[d] += 1                # a landing, a re-landing or a miss: 1 SEEK
        if i >= 0:
            self.cache[d].add(i)          # a new chunk: read once


def distinct(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def oracle_seeks(srcs, etype, dirs, ranges, sch, labels=(), call=None):
    """{dir: SEEKs} for one read_neighbors call (or one closure level, on the closure's `call`) by the reference rule"""
    call = call or Call(etype, ranges)
    for s in distinct(srcs):
        u = int(s, 16)
        for d in dirs:
            if sch.probed(labels, etype, d):
                call.probe(u, d)
    return call.seeks


def oracle_chase(srcs, etype, dirs, max_hops, ranges, nbrs, old_rule=False):
    """{dir: SEEKs} for one read_chase call: level by level, a node read once per call, its first neighbour next; a
    level's heads are one batch (their keys are known before the level's first read), so the chunk cache is the
    level's (the once-per-batch rule).
    old_rule: one cache over the whole call (meta-v4 row 1u, before the once-per-batch rule) -- the rule control"""
    call = Call(etype, ranges)
    seeks = defaultdict(int)
    cur = [int(s, 16) for s in srcs]
    done = [False] * len(cur)
    next_of = {}
    hops = 0
    while max_hops is None or hops < max_hops:
        frontier = distinct(u for i, u in enumerate(cur) if not done[i] and u not in next_of)
        level = call if old_rule else Call(etype, ranges)
        for u in frontier:
            nxt = None
            for d in dirs:
                level.probe(u, d)
                lst = nbrs[(etype, d)].get(u)
                if lst:
                    nxt = lst[0]
                    break
            next_of[u] = nxt
        if not old_rule:
            for d, n in level.seeks.items():
                seeks[d] += n
        moved = False
        for i in range(len(cur)):
            if done[i]:
                continue
            if next_of[cur[i]] is None:
                done[i] = True
                continue
            cur[i] = next_of[cur[i]]
            moved = True
        if not moved:
            break
        hops += 1
    return call.seeks if old_rule else seeks


def oracle_closure(srcs, etype, dirs, max_levels, ranges, nbrs, sch, old_rule=False):
    """{dir: SEEKs} for one read_closure call: each level a read_neighbors over the new frontier, with the level's
    own cache (a level is one batch: the once-per-batch rule).
    old_rule: one cache over the whole call (before the once-per-batch rule) -- the rule control"""
    call = Call(etype, ranges)
    total = defaultdict(int)
    seen = set()
    frontier = []
    for s in srcs:
        u = int(s, 16)
        if u not in seen:
            seen.add(u)
            frontier.append(u)
    levels = 0
    while frontier and (max_levels is None or levels < max_levels):
        if old_rule:
            oracle_seeks([f"{u:016x}" for u in frontier], etype, dirs, ranges, sch, call=call)
        else:
            for d, n in oracle_seeks([f"{u:016x}" for u in frontier], etype, dirs, ranges, sch).items():
                total[d] += n
        nxt = []
        for u in frontier:
            for d in dirs:
                nxt.extend(nbrs[(etype, d)].get(u, []))
        frontier = [v for v in distinct(nxt) if v not in seen]
        seen.update(frontier)
        levels += 1
    return call.seeks if old_rule else total


def oracle_props(vids, tags, labs, props, ranges, label_of, by_id):
    """{label: SEEKs} for one read_vertex_props call on a Chunked props cell (the rule in the docstring)"""
    if not [p for p in props if p != "id"]:
        return {}
    first = {}
    for v, t in zip(vids, tags or [None] * len(vids)):
        if v not in first or (first[v] is None and t is not None):
            first[v] = t
    calls = {}
    for v, t in first.items():
        mine = labs if t is None else ([by_id[t]] if by_id[t] in labs else [])
        u = int(v, 16)
        for lab in mine:
            call = calls.setdefault(lab, Call(lab, ranges))
            call.probe(u, "props")
            if label_of.get(v) == lab:
                break
    return {lab: c.seeks["props"] for lab, c in calls.items() if c.seeks["props"]}


def main_props(a):
    """--props: every read_vertex_props step on the Chunked-props cells, every candidate body, vs the oracle"""
    from kvevaluator.plan_choice.candidates import candidates
    import check_label_consumers as clc
    label_of, _, _, by_id = clc.load(a.sf)
    cells = a.cells.split(",") if a.cells else PROPS_CELLS
    queries = a.queries.split(",") if a.queries else grid_capture.workload_queries(a.workload)[0]
    compared = control_off = unrunnable = 0
    fails, per_cell = [], defaultdict(int)
    for cell in cells:
        spec = os.path.join(grid_capture.GRID, f"{cell}.json")
        store = grid_capture.meta_store(a.sf, a.workload, spec)
        ranges = chunk_ranges(store, props=True)
        if not ranges:
            raise SystemExit(f"{cell}: census lists no props chunks (a binary without props chunk ranges?)")
        invalid = grid_capture.invalid_on_cell(a.workload, spec)
        with server.CppServer(store, "meta") as srv:
            for q in queries:
                ir, cands = candidates(a.workload, q)
                runs = [("(plan)", ir)] if "plans" not in ir else []
                runs += [(cname, cir) for cname, cir in cands]
                for cname, cir in runs:
                    if cname in invalid.get(q, {}):
                        unrunnable += 1
                        continue
                    for idx, p in enumerate(grid_capture.query_params(a.workload, a.sf, q, a.nparam), 1):
                        r = srv.run(cir, p, run={"P": 1, "trace_args": True, "parts": True})
                        if r.get("error"):
                            e = str(r["error"])
                            if any(t in e for t in ("not runnable", "capabilit", "unknown op", "not built")):
                                unrunnable += 1
                                continue
                            raise SystemExit(f"{cell} {q} {cname} p{idx}: {e}")
                        for st in r["steps"]:
                            if st.get("op") != "read_vertex_props":
                                continue
                            if "arg_tags" not in st:
                                raise SystemExit(f"{cell} {q}: step {st['name']} has no arg_tags (an old binary?)")
                            args = st.get("args") or {}
                            vids, labs = args.get("vids") or [], clc.labels_arg(args, "label")
                            want = oracle_props(vids, st["arg_tags"].get("vids"), labs, args.get("props") or [],
                                                ranges, label_of, by_id)
                            if a.control and oracle_props(vids, st["arg_tags"].get("vids"), labs, args.get("props") or [],
                                                          merged(ranges), label_of, by_id) != want:
                                control_off += 1
                            got = defaultdict(int)
                            for part, c in (st.get("parts") or {}).items():
                                if part.startswith("props:") and c.get("S", 0):
                                    got[part[6:]] += c["S"]
                            compared += 1
                            per_cell[cell] += 1
                            if dict(got) != want:
                                fails.append(f"{cell} {q} {cname} p{idx} step {st['name']}: replay S {dict(got)} vs "
                                             f"oracle {want} ({len(vids)} vids, labels {labs})")
    for f in fails[:12]:
        print("  FAIL", f)
    empty = [c for c in cells if not per_cell[c]]
    caught = (control_off > 0) if a.control else None
    ok = not fails and compared > 0 and not empty and caught is not False
    print(f"[chunk-landings props] sf{a.sf} {a.workload}: {compared} read_vertex_props steps on {len(cells)} Chunked-props "
          f"cells {dict(per_cell)}; {unrunnable} bodies the cell cannot run (skipped, as the grid does); cells with 0 "
          f"compared: {empty} -> {'OK: replay S == the oracle on every step' if not fails and compared else f'FAIL ({len(fails)})'}"
          + ("" if caught is None else f"; control (adjacent chunks merged) {'caught' if caught else 'NOT CAUGHT'} on "
             f"{control_off} steps"))
    return 0 if ok else 1


def body_ir(workload, q, spec, store):
    """the IR sent for q on a cell: the pick record's body (plan_runs.ir_to_run); on a store the record has no entry for
    (a per-direction mix), the first legal body: any body walks the chunks this check compares"""
    from kvevaluator.plan_choice import pick_record
    from kvevaluator.plan_choice.candidates import forced
    try:
        return plan_runs.ir_to_run(workload, q, "meta", store, spec=spec)
    except ValueError:
        ir = plan_runs.ir_for(workload, q)
        caps, of_label = cells.store_caps(workload, spec)
        return forced(ir, pick_record.legal_bodies(ir, caps, of_label)[0])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--queries", default=None, help="default ic5,ic12,ic9,is2,is6 (--props: every query)")
    ap.add_argument("--cells", default=None, help="default the 3 Chunked-adjacency cells (--props: Chunked-props)")
    ap.add_argument("--spec-dir", default=grid_capture.GRID,
                    help="the cells' spec dir (default the grid). A mix (per-type / per-direction adjacency) is checked "
                         "on its Chunked (edge type, direction) families only")
    ap.add_argument("--params", default=None, help="a capture holding slice_params.json (typical + hub per query)")
    ap.add_argument("--control", action="store_true", help="also run the oracle on merged chunk ranges, and chases / "
                    "closures under the whole-call-cache rule (before once-per-batch): each must disagree somewhere")
    ap.add_argument("--family", dest="workload", default="interactive", choices=("interactive", "microbench"))
    ap.add_argument("--props", action="store_true", help="the Chunked props reads instead (see the docstring)")
    ap.add_argument("--nparam", type=int, default=2, help="--props: params per query")
    a = ap.parse_args()
    if a.props:
        return main_props(a)
    sch = Schema()
    a.queries = a.queries or "ic5,ic12,ic9,is2,is6"
    a.cells = a.cells or ",".join(CELLS)
    slices = json.load(open(os.path.join(a.params, "slice_params.json"))).get(a.workload, {}) if a.params else {}
    compared, fails, control_off, skipped_steps = 0, [], 0, 0
    by_op, control_by_op, rule_off = defaultdict(int), defaultdict(int), defaultdict(int)
    walked = {"replyOf", "isSubclassOf", "knows"}   # the etypes the plans chase or close over
    nbrs = neighbour_lists(a.sf, walked)
    for cell in a.cells.split(","):
        spec = os.path.join(a.spec_dir, f"{cell}.json")
        layout = json.load(open(spec))
        store = grid_capture.meta_store(a.sf, a.workload, spec)
        ranges = chunk_ranges(store)
        with server.CppServer(store, "meta") as srv:
            for q in a.queries.split(","):
                ir = body_ir(a.workload, q, spec, store)
                params = grid_capture.query_params(a.workload, a.sf, q, 5)
                sp = slices.get(q) or {"typical": 1, "hub": len(params)}
                for kind, idx in (("typical", sp["typical"]), ("hub", sp["hub"])):
                    r = srv.run(ir, params[idx - 1], run={"P": 1, "trace_args": True, "parts": True})
                    if r.get("error"):
                        raise SystemExit(f"{cell} {q} p{idx}: {r['error']}")
                    for st in r["steps"]:
                        op = st.get("op")
                        if op not in ("read_neighbors", "read_chase", "read_closure"):
                            continue
                        args = st.get("args") or {}
                        etype, d = args.get("etype"), args.get("dir", "out")
                        walked_dirs = ["out", "in"] if d == "both" else [d]
                        # a mix: the oracle knows Chunked families only. A hop is checked on its Chunked directions; a
                        # chase / closure only when every direction it walks is Chunked (a uniform Chunked cell: all)
                        dirs = [x for x in walked_dirs if chunked_family(layout, etype, x)]
                        if not dirs or (op != "read_neighbors" and dirs != walked_dirs):
                            skipped_steps += 1
                            continue
                        srcs = args.get("srcs") or []
                        if op in ("read_chase", "read_closure") and etype not in walked:
                            raise SystemExit(f"{q}: a {op} over {etype}: add it to the walked etypes")
                        lab = args.get("label") or []
                        labels = [lab] if isinstance(lab, str) else list(lab)

                        def oracle(rg, old=False):
                            if op == "read_chase":
                                return oracle_chase(srcs, etype, dirs, args.get("max_hops"), rg, nbrs, old)
                            if op == "read_closure":
                                return oracle_closure(srcs, etype, dirs, args.get("max_levels"), rg, nbrs, sch, old)
                            return oracle_seeks(srcs, etype, dirs, rg, sch, labels)
                        want = oracle(ranges)
                        if a.control and dict(oracle(merged(ranges))) != dict(want):
                            control_off += 1
                            control_by_op[op] += 1
                        if a.control and op != "read_neighbors" and dict(oracle(ranges, old=True)) != dict(want):
                            rule_off[op] += 1
                        by_op[op] += 1
                        got = defaultdict(int)
                        for part, c in (st.get("parts") or {}).items():
                            f = part.split(":")
                            if f[0] == "adj" and f[2] == etype and f[3] in dirs:
                                got[f[3]] += c.get("S", 0)
                        compared += 1
                        if dict(got) != {k: v for k, v in want.items() if v}:
                            fails.append(f"{cell} {q} p{idx} ({kind}) step {st['name']} {etype} {d}: replay S "
                                         f"{dict(got)} vs oracle {dict(want)} ({len(args.get('srcs') or [])} sources)")
    for f in fails[:12]:
        print("  FAIL", f)
    # the controls: merged chunks must move some step; the whole-call-cache rule (before once-per-batch) must move some step of each op the
    # run compared (a check whose steps cannot tell the rules apart is not a check of the rule)
    # (run per workload: at sf0.1 LDBC's IC12 closure reads one small chunk and cannot tell the rules apart; the
    # microbench's closure can -- so the closure rule is checked there)
    walks = sum(by_op.get(o, 0) for o in ("read_chase", "read_closure"))
    caught = ((control_off > 0 or not by_op.get("read_neighbors")) and (sum(rule_off.values()) > 0 or not walks)) \
        if a.control else None
    ok = not fails and compared > 0 and caught is not False
    print(f"[chunk-landings] sf{a.sf}: {compared} steps ({dict(sorted(by_op.items()))}) on {len(a.cells.split(','))} Chunked cells "
          f"({a.queries}; {skipped_steps} steps on a family that is not Chunked skipped) -> "
          f"{'OK: replay S == the oracle on every step' if not fails and compared else f'FAIL ({len(fails)})'}"
          + ("" if caught is None else f"; control (adjacent chunks merged) {'caught' if caught else 'NOT CAUGHT'}: "
             f"the oracle's S changes on {control_off} steps {dict(sorted(control_by_op.items()))}; the whole-call-cache "
             f"rule differs on {dict(sorted(rule_off.items()))}"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
