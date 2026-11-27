"""The C++ WRITE-run helpers two drivers share: the fixed write-cell SEQUENCE over the writes plan
dirs (conflict groups split, destructive plans last), the disposable ARENA (measure/arenas.py's link
copy, which the apply-writes mode requires), the shadow-then-apply cell runner and the shadow == apply
CROSS-CHECK (every step count, every byte channel, the rows). measure/cppwlatency.py (the write-
latency study) and tests/gates/check_cpp_write_counts.py (the write-count oracle) both import from here.
"""
import glob
import json
import os
import sys
import time
from kvevaluator.measure.stores import store_dir
from kvevaluator.measure.plan_runs import PLAN_DIRS
from kvevaluator.paths import ROOT
from kvevaluator.workloads.writes import params as w_params
from kvevaluator.measure import arenas
from kvevaluator.capture.step_record import HIT_BYTE_LANES, MISS_OPS, WRITE_BYTE_LANES

_EXP = str(ROOT)

_PLANS = {d: os.path.join(_EXP, "python", "kvevaluator", "workloads", *d.split("/"))
          for d in PLAN_DIRS["writes"]}

_CHECKED = ("S", "G", "N", "P", "D")


_BYTE_CHECKED = (*WRITE_BYTE_LANES, *HIT_BYTE_LANES)   # the lanes shadow and apply must agree on (misses are not compared)


_DESTRUCTIVE = ["w_del_e", "w_del_v_record", "w_del_v"]   # in this order, after everything else


_SPLIT_GROUPS = [["w_del_v_record", "w_del_v"],           # person_incident: split
                 ["w_ins_e_plain", "w_ins_e_facet", "iu8"],  # new_pairs: split
                 ["iu2", "iu3"],                          # person_incident: same likes edge
                 ["w_ins_v_full", "w_ins_v_min"]]         # person_incident: same new vid


def sequence(sf, queries, nparam):
    """The fixed, recorded cell order: non-destructive plans in (workload, filename) order
    with up to `nparam` params each, then the destructive plans last. Conflict-group plans
    split their (5-row) stratified family as evenly as possible (remainder spread from the
    front) instead of sharing it -- so a cell never targets what an earlier cell deleted or
    inserted. An empty slice is an error, never a silent drop."""
    plans = []
    for wl in ("writes/microbench/plans", "writes/ldbc/plans"):
        for path in sorted(glob.glob(os.path.join(_PLANS[wl], "*.json"))):
            q = os.path.basename(path)[:-5]
            if queries and q not in queries:
                continue
            plans.append((wl, q, path))
    plans.sort(key=lambda t: (_DESTRUCTIVE.index(t[1]) if t[1] in _DESTRUCTIVE else -1))
    splits = {}
    for group in _SPLIT_GROUPS:
        present = [q for q in group if any(p[1] == q for p in plans)]
        for i, q in enumerate(present):
            splits[q] = (i, len(present))                 # my slice of the family rows
    seq = []
    for wl, q, path in plans:
        rows = w_params.param_rows(sf, q)
        if q in splits:
            i, n = splits[q]
            base, rem = divmod(len(rows), n)
            counts = [base + (1 if k < rem else 0) for k in range(n)]
            off = sum(counts[:i])
            rows = rows[off:off + counts[i]]
            if not rows:
                raise SystemExit(f"{q}: its {n}-way split of {sum(counts)} family rows "
                                 f"leaves it no params -- family too small")
        take = rows[:nparam]
        for idx, r in enumerate(take, 1):
            seq.append((wl, q, idx, w_params.args_from_row(q, r), json.load(open(path))))
    return seq


def cross_check(shadow, apply_, where, names=("shadow", "apply")):
    """Everything the two runs compute over the same store state must be identical: per-step
    op counts, ALL byte channels and the last-chunk touches (a value-level overlay-vs-store divergence moves get/scan
    bytes and rows while leaving G unchanged), per-step rows, and the final row payload.
    The free real-vs-model check every sweep carries; any difference aborts. names: the two runs, as the errors say them."""
    x, y = names
    ss, aa = shadow["steps"], apply_["steps"]
    if len(ss) != len(aa):
        raise SystemExit(f"{where}: step count differs {x} {len(ss)} vs {y} {len(aa)}")
    for i, (s, a) in enumerate(zip(ss, aa)):
        for k in _CHECKED + ("rows",):
            if s[k] != a[k]:
                raise SystemExit(f"{where} step {i} ({s['name']}): {k} {x} {s[k]} != {y} {a[k]}")
        for k in _BYTE_CHECKED:
            if s["bytes"][k] != a["bytes"][k]:
                raise SystemExit(f"{where} step {i} ({s['name']}): {k}_bytes {x} "
                                 f"{s['bytes'][k]} != {y} {a['bytes'][k]}")
        # the item-size histograms (one entry per GET / range read / PUT / DELETE; a delete's size is what the plan read)
        if s.get("hist") != a.get("hist"):
            raise SystemExit(f"{where} step {i} ({s['name']}): item-size histograms {x} {s.get('hist')} != {y} {a.get('hist')}")
        # the last-chunk touches (the overlay-aware probe in shadow, the store's own key in apply: they must agree)
        if s.get("last_chunk") != a.get("last_chunk"):
            raise SystemExit(f"{where} step {i} ({s['name']}): last-chunk touches {x} {s.get('last_chunk')} != {y} "
                             f"{a.get('last_chunk')}")
    if json.dumps(shadow["rows"], sort_keys=True) != json.dumps(apply_["rows"], sort_keys=True):
        raise SystemExit(f"{where}: final row payload differs {x} vs {y}")


def same_counts(one, other, where, names=("4-leg apply", "apply after warm-up")):
    """Two APPLY runs of one cell on the same store contents (cppwlatency --same-counts-as): what cross_check compares,
    plus the misses and every byte lane (shadow and apply may differ there; two applies must not). The cache decides
    which reads hit, never a count, so any difference is a bug."""
    cross_check(one, other, where, names)
    for i, (s, a) in enumerate(zip(one["steps"], other["steps"])):
        for k in MISS_OPS:
            if s.get(k, 0) != a.get(k, 0):
                raise SystemExit(f"{where} step {i} ({s['name']}): {k} {names[0]} {s.get(k, 0)} != {names[1]} {a.get(k, 0)}")
        if s["bytes"] != a["bytes"]:
            raise SystemExit(f"{where} step {i} ({s['name']}): bytes {names[0]} {s['bytes']} != {names[1]} {a['bytes']}")


def run_cell(srv, cell, run_apply):
    """warm shadow (discarded) -> control shadow (kept) -> apply (kept, mutates).
    Both kept runs are warm, so apply - control isolates the engine write; the shadows
    must run BEFORE apply (after it, the state has moved: an insert would re-insert)."""
    wl, q, idx, args, ir = cell
    run_shadow = {k: v for k, v in run_apply.items() if k != "apply_writes"}
    for leg in ("shadow warm-up", "shadow"):
        shadow = srv.run(ir, args, run=run_shadow)
        if shadow.get("error"):
            raise SystemExit(f"{wl}/{q}/p{idx} {leg}: {shadow['error']}")
    applied = srv.run(ir, args, run=run_apply)            # the timed real write
    if applied.get("error"):
        raise SystemExit(f"{wl}/{q}/p{idx} apply: {applied['error']}")
    cross_check(shadow, applied, f"{wl}/{q}/p{idx}")
    return applied, shadow


class Arena(arenas.Arena):
    """One disposable link copy of a pristine store for a write run: measure/arenas.py's Arena (under <stores root>/arenas/,
    the base never opened and checked unchanged, removed only by its marker). The run id carries the time, the process
    and the tag, so two runs never share an arena."""

    def __init__(self, sf, arm, tag, root=None):
        # root: the stores' scale dir (output/stores/<dataset>/<scale>)
        src = os.path.join(root, arm) if root else store_dir("ldbc", sf, arm)
        run_id = f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-p{os.getpid()}-{tag}"
        super().__init__(src, run_id, driver=os.path.basename(sys.argv[0]))
