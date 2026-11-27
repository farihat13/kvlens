#!/usr/bin/env python3
"""A closed-form oracle on the label consumers (labels on the rows): what each props read and label filter must cost,
given which input vids carry a label, recomputed from the record stream -- never through meta's read code.

Per traced step (run.trace_args: the step's vids and the labels their values carry, arg_tags), on the grid cells:
  read_vertex_props (Condensed props, one row per vertex): per distinct vid -- its carried label if a candidate (1 GET),
    none if not one (0), else the candidates probed in order until the vertex's own label (1 GET each);
  read_vertex_props (Split props, one record per prop): per distinct vid and asked prop (not id), a GET per candidate
    label that declares the prop, in order, until a present cell decides the vertex's label; later props read that
    label only (meta's decide-once rule);
  read_vertex_props (Chunked props): with every vid carrying a label, no op on a props part of a label none carries;
  read_filter_by_label_probe: one probe per distinct vid without a label (Condensed G / Split S), 0 per carried;
  read_filter_by_label_index: 0 ops when every vid carries a label.
The vertex's true label and its present props come from the records; a step's G / S / parts from the run itself.

  python3 tests/gates/meta/check_label_consumers.py [--sf 0.1] [--family interactive|microbench] [--queries q1,q2] [--cells ...]
Exit 0 = every compared step equals the closed form; prints the steps compared per op (0 compared = FAIL).
"""
import argparse
import json
import os
import sys
import time
from collections import defaultdict
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import records, server
from kvevaluator.measure import grid_capture

SCHEMA = schema_path("ldbc")
CONSUMERS = ("read_vertex_props", "read_filter_by_label_probe", "read_filter_by_label_index")


def load(sf):
    """(label of vid, present props of vid, declared props of label, label of label id)"""
    s = json.load(open(SCHEMA))
    declared = {v["label"]: {p["name"] if isinstance(p, dict) else p for p in v["props"]} for v in s["vertices"]}
    by_id = {v["id"]: v["label"] for v in s["vertices"]}
    label, present = {}, {}
    with open(records.records_file("ldbc", sf)) as f:
        for line in f:
            if not line.startswith('["v"'):
                continue
            r = json.loads(line)
            label[r[1]] = r[2]
            present[r[1]] = {k for k, v in r[3].items() if v is not None}
    return label, present, declared, by_id


def props_layout(cell):
    return cell.split("_adj_")[0].removeprefix("props_")


def labels_arg(args, key):
    v = args.get(key)
    return [v] if isinstance(v, str) else list(v or [])


def expected(op, layout, args, tags, truth):
    """the closed-form cost of one consumer step: ({lane: count} or None if not modelled, why)"""
    label, present, declared, by_id = truth
    if op == "read_vertex_props":
        vids, labs = args.get("vids") or [], labels_arg(args, "label")
        tv = tags.get("vids") or [None] * len(vids)
        first = {}
        for v, t in zip(vids, tv):
            if v not in first or (first[v] is None and t is not None):
                first[v] = t
        props = [p for p in (args.get("props") or []) if p != "id"]
        if layout == "chunked":
            if not first or any(t is None for t in first.values()):
                return None, "a Chunked read with an untagged vid: landings, not modelled"
            carried = {by_id[t] for t in first.values()}
            return {"wrong_label_parts": sorted(set(labs) - carried)}, "no op on a label no vid carries"
        g = 0
        for v, t in first.items():
            mine = labs if t is None else ([by_id[t]] if by_id[t] in labs else [])
            if not mine or not props:
                continue
            if layout == "condensed":
                if len(mine) == 1:
                    g += 1
                else:
                    true = label.get(v)
                    g += mine.index(true) + 1 if true in mine else len(mine)
            else:   # split: a GET per (prop, candidate declaring it) until decided, then that label only
                decided = None
                for p in props:
                    for lab in mine:
                        if decided and lab != decided:
                            continue
                        if p not in declared.get(lab, ()):
                            continue
                        g += 1
                        if label.get(v) == lab and p in present.get(v, ()):
                            decided = lab
                            break
        return {"G": g}, "closed form"
    if op == "read_filter_by_label_probe":
        vids = args.get("vids") or []
        tv = tags.get("vids") or [None] * len(vids)
        tagged = {v for v, t in zip(vids, tv) if t is not None}   # a vid is decided by its first tagged occurrence
        untagged = sum(1 for v in dict.fromkeys(vids) if v not in tagged)
        if layout == "chunked":
            return ({"S": 0, "G": 0} if untagged == 0 else None), "chunked: 0 when every vid is tagged"
        return ({"G": untagged} if layout == "condensed" else {"S": untagged}), "one probe per distinct never-tagged vid"
    if op == "read_filter_by_label_index":
        vids = args.get("vids") or []
        tv = tags.get("vids") or [None] * len(vids)
        if vids and all(t is not None for t in tv):
            return {"S": 0, "G": 0, "N": 0}, "every vid tagged: no walk"
        return None, "an untagged vid: the walk, not modelled"
    return None, "not a consumer"


HOPS = {"read_neighbors": "etype", "read_chase": "etype", "read_closure": "etype"}
INDEX_OPS = {"read_index_eq", "read_index_range", "read_vertices_by_label", "read_top_k_by_index"}
# every output value carries a label (a kept vid). read_vertex_by_id is not here: given ONE vid it returns it with no
# read (the vid is the key), so nothing is known and nothing tagged -- a tag only when it probed (may)
ALWAYS_TAG = {"read_filter_by_label_probe", "read_filter_by_label_index"}
SINGLE_INPUT = {"pluck", "dedup", "dedup_rows", "top_k", "rename", "project", "filter", "derive", "group_top",
                "annotate", "as_row", "gate"}
VID_ARG = {"read_vertex_props": "vids", "read_filter_by_label_probe": "vids", "read_filter_by_label_index": "vids",
           "read_vertex_by_id": "vid", "read_anchor_props": "cands"}


def refs(x):
    if isinstance(x, dict):
        r = {x["ref"]} if isinstance(x.get("ref"), str) else set()
        for v in x.values():
            r |= refs(v)
        return r
    if isinstance(x, list):
        r = set()
        for v in x:
            r |= refs(v)
        return r
    return set()


def plan_tags(body, far_k):
    """{step name: (may carry a tag, must carry one)} from the PLAN alone: a hop tags (k = 1 always, k > 1 when the
    cell stores codes: far_k), an index read and a label filter tag every output, a props / by-id read tags what it
    found (may); single-input flow steps pass both on, multi-input ones only `may`"""
    out = {}
    for st in body.get("steps", []):
        op, name = st.get("op"), st.get("name")
        ins = refs({k: v for k, v in st.items() if k not in ("name", "op")})
        may_in = any(out.get(r, (False, False))[0] for r in ins)
        must_in = bool(ins) and all(out.get(r, (False, False))[1] for r in ins)
        if op in HOPS:
            k = far_k(st.get(HOPS[op]), st.get("dir", "out"))
            # a closure's output keeps its start vids, a chase head that never moves its input value: those carry a
            # tag for sure only if their inputs do
            out[name] = (k or may_in, k and (op == "read_neighbors" or must_in))
        elif op in INDEX_OPS or op in ALWAYS_TAG:   # a label's index holds only that label's vertices
            out[name] = (True, True)
        elif op in ("read_vertex_props", "read_anchor_props", "read_vertex_by_id"):
            out[name] = (True, False)
        elif op in SINGLE_INPUT:
            out[name] = (may_in, must_in)
        else:
            out[name] = (may_in, False)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--family", dest="workload", default="interactive", choices=("interactive", "microbench"))
    ap.add_argument("--queries", default=None, help="comma list (default: every query of the workload)")
    ap.add_argument("--cells", default=None, help="comma list (default: the 9 grid cells)")
    ap.add_argument("--nparam", type=int, default=2)
    ap.add_argument("--control", action="store_true", help="also price every step as if no vid carried a label: that "
                    "closed form must disagree with the run somewhere (a check that cannot tell tags from none is no check)")
    a = ap.parse_args()
    from kvevaluator.plan_choice.candidates import candidates
    truth = load(a.sf)
    label, _, _, by_id = truth
    sch = json.load(open(SCHEMA))
    eps = {e["etype"]: e.get("endpoints", []) for e in sch["edges"]}

    def far_k(etype, d):
        """does a hop over (etype, d) tag its neighbours on a grid cell (every grid cell stores codes: far_label on)"""
        if not etype or etype not in eps:
            return False
        out_l, in_l = {b for _, b in eps[etype]}, {x for x, _ in eps[etype]}
        return bool(out_l if d == "out" else in_l if d == "in" else out_l | in_l)

    cells = a.cells.split(",") if a.cells else sorted(os.path.basename(p)[:-5] for p in
                                                      __import__("glob").glob(os.path.join(grid_capture.GRID, "*.json")))
    queries = a.queries.split(",") if a.queries else grid_capture.workload_queries(a.workload)[0]
    compared, skipped, fails = defaultdict(int), defaultdict(int), []
    per_cell = defaultdict(int)
    control_off = tags_checked = plan_checked = mixed = unrunnable = 0
    for cell in cells:
        t_cell = time.time()
        layout = props_layout(cell)
        spec = os.path.join(grid_capture.GRID, f"{cell}.json")
        store = grid_capture.meta_store(a.sf, a.workload, spec)
        invalid = grid_capture.invalid_on_cell(a.workload, spec)   # the bodies the grid never runs on this cell
        with server.CppServer(store, "meta") as srv:
            for q in queries:
                ir, cands = candidates(a.workload, q)
                runs = [("(plan)", ir, ir)] if "plans" not in ir else []
                for cname, cir in cands:
                    runs.append((cname, cir, next(iter(cir["plans"].values())) if "plans" in cir else cir))
                for cname, cir, body in runs:
                    if cname in invalid.get(q, {}):
                        unrunnable += 1
                        continue
                    pt = plan_tags(body, far_k)
                    for idx, p in enumerate(grid_capture.query_params(a.workload, a.sf, q, a.nparam), 1):
                        r = srv.run(cir, p, run={"P": 1, "trace_args": True, "parts": True})
                        if r.get("error"):
                            e = str(r["error"])
                            if any(t in e for t in ("not runnable", "capabilit", "unknown op", "not built")):
                                unrunnable += 1   # a body this cell cannot run: the grid records it not ok and skips it
                                continue
                            raise SystemExit(f"{cell} {q} {cname} p{idx}: {r['error']}")
                        for st in r["steps"]:
                            op = st.get("op")
                            if st.get("kind") == "flow":
                                continue
                            if "arg_tags" not in st:
                                raise SystemExit(f"{cell} {q} {cname}: step {st['name']} has no arg_tags -- this binary "
                                                 "does not trace the labels values carry (an error, not 'no tags')")
                            args, tags = st.get("args") or {}, st["arg_tags"]
                            # 5: every carried label is the vid's TRUE label (the records)
                            for k, tv in tags.items():
                                items = args.get(k)
                                items = items if isinstance(items, list) else [items]
                                tv = tv if isinstance(tv, list) else [tv]
                                for it, t in zip(items, tv):
                                    if t is None:
                                        continue
                                    v = it.get("_vid") if isinstance(it, dict) else it
                                    tags_checked += 1
                                    if by_id.get(t) != label.get(v):
                                        fails.append(f"{cell} {q} {cname} {st['name']}: {v} carries {by_id.get(t)}, "
                                                     f"its label is {label.get(v)}")
                            if op not in VID_ARG:
                                continue
                            # 6: the trace's tags against what the plan allows, both directions
                            vin = refs(next((s2 for s2 in body.get("steps", []) if s2.get("name") == st["name"]), {})
                                       .get(VID_ARG[op]))
                            may = any(pt.get(x, (False, False))[0] for x in vin)
                            must = bool(vin) and all(pt.get(x, (False, False))[1] for x in vin)
                            tv = tags.get(VID_ARG[op])
                            tv = tv if isinstance(tv, list) else ([tv] if tv is not None else [])
                            n_in = len(args.get(VID_ARG[op]) or []) if isinstance(args.get(VID_ARG[op]), list) else 1
                            n_tag = sum(1 for t in tv if t is not None)
                            plan_checked += 1
                            if n_tag and not may:
                                fails.append(f"{cell} {q} {cname} {st['name']}: {n_tag} tagged vids where the plan says "
                                             f"no label source reaches")
                            if must and n_in and n_tag < n_in:
                                fails.append(f"{cell} {q} {cname} {st['name']}: {n_in - n_tag} of {n_in} vids untagged "
                                             f"on a single-input chain from a label source (a dropped tag)")
                            # point 3: a probe filter given one vid both tagged and untagged
                            if op == "read_filter_by_label_probe" and tv:
                                seen = defaultdict(set)
                                for v, t in zip(args.get("vids") or [], tv):
                                    seen[v].add(t is not None)
                                mixed += sum(1 for v in seen if len(seen[v]) == 2)
                            if op not in CONSUMERS:
                                continue
                            want, why = expected(op, layout, args, tags, truth)
                            if a.control and want is not None and "wrong_label_parts" not in want:
                                cw, _ = expected(op, layout, args, {}, truth)
                                if cw is not None and cw != want:
                                    control_off += 1
                            if want is None:
                                skipped[op] += 1
                                continue
                            compared[op] += 1
                            per_cell[cell] += 1
                            if "wrong_label_parts" in want:
                                bad = [part for part, c in (st.get("parts") or {}).items()
                                       if part.split(":")[0] == "props" and part.split(":")[1] in want["wrong_label_parts"]
                                       and (c.get("S", 0) or c.get("G", 0) or c.get("N", 0))]
                                if bad:
                                    fails.append(f"{cell} {q} {cname} p{idx} {st['name']}: ops on {bad} ({why})")
                                continue
                            got = {k: st.get(k, 0) for k in want}
                            if got != want:
                                fails.append(f"{cell} {q} {cname} p{idx} {st['name']} ({op}): got {got}, closed form "
                                             f"{want} ({why})")
        print(f"  {cell}: {time.time() - t_cell:.1f} s, {per_cell[cell]} consumer steps compared", flush=True)
    for f in fails[:15]:
        print("  FAIL", f)
    n = sum(compared.values())
    empty_cells = [c for c in cells if not per_cell.get(c)]
    ok = (not fails and n > 0 and not empty_cells and all(compared.values()) and tags_checked > 0
          and (control_off > 0 if a.control else True))
    print(f"[label-consumers] sf{a.sf} {a.workload} (every candidate body): {n} consumer steps equal-checked "
          f"{dict(sorted(compared.items()))}, {sum(skipped.values())} not modelled {dict(sorted(skipped.items()))}; "
          f"{tags_checked} carried labels == the records; {plan_checked} steps' tags checked against the plan; "
          f"{mixed} probe-filter vids given both tagged and untagged (point 3); {unrunnable} runs of bodies meta cannot run "
          f"(skipped, as the grid does); cells with 0 compared: {empty_cells} -> "
          f"{'OK' if not fails and n and not empty_cells else f'FAIL ({len(fails)})'}"
          + (f"; control (every vid priced untagged) {'caught' if control_off else 'NOT CAUGHT'} on {control_off} steps"
             if a.control else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
