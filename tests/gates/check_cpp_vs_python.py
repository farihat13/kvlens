"""The differential test between the C++ evaluator and the Python reference.

For every (plan file, param) cell: Python runs `run_ir` on the IR file over the in-RAM
AbstractKV oracle, C++ runs the same IR + params over its own store. Per step, S/G/N, the
absence-miss op counts (S_miss/G_miss/N_miss: both seams must declare the same landings
missed) and the value bytes (get_value, scan_value) must be identical; the final rows must
match as a SET and as a SEQUENCE. Any difference fails the cell and names the step.

The chunked per-type formats chunk at KVEVAL_PERTYPE_CSR_CHUNK (default 64K) on both sides:
the reference is built at that target and the store dir defaults to <format>-c<N>k, and the
store manifest's chunk_target is checked against the reference before any cell runs.

Canonical row form (applied to both sides): each row -> JSON with sorted keys, tuples as lists,
null for None; the sequence is the list of those strings, the set is the sorted list.

Run:  python3 tests/gates/check_cpp_vs_python.py [--sf 0.1] [--nparam 5] [--families interactive microbench]
                                          [--queries ic1 ic2 ...] [--store pertype_csr-c64k]
      KVEVAL_PERTYPE_CSR_CHUNK=4096 python3 tests/gates/check_cpp_vs_python.py   # the 4K store pair
Needs the C++ store: kvevaluator ingest ... --chunk 65536 --out output/stores/ldbc/sf0.1/pertype_csr-c64k
"""
import os
import sys
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path
from kvevaluator.capture import binary

if __name__ == "__main__":                           # the binary under test, first (before the dataset loads below)
    _prov = binary.binary_provenance()
    print(f"binary {binary.binary_path()} sha {_prov.get('binary_git_sha', '?')[:12]} dirty {_prov.get('binary_dirty')}",
          flush=True)

os.environ.setdefault("KVEVAL_PERTYPE_CSR_CHUNK", "65536")     # before any format is built

import argparse
import glob
import json
import re
import time

from kvevaluator.measure.server import CppServer, step_costs
from kvevaluator.measure.stores import store_dir, cpp_store_name, store_chunk
from kvevaluator.capture.store_identity import store_chunk_target
from kvevaluator.formats import make_format
from kvevaluator.oracle.interp import run_ir
from kvevaluator.plans.ir.bundle import NoVariant, body_sha, select_variant, expand_fragments
from kvevaluator.plan_choice.candidates import forced, valid_bodies
from kvevaluator.plan_choice import pick_record
from kvevaluator.measure.plan_runs import record_store_key
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.oracle.run import open_store
from kvevaluator.datasets import demo as data
from kvevaluator.datasets.ldbc import params as ldbc_params
from kvevaluator.workloads.microbench import params as mb_params
from kvevaluator.workloads.writes import params as w_params
from kvevaluator.workloads.bi import params as bi_params
from kvevaluator import jsonbytes
from kvevaluator.cost import TwinCountsRefused
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.kvstore.shadow import WriteShadowKV

_HERE = os.path.dirname(os.path.abspath(__file__))
_PLANS = {"interactive": os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "interactive", "plans"),
          "microbench": os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "microbench", "plans"),
          "bi": os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "bi", "plans"),
          "writes/microbench": os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "writes", "microbench", "plans"),
          "writes/ldbc": os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "writes", "ldbc", "plans")}
_PERSON_ONLY = {"fof", "friends", "knows"}        # person-anchored helpers: reuse IC1's persons


def params_for(workload, qname, sf, n):
    if workload.startswith("writes"):
        return w_params.query_params(sf, qname, n)
    if workload == "microbench":
        return mb_params.query_params(sf, qname, n) or [{}]
    if workload == "bi":
        return bi_params.query_params(sf, qname, n)
    if qname in _PERSON_ONLY:
        return [{"person": r["personId"]} for r in ldbc_params.param_rows(sf, "ic1")[:n]]
    m = re.match(r"(ic\d+|is\d+)", qname)               # ic2_merge -> ic2, is6_capped -> is6
    # any other name is a query params.py knows by its own name (the wide-read plans: forum_board_render, ...)
    return ldbc_params.query_params(sf, m.group(1) if m else qname, n)


def canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=jsonbytes.default)


def canon_rows(rows):
    if not isinstance(rows, list):
        return [canon(rows)]
    return [canon(r) for r in rows]


def output_name(ir, caps, caps_of_label=None, scale=None):
    plan = ir if "steps" in ir else ir["plans"][select_variant(ir, caps, caps_of_label, scale)]
    return expand_fragments(plan, ir.get("fragments", []))["output"]


def diff_cell(py_steps, py_rows, cpp, check_bytes=True, check_counts=True):
    """-> None when the cell matches, else a one-line reason naming the first difference.
    check_counts=False: the two sides run DIFFERENT index layouts (a C++ posting-index store vs
    the Python sorted-keys twin), so their per-step counts -- and even their variant picks --
    legitimately differ; only the rows (set + order) carry the check then."""
    if cpp.get("error"):
        return f"cpp error ({cpp['error']['where']}): {cpp['error']['what']}"
    cpp_steps = step_costs(cpp)
    if check_counts:
        if len(cpp_steps) != len(py_steps):
            return f"step count {len(cpp_steps)} (cpp) != {len(py_steps)} (py)"
        for p, c in zip(py_steps, cpp_steps):
            if isinstance(p[2], TwinCountsRefused):          # a step the twin disowns: its counts are not compared
                continue                                     # (counted per cell: refused_steps; its rows still are)
            if not check_bytes:
                p, c = p[:10], c[:10]
            if p != c:
                return (f"step {p[0]} ({p[1]}): py {p[2:]} != cpp {c[2:]} "
                        f"(S G N P D S_miss G_miss N_miss [get_b scan_b wkey_b wval_b])")
    py_c, cpp_c = canon_rows(py_rows), canon_rows(cpp["rows"])
    if sorted(py_c) != sorted(cpp_c):
        only_py = [r for r in py_c if r not in cpp_c][:1]
        only_cpp = [r for r in cpp_c if r not in py_c][:1]
        return (f"rows differ as a SET: {len(py_c)} py vs {len(cpp_c)} cpp; "
                f"py-only {only_py[0][:160] if only_py else '-'} | cpp-only {only_cpp[0][:160] if only_cpp else '-'}")
    if py_c != cpp_c:
        i = next(i for i, (a, b) in enumerate(zip(py_c, cpp_c)) if a != b)
        return f"rows differ in ORDER at {i}: py {py_c[i][:120]} | cpp {cpp_c[i][:120]}"
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--allow-dirty", default=None, metavar="NAME",
                    help="run a dirty binary as the named control (not a gate result; binary.binary_dirty_verdict)")
    ap.add_argument("--families", dest="workloads", nargs="+", default=["interactive", "microbench"])
    ap.add_argument("--queries", nargs="*", default=None)
    ap.add_argument("--format", default="pertype_csr", help="the format under test (Python reference + C++ store)")
    ap.add_argument("--schema", default="canonical",
                    help="schema variant (feature schemas run the ldbc label_only IR family -- their "
                         "index-free base cannot run the canonical plans' index ops; workloads "
                         "default to interactive only; store dir defaults to <format>-<schema>). The "
                         "dgraph/nebula families need --skip-bytes: their value bytes are "
                         "dual-currency by doctrine (neutral model vs encoded store)")
    ap.add_argument("--force", action="append", default=[], metavar="QUERY=BODY",
                    help="run this bundle BODY for QUERY on both sides instead of the planner's pick (candidates.forced): "
                         "the gate on a candidate no format pins yet; the default call is unchanged")
    ap.add_argument("--store", default=None,
                    help="C++ store dir name (default: <format>-c<N>k for the chunked per-type "
                         "formats, N from KVEVAL_PERTYPE_CSR_CHUNK; else the format name)")
    ap.add_argument("--report", default=None, help="write per-cell results as JSON here")
    ap.add_argument("--P", type=int, default=1, help="workers for the C++ side (the counts must not change)")
    ap.add_argument("--pack", action="store_true",
                    help="build the Python reference with kind-1 value packing so its byte model "
                         "matches the C++ store's packed values -- REQUIRED for a meaningful "
                         "write-byte comparison (the C++ side is always packed; unpacked-vs-packed "
                         "byte gaps are pack calibration, not bugs). Pure accounting toggle: "
                         "op counts and stored data are identical either way.")
    ap.add_argument("--skip-bytes", action="store_true",
                    help="compare S/G/N + rows only (a store whose value codec is not Python's bytes)")
    ap.add_argument("--force-body", default=None, metavar="NAME",
                    help="run this body of each query's bundle on BOTH sides instead of the planner's pick (a body "
                         "no store pins is otherwise never compared); a query whose bundle has no such body is skipped")
    ap.add_argument("--selected-only", action="store_true",
                    help="run only the body this store selects (the default also runs every other valid body)")
    ap.add_argument("--twin-counts", default=None, choices=["old-label-rule"],
                    help="read the Python twin's counts on the steps it disowns (Per-type CSR: the label from the id's "
                         "group, no far-label block). The named bypass of runner.run_plan; prints why once. Right only "
                         "against a C++ run under the same rule (KVEVAL_LABEL_TAGS=hops=off,probes=off) on "
                         "chunks without a label block; or to LIST how the old-rule twin differs from C++ (the "
                         "report's step_diffs), never as a pass / fail check")
    ap.add_argument("--compare-counts", action="store_true",
                    help="compare the per-step counts even where the store's index layout differs from the twin's "
                         "(pertype_csr's posting index): right only for queries with no index read (name them)")
    a = ap.parse_args(argv)
    ok, msg = binary.binary_dirty_verdict(binary.binary_provenance(), a.allow_dirty)   # refused here, not at import
    print(msg, flush=True)
    if not ok:
        raise SystemExit(f"check_cpp_vs_python: {msg}")
    force = dict(kv.split("=", 1) for kv in a.force)
    # a C++-only format has no Python oracle to differ against: say so instead of a KeyError
    from kvevaluator.formats import FORMATS as _PY_FORMATS
    if a.format not in _PY_FORMATS:
        raise SystemExit(f"{a.format}: no Python twin -- the cpp-vs-python differential cannot run; "
                         f"its correctness gate is the cross-body answer check of grid_bodies (answer_key)")

    t0 = time.time()
    if a.schema == "canonical":
        schema = data.schema(a.sf)
        schema_json_path = None                        # the server's default (the canonical artifact)
    else:
        from kvevaluator.datasets.ldbc.schema import get_schema
        schema = get_schema(a.schema)
        schema_json_path = schema_path("ldbc", a.schema)
        from kvevaluator.datasets.ldbc.schema import PARKED_SCHEMAS
        if a.schema in PARKED_SCHEMAS:                 # still runs, in no gate
            print(f"note: schema {a.schema} is parked: not maintained; counts not checked")
        if not os.path.exists(schema_json_path):
            sys.exit(f"no committed artifact {schema_json_path} -- python3 -m kvevaluator.datasets write-schema ldbc "
                     f"{a.schema}")
        if a.workloads == ["interactive", "microbench"]:      # the default: feature schemas run interactive only
            a.workloads = ["interactive"]
        # an index-free (label_only-based) schema cannot run the canonical plans' index ops
        # (Python returns [], C++ throws): run the committed label_only IR family instead
        if not (schema.indexed_cols or schema.date_cols):
            _PLANS["interactive"] = os.path.join(_PLANS["interactive"], "label_only")
    chunk = int(os.environ["KVEVAL_PERTYPE_CSR_CHUNK"])
    chunked = a.format == "pertype_csr"
    if a.store is None:
        # the bridge's naming rule (dgraph's opt-in chunk knob, KVEVAL_DGRAPH_CHUNK, included)
        a.store = cpp_store_name(a.format, a.schema, None, store_chunk(a.format))
    fmt = make_format(a.format, schema, a.pack)
    if chunked:
        # the store's chunk target is the layout itself: a reference chunked differently would
        # walk different chunk boundaries and fail every scan cell for a non-bug
        man = json.load(open(os.path.join(store_dir("ldbc", a.sf, a.store), "manifest.json")))
        man_chunk = (man.get("value_codec") or {}).get("chunk_target")
        assert fmt.chunk_target == chunk == man_chunk, (
            f"chunk mismatch: reference {fmt.chunk_target}, env {chunk}, store {a.store} "
            f"manifest {man_chunk} -- set KVEVAL_PERTYPE_CSR_CHUNK to the store's target")
    store = open_store(fmt, "abstract", a.sf, lambda: data.load(a.sf), a.schema)
    # the store's chunk target selects too (coarse_chunks, via the format's pack target), as in the C++ session
    store_chunk_bytes = store_chunk_target(store_dir("ldbc", a.sf, a.store))
    caps = caps_for(a.format, schema, chunk=store_chunk_bytes)
    # per-label variant selection (the `on` annotations) -- must match the C++ side's
    # Format::store_capabilities_for, or the two run different plan shapes
    of_label = caps_resolver(a.format, schema, chunk=store_chunk_bytes)
    # the write cells run over a copy-on-write shadow (writes land in RAM, dropped per cell);
    # counting sits above it, so P/D and the read counts are of the merged view
    shadow = WriteShadowKV(store.backend)
    wstore = CountingKV(shadow, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes, write_mode=True)
    print(f"[ref] AbstractKV sf{a.sf} ready in {time.time() - t0:.1f}s", flush=True)

    cells, nok, nbad, nskip, nnovar, nnotrun = [], 0, 0, 0, 0, 0
    nbody = 0                                  # cells whose variant pick was checked (bundles only)
    nsha = 0                                      # cells whose body sha (C++ vs Python) was checked
    srv_kw = {} if a.schema == "canonical" else {"schema": os.path.abspath(schema_json_path)}
    # The per-step COUNT parity holds only when both sides run the same index layout. A C++
    # per-type store ingested with the posting index (pertype_csr's default;
    # manifest value_codec.index_layout) does different index work than the Python twin's
    # sorted keys -- and can pick a different plan variant (type_index_single_posting). Rows
    # stay the check; the count divergence is the known follow-on (Python posting twin), not a
    # failure. A `-sorted_keys` store (KVEVAL_PERTYPE_INDEX_LAYOUT=sorted_keys) restores it.
    # Likewise a chunked dgraph store (dgraph-c64k, manifest chunk_target): the Python twin holds
    # unbounded postings, so a split unit's extra NEXTs and part bytes are the arm's, not a bug.
    counts_comparable = True
    try:
        _vc = json.load(open(os.path.join(store_dir("ldbc", a.sf, a.store), "manifest.json"))).get("value_codec") or {}
        counts_comparable = _vc.get("index_layout", "sorted_keys") != "posting" \
            and not (a.format == "dgraph" and _vc.get("chunk_target"))
    except OSError:
        pass
    if a.compare_counts:
        counts_comparable = True
    if not counts_comparable:
        print(f"[note] {a.store} runs a layout the Python twin does not (posting index / chunked postings): "
              f"per-step counts are NOT compared (rows + order are)")
    seen_queries = set()   # every plan file's query over the workloads: a --queries name outside it is a typo
    record_key = record_store_key(a.format, store_dir("ldbc", a.sf, a.store))   # the store's name in the pick record
    with CppServer(store_dir("ldbc", a.sf, a.store), a.format, **srv_kw) as srv:
        for wl in a.workloads:
            paths = sorted(glob.glob(os.path.join(_PLANS[wl], "*.json")))
            seen_queries |= {os.path.basename(p)[:-5] for p in paths}
            for path in paths:
                q = os.path.basename(path)[:-5]
                if a.queries and q not in a.queries:
                    continue
                ir = json.load(open(path))
                full = ir                                        # every body of the file (the forced runs below)
                if q in force:                                   # --force QUERY=BODY: the bundle with one unconditional body
                    ir = forced(ir, force[q])
                elif a.force_body:                               # --force-body NAME: every query that has that body
                    if a.force_body not in ir.get("plans", {}):
                        print(f"  skip {wl}/{q}: no body {a.force_body!r} in its bundle")
                        nskip += 1
                        continue
                    ir = forced(ir, a.force_body)
                elif pick_record.names_its_body(wl, ir):
                    # what a runner sends (plan_runs.ir_to_run): the record's body for this store, alone
                    if not pick_record.legal_bodies(ir, caps, of_label):
                        print(f"  skip {wl}/{q}: no legal body on this format")
                        nskip += 1
                        nnovar += 1
                        continue
                    ir = forced(ir, pick_record.body_for(wl, record_key, q, ir, caps, of_label, str(a.sf)))
                try:
                    plist = params_for(wl, q, a.sf, a.nparam)
                except Exception as e:
                    # a plan the check cannot give params is a plan it never compares: a failure, not a skip
                    print(f"  NO PARAMS {wl}/{q}: {type(e).__name__}: {e}")
                    nbad += 1
                    continue
                try:
                    out = output_name(ir, caps, of_label, str(a.sf))
                except NoVariant:                    # a twin whose capability this format lacks (hop1_posts_props_fused on dgraph)
                    print(f"  skip {wl}/{q}: no variant for this format")
                    nskip += 1
                    nnovar += 1
                    continue
                # every body of the bundle valid on this store runs, not only the selected one (one per body_sha): a
                # body no format selects is still checked on each format it is valid on (a forced run names its body)
                runs = [(None, ir, out)]
                if "plans" in ir and not (q in force or a.force_body) and not a.selected_only:
                    sel_sha = body_sha(ir, select_variant(ir, caps, of_label, str(a.sf)))
                    for b in valid_bodies(full, caps, of_label):
                        if body_sha(full, b) != sel_sha:
                            f_ir = forced(full, b)
                            runs.append((b, f_ir, output_name(f_ir, caps, of_label, str(a.sf))))
                for body, ir, out in runs:
                    for idx, args in enumerate(plist, 1):
                        t1 = time.time()
                        try:
                            cell_store = store
                            if wl.startswith("writes"):
                                shadow.reset()                       # each write cell starts pristine
                                cell_store = wstore
                            outs, kv = run_ir(ir, fmt, cell_store, params=args, format_caps=caps,
                                              caps_of_label=of_label, scale=str(a.sf), twin_counts=a.twin_counts)
                            # a step whose counts the twin disowns (cost.TwinCountsRefused: Per-type CSR's label rule)
                            # keeps its name and the refusal; its rows are compared like any other step's
                            py_steps = [(s.out, s.op, c) if isinstance(c, TwinCountsRefused) else
                                        (s.out, s.op, c.S, c.G, c.N, c.P, c.D,
                                         c.S_miss, c.G_miss, c.N_miss, c.get_value_bytes,
                                         c.scan_value_bytes, c.write_key_bytes, c.write_value_bytes)
                                        for s, _p, c in kv]
                            py_rows = outs[out]
                        except Exception as e:
                            reason = f"py error: {type(e).__name__}: {e}"
                            py_steps, py_rows = None, None
                            # Python's signal that the format lacks an op -- an AttributeError on the
                            # missing format-specific op. Captured HERE, from the exception itself: a
                            # C++-only refusal must never read as not runnable (its text is quoted into
                            # `reason` later, so string-matching reason would score a dispatch BUG as green).
                            py_missing_op = isinstance(e, AttributeError) and "has no attribute" in str(e)
                        else:
                            reason = None
                            py_missing_op = False
                        t2 = time.time()
                        cpp = srv.run(ir, args, run={"P": a.P})
                        t3 = time.time()
                        if reason is None:
                            reason = diff_cell(py_steps, py_rows, cpp, not a.skip_bytes, counts_comparable)
                        if reason is None and "variants" in ir and not cpp.get("error"):
                            # the SELECTION itself: the body C++ ran (provenance.body) must be the one Python selects
                            # with the same caps -- rows alone cannot show it (a selector's bodies return the same rows)
                            nbody += 1
                            want = select_variant(ir, caps, of_label, str(a.sf))
                            got = (cpp.get("provenance") or {}).get("body")
                            if got != want:
                                reason = f"variant: cpp ran {got!r}, python selects {want!r}"
                        if reason is None and not cpp.get("error"):
                            # the body's IDENTITY: the sha C++ records (provenance.body_sha, ir/plan.cc body_sha_of) must
                            # equal Python's of the same body (planir/bundle.body_sha) -- the two serializers agree
                            nsha += 1
                            want_sha = body_sha(ir, select_variant(ir, caps, of_label, str(a.sf)) if "variants" in ir else None)
                            got_sha = (cpp.get("provenance") or {}).get("body_sha")
                            if got_sha != want_sha:
                                reason = f"body_sha: cpp {got_sha!r} != python {want_sha!r}"
                        if body is not None and py_missing_op and "unknown op" in str((cpp.get("error") or {}).get("what", "")):
                            # a body this store does not select uses an op BOTH sides lack here (a fallback body whose
                            # variant does not name the cap, e.g. entry_range's bucket walk off dgraph): not runnable on
                            # this format, as the grid's choose treats it; counted, not a mismatch
                            status, reason = "NOT_RUNNABLE", None
                        else:
                            status = "OK" if reason is None else "MISMATCH"
                        nok += status == "OK"
                        nbad += status == "MISMATCH"
                        nnotrun += status == "NOT_RUNNABLE"
                        # the cell's Python op total: NONE when a step's counts are refused (a partial sum would read as
                        # the cell's total); the number of refused steps is recorded beside it
                        nref = sum(1 for s in (py_steps or []) if isinstance(s[2], TwinCountsRefused))
                        total = None if nref else sum(s[2] + s[3] + s[4] for s in (py_steps or []))
                        cells.append({"workload": wl, "query": q, "body": body, "param_idx": idx, "status": status,
                                      "reason": reason, "ops": total, "refused_steps": nref,
                                      "steps": len(py_steps or []), "py_s": round(t2 - t1, 3),
                                      "cpp_s": round(t3 - t2, 3)})
                        if counts_comparable and py_steps and not cpp.get("error"):
                            # EVERY step whose counts differ (reason names the first only): [step, op, py, cpp], the
                            # ten count lanes of diff_cell
                            cells[-1]["step_diffs"] = [[p_[0], p_[1], list(p_[2:10]), list(c_[2:10])]
                                                       for p_, c_ in zip(py_steps, step_costs(cpp))
                                                       if not isinstance(p_[2], TwinCountsRefused) and p_[:10] != c_[:10]]
                        if reason:
                            print(f"  MISMATCH {wl}/{q}{'' if body is None else ':' + body} p{idx}: {reason}", flush=True)
                n_q = sum(1 for c in cells if c["query"] == q and c["workload"] == wl)
                n_q_ok = sum(1 for c in cells if c["query"] == q and c["workload"] == wl and c["status"] == "OK")
                print(f"  {wl}/{q}: {n_q_ok}/{n_q} ok", flush=True)
    novar = f" ({nnovar} with no variant for this format)" if nnovar else ""
    nother = sum(1 for c in cells if c.get("body"))
    notrun = f", {nnotrun} not runnable here (both sides lack an op of a body this store does not select)" if nnotrun else ""
    print(f"\n{nok} ok, {nbad} mismatch, {nskip} skipped{novar}; variant pick checked on {nbody} cells, body sha on {nsha}; "
          f"{nother} of the cells ran a valid body this store does not select{notrun}  ({time.time() - t0:.0f}s)")
    nref_all, nsteps_all = sum(c.get("refused_steps", 0) for c in cells), sum(c.get("steps", 0) for c in cells)
    if counts_comparable and nref_all:
        print(f"counts compared on {nsteps_all - nref_all} of {nsteps_all} steps; {nref_all} steps on "
              f"{sum(1 for c in cells if c.get('refused_steps'))} cells are refused by the Python twin (cost.TwinCountsRefused: "
              f"not the format's counts) and were left out" + ("" if not a.twin_counts else f" -- none: --twin-counts {a.twin_counts}"))
    # a check that compared nothing is a failure, never "0 mismatch" (a --queries typo, an empty plans dir, a slice)
    unknown = sorted(set(a.queries or []) - seen_queries)
    if unknown:
        print(f"FAIL: --queries {unknown} match no plan file of {a.workloads}")
        nbad += 1
    if nok + nbad == 0:
        print(f"FAIL: 0 cells compared ({nskip} skipped) on {a.store}, workloads {a.workloads}, queries {a.queries or 'all'}")
        nbad += 1
    if counts_comparable and cells and nsteps_all - nref_all == 0:
        print("FAIL: counts were to be compared and 0 steps were")
        nbad += 1
    if a.report:
        os.makedirs(os.path.dirname(os.path.abspath(a.report)), exist_ok=True)
        json.dump({"sf": a.sf, "nparam": a.nparam, "store": a.store, "P": a.P, "bytes_checked": not a.skip_bytes,
                   "counts_checked": counts_comparable, "twin_counts": a.twin_counts,
                   "cells": cells}, open(a.report, "w"), indent=1)
        print(f"report -> {a.report}")
    return 1 if nbad else 0


if __name__ == "__main__":
    sys.exit(main())
