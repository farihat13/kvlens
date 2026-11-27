"""Guards on the committed neutral IR files (each workload's workloads/<w>/plans/*.json), the
contract a C++/Nebula evaluator reads. Two checks, across ALL workloads:

1. DRIFT: each file must match what the query builders serialize RIGHT NOW (builders are the
   source of truth). If a builder changed and the files weren't regenerated, this fails --
   run `uv run --locked python python/kvevaluator/plan_choice/dump_planir.py` to refresh.
2. SELECTION: for every bundle and every format OR CHUNK ARM, select_variant(its caps on the design) must pick the plan whose body
   equals what the registry resolves for that format -- i.e. the capability `requires` in the
   files reproduce each workload's per-format plan choice, for every known format.
3. ONE NAME PER BODY: no bundle holds one body under two names (with a planted copy that must be refused).

Run: python3 tests/unit/check_plan_files.py
"""
import json
import os
import sys

from kvevaluator.plan_choice import dump_planir
from kvevaluator.datasets import schema_path
from kvevaluator.plan_choice.dump_planir import iter_all, bundle_for, call_pick, lowered_body, pick_for, WORKLOADS, resolves, plans_dir_in
from kvevaluator.plans.ir import plan_to_ir
from kvevaluator.plans.ir.bundle import select_variant
from kvevaluator.plan_choice.optimizer.arms import caps_of as caps_for   # a format, or a chunk arm (its caps carry its chunk)
from kvevaluator.plan_choice.optimizer.arms import resolver_of   # its per-NAME caps: what a variant's `on` checks (a label, or an edge type)
from kvevaluator.paths import ROOT


def _drift(root=None):
    expected = {}                                # plans_dir -> {name: text}
    by_w = {os.path.abspath(w.plans_dir): w for w in WORKLOADS}
    for plans_dir, q, text in iter_all():
        expected.setdefault(plans_dir_in(by_w[os.path.abspath(plans_dir)], root), {})[q] = text
    ok = True
    n = 0
    for plans_dir, files in expected.items():
        on_disk = ({f[:-5] for f in os.listdir(plans_dir) if f.endswith(".json")}
                   if os.path.isdir(plans_dir) else set())
        for q, text in files.items():
            n += 1
            path = os.path.join(plans_dir, f"{q}.json")
            if not os.path.exists(path):
                print(f"  MISSING  {path}"); ok = False
            elif open(path).read() != text:
                print(f"  STALE    {path}"); ok = False
        for q in sorted(on_disk - set(files)):
            print(f"  EXTRA    {os.path.join(plans_dir, q + '.json')}"); ok = False
    print("  drift: interactive/plans/label_only skipped: frozen as committed (the label_only family is parked since "
          "label_only_plan_for refuses; its picks came from v1's search)")
    print(f"  drift: {n} files across {len(expected)} workloads -- {'OK' if ok else 'DRIFTED'}")
    return ok


def _duplicate_bodies(ir):
    """[(body_sha, [names])] of a bundle that holds one body under two names (empty: one name per body)"""
    from kvevaluator.plans.ir.bundle import body_sha
    by = {}
    for name in ir.get("plans", {}):
        by.setdefault(body_sha(ir, name), []).append(name)
    return sorted((sha, sorted(ns)) for sha, ns in by.items() if len(ns) > 1)


def _one_name_per_body(root=None):
    """ONE NAME PER BODY: no committed bundle holds one body (bundle.body_sha) under two names; the dumper writes a body
    two variants select once. A plant first: a real bundle with one body copied under a second name must be refused"""
    by_w = {os.path.abspath(w.plans_dir): w for w in WORKLOADS}
    dirs = sorted({plans_dir_in(by_w[os.path.abspath(d)], root) for d, _q, _t in iter_all()})
    paths = [os.path.join(d, f) for d in dirs if os.path.isdir(d) for f in sorted(os.listdir(d)) if f.endswith(".json")]
    bundles = [(p, ir) for p, ir in ((p, json.load(open(p))) for p in paths) if "plans" in ir]
    if not bundles:
        print("  one name per body: 0 bundles found -- FAILED"); return False
    p0, ir0 = bundles[0]
    plant = json.loads(json.dumps(ir0))
    first = next(iter(plant["plans"]))
    plant["plans"][first + "_copy"] = plant["plans"][first]
    if not _duplicate_bodies(plant):
        print(f"  one name per body: the plant ({os.path.basename(p0)} with {first!r} copied) was NOT refused -- the check is broken")
        return False
    ok = True
    for p, ir in bundles:
        for sha, names in _duplicate_bodies(ir):
            print(f"  DUPLICATE {p}: body {sha} under {names}"); ok = False
    print(f"  one name per body: {len(bundles)} bundles in {len(dirs)} plans dirs, the plant refused -- {'OK' if ok else 'FAILED'}")
    return ok


UNLISTED = "unlisted-scale-lint"   # a scale tag no bundle carries


def _matches(v, ir, caps, resolver=None):
    """does variant `v` of `ir` select for a format with `caps` (its requires / lacks, checked `on` its names through
    `resolver` when given; the scale is the caller's)? -- the overlap lint's helper"""
    one = {"variants": [{k: v[k] for k in ("requires", "lacks", "on", "plan") if k in v}], "plans": ir["plans"]}
    try:
        select_variant(one, caps, resolver)
        return True
    except Exception:  # -- NoVariant
        return False


def _tag_order():
    """Every scale-tagged variant precedes every untagged one (first fit: a tagged variant after its default is dead)."""
    ok, n = True, 0
    for _d, q, text in iter_all():
        ir = json.loads(text)
        if "variants" not in ir:
            continue
        seen_untagged = False
        for v in ir["variants"]:
            if "scales" in v:
                n += 1
                if seen_untagged:
                    print(f"  DEAD TAG      {q}: tagged variant {v['plan']!r} comes after an untagged one"); ok = False
            else:
                seen_untagged = True
    print(f"  tag order: {n} scale-tagged variants -- {'OK' if ok else 'WRONG'}")
    return ok


def _selection():
    """The capability variants must reproduce each workload's per-format plan choice, under every
    reference design (a format's capabilities may depend on the design)."""
    ok, nbundle, nscale = True, 0, 0
    for w in WORKLOADS:
        gen_q, gen_twins = dump_planir._generated(w)
        for q in sorted(w.registry):
            if q in gen_q or q in gen_twins:     # a generated file: its picks are the record's (check_pick_record)
                continue
            try:
                ir = bundle_for(q, w.registry[q], pick_for(w, q), w.formats, w.variant_requires,
                                w.designs, w.fallback_format, base_requires=w.base_requires)
            except Exception:  # -- baked/lambda query
                continue
            if ir is None or "variants" not in ir:
                continue
            nbundle += 1
            for fmt in w.formats:
                if w.pick(w.registry[q], fmt) is None:
                    # subset family (ic3's dated read is nebula-only): the bundle must NOT
                    # cover this format -- selecting a variant for it would be the bug
                    for design, schema in w.designs.items():
                        if not resolves(fmt, design):
                            continue
                        try:
                            picked = select_variant(ir, caps_for(fmt, schema), resolver_of(fmt, schema))
                        except Exception:  # -- NoVariant: correct refusal
                            continue
                        print(f"  LEAKY SELECT  {q}/{fmt}@{design}: undeclared format selected "
                              f"{picked!r}"); ok = False
                    continue
                for design, schema in w.designs.items():
                    if not resolves(fmt, design):
                        continue
                    # per-SF pins: this cell's tagged scales (builder.scales) and every tag
                    # the bundle carries; the body selected at each must be the pick's body at that scale, and a
                    # scale-blind selection on a cell that pins per scale must REFUSE (never a guess)
                    pk = pick_for(w, q)
                    mine = set(getattr(call_pick(pk, w.registry[q], fmt, design), "scales", []))
                    tags = sorted({t for v in ir["variants"] for t in v.get("scales", [])})
                    # UNLISTED is a scale no variant tags: the default must serve every declared profile there (a
                    # format with only tagged variants would read as "not applicable" at a new scale)
                    for scale in [None] + tags + ([UNLISTED] if tags else []):
                        if scale is None and mine:
                            try:
                                select_variant(ir, caps_for(fmt, schema), resolver_of(fmt, schema))
                            except ValueError:
                                continue
                            print(f"  NO REFUSAL    {q}/{fmt}@{design}: pins per scale {sorted(mine)} but a scale-blind "
                                  f"selection went through"); ok = False
                            continue
                        nscale += scale is not None
                        picked = select_variant(ir, caps_for(fmt, schema), resolver_of(fmt, schema), scale)
                        want_scale = None if scale == UNLISTED else scale
                        if tags and scale is not None:
                            # two tagged variants both matching this profile at this scale: the first wins silently
                            hits = [v["plan"] for v in ir["variants"] if "scales" in v and scale in v["scales"]
                                    and _matches(v, ir, caps_for(fmt, schema), resolver_of(fmt, schema))]
                            if len(hits) > 1:
                                print(f"  TAG OVERLAP   {q}/{fmt}@{design} at scale {scale}: {hits}"); ok = False
                        if ir["plans"][picked] != lowered_body(w.registry[q], pk, q, fmt, schema, design, want_scale):
                            print(f"  WRONG SELECT  {q}/{fmt}@{design}" + (f" at scale {scale}" if scale else "")
                                  + f": selected {picked!r}"); ok = False
    print(f"  selection: {nbundle} bundles ({nscale} scale-tagged cells) -- {'OK' if ok else 'WRONG'}")
    return ok


def _schema_artifacts():
    """The committed schema JSON artifacts (datasets/<d>/schema*.json -- the files the C++ evaluator ingests
    from) must match what the compiler serializes RIGHT NOW, for EVERY workload with an authored schema (ldbc,
    linkbench) -- an artifact not regenerated after its .schema changed would hand C++ a stale layout.
    Refresh: python3 -m kvevaluator.datasets write-schema <dataset> <design>."""
    import glob
    import importlib
    import json as _json
    from kvevaluator.schema.schema_json import to_json
    ok, n = True, 0
    for workload in ("ldbc", "linkbench"):
        mod = importlib.import_module(f"kvevaluator.datasets.{workload}.schema")
        SCHEMAS, get_schema = mod.SCHEMAS, mod.get_schema
        here = os.path.dirname(schema_path(workload))   # the dir that holds its compiled schemas
        on_disk = {}
        for p in sorted(glob.glob(os.path.join(here, "schema*.json"))):
            base = os.path.basename(p)
            name = "canonical" if base == "schema.json" else base[len("schema_"):-len(".json")]
            on_disk[name] = p
        for name, p in on_disk.items():
            n += 1
            if name not in SCHEMAS:
                print(f"  EXTRA    {p} (no {name!r} .schema)"); ok = False
                continue
            want = _json.dumps(to_json(get_schema(name), name), indent=1) + "\n"
            if open(p).read() != want:
                print(f"  STALE    {p}"); ok = False
    print(f"  schema artifacts: {n} committed -- {'OK' if ok else 'DRIFTED'}")
    return ok


def _measure_only_where():
    """A variant entry's `measure_only_where` (dump_planir: "measured, never picked on a store that fits <variant>";
    the grid's per-cell measure_only flag, grid_capture.measure_only_on_cell) must name ANOTHER variant of the same
    bundle that has a selector (else it would hold everywhere: that is the bundle-wide `measure_only`), and may sit
    only on a variant with no selector of its own (the default). Both selectors ignore the key."""
    import glob
    ok, n = True, 0
    root = os.path.join(str(ROOT), "python", "kvevaluator", "workloads")
    for p in sorted(glob.glob(os.path.join(root, "*", "plans", "*.json"))):
        ir = json.load(open(p))
        by = {v["plan"]: v for v in ir.get("variants", [])}
        for v in ir.get("variants", []):
            other = v.get("measure_only_where")
            if other is None:
                continue
            n += 1
            where = f"{os.path.basename(p)} variant {v['plan']!r}"
            if v.get("requires") or v.get("lacks"):
                print(f"  BAD measure_only_where  {where}: it has a selector of its own"); ok = False
            if other == v["plan"] or other not in by:
                print(f"  BAD measure_only_where  {where}: {other!r} is not another variant of this bundle"); ok = False
            elif not (by[other].get("requires") or by[other].get("lacks")):
                print(f"  BAD measure_only_where  {where}: {other!r} has no selector"); ok = False
    # a generated file's form: bodies[b].measure_only_where = {capability: [labels]}, the caps a store must hold (each on
    # those labels) for b to be measured but never picked there; not on a body measure-only everywhere
    from kvevaluator.plans.ir.capabilities import SELECTOR_CAPS, VALIDITY_CAPS
    nb = 0
    for p in sorted(glob.glob(os.path.join(root, "*", "plans", "*.json"))):
        for b, m in sorted((json.load(open(p)).get("bodies") or {}).items()):
            where = m.get("measure_only_where")
            if where is None:
                continue
            nb += 1
            at = f"{os.path.basename(p)} body {b!r}"
            if not isinstance(where, dict) or not where or not all(
                    isinstance(ls, list) and ls and all(isinstance(x, str) for x in ls) for ls in where.values()):
                print(f"  BAD measure_only_where  {at}: not a non-empty {{capability: [labels]}}: {where!r}"); ok = False
            elif set(where) - (SELECTOR_CAPS | VALIDITY_CAPS):
                print(f"  BAD measure_only_where  {at}: unknown capabilities {sorted(set(where) - (SELECTOR_CAPS | VALIDITY_CAPS))}"); ok = False
            if m.get("measure_only"):
                print(f"  BAD measure_only_where  {at}: the body is measure-only everywhere already"); ok = False
    print(f"  measure_only_where: {n} variant entries, {nb} generated bodies -- {'OK' if ok else 'WRONG'}")
    return ok


def _in_reads_require_reverse():
    """A bundle variant whose body has a read_edge_exists read of dir "in" must require reverse_adjacency (the read
    throws on an edge type whose in lists are not kept; dump_planir._in_read_caps adds it). A plan file with no
    variants is one format's own body (e.g. the engine-shaped probes): not selected by caps, not linted here."""
    import glob
    ok, n = True, 0
    root = os.path.join(str(ROOT), "python", "kvevaluator", "workloads")
    for p in sorted(glob.glob(os.path.join(root, "*", "plans", "*.json"))):
        ir = json.load(open(p))
        for v in ir.get("variants", []):
            steps = ir["plans"][v["plan"]]["steps"]
            if not any(r.get("dir") == "in" for s in steps if s.get("op") == "read_edge_exists" for r in s.get("reads") or ()):
                continue
            n += 1
            if "reverse_adjacency" not in (v.get("requires") or ()):
                print(f"  BAD in read  {os.path.basename(p)} variant {v['plan']!r}: a read with dir in, "
                      f"requires {v.get('requires')} lacks reverse_adjacency"); ok = False
    print(f"  in reads require reverse_adjacency: {n} variant bodies with an in read -- {'OK' if ok else 'WRONG'}")
    return ok


def _row_scans_are_required():
    """A bundle variant whose body scans a label's rows (read_vertices_by_label with props) must require the cap that
    makes the scan valid: label_rows_one_run or label_scan_carries_props (the Chunked layouts' selector, which implies
    it); with a keep set, label_rows_one_run itself -- the other formats refuse the op. A plan file with no variants
    is one format's own body: not selected by caps, not linted here."""
    import glob
    ok, n, kept = True, 0, 0
    root = os.path.join(str(ROOT), "python", "kvevaluator", "workloads")
    for p in sorted(glob.glob(os.path.join(root, "*", "plans", "*.json"))):
        ir = json.load(open(p))
        for v in ir.get("variants", []):
            scans = [s for s in ir["plans"][v["plan"]]["steps"] if s.get("op") == "read_vertices_by_label" and s.get("props")]
            if not scans:
                continue
            n += 1
            req = set(v.get("requires") or ())
            has_keep = any(s.get("keep") is not None for s in scans)
            kept += has_keep
            # whole_label_scan_by_default is held only where label_rows_one_run is (check_caps_parity asserts it)
            want = {"label_rows_one_run"} if has_keep else {"label_rows_one_run", "label_scan_carries_props", "whole_label_scan_by_default"}
            if not (req & want):
                print(f"  BAD row scan  {os.path.basename(p)} variant {v['plan']!r}: a label scan with props"
                      f"{' and keep' if has_keep else ''}, requires {sorted(req)} holds none of {sorted(want)}"); ok = False
    print(f"  row scans require their capability: {n} variant bodies with a label scan carrying props ({kept} with keep)"
          f" -- {'OK' if ok else 'WRONG'}")
    return ok


# Capabilities that are a property of ONE label's (or edge type's) layout: a body that requires one must say which label or
# edge type it is checked on (`on`), else a per-type store resolves it store-wide and a Split label the body never reads
# makes the body invalid -- and a query's pick stops depending only on the units it reads (the search's premise). The named exceptions and why store-wide is right for them:
LABEL_CAPS = {"whole_row_read", "label_scan_carries_props", "type_index", "type_index_single_posting",
              "per_type_index", "sorted_range_index", "unsorted_range_index"}
ETYPE_CAPS = {"reverse_adjacency", "adjacency_lists"}
STORE_WIDE_OK = {
    "label_rows_one_run": "a guard cap held by EVERY label of every layout that holds it (meta's props layouts, pertype_csr, arangodb: "
                          "every collection's documents are one key run): "
                          "the store-wide check equals the per-label one",
    "whole_label_scan_by_default": "a guard cap held by EVERY label of every layout that holds it (meta's props layouts, pertype_csr; "
                                   "not arangodb, whose default is held): the store-wide check equals the per-label one",
    "label_scoped_props": "held by every label of every layout but dgraph's (label-free keys), which has no mixes: "
                          "the store-wide check equals the per-label one",
    "index_entries_by_docid": "a property of the index (one per store), not of a label",
}


def _label_caps_carry_on():
    """every per-label / per-edge-type capability a variant requires is annotated `on` the labels it is checked on, or is a
    named exception (STORE_WIDE_OK), or -- for an index cap -- the body names no label in its index reads (the index is
    one component per store), or -- for whole_row_read -- the body reads no rows at all (then the cap is the selector
    derivation's fingerprint of the formats that run this body, not a need of the body: listed, store-wide)"""
    import glob
    ok, n, listed = True, 0, []
    root = os.path.join(str(ROOT), "python", "kvevaluator", "workloads")
    for p in sorted(glob.glob(os.path.join(root, "*", "plans", "*.json"))):
        ir = json.load(open(p))
        for v in ir.get("variants", []):
            on = v.get("on") or {}
            steps = ir["plans"][v["plan"]]["steps"]
            for cap in v.get("requires") or ():
                if cap not in LABEL_CAPS | ETYPE_CAPS | set(STORE_WIDE_OK):
                    continue
                n += 1
                if cap in on:
                    continue
                where = f"{os.path.basename(os.path.dirname(os.path.dirname(p)))}/{os.path.basename(p)[:-5]}:{v['plan']}"
                if cap in STORE_WIDE_OK:
                    listed.append((cap, where, STORE_WIDE_OK[cap])); continue
                reads_rows = any(st.get("op") == "read_vertex_props" or (st.get("op") == "read_vertices_by_label" and (st.get("props") or st.get("keep") is not None)) for st in steps)
                if cap == "whole_row_read" and not reads_rows:
                    listed.append((cap, where, "the body reads no rows: the cap is the selector's fingerprint of the formats that run it, not a need")); continue
                if cap in {"per_type_index", "sorted_range_index", "unsorted_range_index"}:
                    listed.append((cap, where, "the index is one component per store; the body's index reads name no label")); continue
                print(f"  BAD label cap  {where}: requires {cap!r} without an `on` label (a property of one label's layout)"); ok = False
    for cap, where, why in listed:
        print(f"  store-wide by reason  {where}: {cap} -- {why}")
    print(f"  per-label capabilities carry their label: {n} required per-label / per-edge-type caps checked, {len(listed)} store-wide "
          f"by a named reason -- {'OK' if ok else 'WRONG'}")
    return ok


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick-by", dest="criterion", default=None, help="check the bundles derived under this pick rule (criteria.NAMED)")
    ap.add_argument("--plans-root", default=None, help="the plans dirs under this root (dump_planir --out-root) instead of the committed ones")
    a = ap.parse_args()
    dump_planir.CRITERION = a.criterion
    ok = (_drift(a.plans_root) & _selection() & _tag_order() & _schema_artifacts()
          & _measure_only_where() & _in_reads_require_reverse() & _row_scans_are_required() & _label_caps_carry_on()
          & _one_name_per_body(a.plans_root))
    if not ok:
        print("  -> if drift: run `uv run --locked python python/kvevaluator/plan_choice/dump_planir.py`")
    print("\n" + ("PLAN FILES OK" if ok else "PLAN FILES FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
