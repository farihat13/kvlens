"""The optimizer's store-free equivalence pre-gate.

Checks over every searched query that has a hand target (hand_targets: every shipped LOGICAL entry), every format x
design the IR bundles serve, no stores and no measuring. A query with no hand target (one a user adds, as in the
layout-cost walkthrough) has no hand builder to compare: it is skipped, named on one line; check 4 still covers it.

1. CANDIDATE <-> BUILDER: each legal choice combo's expansion must serialize
   byte-identically (plan_to_ir + lower_ir, key order included) to the hand
   builder it claims to reproduce. Candidate labels FACTOR per choice point
   (entry x filter x fetch ...); the hand target factors the same way -- the
   FILTER dimension's target is the hand builder lowered under SYNTHETIC caps
   (with/without the type cap), because that hand body varies by caps, not by
   builder. Combos with NO hand counterpart are NEW shapes, counted apart.
2. LEGALITY is implied by 1+3 (an expected candidate missing from the legal set
   fails the winner table loudly).
3. WINNER TABLE: the SIM/LABEL_ONLY registries' resolution equals the expected
   candidate per (query, format, design), with per-dimension expectations
   COMPUTED (the filter dim from caps; the ic8 flip cells exempted: v1's search picked the walk on the row stores,
   and today every legal body is in ic8's plan file, the pick record naming the one a store runs).
4. No candidate expansion emits an execute-only op.

Run: python3 tests/unit/check_optimizer_rules.py
"""
import json
import os
import sys

from kvevaluator.plan_choice.dump_planir import CANON_DESIGNS, CANON_FORMATS, TYPE_CAP, lower_ir_steps
from kvevaluator.datasets.ldbc.schema import get_schema
from kvevaluator.plan_choice.optimizer.rules import choice_points, expand
from kvevaluator.plan_choice.optimizer.rules import legal_choices
from kvevaluator.plans.ir import plan_to_ir
from kvevaluator.formats.caps import caps_for, edge_caps_of
from kvevaluator.plans.ir.lowering import LIST_CAP, REVERSE_CAP
from kvevaluator.workloads.interactive import queries as Q
import importlib
# a query's builders, through its own module
_m = lambda name: importlib.import_module(f"kvevaluator.workloads.interactive.queries.{name}")
ic1_mod, ic3_mod, ic6_mod, ic8_mod = _m("ic1"), _m("ic3"), _m("ic6"), _m("ic8")
ic10_mod, ic11_mod, ic12_mod, is2_mod = _m("ic10"), _m("ic11"), _m("ic12"), _m("is2")

DGRAPH_SIDE = {"dgraph"}     # two-phase side; the rest are row stores
ALL_DESIGNS = {**CANON_DESIGNS, "label_only": get_schema("label_only")}
CAPS_IDX, CAPS_PROBE = frozenset({TYPE_CAP}), frozenset()
PASSTHROUGH = ["ic2_nolimit", "ic3_getdate", "ic5",
               "is1", "is3", "is4", "is5", "is6", "is6_capped",
               # the wide-read / digest queries (no choice points; the search records pick "")
               "forum_board_render", "full_profile_page", "liker_digest",
               "network_profile_digest", "profile_multiget", "reply_thread_render"]
CANON_ONLY = {q for q in Q.LOGICAL if q.endswith(("_capped", "_nolimit", "_getdate"))}

RANK3 = lambda w, g, o: {"index_walk": w, "two_phase": g, "one_phase": o}
_hand = lambda q: Q._pick(Q.REGISTRY[q], "dgraph")


def hand_targets(q):
    """{candidate label: (builder, caps_override or None)} -- the hand twin each
    label must byte-match; caps_override = synthetic caps for the FILTER dim
    (None = the loop format's own caps, both sides). Absent label = NEW shape.
    None: the query has no hand target (a query someone added)."""
    if q in ("ic2", "ic9"):
        m = _m(q)
        b = RANK3(getattr(m, f"{q}_ixwalk"), getattr(m, f"{q}_getrank"), getattr(m, f"{q}_onephase"))
        return {l: (f, None) for l, f in b.items()}
    if q == "ic8":
        return {l: (f, None) for l, f in RANK3(
            ic8_mod.ic8_ixwalk, ic8_mod.ic8_getrank, ic8_mod.ic8_onephase).items()}
    if q == "is2":
        return {l: (f, None) for l, f in RANK3(
            is2_mod.is2_ixwalk, is2_mod.is2_getrank, is2_mod.is2_onephase).items()}
    if q == "is2_capped":
        return {"index_walk": (is2_mod.is2_ixwalk_capped, None),
                "one_phase": (is2_mod.is2_onephase_capped, None)}
    if q == "ic1":
        return {"index_eq+fetch_deferred": (ic1_mod.ic1, None),
                "index_eq+fetch_inline": (ic1_mod.ic1_onephase, None),
                "label_scan+fetch_deferred": (ic1_mod.ic1_labelonly, None),
                "label_scan+fetch_inline": (ic1_mod.ic1_labelonly_onephase, None)}
    if q == "ic3":                # the full read of the window is not offered (rules.py WINDOW_RULES)
        return {"index_eq+index_eq+range_index": (ic3_mod.ic3, None)}
    if q == "ic11":
        return {"index_eq": (ic11_mod.ic11, None),
                "label_scan": (ic11_mod.ic11_labelonly, None)}
    if q == "ic7":
        return {"materialize_intersect": (_hand("ic7"), None)}
    if q == "is7":
        # the hand plan names no reads: the lowering writes the default of the edge type's caps (lowering.py). So it
        # is the twin of edge_probe under "lists + in lists kept" and of edge_probe_out under any other caps
        return {"edge_probe": (_hand("is7"), None, lambda etype: {LIST_CAP, REVERSE_CAP}),
                "edge_probe_out": (_hand("is7"), None, lambda etype: set())}
    if q == "ic4":
        return {"filter_index": (_hand("ic4"), CAPS_IDX),
                "filter_probe": (_hand("ic4"), CAPS_PROBE)}
    if q in ("ic6", "ic12"):
        eb = {"ic6": {"index_eq": ic6_mod.ic6, "label_scan": ic6_mod.ic6_labelonly},
              "ic12": {"index_eq": ic12_mod.ic12, "label_scan": ic12_mod.ic12_labelonly}}[q]
        return {f"{e}+{f}": (b, c) for e, b in eb.items()
                for f, c in (("filter_index", CAPS_IDX), ("filter_probe", CAPS_PROBE))}
    if q == "ic12_capped":
        return {f"index_eq+{f}": (ic12_mod.ic12_capped, c)
                for f, c in (("filter_index", CAPS_IDX), ("filter_probe", CAPS_PROBE))}
    if q == "ic10":
        fb = {"fetch_deferred": ic10_mod.ic10, "fetch_inline": ic10_mod.ic10_onephase}
        return {f"{f}+{d}": (b, c) for d, b in fb.items()
                for f, c in (("filter_index", CAPS_IDX), ("filter_probe", CAPS_PROBE))}
    if q in PASSTHROUGH:
        return {"": (_hand(q), None)}
    return None


def expected_candidate(q, fmt, design, schema, points):
    """The hand resolution as per-point rule names (a LIST in point order;
    'caps_default' marks a filter point verified via the sentinel path)."""
    caps = caps_for(fmt, schema)
    lo = design == "label_only"
    parts = []
    for s in points:
        if s.op == "rank":
            if lo:
                parts.append("two_phase" if fmt in DGRAPH_SIDE else "one_phase")
            elif q in ("ic2", "ic9"):
                parts.append("index_walk")
            else:
                parts.append("index_walk" if fmt in DGRAPH_SIDE else "one_phase")
        elif s.op == "entry_eq":
            parts.append("label_scan" if lo else "index_eq")
        elif s.op == "window_narrow":
            parts.append("range_index")
        elif s.op == "fetch_props":
            parts.append("fetch_deferred" if fmt in DGRAPH_SIDE else "fetch_inline")
        elif s.op == "membership_flag":
            # is7's hand plan probes with the default reads of the edge type's caps on this format
            ecaps = edge_caps_of(fmt, schema)(s.args["etype"])
            probe = "edge_probe" if {LIST_CAP, REVERSE_CAP} <= ecaps else "edge_probe_out"
            parts.append(probe if q == "is7" else "materialize_intersect")
        elif s.op == "filter_by_label":
            # the hand lowering is caps-driven; where the caps-implied candidate is
            # NOT legal by design (arango's intrinsic index cap under no_type_index),
            # emission uses the caps_default sentinel -- expect that path
            single = isinstance(s.args.get("label"), str)
            want = "filter_index" if single and TYPE_CAP in caps else "filter_probe"
            legal = {r["name"] for r in _filter_legal(s, schema)}
            parts.append(want if want in legal else "caps_default")
        else:
            raise AssertionError(f"unknown point {s.op}")
    return parts


def _filter_legal(step, schema):
    from kvevaluator.plan_choice.optimizer.rules import legal_rules
    return legal_rules(step, schema)


def body_of(plan, q, caps, edge_caps=None):
    ir = plan_to_ir(plan, name=q)
    return ({"steps": lower_ir_steps(ir["steps"], caps, edge_caps), "output": ir["output"]},
            ir["params"], ir.get("let") or {})


def main():
    ok = True
    n = new_shapes = 0
    distinct = set()
    skipped = []
    for q in sorted(Q.LOGICAL):
        logical = Q.LOGICAL[q]({})
        points = choice_points(logical)
        targets = hand_targets(q)
        if targets is None:
            skipped.append(q)
            print(f"skip {q}: no hand target (a query someone added, with no hand builder to compare); "
                  f"the execute-only guard still covers it")
            continue
        for design, schema in ALL_DESIGNS.items():
            if design == "label_only" and q in CANON_ONLY:
                continue
            cands = dict(legal_choices(logical, schema))
            if not cands and _no_window_rule(logical, schema):
                print(f"no candidate: {q}@{design} (no sorted date index for its window: the full read is not offered)")
                continue
            for fmt in CANON_FORMATS:
                fmt_caps = caps_for(fmt, schema)
                for label, choice in sorted(cands.items()):
                    if label not in targets:
                        new_shapes += 1
                        continue
                    builder, caps_ovr, *edge_ovr = targets[label]
                    caps = caps_ovr if caps_ovr is not None else fmt_caps
                    ecaps = edge_ovr[0] if edge_ovr else edge_caps_of(fmt, schema)   # an edge-caps override: as caps_ovr
                    hand, hp, hl = body_of(builder({}), q, caps, ecaps)
                    mine, mp, ml = body_of(expand(logical, choice, schema), q, caps, ecaps)
                    if (json.dumps(hand) != json.dumps(mine) or hp != mp or hl != ml):
                        print(f"FAIL {q} {fmt}@{design} {label}: expansion differs from "
                              f"the hand builder")
                        _first_diff(hand, mine)
                        ok = False
                    n += 1
                    distinct.add((q, label, json.dumps(hand)))
                # winner table (ic8 flip cells exempted: see the docstring)
                if q == "ic8" and design != "label_only" and fmt not in DGRAPH_SIDE \
                        and fmt != "arangodb":
                    continue
                reg = Q.LABEL_ONLY.get(q, Q.REGISTRY[q]) if design == "label_only" \
                    else Q.REGISTRY[q]
                parts = expected_candidate(q, fmt, design, schema, points)
                choice = dict(zip([s.out for s in points], parts))
                label = "+".join(parts)
                if "caps_default" not in parts and label not in cands:
                    print(f"FAIL {q} {fmt}@{design}: expected {label} not in legal set "
                          f"{sorted(cands)}")
                    ok = False
                    continue
                hand_body = json.dumps(body_of(Q._pick(reg, fmt)({}), q, fmt_caps, edge_caps_of(fmt, schema))[0])
                cand_body = json.dumps(body_of(expand(logical, choice, schema), q, fmt_caps, edge_caps_of(fmt, schema))[0])
                if hand_body != cand_body:
                    print(f"FAIL {q} {fmt}@{design}: registry body != expected {label}")
                    ok = False
    ok &= _design_family_invariant()
    ok &= _no_execute_only_ops()
    print(f"{'OK' if ok else 'FAILED'}: {n} (query, format, design, candidate) combinations "
          f"byte-checked against the hand builders ({len(distinct)} distinct bodies; "
          f"{new_shapes} combination slots are NEW shapes with no hand counterpart"
          + (f"; {len(skipped)} queries with no hand target skipped: {', '.join(skipped)})" if skipped else ")"))
    return 0 if ok else 1


def _no_window_rule(logical, schema):
    """a window_narrow point with no legal rule on this design: range_index needs the sorted date index"""
    from kvevaluator.plan_choice.optimizer.rules import legal_rules
    return any(s.op == "window_narrow" and not legal_rules(s, schema) for s in choice_points(logical))


def _design_family_invariant():
    """The canonical picks family serves BOTH canonical and no_type_index profiles
    with ONE measured pick per (query, format). That is sound only while the two
    schemas differ in NOTHING but the type-index declarations (a value/date index
    difference would make one measurement speak for two different legality sets --
    the latent entry_eq hazard). Assert it."""
    a, b = CANON_DESIGNS["canonical"], CANON_DESIGNS["no_type_index"]
    ok = True
    if a.indexed_cols != b.indexed_cols or a.date_cols != b.date_cols:
        print("FAIL: canonical vs no_type_index differ beyond the type index "
              "(indexed/date cols) -- one canonical-family pick can no longer "
              "speak for both profiles")
        ok = False
    print(f"design-family invariant: canonical vs no_type_index differ only in the "
          f"type index -- {'OK' if ok else 'FAILED'}")
    return ok


def _no_execute_only_ops():
    """Guard: no candidate expansion emits an op the audit marked execute-only /
    unmodeled -- the search may only offer plans the whole toolchain can cost."""
    from kvevaluator.plan_choice.optimizer.rules import EXECUTE_ONLY_OPS
    bad = []
    for q in sorted(Q.LOGICAL):
        logical = Q.LOGICAL[q]({})
        for design, schema in ALL_DESIGNS.items():
            for label, choice in legal_choices(logical, schema):
                ops = {s.op for s in expand(logical, choice, schema).steps}
                hits = sorted(ops & EXECUTE_ONLY_OPS)
                if hits:
                    bad.append((q, design, label, hits))
    for b in bad[:5]:
        print("FAIL execute-only op in a candidate:", b)
    print(f"execute-only guard: {len(bad)} violations across all candidates "
          f"-- {'OK' if not bad else 'FAILED'}")
    return not bad


def _first_diff(a, b):
    sa, sb = a["steps"], b["steps"]
    for i, (x, y) in enumerate(zip(sa, sb)):
        if x != y:
            print(f"    step {i}: hand {json.dumps(x)[:160]}")
            print(f"            mine {json.dumps(y)[:160]}")
            return
    if len(sa) != len(sb):
        print(f"    step count: hand {len(sa)} vs mine {len(sb)}")
        tail = sa[len(sb):] or sb[len(sa):]
        print(f"    extra: {json.dumps(tail[0])[:160]}")


if __name__ == "__main__":
    sys.exit(main())
