#!/usr/bin/env python3
"""The logical census against a real engine: per query, the shares of each kind (edges, edge props, label checks, vertex
props, index entries) in our plan's census and in NebulaGraph's own plan. NebulaGraph's plan is counted two ways: its
engine twin (the evaluator's copy of its plan, whose S / G / N equal the live engine's) on the C++ nebula_engine store,
and its PROFILE rows, read in place from the committed engine-fidelity capture (the plan's shape; rough counts).

    uv run --locked python experiments/logical_census/engine_profile_check.py --grid <captures/ldbc/<scale> dir of a slice, params 1-5 and --params> \
        --profile <the sf0.1 engine-fidelity capture's ldbc dir> --sf3-census <sf3 census.csv> --sf 0.1 \
        [--queries ic4,ic5,ic9,ic12,is1] [--params 1,7] --out DIR
        -> <out>/shares.csv          per query and side (census, nebula_twin, nebula_profile_rows): each kind's count and
                                     share (the mean over --params), the dominant kind, the live engine's KV counts
           <out>/twin_steps.csv      per twin step: rows, S / G / N, its kinds
           <out>/profile_nodes.csv   per PROFILE storage operator: rows, input rows, the kinds by its rule
           <out>/shares_by_scale.csv per query: the census shares at the SF (--params, params 1-5) and at sf3 (params 1-5)
A dirty binary is warned and marked binary_unclean in <out>/stale.jsonl (kvevaluator.stale).
Gates: each capture's params are the evaluator's param row; the census's layout-free gate (census.layout_free); the
twin's S / G / N equal the capture's sim counts; a twin step that reads the store without a kind, or an unknown PROFILE
operator, stops the run."""
import argparse
import csv
import json
import os
import re
from collections import defaultdict

from kvevaluator import stale
from kvevaluator.datasets import schema_path
from kvevaluator.paths import ROOT

EXP = str(ROOT)
import census  # (this folder is on the path when the script runs)

KINDS = ("edges", "edge_props", "label_checks", "vertex_props", "index_entries")
# NebulaGraph's operators. The storage ones read the store (rows: Executor.cpp:620, stats.rows = numRows_, the
# executor's output rows); the rest work on rows in memory and count nothing
STORAGE = {"Traverse", "AppendVertices", "GetVertices", "GetNeighbors", "GetEdges", "IndexScan", "TagIndexFullScan",
           "TagIndexPrefixScan", "TagIndexRangeScan", "EdgeIndexFullScan", "EdgeIndexPrefixScan", "EdgeIndexRangeScan",
           "ScanVertices", "ScanEdges", "Expand", "ExpandAll", "ShortestPath", "BFSShortest", "MultiShortestPath",
           "AllPaths", "FulltextIndexScan"}
RULED = {"Traverse", "AppendVertices", "GetVertices", "IndexScan", "TagIndexFullScan", "TagIndexPrefixScan",
         "TagIndexRangeScan"}
MEMORY = {"Start", "PassThrough", "Argument", "Dedup", "Project", "Filter", "Aggregate", "TopN", "Sort", "Limit",
          "HashLeftJoin", "HashInnerJoin", "CrossJoin", "Union", "RollUpApply", "PatternApply", "Unwind", "DataCollect",
          "Minus", "Intersect", "Select", "Loop", "SetOp"}
TAG_FILTER = re.compile(r"\b(`?)(\w+)\1\._tag IS NOT EMPTY")
EDGE_PROP = re.compile(r"\b([A-Z][A-Z_]+)\.([a-z]\w*)")      # HAS_MEMBER.joinDate: an edge type is upper case


def props_of(entries):
    """{tagId: [stored props]} of a vertexProps / props list (the _tag, _src, _dst... columns are keys, not props)"""
    return {e["tagId"]: [p for p in e["props"] if not p.startswith("_")] for e in entries}


def tag_ids(nodes_by_plan):
    """{tag name: tagId}, from every storage operator with one tag's props and a filter on that tag's label"""
    seen = defaultdict(set)
    for nodes in nodes_by_plan:
        for n in nodes:
            d = n["description"]
            entries = json.loads(d.get("vertexProps") or d.get("props") or "[]")
            names = {m[1] for k in ("filter", "first step filter", "tag filter") for m in TAG_FILTER.findall(d.get(k, ""))}
            if len(entries) == 1 and len(names) == 1 and "tagId" in entries[0]:
                seen[names.pop()].add(entries[0]["tagId"])
    bad = {k: v for k, v in seen.items() if len(v) != 1}
    if bad:
        raise SystemExit(f"engine_profile_check: a tag name maps to several tagIds: {bad}")
    return {k: v.pop() for k, v in seen.items()}


def vertex_props(entries, tag, ids):
    """(best, lo, hi, note): props read per vertex. One tag: its props. Several (the vertex's tag is in the data, not
    the plan): lo / hi are the fewest and the most of the tags', best is the filtered tag's (a vertex of another tag
    reads its own before the filter drops it), else lo"""
    p = props_of(entries)
    if not p:
        return 0, 0, 0, ""
    ks = sorted(len(v) for v in p.values())
    if len(ks) == 1:
        return ks[0], ks[0], ks[0], ""
    best = len(p[ids[tag]]) if tag is not None and ids.get(tag) in p else ks[0]
    return best, ks[0], ks[-1], f"{len(p)} tags listed: {ks[0]}-{ks[-1]} props a vertex"


def node_kinds(n, rows, ids):
    """one storage operator's elements: {kind: count, vertex_props_hi, approx, rule}"""
    d = n["description"]
    name = n["name"]
    r = rows[n["id"]]
    n_in = sum(rows[x] for x in n["dependencies"])
    e = dict.fromkeys(KINDS, 0)
    e.update(vertex_props_lo=0, vertex_props_hi=0, approx=0, rule="")
    tags = [m[1] for k in ("first step filter", "tag filter", "filter") for m in TAG_FILTER.findall(d.get(k, ""))]
    tag = tags[0] if tags else None
    if name == "Traverse":
        lo, hi = re.match(r"(\d+)\.\.(\d+)", d["steps"]).groups()
        lo, hi = int(lo), int(hi)
        # rows are paths, one edge added per path; a 0-step path is the source itself
        e["edges"] = max(r - n_in, 0) if lo == 0 else r
        eprops = {p for x in json.loads(d.get("edgeProps") or "[]") for p in x["props"] if not p.startswith("_")}
        eprops |= {f"{m[0]}.{m[1]}" for m in EDGE_PROP.findall(d.get("filter", ""))}
        e["edge_props"] = r * len(eprops)
        if d.get("first step filter") or d.get("tag filter"):
            e["label_checks"] = n_in                      # the source vertex's tag, checked in the storage call
        vb, vl, vh, note = vertex_props(json.loads(d.get("vertexProps") or "[]"), tag, ids)
        # the source's props, read with its edge list
        e["vertex_props"], e["vertex_props_lo"], e["vertex_props_hi"] = n_in * vb, n_in * vl, n_in * vh
        e["approx"] = int(hi > 1 or lo == 0)
        e["rule"] = "; ".join(x for x in (f"steps {lo}..{hi}: rows are paths" if e["approx"] else "", note) if x)
    elif name in ("AppendVertices", "GetVertices"):
        if tag is not None:
            e["label_checks"] = n_in
        vb, vl, vh, note = vertex_props(json.loads(d.get("props") or "[]"), tag, ids)
        # per input row, as the census counts
        e["vertex_props"], e["vertex_props_lo"], e["vertex_props_hi"] = n_in * vb, n_in * vl, n_in * vh
        e["rule"] = note
    elif name in RULED:                                    # an index scan: the entries it returns
        e["index_entries"] = r
    else:
        raise SystemExit(f"engine_profile_check: the storage operator {name} has no rule")
    return e


def read_profile(path):
    with open(path) as f:
        d = json.load(f)
    nodes = d["plan"]["nodes"]
    rows = {}
    for n in nodes:
        if len(n["profiles"]) != 1:
            raise SystemExit(f"engine_profile_check: {path} node {n['id']}: {len(n['profiles'])} profiles (a loop)")
        rows[n["id"]] = n["profiles"][0]["rows"]
        if n["name"] not in STORAGE | MEMORY:
            raise SystemExit(f"engine_profile_check: {path}: the operator {n['name']} is unknown")
    return d, nodes, rows


def check_params(sf, queries, params, profile):
    """each capture's params are the evaluator's row (param_rows, the rows the census slice ran): every key the two
    share holds the same value (an engine's id prefix, person_ / msg_, stripped), and the anchor id is one of them"""
    from kvevaluator.datasets.ldbc.params import param_rows
    checked = 0
    for q in queries:
        rows = param_rows(sf, q)
        for i in params:
            got = json.load(open(os.path.join(profile, "nebula", q, f"p{i}", "plan.json")))["params"]
            want = rows[i - 1]
            shared = [k for k in got if k in want]
            same = [k for k in shared if str(got[k]).split("_")[-1] == str(want[k])]
            if not any(k.endswith("Id") for k in same) or len(same) != len(shared):
                raise SystemExit(f"engine_profile_check: {q} p{i}: the capture's params {got} are not row {want}")
            checked += 1
    print(f"[params] {checked} (query, param) captures equal the evaluator's param rows")


def asked_props(nodes, ids, tags):
    """{tag: props asked} of the PROFILE storage operator that lists exactly these tags (the twin's all_tag_props mirrors
    one): the props its plan asks for, whatever else the stored row holds; refuses unless one list matches"""
    name = {v: k for k, v in ids.items()}
    found = set()
    for n in nodes:
        d = n["description"]
        p = props_of(json.loads(d.get("vertexProps") or d.get("props") or "[]"))
        if {name.get(t) for t in p} == set(tags):
            found.add(tuple(sorted((name[t], len(v)) for t, v in p.items())))
    if len(found) != 1:
        raise SystemExit(f"engine_profile_check: {len(found)} PROFILE prop lists match the tags {sorted(tags)}")
    return dict(found.pop())


def twin_elements(op, x, st, own_rows, asked=None):
    """one twin step's elements. The census's own operators: census.elements. NebulaGraph's two engine operators
    (cpp/kvevaluator/src/formats/nebula/engine.cc:70, :101): every per-tag GET is a label check, found or not (the vid does
    not tell the vertex's type, so the engine asks each tag), and the props found are vertex props. Any other step that
    reads the store but has no kind stops the run"""
    if op == "tag_probe":                                  # 1 GET per distinct vid; the found ones carry `props`
        e = dict.fromkeys(KINDS, 0)
        e["label_checks"] = st["G"]
        e["vertex_props"] = st["rows"] * len([p for p in (x.get("props") or []) if p != "id"])
        return e
    if op == "all_tag_props":                              # 1 GET per listed tag per vertex; a vertex has one tag
        e = dict.fromkeys(KINDS, 0)
        e["label_checks"] = st["G"]
        e["vertex_props"] = sum(asked[r["_tag"]] for r in own_rows if r.get("_tag"))   # the props asked of its tag
        return e
    e = census.elements(op, x, int(st.get("rows_in") or 0), int(st["rows"]), int(st.get("keys_crossed") or 0))
    if st["S"] + st["G"] + st["N"] and not any(e[k] for k in KINDS):
        raise SystemExit(f"engine_profile_check: the twin step {x['name']} ({op}) reads the store and has no kind")
    return e


def twin_census(sf, queries, params, profile, plans, ids, out_dir):
    """NebulaGraph's plan as the evaluator's engine twin (engine_fidelity; its S / G / N equal the live engine's), run on
    the C++ nebula_engine store and counted with twin_elements: {(q, i): kinds}, its step rows. Gate: the twin's S / G / N
    equal the capture's sim_S / sim_G / sim_N (the plan the snapshot validated). An all_tag_props step's own rows come
    from a second run of the plan up to that step (not counted in the gate)"""
    from kvengines import live_workloads as W   # needs the engines extra (just setup)
    from kvevaluator.capture import binary, store_identity
    from kvevaluator.measure import server, stores
    from kvevaluator.plans.ir.serialize import plan_to_ir
    prov = binary.binary_provenance()
    print(f"[twin] binary {prov['path']} commit {prov['binary_git_sha']} dirty {prov['binary_dirty']}")
    if prov["binary_dirty"]:   # warned and marked in <out>/stale.jsonl (kvevaluator.stale); the run goes on
        stale.note_into(out_dir, "binary_unclean", f"the binary {str(prov['binary_git_sha'])[:10]} is dirty "
                                                   f"(its dirty flag {prov['binary_dirty']!r})")
    sim = {(r["query"], int(r["param_idx"])): r for r in csv.DictReader(open(os.path.join(profile, "engine_validation.csv")))
           if r["db"] == "nebula"}
    efmt = store_identity.CPP_ENGINE_FORMATS["nebula"]
    schema_tags = [v["label"] for v in json.load(open(schema_path("ldbc")))["vertices"]]
    out, steps = {}, []
    with server.CppServer(stores.ensure_store("ldbc", sf, efmt, chunk=None), efmt) as srv:
        def run(plan, name):
            resp = srv.run(plan_to_ir(plan, name=name), {})
            if resp.get("error"):
                raise SystemExit(f"engine_profile_check: twin {name}: {resp['error']}")
            return resp
        for q in queries:
            for i in params:
                plan = W.engine_plan("interactive", q, "nebula", W.plan_args("interactive", sf, q, i))
                ir = plan_to_ir(plan, name=f"{q}-nebula-engine")
                resp = run(plan, f"{q}-nebula-engine")
                by_name = {x["name"]: x for x in ir["steps"]}
                c, sgn = dict.fromkeys(KINDS, 0), [0, 0, 0]
                for k, st in enumerate(resp["steps"]):
                    x = by_name[st["name"]]
                    own, asked = [], None
                    if x["op"] == "all_tag_props":             # no tags = every tag of the space (engine.cc:242)
                        own = run(plan[:k + 1], f"{q}-prefix-{k}")["rows"]
                        asked = asked_props(plans[(q, i)][1], ids, x.get("tags") or schema_tags)
                    e = twin_elements(x["op"], x, st, own, asked)
                    for kind in KINDS:
                        c[kind] += e[kind]
                    sgn = [sgn[0] + st["S"], sgn[1] + st["G"], sgn[2] + st["N"]]
                    steps.append({"query": q, "param_idx": i, "step": st["name"], "op": x["op"],
                                  "rows_in": st.get("rows_in"), "rows": st["rows"], "S": st["S"], "G": st["G"],
                                  "N": st["N"], "G_miss": st.get("G_miss", ""), **{kind: e[kind] for kind in KINDS}})
                want = [int(sim[(q, i)][f"sim_{k}"]) for k in "SGN"]
                if sgn != want:
                    raise SystemExit(f"engine_profile_check: twin {q} p{i}: S/G/N {sgn}, the capture's {want}")
                out[(q, i)] = c
    print(f"[twin] {len(out)} (query, param) twins: S / G / N equal the capture's sim counts on all")
    return out, steps


def exp_path(p):
    """a path as the experiment-relative one a reader can open in any tree (output/..., not this tree's absolute path)"""
    full = os.path.realpath(p)
    mark = os.sep + os.path.basename(EXP) + os.sep
    return full.split(mark, 1)[1] if mark in full else full


def share_row(side, q, params, counts, extra):
    tot = sum(counts[k] for k in KINDS)
    row = {"query": q, "side": side, "params": params}
    row.update({k: repr(counts[k]) for k in KINDS})
    row["total"] = repr(tot)
    row.update({f"share_{k}": f"{counts[k] / tot:.4f}" if tot else "" for k in KINDS})
    row["dominant"] = max(KINDS, key=lambda k: counts[k]) if tot else ""
    row.update(extra)
    return row


def census_side(grid, queries, params):
    """(chosen body per query, {(q, i): kinds} at `params`, the census rows over params 1-5). The census's own pass
    (census.REPORTED, params 1-5) picks the body; a second pass at `params` gives their counts (census.py reads only
    the params in census.REPORTED, so the pass sets it)"""
    cells = census.grid_cells(grid, "interactive")
    crows = census.census_rows("interactive", cells, census.read_steps(cells), None, [])
    chosen = {r["query"]: r["body"] for r in crows if r["chosen"]}
    missing = [q for q in queries if q not in chosen]
    if missing:
        raise SystemExit(f"engine_profile_check: no census for {missing} in {grid}")
    reported, census.REPORTED = census.REPORTED, {str(i) for i in params}
    try:
        step_rows = []
        census.census_rows("interactive", cells, census.read_steps(cells), None, step_rows)
    finally:
        census.REPORTED = reported
    ours = defaultdict(lambda: dict.fromkeys(KINDS, 0.0))
    for s in step_rows:
        if s["query"] in queries and s["body"] == chosen[s["query"]]:
            for k in KINDS:
                ours[(s["query"], int(s["param_idx"]))][k] += float(s[k])
    absent = [(q, i) for q in queries for i in params if (q, i) not in ours]
    if absent:
        raise SystemExit(f"engine_profile_check: the slice has no step rows for {absent}")
    return chosen, ours, crows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--grid", required=True, help="a slice's captures/ldbc/<scale> dir at --sf (steps on the "
                                                  "nine cells, params 1-5 and --params)")
    ap.add_argument("--profile", required=True, help="an engine-fidelity capture's ldbc dir (nebula/<q>/p<i>/plan.json)")
    ap.add_argument("--sf3-census", required=True, help="the sf3 census.csv (the chosen bodies, params 1-5)")
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--queries", default="ic4,ic5,ic9,ic12,is1")
    ap.add_argument("--params", default="1,7", help="the engine-fidelity suite params (its holdout is 2, 8, 14, ...)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    queries, params = a.queries.split(","), [int(p) for p in a.params.split(",")]
    os.makedirs(a.out, exist_ok=True)
    stale.start(a.out)   # an earlier run's notes moved aside: <out>/stale.jsonl holds this run's only
    check_params(a.sf, queries, params, a.profile)
    chosen, ours, crows = census_side(a.grid, queries, params)

    # NebulaGraph's PROFILE rows: the plan's shape (operators, props asked, rows); the counts are rough (README)
    plans = {(q, i): read_profile(os.path.join(a.profile, "nebula", q, f"p{i}", "plan.json")) for q in queries for i in params}
    every = [read_profile(os.path.join(a.profile, "nebula", q, p, "plan.json"))[1]
             for q in sorted(os.listdir(os.path.join(a.profile, "nebula")))
             for p in sorted(os.listdir(os.path.join(a.profile, "nebula", q)))]
    ids = tag_ids(every)
    print(f"[tags] {len(ids)} tag names resolved from {len(every)} plans: {dict(sorted(ids.items()))}")
    twin, twin_steps = twin_census(a.sf, queries, params, a.profile, plans, ids, a.out)
    prof, node_rows, live = defaultdict(lambda: dict.fromkeys(KINDS, 0)), [], {}
    for (q, i), (d, nodes, rows) in sorted(plans.items()):
        for n in sorted(nodes, key=lambda n: n["id"]):
            if n["name"] not in STORAGE:
                continue
            e = node_kinds(n, rows, ids)
            for k in KINDS:
                prof[(q, i)][k] += e[k]
            node_rows.append({"query": q, "param_idx": i, "node": n["id"], "operator": n["name"], "rows": rows[n["id"]],
                              "rows_in": sum(rows[x] for x in n["dependencies"]), **{k: e[k] for k in KINDS},
                              "vertex_props_lo": e["vertex_props_lo"], "vertex_props_hi": e["vertex_props_hi"],
                              "approx": e["approx"], "rule": e["rule"]})
        kv = json.load(open(os.path.join(a.profile, "nebula", q, f"p{i}", "kvstats.json")))["raw"]
        live[(q, i)] = {"live_get": kv["get_count"], "live_get_found": kv["get_found_count"],
                        "live_prefix": kv["prefix_count"], "live_next": kv["iterator_next_count"]}

    src = exp_path(a.profile)
    shares, plabel = [], "mean " + ",".join(map(str, params))
    blank = dict.fromkeys(("live_get", "live_get_found", "live_prefix", "live_next"), "")
    mean = lambda by, q: {k: sum(by[(q, i)][k] for i in params) / len(params) for k in KINDS}
    for q in queries:
        lv = {k: repr(sum(live[(q, i)][k] for i in params) / len(params)) for k in blank}
        shares.append(share_row("census", q, plabel, mean(ours, q),
                                {"plan": f"ours ({chosen[q]})", "source": exp_path(a.grid), **blank}))
        shares.append(share_row("nebula_twin", q, plabel, mean(twin, q),
                                {"plan": "NebulaGraph's, its engine twin (S/G/N = live)", "source": src, **lv}))
        shares.append(share_row("nebula_profile_rows", q, plabel, mean(prof, q),
                                {"plan": "NebulaGraph's, PROFILE rows (rough: README)", "source": src, **lv}))

    # the census at the SF against sf3: params as compared, params 1-5, and sf3's params 1-5
    sf3 = {r["query"]: r for r in csv.DictReader(open(a.sf3_census)) if r["suite"] == "interactive" and r["chosen"] == "1"}
    five = {r["query"]: r for r in crows if r["chosen"]}
    scale = []
    for q in queries:
        views = {f"sf{a.sf} {plabel}": mean(ours, q),
                 f"sf{a.sf} params 1-5": {k: float(five[q][k]) for k in KINDS},
                 "sf3 params 1-5": {k: float(sf3[q][k]) for k in KINDS}}
        sh = {v: {k: c[k] / sum(c.values()) for k in KINDS} for v, c in views.items()}
        for v, c in views.items():
            scale.append({"query": q, "view": v, "body": sf3[q]["body"] if v.startswith("sf3") else chosen[q],
                          **{k: repr(c[k]) for k in KINDS}, **{f"share_{k}": f"{sh[v][k]:.4f}" for k in KINDS},
                          "max_share_gap_to_sf3": f"{max(abs(sh[v][k] - sh['sf3 params 1-5'][k]) for k in KINDS):.4f}"})

    for name, rows in (("shares.csv", shares), ("twin_steps.csv", twin_steps), ("profile_nodes.csv", node_rows),
                       ("shares_by_scale.csv", scale)):
        census.write(os.path.join(a.out, name), rows)
    print(f"[cells] {len(queries)} queries x {len(params)} params; {len(twin_steps)} twin steps; {len(node_rows)} "
          f"NebulaGraph storage operators; PROFILE read in place from {src}")
    for q in queries:
        c, t, p = (r for r in shares if r["query"] == q)
        print(f"  {q}: census {c['dominant']} {float(c['share_' + c['dominant']]):.0%}; twin {t['dominant']} "
              f"{float(t['share_' + t['dominant']]):.0%} -> {'same' if t['dominant'] == c['dominant'] else 'DIFFERENT'}"
              f" (PROFILE rows: {p['dominant']})")


if __name__ == "__main__":
    main()
