#!/usr/bin/env python3
"""The logical access census: per query, the elements touched by its cheapest plan body that both Dgraph and
NebulaGraph can run, the same on every uniform layout (edges and edge properties, each by access path: with a list
expansion or by a direct edge probe; label checks, vertex properties, index entries crossed, items written). From the
step rows of the sf3 captures (each step's rows in and rows out) and the plans (the properties a step asks for);
analysis only.

    uv run --locked python experiments/logical_census/census.py --ldbc-grid <captures/ldbc/sf3 dir> --bi-grid <the same, BI's> \
        --out DIR [--ldbc-chain <chain dir>] [--bi-chain <chain dir>] [--preview-grid <captures/ldbc/<scale> dir>]
        -> <out>/census_steps.csv   per (suite, query, body, param, step): the operator, rows in / out, the kinds
           <out>/census.csv         per (suite, query, body): the mean over the reported params; chosen = the body drawn
           <out>/census_totals.csv  per workload (I01, I02, BI once): the frequency-weighted sum of the chosen bodies
Gates: every step key is on every cell the grid holds, with equal rows (the counts do not depend on the layout): the
uniform layouts of the grid's and the chunk-size spec sets (grid_chunked_<k>) the capture holds; the
three diagonals are required, the six others are read when measured (a grid measured on the diagonals composes them); every
operator the plans cost maps to a kind, the part read_parts.PART gives it; a body renamed since the capture resolves by
body_sha to one HEAD body; with --preview-grid, the overlaid grid has the preview's keys and rows.
"""
import argparse
import csv
import functools
import json
import os
import sys
from collections import Counter, defaultdict

from kvevaluator import stale
from kvevaluator.layouts import cell_id
from kvevaluator.datasets import schema_path
from kvevaluator.layouts import SETTINGS, SIZE_DIRS, SPECS, settings_of
from kvevaluator.capture.spec_sets import spec_cells
from kvevaluator.output_tree import Capture, capture_in, writes_in
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plans.ir.capabilities import VALIDITY_CAPS, validity_caps
from kvevaluator.plans.units import costed_ops
from kvcost.layout_cost.compose import BI_DRIFT_OK   # a body changed since the capture whose counts are known equal
from kvcost.layout_cost import benchmarks   # I01 / I02: LDBC's frequencies
from kvcost.layout_cost.workloads import ldbc as ldbc_mix
from kvevaluator.capture import capture_layout

csv.field_size_limit(1 << 30)
PLANS = {"interactive": C.plans_dir("interactive"), "bi": C.plans_dir("bi"),
         "writes": os.path.join(C.PLANS_ROOT, "writes", "ldbc", "plans")}
CELLS = [cell_id(p, a) for p in SETTINGS for a in SETTINGS]
SP_CO = "props_split_adj_condensed"
REPORTED = {"1", "2", "3", "4", "5"}
# The census counts the plans Dgraph and NebulaGraph can both run: a body counts when its validity
# caps (planir/capabilities.validity_caps) are within what both engines' formats give on every LDBC label, Dgraph at its
# 512 KB chunk (capabilities.caps_resolver, the store's own caps included). Computed at run time from the formats' code,
# so it follows them: on the code it is {property_index, type_index}. Every body's elements are the same on
# all nine cells; a selector cap (whole_row_read, ...) only says where a body is preferred.
ENGINES = ("dgraph-c512k", "nebula")


@functools.cache
def engine_caps():
    """the validity caps every LDBC label has on every store in ENGINES"""
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
    from kvevaluator.plan_choice.optimizer.arms import resolver_of
    out = set(VALIDITY_CAPS)
    for name in ENGINES:
        of = resolver_of(name, LDBC_SCHEMA)
        for label in sorted(LDBC_SCHEMA.vertex_props):
            out &= set(of(label))
    return frozenset(out)
# the bar's kinds (elements), bottom-up, and the extra counts kept beside them. Edges and edge properties by access path:
# read with a list expansion (ADJ, WALKS) or by a direct probe of one edge (PROBE). A label check is its own kind: on every
# layout here it is answered from the edge entry already read (rows_in_labelled == rows_in), not from the vertex
KINDS = ("edges_list", "edges_probe", "edge_props_list", "edge_props_probe", "label_checks", "vertex_props",
         "index_entries", "items_written")
DERIVED = ("edges", "edge_props")                            # the sums over both paths (the census before the split)
EXTRA = ("lists", "vertices", "edge_prop_edges", "written_vertices", "written_edges", "written_props",
         "written_vertex_props", "written_edge_props",   # written_props split by what carries them
         "written_edge_entries", "written_index_entries")  # what the schema stores per write (below)
SCHEMA = schema_path("ldbc")
SP_SP = "props_split_adj_split"
ADJ = {"read_neighbors"}
WALKS = {"read_closure", "read_chase"}                       # rows out = vertices reached: lists expanded ~ that (approx)
PROBE = {"read_edge_exists"}                                 # one edge per input pair, looked up directly
LABEL_CHECKS = {"read_filter_by_label_probe", "read_filter_by_label_index"}
INDEX = {"read_index_eq", "read_index_range", "index_range_buckets"}
# the ops that walk an index: their entries are the keys the walk crosses (a top-k skips non-candidates; a label scan
# crosses the label's whole list), gated equal on the nine layouts
CROSSES = INDEX | {"read_top_k_by_index", "read_vertices_by_label"}


def n_props(step):
    """the properties a step reads; the id is the vertex's key, not a stored property"""
    return len([p for p in (step.get("props") or []) if p != "id"])


def elements(op, step, rows_in, rows, crossed):
    """one step's logical elements: {kind or extra: count, "approx": 0/1}. rows_in counts rows from earlier steps only
    (cpp/kvevaluator/src/ir/plan.cc:382-408); a literal input counts 0, so the input is then the output. crossed = the
    index keys the step walks (keys_crossed)"""
    e = dict.fromkeys(KINDS + DERIVED + EXTRA + ("approx",), 0)
    n_in = rows_in if rows_in > 0 else rows
    props = n_props(step)
    n_ep = len(step.get("edge_props") or [])
    if op in ADJ:                                            # a list per input vertex, an edge per output row
        e["lists"], e["edges_list"] = n_in, rows
        if n_ep:
            e["edge_prop_edges"], e["edge_props_list"] = rows, rows * n_ep
    elif op in PROBE:                                        # one probe of one edge per input pair
        e["lists"], e["edges_probe"] = n_in, n_in
        if n_ep:                                             # no plan asks a probe for edge props today (0 everywhere)
            e["edge_prop_edges"], e["edge_props_probe"] = n_in, n_in * n_ep
    elif op in WALKS:
        e["lists"], e["edges_list"], e["approx"] = rows, rows, 1
    elif op == "read_vertex_props":                          # a vertex counts only when a stored property is read:
        e["vertices"], e["vertex_props"] = (n_in if props else 0), n_in * props   # an id-only ask reads no item
    elif op == "read_vertex_by_id":                          # the vertex itself (its label): one element
        e["vertices"], e["vertex_props"] = rows, rows
    elif op == "read_anchor_props":
        e["vertices"], e["vertex_props"] = rows, rows * max(props, 1)
    elif op in LABEL_CHECKS:                                 # one label check per input vertex, on either body
        e["label_checks"] = n_in
    elif op in INDEX:                                        # an index scan: the entries it crosses
        e["index_entries"] = crossed
    elif op in ("read_top_k_by_index", "read_vertices_by_label"):     # the entries crossed, then the kept rows' props
        e["index_entries"], e["vertices"], e["vertex_props"] = crossed, (rows if props else 0), rows * props
    elif op in costed_ops():
        raise SystemExit(f"census: the costed operator {op} has no kind (experiments/logical_census/census.py)")
    e["edges"], e["edge_props"] = e["edges_list"] + e["edges_probe"], e["edge_props_list"] + e["edge_props_probe"]
    return e


def agrees_with_read_parts():
    """the gate: each operator counts the kind read_parts.PART puts it on (adjacency = edges); the one difference is
    the label checks, a kind of their own here (read_parts puts the probe on vertex props, the index check on index)"""
    from kvevaluator.capture.step_record import PART   # read_parts.PART: the operator -> part map, one home
    first = {"adjacency": "edges", "vertex_props": "vertex_props", "index": "index_entries"}
    bad = []
    for op, part in sorted(PART.items()):
        e = elements(op, {"props": ["p"], "edge_props": []}, 2, 3, 4)
        if not e["label_checks" if op in LABEL_CHECKS else first[part]]:
            bad.append((op, part, [k for k in KINDS if e[k]]))
    print(f"[mapping] {len(PART)} operators shared with read_parts.PART; {len(bad)} disagree")
    if bad:
        raise SystemExit(f"census: the kinds disagree with read_parts.PART: {bad}")


def plan_file(suite, name):
    with open(os.path.join(PLANS[suite], name + ".json")) as f:
        return json.load(f)


def body_of(suite, query, variant):
    """(steps, requires) of a captured body: '<file>:<body>', a single-body file named by the variant, or a body of the
    query's own bundle"""
    f, b = variant.split(":", 1) if ":" in variant else (variant, None)
    if b is None and not os.path.exists(os.path.join(PLANS[suite], f + ".json")):
        f, b = query, variant
    d = plan_file(suite, f)
    if b is None and variant in d.get("plans", {}):      # a generated file: the body named like its file is under plans
        b = variant
    steps = d["plans"][b]["steps"] if b is not None and "plans" in d and b in d["plans"] else d.get("steps")
    if steps is None:
        raise SystemExit(f"census: {suite} {query}: body {variant} not in the plans")
    if b is None:                                            # a single-body file: its own requires
        return steps, set(d.get("requires", []))
    if "bodies" in d:                                        # a generated file: the body's own requirements
        return steps, set((d["bodies"].get(b) or {}).get("requires", {}))
    req = [v.get("requires", []) for v in d.get("variants", []) if v.get("plan") == b]   # the file that holds the body
    if len(req) != 1:
        raise SystemExit(f"census: {suite} {query}: body {variant} has {len(req)} selectors in {f}.json")
    return steps, set(req[0])


def grid_cells(grid, workload):
    """{cell: its capture dir}: every layout of the grid's and the chunk-size spec sets with step rows in `workload`'s
    capture below `grid` (a captures/ldbc/<scale> dir). Refuses a grid without the three diagonals (Sp-Sp, Co-Co, Ch64k-Ch64k,
    by their settings)"""
    cap = capture_in(grid, workload)
    have = set(cap.stores("steps"))
    out = {c: cap.path for d in ("grid", *SIZE_DIRS.values()) for c in spec_cells(os.path.join(SPECS, d)) if c in have}
    missing = [s for s in SETTINGS if not any(settings_of(c) == (s, s) for c in out)]
    if missing:
        raise SystemExit(f"census: {grid} has no {', '.join(missing)} diagonal cell (step rows of {sorted(out)})")
    return out


def head_names(suite, cells):
    """({(query, captured variant): HEAD's name of that body}, {the same key: its head_body label where it is not the
    name}): the same name where HEAD has it (a body that changed since the capture is labelled with its captured sha
    and marked body_moved: its steps are HEAD's), else the HEAD
    candidate with the rows' body_sha (a body renamed since the capture, said on stderr); a body that matches no HEAD
    candidate refuses (no steps to read)"""
    shas = defaultdict(set)
    for c, d in cells.items():
        with open(Capture(d).path_of("counts", c)) as f:
            for r in capture_layout.reader(f):
                if r["ok"] == "1" and r["param_idx"] in REPORTED:
                    shas[(r["query"], r["body"])].add(r.get("body_sha") or "")
    out, labels = {}, {}
    for (q, v), s in sorted(shas.items()):
        # a body rewritten count-equal since the capture: its successor (candidates.successor_sha)
        head, s = C.candidate_bodies(suite, q), {C.successor_sha(suite, q, x) or "" for x in s}
        if v in head:
            if head[v] not in s and not (suite == "bi" and q in BI_DRIFT_OK):
                stale.note("body_moved", f"census: {suite} {q}: the captured body {v!r} ran sha {sorted(s)}, HEAD's is "
                           f"{head[v]}: counted with HEAD's steps, labelled with the captured sha")
                labels[(q, v)] = f"{v} (captured {','.join(sorted(s))}, moved since)"
            out[(q, v)] = v
            continue
        same = [n for n, sha in sorted(head.items()) if len(s) == 1 and sha in s]
        if not same:
            raise SystemExit(f"census: {suite} {q}: the captured body {v!r} (sha {sorted(s)}) matches no HEAD candidate: "
                             f"no body to read its steps from")
        print(f"[{suite}] {q}: the captured body {v!r} is HEAD's {same[0]!r} (the same body {next(iter(s))})",
              file=sys.stderr)
        out[(q, v)] = same[0]
    return out, labels


def read_steps(cells):
    """{cell: [step rows of the reported params]} for {cell: its capture dir} (grid_cells)"""
    out = {}
    for c, d in cells.items():
        with open(Capture(d).path_of("steps", c)) as f:
            out[c] = [r for r in capture_layout.reader(f) if r["param_idx"] in REPORTED]
    return out


def layout_free(suite, by_cell):
    """the gate: each (query, param, body, step) key on every cell, with the same operator, rows, rows in, step name
    and, for an index walk, keys crossed"""
    seen = defaultdict(dict)
    for c, rows in by_cell.items():
        for r in rows:
            crossed = r["keys_crossed"] if r["op"] in CROSSES else "0"
            seen[(r["query"], r["param_idx"], r["body"], r["step_idx"])][c] = (r["op"], r["rows"], r["rows_in"], r["step"],
                                                                                  crossed)
    partial = [k for k, v in seen.items() if len(v) != len(by_cell)]
    differ = [k for k, v in seen.items() if len(set(v.values())) > 1]
    print(f"[{suite}] layout-free: {len(seen)} (query, param, body, step) keys on {len(by_cell)} cells; {len(partial)} "
          f"missing on some cell, {len(differ)} differ in operator, rows, rows in, step name or keys crossed")
    if not seen or partial or differ:
        raise SystemExit(f"[{suite}] the step counts are not layout-free: e.g. {(partial + differ)[:3]}")
    first = next(iter(by_cell))         # every cell's values are equal (the gate above)
    return {k: v[first] for k, v in seen.items()}


def picked(chain_dir, family):
    """{query: (the body picked on most of the nine cells, how many)} from the chain's cost table (a tie: Sp-Co's);
    {} without a chain (then every query needs a body valid on both engines)"""
    if chain_dir is None:
        return {}
    per = defaultdict(dict)
    with open(os.path.join(chain_dir, "cost_table.csv")) as f:
        for r in capture_layout.reader(f):
            if r["family"] == family and r["param_set"] == "reported" and r["layout"] in CELLS:
                per[r["query"]][r["layout"]] = r["body"]
    out = {}
    for q, cells in per.items():
        n = Counter(cells.values())
        top = max(n.values())
        out[q] = (cells[SP_CO] if n[cells[SP_CO]] == top else min(b for b, k in n.items() if k == top), top)
    return out


def census_rows(suite, cells, by_cell, chain_dir, step_rows):
    """per (query, body): the mean over the reported params; the chosen body by the rule"""
    keys = layout_free(suite, by_cell)
    names, labels = head_names(suite, cells)
    sums = defaultdict(lambda: defaultdict(float))
    params = defaultdict(set)
    bodies = {}
    for (q, p, v, si), (op, rows, rows_in, name, crossed) in sorted(keys.items()):
        if (q, v) not in bodies:
            bodies[(q, v)] = body_of(suite, q, names[(q, v)])
        steps, _ = bodies[(q, v)]
        step = next((s for s in steps if s.get("name") == name), None)
        if step is None:
            raise SystemExit(f"census: {suite} {q} {v}: step {name} not in the body")
        e = elements(op, step, int(rows_in or 0), int(rows), int(crossed))
        step_rows.append({"suite": suite, "query": q, "body": v, "param_idx": p, "step_idx": si, "step": name, "op": op,
                          "rows_in": rows_in, "rows": rows, "keys_crossed": crossed, "n_props": n_props(step),
                          "n_edge_props": len(step.get("edge_props") or []), **e})
        for k, x in e.items():
            sums[(q, v)][k] = max(sums[(q, v)][k], x) if k == "approx" else sums[(q, v)][k] + x
        params[(q, v)].add(p)
    pick = picked(chain_dir, suite)
    out = []
    for (q, v), s in sorted(sums.items()):
        n = len(params[(q, v)])
        free = validity_caps(bodies[(q, v)][1]) <= engine_caps()
        out.append({"suite": suite, "query": q, "body": v, "head_body": labels.get((q, v), names[(q, v)]), "valid_on_both_engines": int(free),
                    "picked_on_cells": pick[q][1] if pick.get(q, ("",))[0] == v else 0, "n_params": n,
                    **{k: repr(s[k] / n) for k in KINDS + DERIVED + EXTRA}, "approx": int(s["approx"])})
    for q in sorted({r["query"] for r in out}):
        rows = [r for r in out if r["query"] == q]
        free = [r for r in rows if r["valid_on_both_engines"]]
        major = [r for r in rows if r["picked_on_cells"]]
        # several bodies are valid on both engines: the one touching the fewest elements
        if free:
            chosen = min(free, key=lambda r: (sum(float(r[k]) for k in KINDS), r["body"]))
            rule = "valid on both Dgraph and NebulaGraph" + (f", the fewest elements of {len(free)}" if len(free) > 1 else "")
        elif major:
            chosen, rule = major[0], f"picked on {major[0]['picked_on_cells']} of 9 cells"
        else:
            raise SystemExit(f"census: {suite} {q}: no body is valid on both Dgraph and NebulaGraph, and no chain's pick was captured "
                             f"(give --{suite}-chain)")
        for r in rows:
            r["chosen"], r["rule"] = int(r is chosen), rule if r is chosen else ""
    return out


def schema_rules():
    """(declared directions per edge type, index entries a new vertex adds per label), from the stores' schema.
    An index entry per indexed property of the label; a label with no property index has one type-index entry
    instead (cpp/kvevaluator/src/index/sorted_index.cc, SortedIndex::vertex_write_keys: the prop index enumerates it)"""
    d = json.load(open(SCHEMA))
    dirs = {e["etype"]: 2 if e["reverse"] else 1 for e in d["edges"]}
    prop_ix = Counter(i["label"] for i in d["indexes"] if i["kind"] != "type")
    return dirs, {v["label"]: prop_ix[v["label"]] or 1 for v in d["vertices"]}


def writes_rows():
    """the updates (IU1-8): the items each writes, from its plan (fixed inserts: the same on every param and layout).
    items_written = what the schema stores: an edge entry per declared direction (its props inside it), a vertex's
    prop values (the vertex has no item of its own), the index entries the vertex adds. The plan's logical counts
    (written_vertices, written_edges, written_props) are kept beside them"""
    dirs, index_entries = schema_rules()
    out = []
    for name in sorted(os.listdir(PLANS["writes"]), key=lambda n: int(n[2:-5])):
        d = plan_file("writes", name[:-5])
        e = dict.fromkeys(KINDS + DERIVED + EXTRA, 0)
        for s in d["steps"]:
            n_props = sum(v is not None for v in (s.get("props") or {}).values())
            if s["op"] == "insert_vertex":
                e["written_vertices"] += 1
                e["written_vertex_props"] += n_props
                e["written_index_entries"] += index_entries[s["label"]]
            elif s["op"] == "insert_edge":
                e["written_edges"] += 1
                e["written_edge_props"] += n_props
                e["written_edge_entries"] += dirs[s["etype"]]
            elif s["op"].startswith(("insert", "update", "delete")):
                raise SystemExit(f"census: {name} {s['op']}: a write operator the census does not count")
            e["written_props"] += n_props
        e["items_written"] = e["written_edge_entries"] + e["written_vertex_props"] + e["written_index_entries"]
        assert e["written_props"] == e["written_vertex_props"] + e["written_edge_props"], name
        out.append({"suite": "writes", "query": d["plan"], "body": d["plan"], "head_body": d["plan"], "valid_on_both_engines": 1,
                    "picked_on_cells": "", "n_params": "", **{k: repr(float(v)) for k, v in e.items()}, "approx": 0,
                    "chosen": 1, "rule": "the update's own plan"})
    return out


def check_writes_vs_split(grid, rows):
    """gate: per update and part, the census' stored items equal Sp-Sp's items written (P + D, the mean over params):
    Split stores one item per element, so the two must agree"""
    path = writes_in(grid, "ldbc").path_of("parts", SP_SP)
    got, params = defaultdict(float), defaultdict(set)
    for r in capture_layout.write_rows(path, cell=SP_SP)[2]:
        if r["query"].startswith("iu"):
            got[(r["query"], r["part"].split(":")[0])] += float(r["P"]) + float(r["D"])
            params[r["query"]].add(r["param_idx"])
    bad, n = [], 0
    for r in rows:
        q = r["query"]
        want = {"adj": float(r["written_edge_entries"]), "props": float(r["written_vertex_props"]),
                "index": float(r["written_index_entries"])}
        for part, w in want.items():
            n += 1
            have = got.get((q, part), 0.0) / max(1, len(params[q]))
            if abs(have - w) > 1e-9:
                bad.append(f"{q} {part}: census {w:g}, Sp-Sp {have:g}")
    print(f"[writes] census stored items vs Sp-Sp items written: {n} (update, part) pairs, {len(bad)} differ")
    if bad or not n:
        raise SystemExit("census: " + ("; ".join(bad) if bad else "no updates compared"))


def totals(rows):
    """per workload: I01 (LDBC Interactive's frequencies, reads and updates), I02 (its reads), every query once
    (Interactive: IC + IS + IU; BI): count x the chosen body, in elements; every query equal (Interactive, BI): each
    query's shares of its own elements, averaged over the queries (no query dominates), as shares"""
    chosen = {(r["suite"], r["query"]): r for r in rows if r["chosen"]}
    interactive = [(s, q) for s, q in sorted(chosen) if s in ("interactive", "writes")]
    bi = [(s, q) for s, q in sorted(chosen) if s == "bi"]
    # the old module name stays: this text lands in census_totals.csv, so the census snapshot stays byte-identical;
    # the next census refresh names the module layout_pricing/workloads/ldbc.py
    per = f"LDBC's frequencies, runs per {ldbc_mix.PER:,} updates (workloads_ldbc_mix)"
    by_id = {w["id"]: w for w in benchmarks.workloads("ldbc", "3").values()}
    sets = {"i01": ([(f, q, n) for f, q, n, _ in by_id["i01"]["items"]], per),
            "i02": ([(f, q, n) for f, q, n, _ in by_id["i02"]["items"]], per),
            "ldbc_once": ([(f, q, 1) for f, q in interactive], "each query once (IC, IS and the updates IU)"),
            "bi_once": ([(f, q, 1) for f, q in bi], "each query once")}
    out = []
    for wl, (items, weights) in sets.items():
        missing = [(f, q) for f, q, _ in items if (f, q) not in chosen]
        if missing:
            raise SystemExit(f"census: {wl}: no census for {missing}")
        t = {k: sum(n * float(chosen[(f, q)][k]) for f, q, n in sorted(items)) for k in KINDS + DERIVED + EXTRA}
        out.append({"workload": wl, "measure": "elements", "n_queries": len(items), "n_runs": sum(n for *_, n in items),
                    **{k: repr(v) for k, v in t.items()}, "weights": weights})
    for wl, keys, weights in (("ldbc_equal", interactive, "each query equal (IC, IS and the updates IU): its shares, averaged"),
                              ("bi_equal", bi, "each query equal: its shares, averaged")):
        tot = {fq: sum(float(chosen[fq][k]) for k in KINDS) for fq in keys}
        sh = {k: sum(float(chosen[fq][k]) / tot[fq] for fq in keys) / len(keys) for k in KINDS + DERIVED + EXTRA}
        out.append({"workload": wl, "measure": "mean share", "n_queries": len(keys), "n_runs": "",
                    **{k: repr(v) for k, v in sh.items()}, "weights": weights})
    for r in out:                                            # the split keeps the census before it: its sums
        for d, parts in (("edges", ("edges_list", "edges_probe")), ("edge_props", ("edge_props_list", "edge_props_probe"))):
            if abs(float(r[d]) - sum(float(r[k]) for k in parts)) > 1e-9 * max(1.0, float(r[d])):
                raise SystemExit(f"census: {r['workload']}: {d} is not the sum of {parts}")
    return out


def write(path, rows):
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    print(f"-> {path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ldbc-grid", required=True, help="the LDBC captures: a captures/ldbc/<scale> dir")
    ap.add_argument("--bi-grid", required=True, help="the dir holding BI's capture: a captures/ldbc/<scale> dir")
    ap.add_argument("--ldbc-chain", default=None, help="an LDBC chain dir: cost_table.csv (the picks; only for a query "
                    "with no body valid on both Dgraph and NebulaGraph)")
    ap.add_argument("--bi-chain", default=None, help="a BI chain dir: the same, for BI")
    ap.add_argument("--preview-grid", default=None, help="a captures/ldbc/<scale> dir whose step keys and rows must equal --ldbc-grid's")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    agrees_with_read_parts()
    ldbc_cells, bi_cells = grid_cells(a.ldbc_grid, "interactive"), grid_cells(a.bi_grid, "bi")
    ldbc = read_steps(ldbc_cells)
    if a.preview_grid:
        prev = read_steps(grid_cells(a.preview_grid, "interactive"))
        key = lambda r: (r["query"], r["param_idx"], r["body"], r["step_idx"], r["op"], r["rows"], r["rows_in"])
        n = sum(len(v) for v in ldbc.values())
        same = set(prev) == set(ldbc) and all(sorted(map(key, ldbc[c])) == sorted(map(key, prev[c])) for c in ldbc)
        print(f"[ldbc] the overlaid grid vs the preview: {n} step rows on {len(ldbc)} cells, {'equal' if same else 'DIFFER'}")
        if not same:
            raise SystemExit("census: the overlaid grid's step rows differ from the preview's")
    steps = []
    rows = (census_rows("interactive", ldbc_cells, ldbc, a.ldbc_chain, steps)
            + census_rows("bi", bi_cells, read_steps(bi_cells), a.bi_chain, steps))
    wrows = writes_rows()
    check_writes_vs_split(a.ldbc_grid, wrows)
    rows += wrows
    tot = totals(rows)
    write(os.path.join(a.out, "census_steps.csv"), steps)
    write(os.path.join(a.out, "census.csv"), rows)
    write(os.path.join(a.out, "census_totals.csv"), tot)
    for r in tot:
        fmt = (lambda v: f"{v:,.0f}") if r["measure"] == "elements" else (lambda v: f"{v:.4f}")
        print(f"  {r['workload']:10} {r['measure']:10} " + "  ".join(f"{k} {fmt(float(r[k]))}" for k in KINDS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
