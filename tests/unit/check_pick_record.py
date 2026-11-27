#!/usr/bin/env python3
"""The pick record (kvevaluator.plan_choice.pick_record) for the named stores: no store, no binary.

  1. every LDBC named store x generated query x scale (sf0.1, SF3, SF30, and None = the deciding scale) whose entry
     is v1's carried selection (rule carried_v1), or that has one legal body: the body the record resolves
     (pick_record.body_for) is the body v1's selection selects there (the plan file at plan_choice/generate.V1_TREE, its
     `variants`), by body sha. An entry decided on our captures may differ by design: counted and skipped here
     (python/kvcost/layout_cost/grid_pick_record.py --check checks those); a choice with no entry fails;
  2. one legal body resolves itself with no entry; a choice with no entry is refused;
  3. a carried entry is no anchor (anchor_of): a planted carried entry within the margin of the cheapest does not
     survive the decision (candidates.decide picks the cheapest), a decided entry does;
  4. the engine bound's plan builds: microbench cheapest_plan_for on every shape x named format (a shape the format
     lacks the caps for refuses by name, as plan_for does); the record route counted.
  5. the record keys a named store by its store dir's name (pick_record.store_key = stores.cpp_store_name at the
     canonical schema and the default knobs): every named store's key; a format name reads the same entries as its
     store's name.
  6. a cell's picks at a scale (grid_capture.record_cell_entries(scale=), what predict mode prices and joins) are the
     scale's own recorded entry where the record has one (pick_record.entry), else the decided one: every non-carried
     cell x query x scale (None, sf0.1, sf0.3, SF3, SF30), at least one a scale's own.
  7. the one identity and binding rule, on a planted copy of the record at a scale that is not the
     deciding one and on fixture run records: a capture's store is its layout (picks.layout_store: its own name when
     measured; a composed cell by its spec, a copied diagonal by its source); a row binds there only for a store the
     record checked at that scale (bound_store), and so does the anchor (record_anchor); the deciding scale binds
     nothing and anchors as before; store mode binds a whole assignment where checked, option mode (the default, the
     the mix cost cache, the front's re-price) never; the writer records under the same identity (grid_pins.stores_of). Control:
     identity by name and every store bound make the composed and unchecked cases wrong.
  8. a dataset scale that is not a number (LinkBench's n1m, JOB's full) is a key as written (pick_record.tag), and the
     scale-aware readers (pick_record.entry, picks.bound_store) take it: the decided entry, unbound unless checked there.
  9. a --picks run reads the workload's own section (grid_bodies.cell_picks): for interactive, bi and microbench, every
     meta cell the section holds: the bodies are the section's entries for the workload's queries.

 10. the three files (pick_record.dump, load, load_full): the tracked record round-trips; every row has its decision
     and back; a wrong header, a repeated row, a row with no decision, a decision with no row and a one-file record
     (pick_record.json) are refused.

    python3 tests/unit/check_pick_record.py [--record <a pick record's picks.csv>]   (default the tracked one)
"""
import glob
import json
import os
import shutil
import sys

from kvevaluator.paths import ROOT
from kvevaluator.plan_choice import generate as G
from kvevaluator.plan_choice import pick_record as P
from kvevaluator.plan_choice.candidates import MARGIN, _plan_file_at, decide
from kvevaluator.plans.ir.bundle import NoVariant, body_sha, select_variant


def check_selection():
    schema = G._schema("interactive")
    labels = sorted(set(schema.vertex_props) | set(schema.etypes))
    stores = {st.name: st for st in G.stores("interactive")}
    cells = differ = decided = carried = 0
    for path in sorted(glob.glob(os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "interactive", "plans",
                                              "*.json"))):
        ir = json.load(open(path))
        if "generated_by" not in ir:
            continue
        q = os.path.basename(path)[:-5]
        v1 = _plan_file_at(path, G.V1_TREE)            # v1's file: its selectors (today's files carry none)
        if v1 is None or "variants" not in v1:
            continue
        for name in P.LDBC_NAMED:
            sc = G.store_caps(stores[name], schema, labels)
            of_label = lambda label, sc=sc: sc.of_label.get(label, frozenset())
            for scale in (None, "0.1", "3", "30"):
                try:
                    want = body_sha(v1, select_variant(v1, sc.caps, of_label, scale or "3"))
                except NoVariant:
                    continue
                e = P.entry("interactive", name, q, scale)
                if e is not None and not P.is_carried(e) and len(P.legal_bodies(ir, sc.caps, of_label)) > 1:
                    decided += 1        # decided on our captures: grid_pick_record.py --check compares it to the chooser
                    continue
                cells += 1
                carried += e is not None and P.is_carried(e)
                try:
                    got = body_sha(ir, P.body_for("interactive", name, q, ir, sc.caps, of_label, scale))
                except ValueError as err:
                    differ += 1
                    print(f"  REFUSED {q} {name} scale {scale}: {err}")
                    continue
                if got != want:
                    differ += 1
                    print(f"  DIFFER {q} {name} scale {scale}: record {got}, v1's selection {want}")
    ok = carried > 0 and differ == 0
    print(f"{'PASS' if ok else 'FAIL'} 1 the record resolves v1's selected body: {cells} carried or single-body "
          f"(query, store, scale) cells ({carried} carried), {differ} differ; {decided} decided on our captures skipped (grid_pick_record.py "
          f"--check compares them to the chooser)")
    return not ok


def check_refusal():
    ir = {"plans": {"a": {"steps": [], "output": "x"}, "b": {"steps": [{"name": "s", "op": "dedup"}], "output": "s"}},
          "bodies": {"a": {"requires": []}, "b": {"requires": ["no_such_cap"]}}}
    one = P.body_for("interactive", "no-such-store", "no-such-query", ir, set(), lambda label: set())
    ir["bodies"]["b"]["requires"] = []
    try:
        P.body_for("interactive", "no-such-store", "no-such-query", ir, set(), lambda label: set())
        refused = False
    except ValueError:
        refused = True
    ok = one == "a" and refused
    print(f"{'PASS' if ok else 'FAIL'} 2 one legal body resolves itself ({one!r}); a choice with no entry is refused: "
          f"{refused}")
    return not ok


def check_anchor():
    scores = {"cheap": 100.0, "near": 100.0 * (1 + MARGIN / 2)}       # 'near' is within the margin of the cheapest
    carried = {"body": "near", "rule": P.CARRIED}
    decided = {"body": "near", "rule": "re-decided, the recorded plan the anchor, margin 0.1"}
    got_c = decide("q", {}, scores, P.anchor_of(carried) or min(sorted(scores), key=scores.get))[0]
    got_d = decide("q", {}, scores, P.anchor_of(decided))[0]
    ok = got_c == "cheap" and got_d == "near"
    print(f"{'PASS' if ok else 'FAIL'} 3 a carried entry is no anchor (picked {got_c!r}); a decided one is "
          f"(kept {got_d!r})")
    return not ok


def check_cheapest_builds():
    from kvevaluator.datasets.ldbc.schema import get_schema
    from kvevaluator.plan_choice import resolve
    from kvevaluator.workloads.microbench import queries as MQ
    from kvevaluator.workloads.microbench.params import query_params
    schema = get_schema("canonical")
    fmts = ["dgraph", "nebula", "arangodb", "pertype_csr"]
    built = recorded = lacks = 0
    bad = []
    for name in sorted(MQ.REGISTRY):
        args = query_params("0.1", name, 1)[0]
        for fmt in fmts:
            try:
                resolve.cheapest_plan_for("microbench", name, fmt, args, schema)
            except ValueError as e:
                if " lacks " in str(e):
                    lacks += 1
                    continue
                bad.append((name, fmt, str(e)[:120]))
                continue
            except Exception as e:  # -- any other failure is the finding
                bad.append((name, fmt, f"{type(e).__name__}: {str(e)[:120]}"))
                continue
            built += 1
            recorded += resolve.recorded_pick("microbench", name, fmt) is not None
    for b in bad:
        print("  FAIL", *b)
    ok = built > 0 and recorded > 0 and not bad
    print(f"{'PASS' if ok else 'FAIL'} 4 the engine bound's microbench plan builds: {built} (shape, format) cells "
          f"({recorded} through the record), {lacks} refused for missing caps, {len(bad)} failed")
    return not ok


def check_scale_entries():
    from kvevaluator.measure import grid_capture
    rec = P.load(P.PICKS_FILE)
    carried = set(P.carried_cells(P.load_full(P.PICKS_FILE)))
    n = own = 0
    bad = []
    for cell in sorted(c for c in rec.stores("interactive") if c not in carried):
        decided = rec.cell("interactive", cell)
        qs = sorted(decided)
        for scale in (None, "0.1", "0.3", "3", "30"):
            got = grid_capture.record_cell_entries(cell, qs, P.PICKS_FILE, scale)
            for q in qs:
                want = decided[q] if scale is None else rec.entry("interactive", cell, q, scale)
                n += 1
                own += want != decided[q]
                if got[q] != want:
                    bad.append((cell, q, scale, got[q]["body"], want["body"]))
    for b in bad:
        print("  FAIL", *b)
    ok = n > 0 and own > 0 and not bad
    print(f"{'PASS' if ok else 'FAIL'} 6 a cell's picks at a scale are the scale's own where the record has one: {n} "
          f"(cell, query, scale) cells, {own} a scale's own entry, {len(bad)} differ")
    return not ok


def check_store_keys():
    from kvevaluator.measure.stores import cpp_store_name
    from kvevaluator.plan_choice.optimizer.arms import split_arm, store_chunk_of
    rec = P.load(P.PICKS_FILE)
    sections = [(w, {k: rec.cell(w, k) for k in rec.stores(w)}) for w in ("interactive", "bi", "microbench", "job")]
    named = sorted({k for _w, s in sections for k in s if split_arm(k)[0] in {split_arm(n)[0] for n in P.LDBC_NAMED}})
    bad = [k for k in named if not (P.store_key(k) == k == cpp_store_name(split_arm(k)[0], "canonical", None,
                                                                        split_arm(k)[1] or store_chunk_of(split_arm(k)[0])))]
    same = all(rec.entry(w, split_arm(k)[0], q) == rec.entry(w, k, q) for w, s in sections for k in s
               if k in named and split_arm(k)[1] == store_chunk_of(split_arm(k)[0]) for q in s[k])
    ok = named and not bad and same and "pertype_csr-c64k" in named
    print(f"{'PASS' if ok else 'FAIL'} 5 store keys: {len(named)} named stores keyed by their store dir ({len(bad)} not: "
          f"{bad}); a format name reads its store's entries: {same}")
    return not ok


def check_three_files():
    """check 10: the record's three files (pick_record.dump / load / load_full): the tracked record round-trips; every
    row has its decision and back; the bound scales are the scale fingerprints' keys; the deciding rows' scale is the
    rule's decided_at; and load refuses a wrong header, a repeated row, a row with no decision and a one-file record"""
    import csv
    import shutil
    import tempfile
    full = P.load_full(P.PICKS_FILE)
    rows, stores, dec = P.split(full)
    bad = []
    if P.join([dict(zip(P.COLUMNS, r)) for r in rows], stores, dec) != full:
        bad.append("join(split(record)) is not the record")
    if sorted({r[2] for r in rows if r[1] == "interactive" and r[3] in full["cells"]}) and \
            not any(r[2] == P.tag(full["decided_at"]) for r in rows):
        bad.append("no row at the deciding scale")
    if stores["rule"]["decided_at"] != full["decided_at"]:
        bad.append("the stores file's decided_at is not the record's")
    if stores["bound_scales"].get("interactive") != {sf: sorted(v) for sf, v in full.get("scale_fingerprints", {}).items()}:
        bad.append("bound scales are not the scale fingerprints' keys")
    refused = {}
    with tempfile.TemporaryDirectory() as t:
        table, stores_p, dec_p = P.files_of(os.path.join(t, "picks.csv"))
        P.dump(full, table)
        if P.load_full(table) != full:
            bad.append("load_full(dump(record)) is not the record")
        lines = open(table).read().splitlines(keepends=True)

        def plant(name, text, other=None):
            for p_ in P.files_of(table):
                shutil.copy(p_, p_ + ".keep")
            with open(table, "w") as f:
                f.write(text)
            try:
                (P.load_full if other else P.load)(table)
                refused[name] = False
            except (ValueError, KeyError):
                refused[name] = True
            for p_ in P.files_of(table):
                shutil.copy(p_ + ".keep", p_)
        plant("a wrong header", lines[0].replace("carried", "carried_v1") + "".join(lines[1:]))
        plant("a repeated row", "".join(lines) + lines[1])
        first = next(csv.reader([lines[1]]))
        moved = ",".join(first[:4] + ["no_such_query"]) + "," + lines[1].split(",", 5)[5]
        plant("a row with no decision", "".join(lines[:1] + lines[2:]) + moved, other=True)
        plant("a decision with no row", "".join(lines[:1] + lines[2:]), other=True)
    try:
        P.load(os.path.join(str(ROOT), "inputs", "picks", "pick_record.json"))
        refused["a one-file record"] = False
    except ValueError:
        refused["a one-file record"] = True
    bad += [f"not refused: {k}" for k, v in refused.items() if not v]
    ok = not bad and len(refused) == 5
    print(f"{'PASS' if ok else 'FAIL'} 10 the three files: {len(rows)} rows round-trip; {sum(refused.values())} of "
          f"{len(refused)} bad files refused{': ' + '; '.join(bad) if bad else ''}")
    return not ok


def check_bound_store(rule_off=False):
    """check 7 on a planted copy of the record (its own scale fingerprints at a scale that is not the deciding one) and
    fixture run records; returns the names of the wrong cases"""
    import copy
    import tempfile
    from types import SimpleNamespace
    from kvcost.layout_cost import picks as K, compose as PR
    from kvevaluator.measure import grid_capture
    real = P.load_full(grid_capture.PICKS_FILE)
    S = next(s for s in ("0.1", "0.3", "30") if s != str(real["decided_at"]))        # a scale that is not the deciding one
    D = str(real["decided_at"])
    c1k, spco, spsp, coch = ("props_chunked_c1k_adj_chunked_c1k", "props_split_adj_condensed", "props_split_adj_split",
                             "props_condensed_adj_chunked")
    rec = copy.deepcopy(real)
    rec["scale_fingerprints"][S] = {x: "planted" for x in (c1k, spco, spsp, "w16_b3")}     # coch NOT checked at S
    some_q = sorted(rec["cells"][spco])[0]
    rec.setdefault("scales", {})[S] = {spco: {some_q: dict(rec["cells"][spco][some_q], why="planted")}}
    unchecked = copy.deepcopy(rec)
    unchecked["scale_fingerprints"][S].pop("w16_b3")

    def spec(name, set_="grid"):
        with open(os.path.join(str(ROOT), "inputs", "specs", set_, f"{name}.json")) as f:
            return json.load(f)

    def record(t, sf, set_, cell, **extra):
        """one cell's run record in a capture of its own below `t` (set_: the spec set its spec names, for the reader)"""
        d = os.path.join(t, "captures", set_, "interactive", "reported")
        os.makedirs(os.path.join(d, "counts"), exist_ok=True)
        with open(os.path.join(d, "counts", f"{cell}.json"), "w") as f:
            json.dump({"sf": sf, "cell": cell, **extra}, f)
        return d

    def row(store):                                  # recorded_picks' first value: a carried entry pins nothing (None)
        return {q: None if x.get("rule") == P.CARRIED else x["body"] for q, x in rec["cells"][store].items()}

    def anchor(store):
        return {q: P.anchor_of(e) or "" for q, e in rec["cells"][store].items()}

    c1k_spec = spec(c1k, "grid_chunked_1k")
    comp = {"composed_from": {"adjacency parts": "x"}}
    got, want = {}, {}
    saved = (grid_capture.PICKS_FILE, K.layout_store, K.bound_store)
    try:
        with tempfile.TemporaryDirectory() as t:
            planted = os.path.join(t, "planted", "picks.csv")
            P.dump(rec, planted)
            grid_capture.PICKS_FILE = planted
            pr = P.load(planted)                     # the readers' view of the planted record
            P.dump(unchecked, os.path.join(t, "unchecked", "picks.csv"))
            pr_unchecked = P.load(os.path.join(t, "unchecked", "picks.csv"))
            if rule_off:                             # the control: identity by name, every store bound off the deciding scale
                K.layout_store = lambda r, d, c: P.store_key(c)
                K.bound_store = lambda r, sf, st: None if not r or st is None or str(sf) == str(r.decided_at) else st
            # identity (layout_store)
            d = record(t, S, "grid", coch)
            got["identity: measured under its name"], want["identity: measured under its name"] = K.layout_store(pr, d, coch), coch
            d = record(t + "/a", S, "grid", spco, spec=spec(spco), **comp)
            got["identity: composed 64 KB cell"], want["identity: composed 64 KB cell"] = K.layout_store(pr, d, spco), spco
            d = record(t + "/b", S, "grid", "props_split_adj_chunked", spec={"planted": "a layout no store has"}, **comp)
            got["identity: composed 1 KB Sp-ChK"] = K.layout_store(pr, d, "props_split_adj_chunked")
            want["identity: composed 1 KB Sp-ChK"] = None
            dcc = record(t + "/c", S, "grid", "props_chunked_adj_chunked", spec=c1k_spec, **comp)
            got["identity: composed 1 KB ChK-ChK"] = K.layout_store(pr, dcc, "props_chunked_adj_chunked")
            want["identity: composed 1 KB ChK-ChK"] = c1k
            d = record(t + "/d", S, "grid", "props_chunked_adj_chunked", spec=c1k_spec,
                       named_as_the_grids_cell_from=f"x/grid_chunked_1k/interactive/reported/counts/{c1k}.json")
            got["identity: a copied 1 KB diagonal"] = K.layout_store(pr, d, "props_chunked_adj_chunked")
            want["identity: a copied 1 KB diagonal"] = c1k
            d = record(t + "/m", S, "grid", "mix_0123456789", spec=spec(coch))      # a built mix, named by its content
            got["identity: a mix measured under another name"] = K.layout_store(pr, d, "mix_0123456789")
            want["identity: a mix measured under another name"] = coch               # the record store of its layout
            # binding (recorded_picks) at S
            d = record(t + "/f", S, "grid", spco, spec=spec(spco), **comp)
            got["bound: composed 64 KB Sp-Co, row"] = K.recorded_picks(d, spco, K.PlanRule())[:2]
            want["bound: composed 64 KB Sp-Co, row"] = (row(spco), {some_q: rec["cells"][spco][some_q]["body"]})
            got["bound: composed 1 KB ChK-ChK, the 1 KB row"] = K.recorded_picks(dcc, "props_chunked_adj_chunked", K.PlanRule())[0]
            want["bound: composed 1 KB ChK-ChK, the 1 KB row"] = row(c1k)
            d = record(t + "/g", S, "grid", "props_split_adj_chunked", spec={"planted": "x"}, **comp)
            got["unbound: a layout no store has"] = K.recorded_picks(d, "props_split_adj_chunked", K.PlanRule())[:2]
            want["unbound: a layout no store has"] = (None, None)
            d = record(t + "/h", S, "grid", coch)
            got["unbound: a store not checked at S"] = K.recorded_picks(d, coch, K.PlanRule())[:2]
            want["unbound: a store not checked at S"] = (None, None)
            d = record(t + "/i", D, "grid", spco, spec=spec(spco), **comp)
            got["the deciding scale binds nothing"] = K.recorded_picks(d, spco, K.PlanRule())[:2]
            want["the deciding scale binds nothing"] = (None, None)
            # the anchor (record_anchor), through the same rule
            got["anchor: composed 1 KB ChK-ChK at S"] = K.record_anchor("interactive", "props_chunked_adj_chunked", K.PlanRule(), dcc)
            want["anchor: composed 1 KB ChK-ChK at S"] = anchor(c1k)
            got["anchor: unbound at S is none"] = isinstance(K.record_anchor("interactive", coch, K.PlanRule(), d := record(t + "/j", S, "grid", coch)), K.NoAnchor)
            want["anchor: unbound at S is none"] = True
            d = record(t + "/k", D, "grid", "props_chunked_adj_chunked", spec=c1k_spec, **comp)
            got["anchor: composed 1 KB ChK-ChK at the deciding scale"] = K.record_anchor("interactive", "props_chunked_adj_chunked", K.PlanRule(), d)
            want["anchor: composed 1 KB ChK-ChK at the deciding scale"] = anchor(c1k)
            d = record(t + "/l", D, "grid", spsp)
            got["anchor: a measured cell at the deciding scale"] = K.record_anchor("interactive", spsp, K.PlanRule(), d)
            want["anchor: a measured cell at the deciding scale"] = anchor(spsp)
            # store mode: a whole assignment binds its store where it was checked (recorded_for_option)
            assign = {tuple(k.split(":")): v for k, v in rec["assignment"]["w16_b3"].items()}
            for name, r_, w_ in (("store mode: w16_b3 checked at S", pr, {q: x["body"] for q, x in rec["cells"]["w16_b3"].items()}),
                                 ("store mode: w16_b3 not checked at S", pr_unchecked, None)):
                dd = {"record": r_, "sf": S, "read_dir": d, "composer": SimpleNamespace(bind_per_type_record=False)}
                got[name], want[name] = PR.recorded_for_option(dd, assign, set(assign))[0], w_
            # the writer records under the same identity (grid_pins.stores_of)
            import importlib.util
            spec_ = importlib.util.spec_from_file_location("grid_pins", os.path.join(str(ROOT), "python", "kvcost", "layout_cost", "grid_pick_record.py"))
            GP = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(GP)
            dco = record(t + "/m", S, "grid", spco, spec=spec(spco), **comp)
            cases = {"writer: composed 64 KB Sp-Co and 1 KB ChK-ChK": ([(spco, dco), ("props_chunked_adj_chunked", dcc)],
                                                                      [(spco, dco, spco), ("props_chunked_adj_chunked", dcc, c1k)]),
                     "writer: a layout no store has is refused": ([("props_split_adj_chunked", record(t + "/n", S, "grid", "props_split_adj_chunked", spec={"p": 1}, **comp))], "refused"),
                     "writer: two cells of one store are refused": ([("props_chunked_adj_chunked", dcc), (c1k, record(t + "/o", S, "grid_chunked_1k", c1k, spec=c1k_spec))], "refused")}
            real_spec_kind = GP.spec_kind
            GP.spec_kind = lambda *_a: "grid"                  # the fixtures' records name no spec_path: none is a format
            for i, (name, (cells, w_)) in enumerate(cases.items()):
                cap = os.path.join(t, f"writer-{i}", "counts")   # the case's cells in one capture, as grid_pins reads them
                os.makedirs(cap)
                for cell, d_ in cells:
                    shutil.copy(os.path.join(d_, "counts", f"{cell}.json"), cap)
                    open(os.path.join(cap, f"{cell}.csv"), "w").close()
                root = os.path.dirname(cap)
                try:
                    got[name] = sorted(GP.stores_of(pr, SimpleNamespace(cap=root)))
                except SystemExit:
                    got[name] = "refused"
                want[name] = w_ if w_ == "refused" else sorted((cell, root, store) for cell, _d, store in w_)
            GP.spec_kind = real_spec_kind
    finally:
        grid_capture.PICKS_FILE, K.layout_store, K.bound_store = saved
    # the modes: option mode is the default everywhere a search prices, and the front's re-price passes no other
    import inspect
    from kvcost.layout_search import search as SE
    import importlib.util
    import subprocess
    # the front's re-price, run where the script runs (its own imports sit beside it): which mode does it pass?
    probe = ("import equal_bytes_front as E\n"
             "seen = []\n"
             "E.solve = lambda pb, cur, cap, start, plain: ({('props', 'x'): 'split'}, 1.0, 2.0, 1, None)\n"
             "E.cost_mix = lambda w, data, assign, **kw: seen.append(kw.get('mode', 'option')) or {'x': 1}\n"
             "E.currencies = lambda t: {'ops': 1.0, 'bytes': 2.0}\n"
             "E.raw_of = lambda t: {}\n"
             "E._W.update(w={}, sets={False: (None, None, None, None)})\n"
             "E._solve_one((0, 1.0, False, True, None))\n"
             "print(','.join(seen))\n")
    out = subprocess.run([sys.executable, "-c", probe], cwd=os.path.join(str(ROOT), "experiments", "layout_choice"),
                         capture_output=True, text=True)
    seen = out.stdout.strip().split(",") if out.returncode == 0 else [f"rc {out.returncode}: {out.stderr.strip()[-200:]}"]
    defaults = {f.__name__: inspect.signature(f).parameters["mode"].default for f in (PR.query_pick, PR.query_cost, PR.cost_mix)}
    got["option mode: the front's re-price"], want["option mode: the front's re-price"] = seen, ["option"]
    got["option mode: the default"] = defaults
    want["option mode: the default"] = {"query_pick": "option", "query_cost": "option", "cost_mix": "option"}
    got["option mode: the cost cache pass no mode"] = "mode=" not in inspect.getsource(SE.Problem.fill_cost_cache)
    want["option mode: the cost cache pass no mode"] = True
    bad = [k for k in want if got[k] != want[k]]
    if not rule_off:
        for k in bad:
            print(f"    WRONG {k}: got {str(got[k])[:160]} want {str(want[k])[:160]}")
        print(f"{'PASS' if not bad else 'FAIL'} 7 one identity and binding rule (picks.layout_store, bound_store; price "
              f"modes), at sf{S} against a record decided at sf{D}: {len(want)} cases, {len(bad)} wrong")
    return bad


def check_bound_store_control():
    """the control: identity by name and every store bound must make the composed and the unchecked cases wrong"""
    bad = check_bound_store(rule_off=True)
    need = {"identity: composed 1 KB Sp-ChK", "identity: composed 1 KB ChK-ChK", "identity: a mix measured under another name",
            "unbound: a layout no store has",
            "unbound: a store not checked at S", "anchor: composed 1 KB ChK-ChK at S", "anchor: unbound at S is none"}
    ok = need <= set(bad)
    print(f"{'PASS' if ok else 'FAIL'} 7c the control (identity by name, every store bound): wrong on {len(bad)}: "
          f"{sorted(bad)}; must include {sorted(need - set(bad)) or 'all it needs'}")
    return not ok


def check_dataset_tags():
    """check 8: a dataset scale that is not a number (LinkBench's n1m, JOB's full) is a key as written; a number is
    written one way (pick_record.tag). The scale-aware readers take both: bound_store, entry"""
    import tempfile
    from kvcost.layout_cost import picks as K
    rec, full = P.load(), P.load_full()
    bad = [f"tag({s!r}) = {_tag_or_error(s)!r}, not {w!r}" for s, w in (("n1m", "n1m"), ("full", "full"), ("3.0", "3"), (3, "3"),
                                                                ("0.10", "0.1"), ("30", "30"))
           if _tag_or_error(s) != w]
    cells = 0
    for wl in ("linkbench", "job"):
        sec = full.get("workloads", {}).get(wl, {}).get("cells", {})
        for store in sorted(sec)[:3]:
            for q in sorted(sec[store])[:3]:
                for scale in ("n1m", "full"):
                    try:
                        e = rec.entry(wl, store, q, scale=scale)
                        b = K.bound_store(rec, scale, store)
                    except Exception as x:  # the failure this check is for
                        bad.append(f"{wl} {store} {q} at {scale}: {type(x).__name__}: {x}")
                        continue
                    cells += 1
                    decided = e is not None and e["body"] == sec[store][q]["body"]
                    if not decided or b is not None:
                        bad.append(f"{wl} {store} {q} at {scale}: entry {'the decided one' if decided else e}, "
                                   f"bound {b} (want the decided entry, unbound: no scale entry there)")
    try:
        with tempfile.TemporaryDirectory() as t:
            P.dump(dict(full, scale_fingerprints=dict(full.get("scale_fingerprints", {}), n1m={"some_store": "planted"})),
                   os.path.join(t, "picks.csv"))
            planted = P.load(os.path.join(t, "picks.csv"))
        if K.bound_store(planted, "n1m", "some_store") != "some_store":
            bad.append("a store checked at n1m does not bind there")
    except Exception as x:
        bad.append(f"a store checked at n1m: {type(x).__name__}: {x}")
    ok = cells > 0 and not bad
    print(f"{'PASS' if ok else 'FAIL'} 8 non-numeric dataset scales (n1m, full) are keys: {cells} (store, query, scale) "
          f"read, {len(bad)} wrong{': ' + '; '.join(bad[:4]) if bad else ''}")
    return not ok


def _tag_or_error(s):
    try:
        return P.tag(s)
    except Exception as x:
        return f"{type(x).__name__}"


def check_workload_picks():
    from kvevaluator.measure import grid_bodies
    rec = P.load(P.PICKS_FILE)
    n = 0
    bad = []
    for wl in ("interactive", "bi", "microbench"):
        sec = {c: rec.cell(wl, c) for c in rec.stores(wl)}
        for cell in sorted(c for c in sec if c.startswith("props_")):
            try:
                got = grid_bodies.cell_picks(cell, wl, P.PICKS_FILE, sorted(sec[cell]))
            except SystemExit as e:
                bad.append((wl, cell, f"refused: {str(e)[:100]}"))
                continue
            for q, e in sorted(sec[cell].items()):
                n += 1
                if got.get(q) != e["body"]:
                    bad.append((wl, cell, q, got.get(q), e["body"]))
    for b in bad[:10]:
        print("  FAIL", *b)
    ok = n > 0 and not bad
    print(f"{'PASS' if ok else 'FAIL'} 9 a --picks run reads the workload's own section: {n} (workload, cell, query) "
          f"cells, {len(bad)} differ")
    return not ok


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--record", default=None, help="a pick record's table to check (default the tracked one)")
    a = ap.parse_args()
    if a.record:
        P.PICKS_FILE = os.path.abspath(a.record)
    print(f"record: {os.path.relpath(P.PICKS_FILE, str(ROOT))}")
    bad = (check_selection() + check_refusal() + check_anchor() + check_cheapest_builds() + check_store_keys()
           + check_scale_entries() + check_three_files() + bool(check_bound_store()) + check_bound_store_control() + check_dataset_tags()
           + check_workload_picks())
    print(f"check_pick_record: 9 checks and a control, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
