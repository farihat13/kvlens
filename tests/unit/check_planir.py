"""Validate the language-neutral plan IR (python/kvevaluator/plans/ir).

1. expr grammar selftest.
2. bundle: capability variant-select + fragment inlining (hygiene) -- structural.
3. a hand-authored filter-IR run end-to-end through the interpreter (expr path).
4. round-trip EVERY lambda-free registry query: Step-plan -> IR -> JSON -> IR ->
   Step -> run_plan, and assert the IR run gives byte-identical S/G/N + bytes and
   the same result as the original Python plan, on dgraph / nebula / pertype_csr.
   Queries whose plan embeds a lambda (filter.pred / derive.fn) are SKIPPED -- they
   await expression-AST authoring (that is the next slice, not a failure here).

Run: python3 tests/unit/check_planir.py
"""
import os
import sys

import copy
import json

from kvevaluator.kvstore.kvstore import AbstractKV, CountingKV
from kvevaluator.oracle.runner import run_plan
from kvevaluator.measure.report import normalize
from kvevaluator.datasets.demo import tiny, DEMO_SCHEMA
from kvevaluator.workloads.interactive.queries import REGISTRY, lower_filter
from kvevaluator.formats.dgraph import DgraphFormat
from kvevaluator.formats.nebula import NebulaFormat
from kvevaluator.plans.plan import Plan
from kvevaluator.plans.ir import plan_to_ir, NeedsExpr, bind, bind_ir
from kvevaluator.oracle.interp import run_ir
from kvevaluator.plans.ir.expr import _selftest as expr_selftest
from kvevaluator.plans.ir.bundle import body_sha, covers, select_variant, expand_fragments
from kvevaluator.formats.caps import caps_for, edge_caps_of
from kvevaluator.datasets.ldbc.schema import get_schema
from declared_skips import DEMO_RUNNABLE, failure

_D = 86400000
DEMO_ARGS = {k: str(v) for k, v in {
    "person": "A", "person1": "A", "person2": "D",
    "startDate": 100 * _D, "durationDays": 10, "date": 105 * _D,
    "maxDate": 200 * _D, "minDate": 0,
    "tagName": "Alice", "tagClassName": "Alice",
    "countryXName": "Alice", "countryYName": "Bob",
    "horoscopeSign": 1, "month": 1,
    "message": "A", "messageId": "A", "post": "A", "comment": "A",
}.items()}

# dgraph + nebula sit on opposite corners (split-prop/condensed-adj vs the mirror), so
# an IR run matching both is a strong fidelity check. pertype_csr needs LDBC-encoded uids
# (not the demo's raw vids), so it validates on the rocksdb sf0.1 path -- a follow-up.
FORMATS = [DgraphFormat, NebulaFormat]


def cost_rows(kvcosts):
    return [(s.op, (a.S, a.G, a.N, a.value_bytes, a.scan_key_bytes, a.get_key_bytes))
            for s, _p, a in kvcosts]


def make_store(FmtCls):
    fmt = FmtCls(DEMO_SCHEMA)
    raw = AbstractKV()
    fmt.materialize(tiny(), raw)
    store = CountingKV(raw, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes)
    fmt.warm(store)
    return fmt, store


# ---- 1. expression grammar -------------------------------------------------
def test_expr():
    expr_selftest()


# ---- 2. bundle machinery (structural) --------------------------------------
def test_bundle():
    bundle = {
        "variants": [{"requires": ["sorted_range_index"], "plan": "walk"},
                     {"requires": [], "plan": "scan"}],
        "plans": {"walk": {"steps": []}, "scan": {"steps": []}},
    }
    canon = get_schema("canonical")
    assert select_variant(bundle, caps_for("nebula", canon)) == "walk"    # has sorted index
    assert select_variant(bundle, caps_for("dgraph", canon)) == "scan"    # unsorted only -> fallback
    # the type-filter capability follows the DESIGN: dgraph holds it only with the type index
    assert "type_index_single_posting" in caps_for("dgraph", canon)
    assert "type_index_single_posting" not in caps_for("dgraph", get_schema("no_type_index"))
    assert "type_index_single_posting" not in caps_for("nebula", canon)
    # the NEGATIVE selector: a variant for formats that hold `requires` and NONE of `lacks`
    # (arangodb holds index_entries_by_docid; nebula does not)
    neg = {"variants": [{"requires": ["whole_row_read"], "lacks": ["index_entries_by_docid"], "plan": "plain"},
                        {"requires": ["whole_row_read"], "plan": "one_phase"},
                        {"requires": [], "plan": "scan"}],
           "plans": {"plain": {"steps": []}, "one_phase": {"steps": []}, "scan": {"steps": []}}}
    assert select_variant(neg, caps_for("nebula", canon)) == "plain"      # row store, vid-keyed index
    assert select_variant(neg, caps_for("arangodb", canon)) == "one_phase"   # holds the lacked cap
    assert select_variant(neg, caps_for("dgraph", canon)) == "scan"       # no whole rows at all
    # `on`-annotated lacks: the label's OWNER must lack it
    neg_on = {"variants": [{"requires": [], "lacks": ["whole_row_read"], "on": {"whole_row_read": ["Post"]}, "plan": "a"},
                           {"requires": [], "plan": "b"}],
              "plans": {"a": {"steps": []}, "b": {"steps": []}}}
    assert select_variant(neg_on, set(), lambda l: {"whole_row_read"} if l == "Post" else set()) == "b"
    assert select_variant(neg_on, set(), lambda l: set()) == "a"
    try:
        select_variant({"variants": [{"requires": [], "lacks": ["no_such_cap"], "plan": "x"}], "plans": {"x": {}}}, set())
        raise AssertionError("unknown cap in lacks must raise")
    except ValueError:
        pass
    # per-scale pins: a `scales`-tagged variant runs only at a listed scale; the untagged one is the
    # default everywhere else; a tagged variant with no scale known refuses (never a guess)
    sc = {"variants": [{"requires": [], "scales": ["0.1"], "plan": "small"},
                       {"requires": [], "plan": "big"}],
          "plans": {"small": {"steps": []}, "big": {"steps": []}}}
    assert select_variant(sc, set(), scale="0.1") == "small"
    assert select_variant(sc, set(), scale="30") == "big"
    assert select_variant(sc, set(), scale=3) == "big"            # a numeric tag compares as its string
    try:
        select_variant(sc, set())
        raise AssertionError("a scale-tagged variant with no scale must raise")
    except ValueError:
        pass
    assert select_variant(bundle, caps_for("nebula", canon), scale="30") == "walk"   # untagged bundles ignore the scale
    # the scale decides only among variants whose CAPS match: a tagged variant for another format's caps neither
    # selects nor raises on a scale-blind caller (the first tagged pin must not break every other format's runs)
    sc2 = {"variants": [{"requires": ["whole_row_read"], "scales": ["0.1"], "plan": "rows_small"},
                        {"requires": ["whole_row_read"], "plan": "rows"},
                        {"requires": [], "plan": "scan"}],
           "plans": {"rows_small": {"steps": []}, "rows": {"steps": []}, "scan": {"steps": []}}}
    assert select_variant(sc2, caps_for("dgraph", canon)) == "scan"                   # dgraph: no scale needed
    assert select_variant(sc2, caps_for("nebula", canon), scale="0.1") == "rows_small"
    assert select_variant(sc2, caps_for("nebula", canon), scale="3") == "rows"
    try:
        select_variant(sc2, caps_for("nebula", canon))
        raise AssertionError("nebula matches the tagged variant: no scale must raise")
    except ValueError:
        pass
    assert covers(sc, set(), scale="30") and covers(sc2, caps_for("dgraph", canon))
    for bad in ("30", [], [3], None):                                                 # the shape is validated
        try:
            select_variant({"variants": [{"requires": [], "scales": bad, "plan": "x"}], "plans": {"x": {"steps": []}}}, set(), scale="30")
            raise AssertionError(f"scales={bad!r} must raise")
        except ValueError:
            pass
    assert body_sha(sc, "big") == body_sha({"plans": {"big": sc["plans"]["big"]}}, "big")   # the sha is the body's alone

    frag = {"fragment": "double", "inputs": {"x": {}}, "output": "out",
            "steps": [{"name": "a", "op": "pluck", "rows": {"in": "x"}, "field": "dst"},
                      {"name": "out", "op": "dedup", "items": {"ref": "a"}}]}
    plan = {"steps": [{"name": "d", "use": "double", "args": {"x": {"ref": "src"}}},
                      {"name": "final", "op": "count", "items": {"ref": "d"}}]}
    flat = expand_fragments(plan, [frag])["steps"]
    assert [s["name"] for s in flat] == ["d.a", "d", "final"], flat
    assert flat[0]["rows"] == {"ref": "src"}          # {"in":"x"} substituted
    assert flat[1]["items"] == {"ref": "d.a"}         # internal ref prefixed
    assert flat[2]["items"] == {"ref": "d"}           # export renamed to host
    # every capability is classified exactly once: VALIDITY (a body is wrong / fails without it) or SELECTOR (cost)
    from kvevaluator.plans.ir.capabilities import (CAPABILITIES, SELECTOR_CAPS, VALIDITY_CAPS, VALIDITY_IMPLIED,
                                                   validity_caps)
    assert not (VALIDITY_CAPS & SELECTOR_CAPS), f"classified twice: {sorted(VALIDITY_CAPS & SELECTOR_CAPS)}"
    assert VALIDITY_CAPS | SELECTOR_CAPS == CAPABILITIES, \
        f"unclassified: {sorted(CAPABILITIES - VALIDITY_CAPS - SELECTOR_CAPS)}; unknown: {sorted((VALIDITY_CAPS | SELECTOR_CAPS) - CAPABILITIES)}"
    assert all(k in CAPABILITIES and v <= VALIDITY_CAPS for k, v in VALIDITY_IMPLIED.items())
    assert validity_caps(["type_index_single_posting", "whole_row_read"]) == {"type_index"}
    assert validity_caps(["per_type_index", "unsorted_range_index"]) == {"per_type_index", "unsorted_range_index"}
    # dgraph's per-LABEL caps: the date-index designs scope only Post/Comment creationDate (day tokens, or key-sorted)
    from kvevaluator.formats.caps import caps_resolver
    for design in ("type_scoped_date", "value_sorted_date", "key_sorted_date"):
        sch, of = get_schema(design), caps_resolver("dgraph", get_schema(design))
        assert "per_type_index" not in caps_for("dgraph", sch) and "sorted_range_index" not in caps_for("dgraph", sch)
        assert "per_type_index" in of("Post") and "per_type_index" in of("Comment"), design
        assert "per_type_index" not in of("Person") and "sorted_range_index" not in of("Person"), design
        assert ("sorted_range_index" in of("Post")) == (design == "key_sorted_date"), design   # exact only when key-sorted
    full = get_schema("full_index")
    assert {"per_type_index", "sorted_range_index"} <= caps_for("dgraph", full)          # every index scoped + key-sorted
    assert caps_resolver("dgraph", canon)("Post") == caps_for("dgraph", canon)            # canonical: no per-label change
    print("bundle selftest OK (variant select + fragment inline + capability classes)")


# ---- 3. hand-authored filter IR through the interpreter --------------------
def test_filter_ir():
    # demo: A knows B(35),C(15); D knows A -> A's undirected neighbors {B,C,D=40}.
    # filter age < 40 -> {B, C}. Exercises the expr path through the interpreter.
    from kvevaluator.schema.vids import person_vid
    ir = {"plan": "knows_young", "bind": {"maxAge": 40},
          "steps": [
              {"name": "me", "op": "read_vertex_by_id", "vid": person_vid("A"), "label": "Person"},
              {"name": "nbrs", "op": "read_neighbors", "srcs": {"ref": "me"},
               "etype": "knows", "dir": "both"},
              {"name": "props", "op": "read_vertex_props", "vids": {"ref": "nbrs"},
               "props": ["age"], "label": "Person"},
              {"name": "young", "op": "filter", "rows": {"ref": "props"},
               "pred": {"lt": [{"field": "age"}, {"param": "maxAge"}]}}],
          "output": "young"}
    for FmtCls in FORMATS:
        fmt, store = make_store(FmtCls)
        outs, _ = run_ir(ir, fmt, store)
        ages = sorted(r["age"] for r in outs["young"])
        assert ages == [15, 35], f"{FmtCls.name}: age<40 gave {ages}"
    print("filter-IR selftest OK (expr path end-to-end: neighbors age<40 -> B,C)")
    # a pair test (join_multi's where / where_key / where_in / where_not_in) on another op is refused, not ignored
    # (the C++ side: tests/test_variant_select.cc)
    from kvevaluator.plans.ir.interp import ir_plan_to_steps
    for arg, v in (("where", {"lit": True}), ("where_key", {"field": "a"}), ("where_in", {"ref": "x"}),
                   ("where_not_in", {"ref": "x"})):
        try:
            ir_plan_to_steps({"steps": [{"name": "f", "op": "filter", "rows": {"ref": "x"}, arg: v}]}, {}, {})
        except ValueError:
            continue
        raise AssertionError(f"a filter with {arg} was not refused")
    ir_plan_to_steps({"steps": [{"name": "j", "op": "join_multi", "left": {"ref": "x"}, "right": {"ref": "y"},
                                 "left_on": "a", "right_on": "a", "where": {"lit": True}}]}, {}, {})
    print("pair tests: 4 refused on a filter, taken on join_multi")


# ---- 4. round-trip every query: template -> IR -> JSON -> bind -------------
# A migrated builder returns a parameterized Plan. We serialize the TEMPLATE (holes +
# Expr preds -> .ast) ONCE, JSON round-trip it, and bind BOTH the in-memory Plan (reference)
# and the JSON'd IR (round-trip) to concrete steps, then run each. So the check is: does the
# language-neutral JSON preserve the plan exactly? (A bound plan also re-serializes:
# bind keeps each pred as an authoring.Expr with its params bound as literals -- interp._bind_ast.)
def _builder(v):
    return v.get("default") if isinstance(v, dict) else v


def _compare(ref_steps, ir_steps, FmtCls):
    """Run the reference steps and the round-tripped steps on SEPARATE fresh stores (so no
    read-time cache from run #1 can undercount run #2), and diff per-step cost + final result."""
    fmt_r, store_r = make_store(FmtCls)
    out_ref, kv_ref = run_plan(ref_steps, fmt_r, store_r)
    fmt_i, store_i = make_store(FmtCls)
    out_ir, kv_ir = run_plan(ir_steps, fmt_i, store_i)
    rows_ref, rows_ir = cost_rows(kv_ref), cost_rows(kv_ir)
    ok = (rows_ref == rows_ir
          and repr(normalize(out_ref[ref_steps[-1].out])) == repr(normalize(out_ir[ir_steps[-1].out])))
    return ok, [(a, b) for a, b in zip(rows_ref, rows_ir) if a != b]


def test_roundtrip():
    """the round trip on the queries the demo graph can run (declared_skips.DEMO_RUNNABLE), on every format: each
    must run; any exception fails the check with the query and format. The other registry queries are declared skips."""
    npass = nmis = ncmp = 0
    skipped, failed = [], {}                            # declared skips; (query, format or "*") -> why
    for q in sorted(REGISTRY):
        if q not in DEMO_RUNNABLE:
            skipped.append(q); continue
        builder = _builder(REGISTRY[q])
        try:
            raw = builder(dict(DEMO_ARGS))              # a Plan (migrated) or a Step list (baked)
            ir = json.loads(json.dumps(plan_to_ir(raw, name=q)))
            ref_steps = bind(raw, dict(DEMO_ARGS)) if isinstance(raw, Plan) else raw
            ir_steps = bind_ir(ir, dict(DEMO_ARGS))
        except Exception as e:
            failed[q, "*"] = "build / bind: " + failure(e); continue

        ran, bad = [], False
        for FmtCls in FORMATS:                          # try EVERY format (don't break early)
            # the logical type filter is lowered per format (what plan_for / the dumper do)
            caps = FmtCls.capabilities(DEMO_SCHEMA)
            try:
                edge_caps = edge_caps_of(FmtCls.name, DEMO_SCHEMA)   # a read_edge_exists with no reads takes its default
                ref_l = lower_filter(copy.deepcopy(ref_steps), caps, edge_caps)
                ir_l = lower_filter(copy.deepcopy(ir_steps), caps, edge_caps)
                ok, diff = _compare(ref_l, ir_l, FmtCls)
            except Exception as e:
                failed[q, FmtCls.name] = failure(e); continue
            ncmp += 1
            ran.append(FmtCls.name)
            if not ok:
                nmis += 1
                bad = True
                print(f"  MISMATCH {q} [{FmtCls.name}]")
                for a, b in diff:
                    print(f"      {a}  vs  {b}")
        if ran and not bad:
            npass += 1
            print(f"  OK       {q:8s} ({', '.join(ran)})")
    want = len(DEMO_RUNNABLE & set(REGISTRY)) * len(FORMATS)
    print(f"\n  round-trip: {npass} queries OK, {ncmp} of {want} (query, format) cells run, {nmis} mismatch, "
          f"{len(failed)} failed; {len(skipped)} registry queries are not demo-graph queries (declared skips: "
          f"check_planir_ldbc runs them at sf0.1)")
    for (q, f), why in sorted(failed.items()):
        print(f"    FAILED {q:8s} {f:8s} {why}")
    return nmis == 0 and not failed and ncmp == want and want > 0


def main():
    test_expr()
    test_bundle()
    test_filter_ir()
    ok = test_roundtrip()
    print("\n" + ("PLAN IR OK" if ok else "PLAN IR FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
