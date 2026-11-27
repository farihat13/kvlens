#!/usr/bin/env python3
"""The grid's recorded plan choice (the plan rule, python/kvevaluator/plan_choice/candidates.py): per cell and query, the pick under the held-out rule
(candidates.choose with the capture's held-out set) at the LARGEST reported scale, written to
inputs/picks/picks.csv -- the one record every smaller scale takes its variant from (picks.recorded_picks),
so a later decision at another scale is a listed diff, never a silent move.

    python -m kvcost.layout_cost.grid_pick_record --sf 3 [--grid output/captures/ldbc/sf3] [--family WL=CAPTURE]...   # write the record
        # every store of the Interactive capture below --grid (the cells, the chunk-size cells, the mixes, the named stores)
    python -m kvcost.layout_cost.grid_pick_record --sf 3 --grid ... --check [--scales 0.1]   # the record equals what the captures give
        # now; a smaller scale's held-out fingerprints are checked only when --scales names it (else a note). It checks every
        # section: LDBC and the named stores (--formats), microbench decided vs the chooser, BI and JOB carried (the body at
        # HEAD, ok on its capture, the selector's); --before <previous record>: a re-decided record's anchors against it
        # (gate: tests/gates/meta/check_grid_pins_check.py)
    python -m kvcost.layout_cost.grid_pick_record --sf 3 --grid ... --repick --file <new.json>   # a re-measure at the deciding scale:
        # the tracked record's plan is the incumbent (the plan rule: replaced only by one more than 10% cheaper on the
        # held-out params, an exact tie keeps it); refused when that differs from the pick every sf3 pricer takes
    python -m kvcost.layout_cost.grid_pick_record --sf 3 --grid ... --repick --keep-by-reference inputs/picks/pick_record_by_reference.json
        # the same, keeping the record's cells this run does not capture as they were decided (each with its provenance in
        # the record's by_reference field); tests/gates/meta/check_picks_by_reference.py --before <old record> checks them
    python -m kvcost.layout_cost.grid_pick_record --diff <a.json> <b.json>              # every (cell, query) whose variant moves
    python -m kvcost.layout_cost.grid_pick_record --sf 0.1 --small-scale [--record]   # the table: the
        # recorded variant against this scale's own held-out rows; over 1.10x the cheapest = the plan rule's scale exception: that
        # scale's own pick, which --record writes into the file's "scales" entry (the numbers kept); not valid here = a finding;
        # without --record, this scale's recorded own picks must equal what the capture gives now (exit 1 otherwise)
    python -m kvcost.layout_cost.grid_pick_record --sf 30 --grid ... --small-scale --record --replace-scale   # a refresh of a scale:
        # each store the call decides has its entries and fingerprint replaced whole (an entry it no longer gives is dropped,
        # listed); a store it does not decide is kept (listed). --small-scale decides every store but the named ones
Reads captures; measures nothing. A capture without a held-out set (kvcost measure measures one for LDBC Interactive) refuses.
The record carries, per (cell, query), the body sha of the variant at HEAD; per cell the store fingerprint (key codec, value
codec hash, keys), the reported and the held-out captures' inputs hash and binary sha, a mix's spec sha and assignment: --check
fails on a row with no capture, a capture whose inputs or binary differ from the record's, a spec that moved under a mix's
name, or a body that is no longer that body at HEAD; --diff exits 1 when any pick moves. A rewrite at the deciding scale
keeps a smaller scale's entries that still hold (carry_scales: the deciding pick they were measured against unchanged, the
mix's spec unchanged) and prints the ones it drops.
"""
import argparse
import datetime
from collections import defaultdict
import json
import os
import sys

from kvevaluator.measure import counting, grid_capture
from kvevaluator.capture.spec_sets import format_of
from kvcost.layout_cost import picks
TRACKED = grid_capture.PICKS_FILE   # the tracked record: --repick's incumbents and carry_scales' previous record, whatever --file says
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plan_choice import pick_record
from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, resolve_criterion
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture, Tree, capture_in


def stores_in(cap):
    """[(store name in the record, its capture's file stem)]: every store a capture holds (the record keys a store by its
    store dir's name, which is the capture's file stem: pick_record.store_key)"""
    out = [(pick_record.store_key(s), s) for s in Capture(cap).stores()]
    keys = [k for k, _s in out]
    if len(set(keys)) != len(keys):
        raise SystemExit(f"{cap}: two layouts name one store: {sorted({k for k in keys if keys.count(k) > 1})}")
    return out


def spec_kind(cap, stem):
    """what the record keeps of a store beside its picks, from the spec its run record names: grid (a cell of the grid),
    chunk (a chunk-size cell: its spec's sha), mix (any other spec: its spec's sha and assignment), format (a named store)"""
    p = run_record(cap, stem)["spec_path"]
    if format_of(p):
        return "format"
    d = os.path.basename(os.path.dirname(grid_capture.recorded_spec_path(p)))
    return "grid" if d == os.path.basename(grid_capture.GRID) else "chunk" if d.startswith("grid_chunked_") else "mix"


def spec_path_of(cap, stem):
    return grid_capture.recorded_spec_path(run_record(cap, stem)["spec_path"])


def today_names(root, cell, workload="interactive"):
    """(q, a variant name as the capture names it) -> (the candidate's name today, its body sha at HEAD): by the body
    the capture's rows ran (candidates.captured_bodies, the name resolved at the capture's commit where a row carries no
    sha) and the candidate with that sha today (candidates.current_name); a body gone at HEAD keeps its name, sha None.
    The record names today's candidates; a capture made before a renaming names the old ones."""
    runs = grid_capture.load_runs(root, cell)
    tree = grid_capture.capture_tree_of(root, cell)
    per_q = {}

    def name(q, v):
        if q not in per_q:
            heads = C.candidate_bodies(workload, q)
            shas = C.captured_bodies(runs, workload, q, tree)
            per_q[q] = {n: (C.current_name(workload, q, n, sh, heads) if sh else n, sh) for n, sh in shas.items()}
        return per_q[q].get(v, (v, C.candidate_bodies(workload, q).get(v)))
    return name


def decide(root, cell, criterion, incumbent=None, disagree=None, workload="interactive", in_sample=False):
    """{query: {variant, body_sha, cheapest, rule, why, held_out_ratio_vs_cheapest}} under the held-out rule; loud
    without a held-out set or on a query with no valid candidate (a record with a hole is no record). A first decision is
    the cheapest on the held-out params (no anchor). `incumbent` ({query: the tracked record's row}, --repick): a
    decided entry's variant is the anchor of the same rule (C.held_out_pick: kept unless one is more than the margin
    cheaper on the held-out params; a carried entry is no anchor); each (cell, query, recorded, re-decided, priced) where
    the re-decided pick is not the one every pricer takes (choose_cell, anchored on the tracked record) is appended to
    `disagree`"""
    hr = grid_capture.load_held_out(root, cell)
    if hr is None and not in_sample:
        raise SystemExit(f"{cell}: no held-out capture under {Capture(root).held_out().path} (kvcost measure --workload i01)")
    out = {}
    today = today_names(root, cell, workload)
    # through choose_cell (N6): a measure-only body is never picked, candidate drift loud; neither the record's pick nor its
    # incumbents are applied (pinned=False, NoAnchor: this IS the decision)
    rule = picks.PlanRule()      # the record --file names (grid_capture.PICKS_FILE, set in main)
    fresh = picks.choose_cell(root, cell, criterion, rule=rule, pinned=False, anchor=picks.NoAnchor())
    # anchored on the incumbents' record, not on --file (a new file has none yet; an old one holds no incumbent)
    picked = picks.choose_cell(root, cell, criterion, rule=rule.but(record=TRACKED), pinned=False) if incumbent is not None else {}
    for q, d in sorted(fresh.items()):
        if d["body"] is None:
            raise SystemExit(f"{cell} {q}: no valid candidate on the capture ({d.get('rejected')}): the record would have a hole")
        sh = {today(q, v)[0]: x for v, x in d.get("held_out_scores", {}).items()}   # keyed by today's names, as the record is
        variant, body = today(q, d["body"])
        if body is None:
            raise SystemExit(f"{cell} {q}: the pick {variant!r} is no body at HEAD (gone since the capture): re-measure the cell")
        out[q] = {"body": variant, "body_sha": body, "cheapest": variant, "in_sample": in_sample,
                  "rule": d["rule"], "why": d["why"], "held_out_ratio_vs_cheapest": 1.0}
        was = pick_record.anchor_of((incumbent or {}).get(q))       # a carried entry (v1's selection) is no anchor
        if incumbent is not None and was is not None:
            if was in sh:
                v, _ = C.held_out_pick(sh, was, C.MARGIN, q)
                g = sh[v] / sh[was] if sh[was] else None
                out[q].update({"body": v, "body_sha": C.candidate_bodies(workload, q).get(v),   # v: today's name
                               "recorded_before": was, "held_out_ratio_vs_recorded": None if g is None else round(g, 4),
                               "held_out_ratio_vs_cheapest": round(sh[v] / sh[variant], 4) if sh.get(variant) else None,
                               "rule": f"re-decided, the recorded plan the anchor, margin {C.MARGIN:g}",
                               "why": "the recorded plan (no candidate under the margin on the held-out params)" if v == was else
                                      f"{g:.3f}x the recorded plan's on the held-out params"})
            else:
                v = variant
                out[q].update({"recorded_before": was, "why": f"the recorded plan {was!r} is not valid on the held-out params: "
                                                                 f"the cheapest (a finding)"})
            p = today(q, picked[q]["body"])[0] if q in picked and picked[q]["body"] is not None else None
            if v != p:
                disagree.append((cell, q, was, v, p))
    return out


def run_record(root, cell):
    p = Capture(root).record(cell)
    if not os.path.exists(p):
        raise SystemExit(f"{cell}: no run record at {p}")
    return json.load(open(p))


def composed_sources(r, root):
    """a composed cell's run record (under the capture `root`) -> its two source run records, props side first.
    compose_cells' composed_from names each side's parts file, <source capture>/parts/<source cell>.csv, as
    paths.recorded_path wrote it (relative to the composed record's tree); where that is gone (a snapshot), the source
    cell where a reader finds it from the composed capture's place (output_tree.layout_capture: its measured capture)"""
    from kvevaluator.output_tree import layout_capture, tree_root
    from kvevaluator.paths import resolve_recorded
    cap = Capture(root)
    tree = tree_root(os.path.dirname(os.path.dirname(os.path.normpath(root))))
    out = []
    for side in ("props and index parts", "adjacency parts"):
        f = os.path.normpath(resolve_recorded(r["composed_from"][side], tree))
        src, cell = os.path.dirname(os.path.dirname(f)), os.path.basename(f)[:-len(".csv")]
        if not os.path.exists(Capture(src).record(cell)):
            place = os.path.dirname(os.path.dirname(os.path.normpath(root)))
            src = layout_capture(place, cap.workload(), cell, cap.param_set()).path
        out.append(run_record(src, cell))
    return out


def fingerprints(root, cell, in_sample=False):
    """what the record ties a cell's picks to: the store (key codec, value codec hash, keys), the reported and the held-out
    captures' inputs hash and binary sha (an in-sample store: no held-out capture, held_out None). A composed cell has no
    store of its own: each entry is the pair of its two sources' (props side, adjacency side)"""
    r = run_record(root, cell)
    h = None if in_sample else run_record(Capture(root).held_out().path, cell)
    if r.get("composed_from"):
        hroot = Capture(root).held_out().path
        rs, hs = composed_sources(r, root), (composed_sources(h, hroot) if h is not None else [None, None])
        pair = [record_fingerprints(x, y) for x, y in zip(rs, hs)]
        return {k: [fp[k] for fp in pair] if pair[0][k] is not None else None for k in ("store", "reported", "held_out")}
    return record_fingerprints(r, h)


def record_fingerprints(r, h):
    """fingerprints() of one measured cell's reported run record `r` and held-out one `h` (None: in-sample)"""
    m = r.get("store_manifest") or {}
    # the cell's selection key (grid_bodies.cell_fingerprint): a requires / scales edit moves it and
    # not `inputs`; absent from a record made before it, so an older record's fingerprints keep their form
    cell_key = lambda rec: {"cell_inputs": rec["cell_inputs"]} if rec.get("cell_inputs") else {}
    return {"store": {k: m.get(k) for k in ("key_codec", "value_codec_hash", "keys")},
            "reported": {"inputs": r.get("inputs"), "binary": (r.get("binary") or {}).get("binary_git_sha"), "nparam": r.get("nparam"),
                         **cell_key(r)},
            "held_out": None if h is None else {"inputs": h.get("inputs"), "binary": (h.get("binary") or {}).get("binary_git_sha"),
                         "params": sorted((h.get("slice") or {}).get("params") or [], key=int), **cell_key(h)}}


def mix_identity(mixes, cell, grid):
    """(spec sha, assignment) of a built mix: its spec file (the capture's run record names it), the assignment in
    compose's unit-key form ({"adj:knows": "split", "props:Person": "condensed", ...}: the spec's components matched
    against the grid's settings over the GRID's unit universe (compose.grid_units: every unit any cell's parts name), the
    same universe a composed option is normalised over, so compose.recorded_for_option binds by exact equality"""
    from kvcost.layout_cost import compose
    from kvcost.layout_cost.mixes import assignment, grid_settings, retired_grouped
    spec_path = spec_path_of(mixes, cell)
    spec = json.load(open(spec_path))
    if retired_grouped(spec, cell):         # grouped props: no assignment since the option's retirement
        return grid_capture.spec_sha(spec_path), None
    key = (grid, "adjacency_by_etype_dir" in spec)     # the unit universe, cached per (grid, by direction)
    if key not in _GRID_UNITS:
        data = compose.load(grid, by_dir=key[1], composer=compose.Composer(criterion=None, plan_rule=picks.PlanRule()))
        _GRID_UNITS[key] = compose.grid_units(data)
    a = assignment(spec, _GRID_UNITS[key], grid_settings(), cell)
    return grid_capture.spec_sha(spec_path), {f"{u[0]}:{':'.join(u[1:])}": v for u, v in sorted(a.items())}


_GRID_UNITS = {}


def chunk_cell_spec_sha(root, cell):
    """a chunk-size cell's identity in the record: its spec's sha (written and checked the same way); it gets no
    assignment over the grid's settings (the size is not one of them)"""
    return grid_capture.spec_sha(spec_path_of(root, cell))


def code_states(rec):
    """{binary sha: the cells whose reported and held-out captures it made}: which rows are which code state"""
    out = {}
    for cell, fp in sorted(rec["fingerprints"].items()):
        if fp is None:                                   # a carried cell: decided on no capture of ours
            continue
        b = "+".join(sorted({fp["reported"]["binary"], fp["held_out"]["binary"]}, key=str))
        out.setdefault(b, []).append(cell)
    return out


def write(a, crit):
    rec = {"what": "the grid's recorded plan choice (CONVENTIONS 19): the pick under the held-out rule at the largest reported "
                   "scale; smaller scales take the body from here unless the exception holds on their own held-out rows "
                   "(picks.recorded_picks, candidates.decide); regenerate with grid_pick_record.py",
           "decided_at": a.sf, "criterion": crit, "margin": C.MARGIN, "written": datetime.date.today().isoformat(), "cells": {},
           "fingerprints": {}, "counting": {}, "spec_sha": {}, "assignment": {}, "scales": {}, "scale_fingerprints": {}, "head": head_inputs()}
    prev = pick_record.load_full(TRACKED) if os.path.exists(TRACKED) else {}
    if a.repick and str(prev.get("decided_at")) != str(a.sf):
        raise SystemExit(f"--repick: the tracked record was decided at sf{prev.get('decided_at')}, not sf{a.sf}")
    disagree = []
    root = a.cap
    for key, stem in stores_in(root):           # the cells, the mixes and the named stores (reported + held-out)
        inc = prev.get("cells", {}).get(key, {}) if a.repick else None
        rec["cells"][key] = decide(root, stem, a.criterion, inc, disagree)
        rec["fingerprints"][key] = fingerprints(root, stem)
        rec.setdefault("counting", {})[key] = counting.cell_counting(run_record(root, stem))
        kind = spec_kind(root, stem)
        if kind == "chunk":
            rec["spec_sha"][key] = chunk_cell_spec_sha(root, stem)
        elif kind == "mix":
            rec["spec_sha"][key], asg = mix_identity(root, stem, a.grid)
            if asg is not None:
                rec["assignment"][key] = asg
    decided_wl = {}
    for spec in a.workload or []:                # an in-sample workload (JOB, BI, the microbench): every store under the root
        wl, _, wroot = spec.partition("=")
        if wl not in picks.IN_SAMPLE_WORKLOADS or not wroot:
            raise SystemExit(f"--family {spec}: WL=ROOT with WL one of {picks.IN_SAMPLE_WORKLOADS}")
        sec = decided_wl.setdefault(wl, {"cells": {}, "fingerprints": {}, "counting": {}})
        was = prev.get("workloads", {}).get(wl, {}).get("cells", {})
        for key, stem in stores_in(wroot):
            root = wroot
            sec["cells"][key] = decide(root, stem, a.criterion, was.get(key, {}) if a.repick else None, disagree,
                                       workload=wl, in_sample=True)
            sec["fingerprints"][key] = fingerprints(root, stem, in_sample=True)
            sec["counting"][key] = counting.cell_counting(run_record(root, stem))
    keep = json.load(open(a.keep_by_reference)) if a.keep_by_reference else {}
    if keep and not a.repick:
        raise SystemExit("--keep-by-reference needs --repick: it keeps rows of the tracked record")
    for cell, source in sorted(keep.items()):
        if cell in rec["cells"]:
            raise SystemExit(f"--keep-by-reference {cell}: this run captured it, so it is re-decided, not kept")
        if cell not in prev.get("cells", {}):
            raise SystemExit(f"--keep-by-reference {cell}: not a cell of the tracked record")
        if prev.get("fingerprints", {}).get(cell) is None:
            raise SystemExit(f"--keep-by-reference {cell}: a carried cell (decided on no capture): it is kept as it is anyway")
        for k in ("cells", "fingerprints", "spec_sha", "assignment", "counting"):
            if cell in prev.get(k, {}):
                rec[k][cell] = prev[k][cell]
        fp = prev["fingerprints"][cell]["held_out"]
        rec.setdefault("by_reference", {})[cell] = {"capture": source,       # a cell kept twice keeps its first date
                                                    "decided": prev.get("by_reference", {}).get(cell, {}).get("decided") or prev.get("written"),
                                                    "held_out_params": fp.get("params"), "binary": fp.get("binary")}
    if a.repick:
        rec["redecided"] = {"from": f"{os.path.relpath(TRACKED)} written {prev.get('written')}",
                            "rule": f"the recorded plan is the anchor: replaced only by one more than {C.MARGIN:g} cheaper on the "
                                    f"held-out params, an exact tie keeps it (CONVENTIONS 19); a cell new to the record: the cheapest",
                            "new_cells": sorted(c for c in rec["cells"] if c not in prev.get("cells", {})),
                            "dropped_cells": sorted(c for c in prev.get("cells", {})
                                                    if c not in rec["cells"] and c not in pick_record.carried_cells(prev)),
                            "kept_by_reference": sorted(keep)}
        moves = [(c, q, d["recorded_before"], d["body"], d.get("held_out_ratio_vs_recorded"))
                 for c, qs in sorted(rec["cells"].items()) if c not in keep for q, d in sorted(qs.items())   # a kept row moved nothing now
                 if d.get("recorded_before") is not None and d["recorded_before"] != d["body"]]
        print(f"re-decided against {os.path.relpath(TRACKED)} ({prev.get('written')}): {len(moves)} (cell, query) picks move; "
              f"new cells {rec['redecided']['new_cells']}; dropped cells {rec['redecided']['dropped_cells']}; "
              f"{len(keep)} cells kept by reference (their previous rows, unchanged)")
        for c, q, x, y, g in moves:
            print(f"    MOVED {c:32s} {q:5s} {x} -> {y}  (held-out cost {g}x the recorded plan's)")
        if disagree:
            print(f"REFUSED: {len(disagree)} (cell, query) where the re-decided pick is not the one every sf{a.sf} pricer "
                  f"and --check take (picks.choose_cell, anchored on the tracked record's incumbents):")
            for c, q, was, v, p in disagree:
                print(f"    {c:32s} {q:5s} recorded {was}: re-decided {v}, priced {p}")
            return 1
    # the carried cells (the named stores' v1 selection, pick_record.carried_cells) this run did not capture stay as they
    # are, and the other workloads' sections with them: this run decides the grid's cells only
    def keep_carried_queries(new, old):
        """a decided store keeps its carried entries for the queries this run does not decide (the reads outside the suite:
        generated, never measured); returns how many"""
        n = 0
        for key, qs in new.items():
            for q, e in (old.get(key) or {}).items():
                if q not in qs and e.get("rule") == pick_record.CARRIED:
                    qs[q] = e
                    n += 1
        return n
    kept_queries = keep_carried_queries(rec["cells"], prev.get("cells", {}))
    kept_carried = [c for c in pick_record.carried_cells(prev) if c not in rec["cells"]]
    for cell in kept_carried:
        rec["cells"][cell], rec["fingerprints"][cell] = prev["cells"][cell], None
    if "workloads" in prev:
        rec["workloads"] = prev["workloads"]
    for wl, sec in decided_wl.items():           # a decided workload: its stores replaced, its carried stores not captured kept
        old = (prev.get("workloads") or {}).get(wl, {})
        kept_queries += keep_carried_queries(sec["cells"], old.get("cells", {}))
        for key, e in old.get("cells", {}).items():
            if key not in sec["cells"]:
                sec["cells"][key], sec["fingerprints"][key] = e, old.get("fingerprints", {}).get(key)
                if key in old.get("counting", {}):
                    sec["counting"][key] = old["counting"][key]
        rec.setdefault("workloads", {})[wl] = sec
        print(f"{wl}: {len(sec['cells'])} stores ({sum(1 for k in sec['fingerprints'] if sec['fingerprints'][k] is not None)} "
              f"decided by this run, in-sample)")
    if kept_carried:
        print(f"{len(kept_carried)} carried cells kept as they were (not captured by this run): {', '.join(kept_carried)}")
    if kept_queries:
        print(f"{kept_queries} carried entries of decided stores kept (queries this run does not decide: the reads outside "
              f"the suite)")
    rec["code_states"] = code_states(rec)
    carried, dropped = carry_scales(prev, rec)
    pick_record.dump(rec, grid_capture.PICKS_FILE)
    if carried or dropped:
        print(f"the smaller scales' entries of the previous record: {len(carried)} carried, {len(dropped)} dropped "
              f"(carried: {', '.join(f'sf{sf} {cell} {q}' for sf, cell, q, _ in carried) or 'none'})")
        for sf, cell, q, why in dropped:
            print(f"    dropped sf{sf} {cell:32s} {q:14s} {why}")
    n = sum(len(v) for c, v in rec["cells"].items() if c not in kept_carried)
    moved = [(c, q, d) for c, qs in rec["cells"].items() if c not in kept_carried for q, d in qs.items()
             if d["body"] != d.get("cheapest", d["body"])]
    print(f"code states (binary: cells): " + "; ".join(f"{b[:10]}: {len(cs)}" for b, cs in rec["code_states"].items()))
    print(f"wrote {os.path.relpath(grid_capture.PICKS_FILE)}: {len(rec['cells'])} cells ({len(kept_carried)} carried), {n} (cell, query) "
          f"picks decided at sf{a.sf}; "
          f"{len(moved)} are not the cheapest (a recorded plan kept within the margin on the held-out params):")
    for c, q, d in moved:
        print(f"    {c:32s} {q:5s} {d['cheapest']:40s} -> {d['body']:45s} {d['held_out_ratio_vs_cheapest']}")
    # the two param blocks as samples: per query the held-out / reported cost ratio of the picked body over the cells
    # (three LDBC queries differ by over 2x between rows 1-5 and 6-10 at sf0.1; said here
    # every time the record is written, flagged over 2x either way)
    ratios = {}
    for _key, stem in stores_in(a.cap):
        for q, d in picks.choose_cell(a.cap, stem, a.criterion, rule=picks.PlanRule(), pinned=False).items():
            p = d["body"]
            if p in d.get("costs", {}) and p in d.get("held_out_scores", {}) and d["costs"][p]:
                ratios.setdefault(q, []).append(d["held_out_scores"][p] / d["costs"][p])
    print("the held-out block against the reported block (the picked body, held-out / reported cost, min..max over the stores):")
    for q, rs in sorted(ratios.items()):
        lo, hi = min(rs), max(rs)
        print(f"    {q:5s} {lo:5.2f}..{hi:5.2f}" + ("   <-- the two blocks differ by over 2x: different samples of the param file" if lo < 0.5 or hi > 2 else ""))
    return 0


def _stale(prev, rec, cell, q):
    """why a smaller scale's entry for (cell, q) no longer holds under the new record ("" = it holds)"""
    if cell not in rec["cells"]:
        return "the cell is not in the new record"
    if prev.get("spec_sha", {}).get(cell) != rec.get("spec_sha", {}).get(cell):
        return f"the spec under the mix's name changed ({prev.get('spec_sha', {}).get(cell)} -> {rec.get('spec_sha', {}).get(cell)})"
    if q is not None:
        a, b = prev["cells"].get(cell, {}).get(q, {}), rec["cells"][cell].get(q, {})
        if (a.get("body"), a.get("body_sha")) != (b.get("body"), b.get("body_sha")):
            return f"the deciding pick changed: {a.get('body')} ({a.get('body_sha')}) -> {b.get('body')} ({b.get('body_sha')})"
    return ""


def carry_scales(prev, rec):
    """A rewrite at the deciding scale keeps the smaller scales' entries of the previous record that still hold. A scale's own
    pick for (cell, q) was measured against the deciding pick (variant + body sha) and, for a mix, the spec under its name:
    carried while both are unchanged, dropped otherwise. A scale's held-out fingerprint on a cell is carried while the cell
    and its spec are unchanged. Fills rec["scales"] / rec["scale_fingerprints"]; returns (carried, dropped) as
    (sf, cell, q, why) lists ("(fingerprint)" for the cell's fingerprint)."""
    carried, dropped = [], []
    for sf, cells in sorted(prev.get("scales", {}).items()):
        for cell, qs in sorted(cells.items()):
            for q, entry in sorted(qs.items()):
                why = _stale(prev, rec, cell, q)
                if why:
                    dropped.append((sf, cell, q, why))
                else:
                    rec["scales"].setdefault(sf, {}).setdefault(cell, {})[q] = entry
                    carried.append((sf, cell, q, ""))
    for sf, cells in sorted(prev.get("scale_fingerprints", {}).items()):
        for cell, fp in sorted(cells.items()):
            why = _stale(prev, rec, cell, None)
            if why:
                dropped.append((sf, cell, "(fingerprint)", why))
            else:
                rec["scale_fingerprints"].setdefault(sf, {})[cell] = fp
                carried.append((sf, cell, "(fingerprint)", ""))
    return carried, dropped


def head_inputs():
    """what the record's picks depend on beyond the captures: the weights, the schema, and the
    candidate set (name -> body sha) of every LDBC query at HEAD; --check recomputes and compares"""
    import hashlib
    from kvevaluator import weights as W
    from kvevaluator.schema import schema_json
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
    # the weight values the pick reads, not weights.py's bytes: a comment or dead code there moves no pick
    values = json.dumps({"DEFAULT": W.DEFAULT, "BYTES_PER_GET": W.BYTES_PER_GET}, sort_keys=True)
    weights = hashlib.sha256(values.encode()).hexdigest()[:16]
    from kvevaluator.plan_choice.optimizer.criteria import DEFAULT_CRITERION, resolve_criterion
    return {"weights_sha": weights, "schema_hash": schema_json.to_json(LDBC_SCHEMA, "canonical")["hash"],
            "criterion_spec": resolve_criterion(DEFAULT_CRITERION)[1],         # the pick measure's full definition string
            "candidates": {q: C.candidate_bodies("interactive", q) for q in sorted(C.workload_queries("interactive")[0])}}


def check(a, crit):
    rec = pick_record.load_full(grid_capture.PICKS_FILE)
    if str(rec["decided_at"]) != str(a.sf) or rec["criterion"] != crit:
        raise SystemExit(f"{grid_capture.PICKS_FILE} was decided at sf{rec['decided_at']} under {rec['criterion']}, not sf{a.sf} / {crit}")
    bad, n = [], 0
    now = head_inputs()
    for k in ("weights_sha", "schema_hash", "criterion_spec"):
        if rec.get("head", {}).get(k) != now[k]:
            bad.append(("HEAD", "*", f"{k} {rec.get('head', {}).get(k)}", f"now {now[k]}"))
    # the candidate set by body (a renaming moves no body): a body new since the decision was never measured by its
    # capture, and a body gone was not picked unless a pick below says so; both are said, the picks decide the verdict
    moved_set = []
    for q in sorted(set(now["candidates"]) | set(rec.get("head", {}).get("candidates", {}))):
        was = set((rec.get("head", {}).get("candidates", {}).get(q) or {}).values())
        isnow = set((now["candidates"].get(q) or {}).values())
        if was != isnow:
            moved_set.append(f"{q} +{len(isnow - was)} -{len(was - isnow)}")
    if moved_set:
        print(f"the candidate set moved since the decision (bodies new at HEAD / gone, by sha): {', '.join(moved_set)}")
    # a smaller scale's entries hold while its held-out capture is the one recorded. Its capture root is found from this
    # scale's by text, so a newer capture of that scale in the same tree moves them: checked only when --scales names the
    # scale, else said, with the command that re-records it
    # (the record's key, the capture's file stem, its capture root): the cells, then --formats' named stores
    units = [(k, s, a.cap) for k, s in stores_in(a.cap)]
    for sf, cells in sorted(rec.get("scale_fingerprints", {}).items()):
        moved, compared, no_capture, not_given = [], 0, [], 0
        for cell, fp in sorted(cells.items()):
            stem, root = next(((s, r) for k, s, r in units if k == cell), (None, None))
            if root is None:
                not_given += 1    # a recorded cell this run's roots do not name
                continue
            hr_root = root.replace(f"sf{a.sf}", f"sf{sf}")
            if not os.path.exists(Capture(hr_root).held_out().record(stem)):
                no_capture.append(cell)
                continue
            compared += 1
            if fingerprints(hr_root, stem)["held_out"] != fp:
                moved.append(cell)
        redo = f"grid_pick_record.py --sf {sf} --grid <its grid> --small-scale --record"
        if sf in (a.scales or []):
            print(f"sf{sf} held-out fingerprints: {compared} compared, {len(no_capture)} with no sf{sf} held-out capture, "
                  f"{not_given} recorded cells not under the roots given")
            bad += [(cell, "*", f"sf{sf} held-out fingerprint moved (re-record: {redo})", "") for cell in moved]
            bad += [(cell, "*", f"sf{sf}: no held-out capture to check the recorded fingerprint on", "") for cell in no_capture]
            if compared == 0:
                bad.append((f"sf{sf}", "*", "--scales: 0 held-out fingerprints compared", ""))
        elif moved:
            print(f"note: sf{sf}'s held-out capture moved on {len(moved)} cells ({', '.join(moved[:3])}{', ...' if len(moved) > 3 else ''}): "
                  f"its recorded entries are stale; not checked here (--scales {sf} checks it); re-record: {redo}")
    have = {k for k, _s, _r in units}
    by_ref = sorted(c for c in rec.get("by_reference", {}) if c not in have)
    if by_ref:
        print(f"{len(by_ref)} cells kept by reference (decided on an earlier capture, the record's by_reference field; "
              f"tests/gates/meta/check_picks_by_reference.py checks them): not compared here")
    carried_rows = [c for c in pick_record.carried_cells(rec) if c not in have]
    if carried_rows:
        print(f"{len(carried_rows)} carried cells (v1's selection, decided on no capture of ours): not compared here")
    for cell in rec["cells"]:
        if cell not in have and cell not in by_ref and cell not in carried_rows:
            bad.append((cell, "*", "a record row with no capture", ""))
    for cell, stem, root in units:
        if cell not in rec["cells"]:
            bad.append((cell, "*", "a capture with no record row", "")); continue
        fp = fingerprints(root, stem)
        if fp != rec.get("fingerprints", {}).get(cell):
            bad.append((cell, "*", f"fingerprints differ: record {rec.get('fingerprints', {}).get(cell)}", f"now {fp}")); continue
        kind = spec_kind(root, stem)
        if kind == "chunk":
            if rec.get("spec_sha", {}).get(cell) != chunk_cell_spec_sha(root, stem):
                bad.append((cell, "*", "the chunk cell's spec moved under its name", "")); continue
        elif kind == "mix" and (rec.get("spec_sha", {}).get(cell), rec.get("assignment", {}).get(cell)) != mix_identity(root, stem, a.grid):
            bad.append((cell, "*", "the mix's spec or assignment moved under its name", "")); continue
        # a re-decided entry (--repick) is checked under the same rule: its recorded anchor is the incumbent again
        # (the anchor is taken from the record; that it was the previous record's pick: --before checks it)
        inc = {q: {"body": e["recorded_before"]} for q, e in rec["cells"][cell].items()
               if isinstance(e, dict) and e.get("recorded_before") is not None}
        now = decide(root, stem, a.criterion, inc or None, [])
        runs, tree = grid_capture.load_runs(root, stem), grid_capture.capture_tree_of(root, stem)
        unmeasured = sum(len(set(C.candidate_bodies("interactive", q).values()) - set(C.captured_bodies(runs, "interactive", q, tree).values()))
                         for q in now)
        print(f"    {cell}: {len(now)} queries; {unmeasured} (query, body) candidates at HEAD its capture never ran")
        bad += [(cell, q, "a record entry the capture does not decide", "") for q in extra_entries(rec["cells"][cell], now)]
        for q, d in now.items():
            n += 1
            was = rec["cells"].get(cell, {}).get(q)
            if was is None or was["body"] != d["body"]:
                bad.append((cell, q, was and was["body"], d["body"]))
            elif was.get("body_sha") != d["body_sha"]:
                bad.append((cell, q, f"body {was.get('body_sha')}", f"now {d['body_sha']} (the plan files moved)"))
    print(f"the pick record vs the captures now: {n} (cell, query) compared")
    n_sec = check_sections(a, rec, crit, bad)
    n_car = check_carried(rec, bad)
    n_before = check_before(a, rec, bad)
    print(f"the pick record: {len(bad)} differ or stale (LDBC cells {n}; {', '.join(f'{k} {v}' for k, v in n_sec.items())}; "
          f"carried entries {n_car}; recorded anchors against the previous record {n_before})")
    for b in bad:
        print("    ", *b)
    return 1 if bad or n == 0 or 0 in n_sec.values() else 0


def section_roots(a):
    """{workload: [its capture roots]} from --family WL=ROOT (as the record was written)"""
    roots = {}
    for spec in a.workload or []:
        wl, _, wroot = spec.partition("=")
        if wl not in picks.IN_SAMPLE_WORKLOADS or not wroot:
            raise SystemExit(f"--family {spec}: WL=ROOT with WL one of {picks.IN_SAMPLE_WORKLOADS}")
        roots.setdefault(wl, []).append(wroot)
    return roots


def check_sections(a, rec, crit, bad):
    """--check of the record's in-sample sections (workloads.<WL>: the microbench, BI, JOB), each on its own captures
    (--family WL=ROOT): a decided store's entries equal the chooser's (decide, in-sample, its recorded anchor the
    incumbent again) and its fingerprint holds; a carried store with a capture ran its body ok and, where the capture
    marks the selector's body (planner_pick), it is that body; and, as information only, how many carried entries our
    chooser would pick otherwise. Returns {section: (store, query) entries compared}; a section with 0 fails the check"""
    roots, out = section_roots(a), {}
    for wl, sec in sorted((rec.get("workloads") or {}).items()):
        units = {k: (s, r) for r in roots.get(wl, []) for k, s in stores_in(r)}
        decided = sorted(k for k, fp in sec.get("fingerprints", {}).items() if fp is not None)
        n = 0
        for key in decided:
            if key not in units:
                bad.append((f"{wl}:{key}", "*", f"a decided store with no capture given (--family {wl}=ROOT)", "")); continue
            stem, root = units[key]
            fp = fingerprints(root, stem, in_sample=True)
            if fp != sec["fingerprints"][key]:
                bad.append((f"{wl}:{key}", "*", f"fingerprints differ: record {sec['fingerprints'][key]}", f"now {fp}")); continue
            inc = {q: {"body": e["recorded_before"]} for q, e in sec["cells"][key].items()
                   if isinstance(e, dict) and e.get("recorded_before") is not None}
            now = decide(root, stem, a.criterion, inc or None, [], workload=wl, in_sample=True)
            bad += [(f"{wl}:{key}", q, "a record entry the capture does not decide", "") for q in extra_entries(sec["cells"][key], now)]
            for q, d in now.items():
                n += 1
                was = sec["cells"][key].get(q)
                if was is None or was["body"] != d["body"]:
                    bad.append((f"{wl}:{key}", q, was and was["body"], d["body"]))
                elif was.get("body_sha") != d["body_sha"]:
                    bad.append((f"{wl}:{key}", q, f"body {was.get('body_sha')}", f"now {d['body_sha']} (the plan files moved)"))
        for key in sorted(units):
            if key not in sec["cells"]:
                bad.append((f"{wl}:{key}", "*", "a capture with no record row", ""))
        carried = {k: qs for k, qs in sec["cells"].items() if sec.get("fingerprints", {}).get(k) is None}
        nc, ratios, skipped, uncaptured = 0, [], [], []
        for key, qs in sorted(carried.items()):
            if key not in units:
                uncaptured.append(key)
                continue
            stem, root = units[key]
            rows = list(capture_layout.reader(open(Capture(root).path_of("counts", stem))))
            # the record's body_sha is today's: a captured body rewritten count-equal since reads as its successor
            rows = [{**r, "body_sha": C.successor_sha(wl, r["query"], r.get("body_sha"))} for r in rows]
            marks = "planner_pick" in (rows[0] if rows else {})
            if not marks:
                skipped.append(key)
            by_q = defaultdict(list)
            for r in rows:
                by_q[r["query"]].append(r)
            for q, e in sorted(qs.items()):
                mine = [r for r in by_q.get(q, []) if r.get("body_sha") == e.get("body_sha")]
                nc += 1
                if not mine:
                    bad.append((f"{wl}:{key}", q, f"carried body {e['body']} never ran on its capture", "")); continue
                if any(str(r["ok"]) != "1" for r in mine):
                    bad.append((f"{wl}:{key}", q, f"carried body {e['body']} is not ok on its capture", "")); continue
                sel = {r.get("body_sha") for r in by_q[q] if str(r.get("planner_pick")) == "1"}
                if marks and not sel:
                    skipped.append(f"{key} {q} (no row marked)")
                if marks and sel and e.get("body_sha") not in sel:
                    bad.append((f"{wl}:{key}", q, f"carried body {e['body']} is not the one the selector ran", f"{sorted(sel)}"))
                sc = sha_scores(by_q[q], crit)
                best = min(sc, key=sc.get) if sc else None
                if best is not None and best != e.get("body_sha") and sc.get(e.get("body_sha")):
                    ratios.append(sc[e["body_sha"]] / sc[best] if sc[best] else float("inf"))
        if skipped:
            print(f"{wl}: the selector check skipped on {len(skipped)} stores or queries: their capture does not say which "
                  f"body the selector ran (no planner_pick column, or no row marked): {', '.join(skipped)}")
        if uncaptured:
            print(f"{wl}: {len(uncaptured)} carried stores not checked on a capture (none given under --family {wl}=ROOT): "
                  f"{', '.join(uncaptured)}")
        if nc:
            med = sorted(ratios)[len(ratios) // 2] if ratios else None
            print(f"{wl}: {nc} carried entries checked on their capture; information only: {len(ratios)} are not what our "
                  f"chooser picks on that capture" + (f" (median {med:.3f}x ours by {crit})" if med is not None else ""))
        print(f"{wl}: {n} decided (store, query) compared on {len(decided)} stores; {nc} carried checked on their capture")
        out[wl] = n + nc
    return out


def extra_entries(stored, now):
    """the queries a stored row holds that the chooser does not decide now (a stale entry); carried entries are not decided"""
    return sorted(q for q, e in stored.items() if isinstance(e, dict) and e.get("rule") != pick_record.CARRIED and q not in now)


def sha_scores(rows, crit):
    """{body sha: the criterion's mean score over its ok rows}: the chooser's measure, per body"""
    from kvevaluator.plan_choice.candidates import vector
    from kvevaluator.plan_choice.optimizer.criteria import parse_criterion
    by = defaultdict(list)
    for r in rows:
        if str(r["ok"]) == "1":
            by[r.get("body_sha")].append(r)
    if not by:
        return {}
    sc = parse_criterion(resolve_criterion(crit)[1]).scores({h: vector(rs) for h, rs in by.items()})
    return {h: sc[h] / len(by[h]) for h in by}


def check_carried(rec, bad):
    """every carried entry (v1's selection, decided on no capture of ours; the LDBC cells and every section): its body is
    a candidate at HEAD with the recorded body sha (the plan files did not move under it). Returns how many"""
    n = 0
    sections = [("interactive", rec["cells"])] + [(wl, s["cells"]) for wl, s in sorted((rec.get("workloads") or {}).items())]
    for wl, stores in sections:
        for key, qs in sorted(stores.items()):
            for q, e in sorted(qs.items()):
                if not isinstance(e, dict) or e.get("rule") != pick_record.CARRIED:
                    continue
                n += 1
                head = C.candidate_bodies(wl, q)
                if head.get(e["body"]) != e.get("body_sha"):
                    bad.append((f"{wl}:{key}", q, f"carried {e['body']} {e.get('body_sha')}",
                                f"at HEAD {head.get(e['body'])} (the plan files moved under it)"))
    return n


def check_before(a, rec, bad):
    """--before <previous record>: each re-decided entry's recorded_before is that record's anchor for the same (store,
    query) (pick_record.anchor_of); the cells kept by reference are not re-decided and are skipped. A re-decided record
    checked without it fails: its anchors would be trusted"""
    if not a.before:
        if rec.get("redecided"):
            bad.append(("record", "*", "a re-decided record: give --before <the previous record> (its anchors are checked "
                                       "against it)", ""))
        return 0
    prev = load_record_at(a.before)
    kept = set(rec.get("by_reference", {}))       # rows kept unchanged: their anchors are an earlier decision's
    n = 0
    sections = [("interactive", rec["cells"], prev.get("cells", {}))] + [
        (wl, s["cells"], (prev.get("workloads") or {}).get(wl, {}).get("cells", {}))
        for wl, s in sorted((rec.get("workloads") or {}).items())]
    for wl, stores, old in sections:
        for key, qs in sorted(stores.items()):
            if wl == "interactive" and key in kept:     # tests/gates/meta/check_picks_by_reference.py checks them
                continue
            for q, e in sorted(qs.items()):
                if not isinstance(e, dict) or e.get("rule") == pick_record.CARRIED:
                    continue
                was = pick_record.anchor_of((old.get(key) or {}).get(q))
                if e.get("recorded_before") is None:
                    if was is not None:       # re-decided with no incumbent where the previous record had one
                        bad.append((f"{wl}:{key}", q, "no recorded_before", f"the previous record's anchor {was}"))
                    continue
                n += 1
                if was != e["recorded_before"]:
                    bad.append((f"{wl}:{key}", q, f"recorded_before {e['recorded_before']}", f"the previous record's anchor {was}"))
    if n == 0:
        bad.append(("record", "*", "--before: 0 recorded anchors compared", ""))
    return n


def load_record_at(spec):
    """a record from a table's path or from <git ref>:<table path> (pick_record.load_full_at)"""
    return pick_record.load_full_at(spec)


def diff(a):
    x, y = (pick_record.load_full(p) for p in a.diff)
    moves = []
    for cell in sorted(set(x["cells"]) | set(y["cells"])):
        for q in sorted(set(x["cells"].get(cell, {})) | set(y["cells"].get(cell, {}))):
            vx = x["cells"].get(cell, {}).get(q, {}).get("body"); vy = y["cells"].get(cell, {}).get(q, {}).get("body")
            if vx != vy:
                g = y["cells"].get(cell, {}).get(q, {}).get("held_out_ratio_vs_recorded")
                moves.append((cell, q, vx, f"{vy}  (held-out cost {g}x the recorded plan's)" if g is not None else vy))
    for sf in sorted(set(x.get("scales", {})) | set(y.get("scales", {}))):
        xs, ys = x.get("scales", {}).get(sf, {}), y.get("scales", {}).get(sf, {})
        for cell in sorted(set(xs) | set(ys)):
            for q in sorted(set(xs.get(cell, {})) | set(ys.get(cell, {}))):
                vx = xs.get(cell, {}).get(q, {}).get("body"); vy = ys.get(cell, {}).get(q, {}).get("body")
                if vx != vy:
                    moves.append((f"{cell}@sf{sf}", q, vx, vy))
    print(f"{os.path.basename(a.diff[0])} (sf{x['decided_at']}) -> {os.path.basename(a.diff[1])} (sf{y['decided_at']}): {len(moves)} (cell, query) picks move (the per-scale entries included)")
    for m in moves:
        print(f"    {m[0]:32s} {m[1]:5s} {m[2]} -> {m[3]}")
    return 1 if moves else 0


def record_scale(rec, sf, own, fps, replace):
    """write a scale's own picks `own` ({cell: {query: entry}}) and its held-out fingerprints `fps` ({cell: fp}, one per
    store this call decides) into the record, in place. Without `replace`: per cell, merged into what is there. With it:
    each store this call decides has its entries and fingerprint replaced whole (an earlier entry of it that this call
    does not find again is dropped); a store the call does not decide keeps its entries and fingerprint as they are.
    Returns (dropped, kept): the dropped entries as (cell, query, body), and the stores kept untouched, both sorted"""
    scales, sfps = rec.setdefault("scales", {}), rec.setdefault("scale_fingerprints", {})
    dropped, kept = [], []
    if replace:
        old = scales.get(sf, {})
        dropped = sorted((cell, q, e.get("body")) for cell, qs in old.items() if cell in fps for q, e in qs.items()
                         if own.get(cell, {}).get(q) is None)
        kept = sorted(c for c in set(old) | set(sfps.get(sf, {})) if c not in fps)
        for cell in fps:
            old.pop(cell, None)
        scales[sf] = old
    scales.setdefault(sf, {}).update(own)                 # per cell: a cell's entries are this capture's
    sfps.setdefault(sf, {}).update(fps)
    if not scales[sf]:
        del scales[sf]
    return dropped, kept


def stores_of(rec, a):
    """[(cell, its capture root, the record store whose layout it is)] of this call: every store of the capture but the
    named ones (picks.layout_store, the identity every reader binds by, so what is recorded is what pricing reads; `rec`
    the reader's view, pick_record.load); a cell whose layout no record store has, a store with no row, or two cells of
    one store, are refused"""
    out, seen = [], {}
    root = a.cap
    for _key, cell in stores_in(root):
        if spec_kind(root, cell) == "format":
            continue
        store = picks.layout_store(rec, root, cell)
        if store is None:
            raise SystemExit(f"{cell} under {root}: a composed layout no record store has -- nothing to decide for it")
        if not rec.has_cell("interactive", store):
            raise SystemExit(f"{store} ({cell}): no row in the record decided at sf{rec.decided_at}")
        if store in seen:
            raise SystemExit(f"{store}: two cells of this call are its layout ({seen[store]}, {cell} under {root})")
        seen[store] = f"{cell} under {root}"
        out.append((cell, root, store))
    return out


def small_scale(a, crit):
    """the recorded variant against THIS scale's own held-out rows: over 1 + MARGIN x the cheapest valid candidate =
    the plan rule's scale exception, this scale's own pick (--record writes it into the file's "scales" entry with the numbers);
    not valid here = a finding, printed, never a pick"""
    rec = pick_record.load_full(grid_capture.PICKS_FILE)
    if str(rec["decided_at"]) == str(a.sf):
        raise SystemExit(f"sf{a.sf} is the deciding scale of the record: --small-scale is for the others")
    rows, n, own = [], 0, {}
    recorded_fp, moved = rec.get("scale_fingerprints", {}).get(str(a.sf), {}), []
    stores = stores_of(pick_record.load(grid_capture.PICKS_FILE), a)      # (cell, root, the record store whose layout it is: picks.layout_store, THE identity)
    for cell, root, store in stores:
        hr = grid_capture.load_held_out(root, cell)
        if hr is None:
            raise SystemExit(f"{cell}: no held-out capture at sf{a.sf} under {Capture(root).held_out().path}")
        if store in recorded_fp and fingerprints(root, cell)["held_out"] != recorded_fp[store]:
            moved.append(store)
        today = today_names(root, cell)                          # the record names today's candidates
        for q, d in picks.choose_cell(root, cell, a.criterion, rule=picks.PlanRule(), pinned=False).items():   # this scale's own held-out rule (N6)
            want = rec["cells"][store].get(q, {}).get("body")
            if want is None:
                raise SystemExit(f"{store} {q}: no recorded pick")
            n += 1
            # scored on this scale's held-out rows, over both-valid bodies; keyed by today's names, as the record is
            sc = {today(q, v)[0]: x for v, x in d["held_out_scores"].items()}
            rejected = {today(q, v)[0]: x for v, x in d.get("rejected", {}).items()}
            if want not in sc:
                rows.append((store, q, want, "NOT VALID on this scale's held-out params (a finding)", rejected.get(want, "not measured")))
                continue
            cheapest = min(sorted(sc), key=sc.get)
            ratio = sc[want] / sc[cheapest] if sc[cheapest] else float("inf")
            if ratio > 1 + C.MARGIN:
                rows.append((store, q, want, f"{ratio:.3f}x the cheapest here ({cheapest}): this scale's own pick", ""))
                own.setdefault(store, {})[q] = {"body": cheapest, "body_sha": C.candidate_bodies("interactive", q).get(cheapest),
                                                "recorded_body_ratio": round(ratio, 4), "why": f"the sf{rec['decided_at']} plan is "
                                                f"{ratio:.3f}x the cheapest on sf{a.sf}'s held-out params (CONVENTIONS 19's exception)"}
    print(f"sf{a.sf} against the record decided at sf{rec['decided_at']}: {n} (cell, query) compared; {len(rows)} where the recorded "
          f"variant is over {1 + C.MARGIN:.2f}x the cheapest on this scale's held-out rows or not valid here:")
    for r in rows:
        print(f"    {r[0]:32s} {r[1]:5s} {r[2]:45s} {r[3]} {r[4]}")
    # the pricers take this scale's recorded own picks without checking the capture: they must equal what it gives now
    stale = []
    if not a.record:
        recorded = rec.get("scales", {}).get(str(a.sf), {})
        for _cell, _root, store in stores:
            was = {q: e["body"] for q, e in recorded.get(store, {}).items()}
            isnow = {q: e["body"] for q, e in own.get(store, {}).items()}
            stale += [(store, q, was.get(q), isnow.get(q)) for q in sorted(set(was) | set(isnow)) if was.get(q) != isnow.get(q)]
        for cell, q, was, isnow in stale:
            print(f"    STALE {cell:32s} {q:5s} recorded sf{a.sf} pick {was}, this capture gives {isnow}")
        redo = f"grid_pick_record.py --sf {a.sf} --grid <its grid> --small-scale --record"
        if stale:
            print(f"sf{a.sf}'s recorded own picks differ from this capture on {len(stale)} (cell, query): re-record ({redo})")
        elif moved:
            print(f"note: sf{a.sf}'s held-out capture moved since its entries were recorded on {len(moved)} cells; its own picks "
                  f"are unchanged ({redo} refreshes the fingerprints)")
    if a.record:
        fps = {store: fingerprints(root, cell)["held_out"] for cell, root, store in stores}
        dropped, kept = record_scale(rec, str(a.sf), own, fps, a.replace_scale)
        for cell, q, body in dropped:
            print(f"    DROPPED sf{a.sf} {cell:32s} {q:5s} {body} (--replace-scale: this call decides the store and does not keep it)")
        for cell in kept:
            print(f"    KEPT    sf{a.sf} {cell:32s} (kept: not decided by this call)")
        if a.replace_scale:
            print(f"--replace-scale: sf{a.sf}'s entries of the {len(fps)} stores this call decides replaced whole: {len(dropped)} "
                  f"earlier (cell, query) entries dropped; {len(kept)} stores not decided by this call kept")
        pick_record.dump(rec, grid_capture.PICKS_FILE)
        print(f"recorded sf{a.sf}'s own picks for {sum(len(v) for v in own.values())} (cell, query) in {os.path.relpath(grid_capture.PICKS_FILE)}")
    return 1 if stale or any("NOT VALID" in r[3] for r in rows) else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf")
    ap.add_argument("--grid", default=None, help="LDBC's captures dir at the scale (default output/captures/ldbc/sf<SF>): "
                    "every store of its Interactive capture is decided (the cells, the mixes, the named stores; a carried "
                    "entry, v1's selection, is no anchor: the cheapest on the held-out params)")
    ap.add_argument("--family", dest="workload", action="append", default=None, metavar="WL=CAPTURE", help="an in-sample workload's "
                    "capture (job, bi, microbench): every store in it decided in-sample into the record's workloads.<WL>; repeat")
    ap.add_argument("--pick-by", dest="criterion", default=None, help="the plan pick rule (default the optimizer's)")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--scales", nargs="*", default=None, help="--check: the smaller scales whose recorded held-out fingerprints "
                    "must still hold (default none: a moved one is a note)")
    ap.add_argument("--diff", nargs=2, default=None)
    ap.add_argument("--before", default=None, help="--check: the previous record (a file, or <git ref>:inputs/picks/"
                    "the pick record); a re-decided record's anchors (recorded_before) must be its picks")
    ap.add_argument("--small-scale", action="store_true")
    ap.add_argument("--record", action="store_true", help="--small-scale: write this scale's own picks into the record")
    ap.add_argument("--replace-scale", action="store_true", help="--small-scale --record: for each store this call decides "
                    "(every unit of its --grid and --sets), replace its entries and fingerprint whole (an earlier entry it no "
                    "longer gives is dropped, listed); a store the call does not decide is kept (listed); default per cell")
    ap.add_argument("--repick", action="store_true", help="write: the tracked record's plans are the incumbents (a re-measure "
                    "at the deciding scale)")
    ap.add_argument("--file", default=None, help="the record written or checked (default the tracked inputs/picks/picks.csv)")
    ap.add_argument("--keep-by-reference", default=None, help="--repick: a JSON {cell: the capture it was decided on} of the "
                    "tracked record's cells this run does not capture; their rows are kept unchanged, each with its provenance in "
                    "the record's by_reference field (held-out params, capture, date, binary)")
    ap.add_argument("--incumbents", default=None, help="--repick: the record whose plans are the incumbents (default the "
                    "tracked one; another file is a control)")
    a = ap.parse_args(argv)
    if a.file:
        grid_capture.PICKS_FILE = os.path.abspath(a.file)
    if a.incumbents:
        global TRACKED
        TRACKED = os.path.abspath(a.incumbents)
    crit = resolve_criterion(a.criterion or DEFAULT_CRITERION)[0]
    if a.diff:
        return diff(a)
    if not a.sf:
        raise SystemExit("--sf is required")
    a.grid = a.grid or str(Tree().place_dir("captures", "ldbc", a.sf))
    a.cap = capture_in(a.grid, "interactive").path
    if a.replace_scale and not (a.small_scale and a.record):
        raise SystemExit("--replace-scale goes with --small-scale --record")
    if a.small_scale:
        return small_scale(a, crit)
    return check(a, crit) if a.check else write(a, crit)


if __name__ == "__main__":
    sys.exit(main())
