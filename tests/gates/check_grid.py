#!/usr/bin/env python3
"""Cross-cell check of the design-space grid, over the variant sweep (grid_bodies.py) with each cell's
plan picked by python/kvcost/layout_cost/picks.py (the cheapest variant whose rows match the planner's pick).

It must hold for the grid to mean what the paper says it means:

1. COVERAGE: every cell has a chosen plan for every (query, param) -- a missing cell fails, never skips.
   A query's params are the ones the workload gives it (the first --nparam; fewer only when its param
   family has fewer, e.g. the no-param microbench queries run once).
2. ANSWERS: every cell returns the same rows (result hash) on every (query, param).
3. DECOMPOSITION: a step's cost depends only on its own component's setting. A vertex-prop step costs
   the same in every cell with the same vertex-prop setting; an adjacency step the same in every cell
   with the same adjacency setting; an index step the same everywhere (the index is fixed). Compared on
   S/G/N, the miss counts and every read-byte channel, matched by (query, param, step name, op) and only
   where both cells' step saw the same rows. Two cells are compared UNDER ONE PLAN: the picked plan where both
   picked the same one, else a plan both ran (the first cell's pick, then the second's, then the first by name):
   a step's count may depend on the ORDER of its input rows (the composition proof's C3), and
   two plans may hand the same rows on in another order, so a step is never compared across two plans. No plan
   in common, or other rows, leaves a step "not comparable" -- counted, not passed. A positive control flips one
   compared value and must see the break, so the check cannot pass vacuously; a second one does it in a pair of
   cells with different picks, where the capture holds one.

It also lists where the cheapest plan differs from the planner's pick (the B1 cases) and every variant
rejected for a cell (did not run / different rows).

--spec-dir DIR checks another spec set (e.g. the per-type mixes, inputs/specs/mixes) captured by
grid_bodies.py --spec-dir: 1 (coverage) on its cells, and 2 (answers) on its cells TOGETHER with the grid's
nine (a mix must return what every uniform cell returns; the grid must be measured). 3 is skipped: the
decomposition compares cells by their uniform (props, adjacency) settings, which a mix does not have.
A set of UNIFORM layouts (inputs/specs/grid_chunked_4k) takes --decompose: 3 runs over its cells and the reference grid
cells together, the settings read from the spec files; --grid-cells a,b names the grid cells that are its reference
(default: the nine), e.g. the four with no Chunked side.

  python3 tests/gates/check_grid.py --sf 0.1 [--family interactive|microbench|bi|linkbench] [--nparam 5] [--spec-dir DIR]
  (linkbench: --sf is its scale tag, --param-set its params)
Exit 1 on any failure.
"""
import argparse
import itertools
import json
import os
import sys
import time
from collections import defaultdict

from kvevaluator.measure.grid_capture import (GRID, WORKLOADS, candidate_drift, invalid_on_cell, load_runs,
                                              load_steps, query_params, workload_queries)
from kvevaluator.capture.spec_sets import is_grid, spec_cells
from kvevaluator.layouts import GRID_MEASURED
from kvcost.layout_cost.picks import PlanRule, choose_cell
from kvevaluator.plan_choice.candidates import capture_plan_pipeline, capture_tree
from kvevaluator.paths import ROOT
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture, Tree

EXP = str(ROOT)
PROPS_OPS = {"read_vertex_by_id", "read_vertex_props", "read_anchor_props", "read_filter_by_label_probe",
             "read_top_k_by_index"}   # top-k reads its winners' props through the props layout (the index is fixed)
ADJ_OPS = {"read_neighbors", "read_chase", "read_closure", "read_edge_exists"}
INDEX_OPS = {"read_index_eq", "read_index_range", "read_vertices_by_label", "read_filter_by_label_index"}
from kvevaluator.capture.step_record import READ_BYTE_LANES, READ_OPS  # (the lanes, one definition)
COST = (*READ_OPS, *READ_BYTE_LANES)


def axes(cell):
    p, a = cell[len("props_"):].split("_adj_")
    return p, a


def part_kind(part):
    """the component a store part belongs to, from its name (compose.unit_of's vocabulary): props:<label> -> props,
    adj:... / eprops:... -> adjacency, index:... -> index"""
    k = part.split(":")[0]
    return {"props": "props", "adj": "adjacency", "eprops": "adjacency", "index": "index"}.get(k)


def spec_axes(spec_path):
    """a cell's (props, adjacency) settings read from its spec file, for a set whose names do not carry them the grid's way"""
    spec = json.load(open(spec_path))
    return json.dumps(spec["vertex_props"], sort_keys=True), json.dumps(spec["adjacency"], sort_keys=True)


def decomposition(every, picked, cells, parts=None, axes=axes):
    """`every`: {cell: {(query, param): {plan: {(step, op): step row}}}}, every plan the cell ran; `picked`: {cell:
    {(query, param): its picked plan}}. A pair of cells is compared under ONE plan per (query, param): the picked one
    where both picked it, else a plan both ran. `parts`: {cell: {(query, param, plan, step): {part kinds}}} from the
    capture's parts CSV; a step is classified by the parts
    it READ when known (read_vertices_by_label with props walks the label's own props keys: a props step, not an index
    one -- the op map alone would put that label scan on the fixed index axis), else by its op.
    Returns the tally, the breaks, the unclassified ops, one compared-equal step (cell, (query, param), plan, (step, op)),
    one such step of a pair with DIFFERENT picks (or None), and how many steps were compared under a shared plan."""
    tally = defaultdict(lambda: [0, 0, 0])   # kind -> [equal, differ, not comparable]
    breaks, unclassified, equal_example, shared_example, shared = [], set(), None, None, 0
    for c1, c2 in itertools.combinations(cells, 2):
        (p1, a1), (p2, a2) = axes(c1), axes(c2)
        for qi, v1 in picked[c1].items():
            v2 = picked[c2].get(qi)
            ran1, ran2 = every[c1].get(qi, {}), every[c2].get(qi, {})
            common = [v for v in [v1, v2] + sorted(ran1) if v in ran1 and v in ran2]
            plan = common[0] if common else v1          # no plan in common: c1's pick, whose steps count as not comparable
            for (step, op), r1 in ran1.get(plan, {}).items():
                kinds = (parts or {}).get(c1, {}).get((qi[0], qi[1], plan, step))
                if kinds and len(kinds) == 1:
                    kind = next(iter(kinds))
                elif kinds and len(kinds) > 1:        # a step over several components (read_top_k_by_index: index + props):
                    kind = "+".join(sorted(kinds))    # its own class, compared where every component it reads is the same
                else:
                    kind = ("props" if op in PROPS_OPS else "adjacency" if op in ADJ_OPS else
                            "index" if op in INDEX_OPS else None)
                if kind is None:
                    unclassified.add(op)
                    continue
                if ("props" in kind and p1 != p2) or ("adjacency" in kind and a1 != a2):
                    continue                          # a different setting of a component this step reads
                r2 = ran2.get(plan, {}).get((step, op))
                if r2 is None or r2["rows"] != r1["rows"]:
                    tally[kind][2] += 1
                    continue
                k = (qi[0], qi[1], step, op)
                if all(r1[m] == r2[m] for m in COST):
                    tally[kind][0] += 1
                    equal_example = equal_example or (c1, qi, plan, (step, op))
                    if v1 != v2:
                        shared += 1
                        shared_example = shared_example or (c1, qi, plan, (step, op))
                else:
                    tally[kind][1] += 1
                    breaks.append((kind, c1, c2, k, plan, {m: (r1[m], r2[m]) for m in COST if r1[m] != r2[m]}))
    return tally, breaks, unclassified, equal_example, shared_example, shared


T0 = time.time()


def progress(what):
    """one line per stage, with the seconds since the start (a large capture takes minutes)"""
    print(f"[check_grid {time.time() - T0:4.0f}s] {what}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", required=True)
    ap.add_argument("--family", dest="workload", default="interactive", choices=WORKLOADS)
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--pick-by", dest="criterion", default=None, help="the plan pick rule (criteria.NAMED; default the optimizer's)")
    ap.add_argument("--in-sample", action="store_true", help="price a capture with no held-out set in-sample (a slice, a smoke, an old "
                    "copy; the plan rule (python/kvevaluator/plan_choice/candidates.py) needs the held-out set); every output then says 'in-sample'")
    ap.add_argument("--spec-dir", default=GRID, help="the spec set (default: the grid); another set is checked for "
                    "coverage and for the grid's answers, no decomposition")
    ap.add_argument("--grid-cells", default="", help="with --spec-dir: the grid cells used as the reference (default: the three diagonals a default grid run measures, layouts.GRID_MEASURED, and every other grid cell measured)")
    ap.add_argument("--decompose", action="store_true", help="with --spec-dir, a set of uniform layouts: run the decomposition "
                    "over its cells and the reference grid cells, the settings read from the spec files")
    ap.add_argument("--cells", default="", help="check only these cells of the set (comma list): a slice, said in the result; "
                    "without it a set with an unmeasured cell fails")
    ap.add_argument("--param-set", default=None, help="the param set the capture holds (LDBC Interactive: ldbc, the "
                    "default, or uniform)")
    ap.add_argument("--root", default=None, help="the output tree to read (default: the output dir)")
    a = ap.parse_args()
    rule = PlanRule(in_sample=True if a.in_sample else None)
    spec_dir = os.path.abspath(a.spec_dir)
    grid = is_grid(spec_dir)
    cap = Tree(a.root).capture(a.workload, a.sf, a.param_set)
    out_dir = cap.path
    QUERIES, _ = workload_queries(a.workload)
    # query -> the param indexes it must have (from the workload's params, never from the capture itself)
    expected = {q: [str(i) for i in range(1, len(query_params(a.workload, a.sf, q, a.nparam, a.param_set)) + 1)]
                for q in QUERIES}
    print(f"workload {a.workload} sf{a.sf}: {len(QUERIES)} queries, {sum(map(len, expected.values()))} (query, param)")
    cells = spec_cells(spec_dir)
    n_set = len(cells)
    if a.cells:
        named = [c for c in a.cells.split(",") if c]
        unknown = [c for c in named if c not in cells]
        if unknown:
            sys.exit(f"--cells: {unknown} are not cells of {spec_dir}")
        cells = [c for c in cells if c in named]
        print(f"SLICE: {len(cells)} of the set's {n_set} cells ({', '.join(cells)}); the other cells are not checked")
    # the verdict's coverage words: a slice never reads as the whole set
    coverage = f"a slice: {len(cells)} of {n_set} cells, full coverage on them" if a.cells else "full coverage"
    missing_cells = [c for c in cells if not os.path.exists(cap.path_of("counts", c))]
    fail = not cells
    if missing_cells:
        print("MISSING CELLS (not measured):", missing_cells, "(a tree measured on the seven diagonals holds the other "
              "layouts composed: run this check on a capture of all the set's cells, as kvcost measure's grid check does)")
        fail = True
        cells = [c for c in cells if c not in missing_cells]

    chosen = {}
    for i, c in enumerate(cells, 1):    # reads each cell's capture and picks its plans: most of the time on a large capture
        chosen[c] = choose_cell(out_dir, c, a.criterion, rule=rule, spec_path=os.path.join(spec_dir, f"{c}.json"))
        progress(f"picks: cell {i}/{len(cells)} {c}")
    # a non-grid spec set: its answers are checked against the grid's cells too (the reference answers)
    ref_cells = []
    if not grid:
        gcap, gdir = cap, out_dir   # every spec set's layouts sit in the one capture
        # the cells a default grid run measures (required), and every other grid cell the capture holds
        ref_cells = [c for c in spec_cells(GRID) if c in GRID_MEASURED or os.path.exists(gcap.path_of("counts", c))]
        if a.grid_cells:
            named = [c for c in a.grid_cells.split(",") if c]
            unknown = [c for c in named if c not in spec_cells(GRID)]
            if unknown:
                sys.exit(f"--grid-cells: {unknown} are not grid cells")
            ref_cells = named
        missing_ref = [c for c in ref_cells if not os.path.exists(gcap.path_of("counts", c))]
        if missing_ref:
            print("MISSING GRID CELLS (the reference answers; measure the grid first):", missing_ref)
            fail = True
            ref_cells = [c for c in ref_cells if c not in missing_ref]
        # the grid's capture must be of the same inputs (params + bodies fingerprint, nparam; the selection is per cell, cell_inputs) as the set's
        def prov(d, c):
            p = Capture(d).record(c)
            rec = json.load(open(p)) if os.path.exists(p) else {}
            return rec.get("inputs"), rec.get("nparam")
        want = {prov(out_dir, c) for c in cells}
        stale = [c for c in ref_cells if prov(gdir, c) not in want]
        if len(want) > 1 or stale:
            print(f"INPUTS DIFFER (re-measure): the set's captures {sorted(map(str, want))}; grid cells not matching: {stale}")
            fail = True
        for c in ref_cells:
            chosen["grid:" + c] = choose_cell(gdir, c, a.criterion, rule=rule)
        print(f"spec set {spec_dir}: {len(cells)} cells, answers checked against {len(ref_cells)} grid cells")
    answer_cells = cells + ["grid:" + c for c in ref_cells]
    progress("coverage")
    # 1. coverage
    holes = [(c, q) for c in cells for q in QUERIES
             if q not in chosen[c] or chosen[c][q]["body"] is None
             or any(i not in chosen[c][q]["rows"] for i in expected[q])]
    print(f"\ncells: {len(cells)}  coverage holes (cell, query with no valid plan on every param): {len(holes)}")
    for h in holes[:10]:
        print("  HOLE", h, chosen[h[0]].get(h[1], {}).get("rejected"))
    fail |= bool(holes)

    progress("validity")
    # 1b. validity: no chosen plan is a candidate that may answer wrong on its cell's layout (planir/capabilities.py
    # VALIDITY_CAPS); grid_bodies never measures one, but a capture from before that rule could hold it
    bad_valid, nv, invalid = [], 0, {}
    for c in cells:
        inv = invalid[c] = invalid_on_cell(a.workload, os.path.join(spec_dir, f"{c}.json"))   # 1c reuses it
        for q in QUERIES:
            if q in chosen[c] and chosen[c][q]["body"] is not None:
                nv += 1
                if chosen[c][q]["body"] in inv.get(q, {}):
                    bad_valid.append((c, q, chosen[c][q]["body"], inv[q][chosen[c][q]["body"]]))
    rules = sorted({d["rule"] for c in cells for d in chosen[c].values() if d.get("rule")})
    print(f"pick rule in force: {', '.join(rules) or 'none'} (the plan rule, python/kvevaluator/plan_choice/candidates.py: 'held-out ..., margin' is the registered rule; "
          f"'in-sample' = a capture without a held-out set; 'recorded' = the largest reported scale's decision)")
    print(f"validity: {nv - len(bad_valid)}/{nv} (cell, query) chosen plans hold their validity caps")
    for b in bad_valid[:10]:
        print("  INVALID PICK", b)
    fail |= bool(bad_valid) or nv == 0

    # 1c. the capture holds HEAD's candidate set (grid_capture.candidate_drift, both directions): a capture that
    # predates a plan file, or that ran a body HEAD now refuses on the cell, prices nothing here -- it fails
    nd = 0
    for i, c in enumerate(cells, 1):
        progress(f"candidate drift: cell {i}/{len(cells)} {c}")
        drift = candidate_drift(a.workload, os.path.join(spec_dir, f"{c}.json"), load_runs(out_dir, c), capture_tree(out_dir, c),
                                inv=invalid[c], pipeline=capture_plan_pipeline(out_dir, c))
        for kind in ("unmeasured", "refused"):
            for q, names in sorted(drift[kind].items()):
                nd += 1
                print(f"  DRIFT {c} {q}: {kind}: {names} (the capture vs HEAD's candidate set)")
    print(f"candidate drift vs HEAD's plan files: {len(cells)} cells, {nd} (cell, query) with drift")
    fail |= nd > 0

    progress("answers")
    # 2. answers
    bad_ans, n = [], 0
    for q in QUERIES:
        for i in expected[q]:
            hashes = {c: chosen[c][q]["rows"][i]["result_hash"] for c in answer_cells
                      if q in chosen[c] and i in chosen[c][q]["rows"]}
            n += 1
            if len(set(hashes.values())) > 1:
                bad_ans.append((q, i, hashes))
    print(f"answers: {n - len(bad_ans)}/{n} (query, param) identical across all {len(answer_cells)} cells")
    for b in bad_ans[:10]:
        # on a spec set: say whether a set cell disagrees with the grid, or the grid's own cells disagree
        who = ("" if grid else " (the grid's own cells disagree)"
               if len({h for c, h in b[2].items() if c.startswith("grid:")}) > 1 else " (a set cell differs from the grid)")
        print("  ANSWER MISMATCH" + who, b)
    fail |= bool(bad_ans)

    # where the cheapest plan is not the planner's pick
    for c in cells:
        moved = [(q, chosen[c][q]["planner"], chosen[c][q]["body"]) for q in QUERIES
                 if q in chosen[c] and chosen[c][q]["body"] not in (None, chosen[c][q]["planner"])]
        if moved:
            print(f"  cheaper than the planner's pick in {c}: " + ", ".join(f"{q} {p}->{v}" for q, p, v in moved))

    if not grid and not a.decompose:
        print("decomposition: skipped (a spec set with per-type layouts has no uniform (props, adjacency) axes)")
        print("RESULT:", "FAIL" if fail else f"PASS ({coverage}; the same answers as every grid cell)")
        return 1 if fail else 0

    progress("decomposition")
    # 3. decomposition over the chosen plans' steps
    # where each compared cell's capture and spec live: the set's own cells, then (--decompose) the reference grid cells
    home = {c: (out_dir, c, spec_dir) for c in cells}
    cell_axes = axes
    if not grid:
        home.update({"grid:" + c: (gdir, c, GRID) for c in ref_cells})
        by_spec = {k: spec_axes(os.path.join(sd, f"{c}.json")) for k, (_, c, sd) in home.items()}
        cell_axes = by_spec.get
        cells = list(home)
        print(f"decomposition over {len(cells)} cells ({len(ref_cells)} of the grid's), settings from the spec files: "
              f"{len({v[0] for v in by_spec.values()})} props settings, {len({v[1] for v in by_spec.values()})} adjacency settings")
    every, picked, parts = {}, {}, {}
    for k, (d, c, _) in home.items():
        picked[k] = {(q, i): dd["body"] for q, dd in chosen[k].items() for i in dd["rows"] if dd["body"] is not None}
        every[k] = defaultdict(lambda: defaultdict(dict))
        for r in load_steps(d, c):
            every[k][(r["query"], r["param_idx"])][r["body"]][(r["step"], r["op"])] = r
        parts[k] = defaultdict(set)
        with open(Capture(d).path_of("parts", c)) as f:
            for r in capture_layout.reader(f):
                if part_kind(r["part"]):
                    parts[k][(r["query"], r["param_idx"], r["body"], r["step"])].add(part_kind(r["part"]))
    tally, breaks, unclassified, probe, shared_probe, shared = decomposition(every, picked, cells, parts, cell_axes)
    for kind in sorted(tally, key=lambda k: (k.count("+"), k)):
        eq, df, nc = tally[kind]
        print(f"decomposition {kind:15s}: {eq} equal, {df} differ, {nc} not comparable (no plan in common / other rows)")
    differing = sum(1 for c1, c2 in itertools.combinations(cells, 2) for qi, v in picked[c1].items() if picked[c2].get(qi) not in (None, v))
    print(f"decomposition: {differing} (cell pair, query, param) with different picks, compared under a plan both ran: {shared} steps equal there")
    for b in breaks[:15]:
        print("  BREAK", b)
    if unclassified:
        print("  ops not classified (not checked):", sorted(unclassified))
    fail |= bool(breaks)

    def flipped(at):
        """True when one S + 1 on that step row is seen as a new break (the row is put back)"""
        c, qi, plan, so = at
        row = every[c][qi][plan][so]
        was = row["S"]
        row["S"] = str(int(was) + 1)
        try:
            return len(decomposition(every, picked, cells, parts, cell_axes)[1]) > len(breaks)
        finally:
            row["S"] = was

    def at_name(at):
        return f"{at[0]} {at[1][0]} p{at[1][1]} {at[3][0]}"

    progress("positive controls")
    # positive control: one flipped value in a compared step must be caught
    if probe:
        caught = flipped(probe)
        print(f"positive control (S+1 on {at_name(probe)}): {'caught' if caught else 'NOT CAUGHT'}")
        fail |= not caught
    else:
        print("positive control: no compared-equal step to flip -- the decomposition checked nothing"
              + (" (a slice with no pair of cells to compare: not a failure)" if a.cells else ""))
        fail |= not a.cells
    # ... in a pair of cells with DIFFERENT picks, compared under a plan both ran (when the capture holds such a pair)
    if shared_probe:
        caught = flipped(shared_probe)
        print(f"positive control (S+1 on {at_name(shared_probe)}, plan {shared_probe[2]}, a pair with different picks): "
              f"{'caught' if caught else 'NOT CAUGHT'}")
        fail |= not caught
    else:
        print("positive control (a pair with different picks): no such pair with an equal step in this capture (not a failure)")
    # ... and in a multi-component step (the class must not drop silently), when the capture holds one
    multis = [(c, qi, plan, so) for c in cells for qi, plan in picked[c].items() for so in every[c].get(qi, {}).get(plan, {})
              if len(parts.get(c, {}).get((qi[0], qi[1], plan, so[0]), ())) > 1]
    # the whole set: the first such step must be caught. A slice, or a set with cells missing (already a failure above),
    # may hold no cell that step is compared with, so there the first 50 are tried and one caught is enough (none
    # compared: said, not a failure of its own)
    partial = bool(a.cells or missing_cells)
    tried = multis[:50] if partial else multis[:1]
    caught_at = next((at for at in tried if flipped(at)), None)
    if tried:
        at = caught_at or tried[0]
        if caught_at or not partial:
            print(f"positive control (S+1 on the multi-component step {at_name(at)}): {'caught' if caught_at else 'NOT CAUGHT'}")
            fail |= not caught_at
        else:
            print(f"positive control (multi-component steps): none of the first {len(tried)} is compared inside "
                  f"{'this slice' if a.cells else 'the cells measured'} (not a failure of its own)")

    print("RESULT:", "FAIL" if fail else f"PASS ({coverage}; same answers everywhere; every step's cost follows its own component)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
