"""CROSS-LANGUAGE write-EFFECT differential: run a write-then-READ plan on BOTH evaluators and
compare the read's ROWS. The write cpp-diff's byte columns compare two different value
currencies (Python's neutral model vs C++'s encoded sizes), so byte equality cannot carry the
correctness burden for the dgraph family -- but a write's EFFECT on a later read is
currency-free: after removing ONE entry from a shared posting, both sides must still return
the surviving co-tenants, and only them. This is the C++ twin of check_write_effects.py's
shared-posting cases, phrased as a differential so Python stays the reference.

Scenarios (dgraph family, sf0.1 canonical stores):
  1. delete_edge of one like -> the message's OTHER likers survive, the deleted one is gone.
  2. update_prop of an indexed firstName -> the OLD token still returns the other holders.
  3. delete_vertex -> a neighbour's posting keeps its other entries.

Run: python3 tests/gates/check_cpp_write_effects.py [--sf 0.1] [--format dgraph]
"""
import argparse
import json as _json_mod
import os
import sys
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.measure.server import CppServer
from kvevaluator.measure.stores import store_dir, cpp_store_name, store_chunk
from kvevaluator.capture.store_identity import store_chunk_target
from kvevaluator.formats import make_format
from kvevaluator.oracle.interp import run_ir
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.datasets import demo as data
from kvevaluator.kvstore.kvstore import CountingKV
from kvevaluator.kvstore.shadow import WriteShadowKV
from kvevaluator.oracle.run import open_store

ERRS = []


def check(ok, what):
    print(f"  {'OK  ' if ok else 'FAIL'} {what}")
    if not ok:
        ERRS.append(what)


def _plan(name, steps, output):
    return {"plan": name, "params": {}, "steps": steps, "output": output}


def _rows(v):
    """Normalize a read_neighbors/read_index_eq result to a sorted vid list."""
    out = []
    for r in v:
        out.append(r["dst"] if isinstance(r, dict) and "dst" in r
                   else (r["_vid"] if isinstance(r, dict) else r))
    return sorted(map(str, out))


def _canon(rows):
    """A row list as a sorted list of canonical JSON strings (dict key order ignored)."""
    return sorted(_json_mod.dumps(r, sort_keys=True, default=str) for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--format", default="dgraph")
    ap.add_argument("--schema", default="canonical",
                    help="the design; the C++ store dir defaults to <format>-<schema>")
    a = ap.parse_args()
    if a.format not in ("dgraph", "nebula", "arangodb", "pertype_csr"):
        ap.error("no fixture discovery for this format")
    nebula_family = a.format == "nebula"
    # arangodb / pertype_csr run ONLY the T2 pin (update one prop, read an UNTOUCHED index
    # column -- the apply-order state-corruption class that hid in pertype_csr because these
    # two formats had no effect gate at all; the per-cell write differential cannot see
    # post-write state). Their store key shapes need no posting discovery for that.
    pin_only = a.format in ("arangodb", "pertype_csr")

    if a.schema == "canonical":
        schema = data.schema(a.sf)
    else:
        from kvevaluator.datasets.ldbc.schema import get_schema
        schema = get_schema(a.schema)
    fmt = make_format(a.format, schema, False)
    base = open_store(fmt, "abstract", a.sf, lambda: data.load(a.sf), a.schema)
    # each scenario runs over a fresh copy-on-write shadow, so a write never leaks into the
    # next scenario (or the cached store) -- the same rule every write cell follows
    shadow = WriteShadowKV(base.backend)
    store = CountingKV(shadow, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes, write_mode=True)
    store_name = cpp_store_name(a.format, a.schema, None, store_chunk(a.format))   # the bridge's naming rule
    # the store's chunk target selects too (coarse_chunks, via the format's pack target), as in the C++ session
    store_chunk_bytes = store_chunk_target(store_dir("ldbc", a.sf, store_name))
    caps = caps_for(a.format, schema, chunk=store_chunk_bytes)
    of_label = caps_resolver(a.format, schema, chunk=store_chunk_bytes)
    # The per-step OP COUNTS are comparable only when both sides run the SAME index layout.
    # Python's per-type format still has the sorted-keys index; a C++ store ingested with the
    # posting index (the pertype_csr default, manifest value_codec.index_layout)
    # legitimately does different index work -- there the ROWS carry the effect check and the
    # count line is reported as an expected divergence, not a failure (known follow-on: the
    # Python twin's posting index).
    counts_comparable = True
    try:
        import json as _json
        with open(os.path.join(store_dir("ldbc", a.sf, store_name), "manifest.json")) as _f:
            _vc = _json.load(_f).get("value_codec") or {}
        # a chunked dgraph store (dgraph-c64k): the Python twin's postings are unbounded
        counts_comparable = _vc.get("index_layout", "sorted_keys") != "posting" \
            and not (a.format == "dgraph" and _vc.get("chunk_target"))
    except OSError:
        pass
    if not counts_comparable:
        print(f"  note: {store_name} runs a layout the Python twin does not (posting index / chunked "
              f"postings) -- per-step op counts are reported, not compared")

    def check_counts(name, py_c, cpp_c):
        if counts_comparable:
            check(py_c == cpp_c, f"{name}: per-step op counts agree"
                  + ("" if py_c == cpp_c else f" -- py {py_c} vs cpp {cpp_c}"))
        else:
            same = "equal" if py_c == cpp_c else "differ"
            print(f"  --   {name}: op counts not compared (layouts differ); they {same} here -- py {py_c} vs cpp {cpp_c}")

    srv_kw = {}
    if a.schema != "canonical":
        srv_kw["schema"] = schema_path("ldbc", a.schema)

    if pin_only:
        from kvevaluator.schema.vids import person_vid
        from kvevaluator.datasets.ldbc import params as ldbc_params
        srv_ctx = CppServer(store_dir("ldbc", a.sf, store_name), a.format, **srv_kw)
        with srv_ctx as srv:
            def run_pin(name, ir):
                shadow.reset()
                # twin_counts: this check compares ROWS on Per-type CSR; its Python counts on the steps the twin
                # disowns (runner.run_plan) are printed, never compared, and the bypass says so in the output
                outs, kv = run_ir(ir, fmt, store, params={}, format_caps=caps,
                                  caps_of_label=of_label, twin_counts="old-label-rule")
                resp = srv.run(ir, {})
                if resp.get("error"):
                    raise SystemExit(f"cpp {name}: {resp['error']['what']}")
                py_c = [(st_.op, c.S, c.G, c.N, c.P, c.D) for st_, _p, c in kv]
                cpp_c = [(st_["op"], st_["S"], st_["G"], st_["N"], st_["P"], st_["D"])
                         for st_ in resp["steps"]]
                check_counts(name, py_c, cpp_c)
                return outs[ir["output"]], resp["rows"]

            pid = ldbc_params.param_rows(a.sf, "ic1")[0]["personId"]
            pv = person_vid(pid)
            ir = _plan("fx_pre", [{"name": "out", "op": "read_vertex_props", "vids": [pv],
                                   "props": ["creationDate"], "label": "Person"}], "out")
            py, cpp = run_pin("fx_pre", ir)
            cdate = py[0].get("creationDate")
            check(cdate is not None and py[0].get("creationDate") == cpp[0].get("creationDate"),
                  "pin prep: creationDate agrees")
            # update firstName -> the UNTOUCHED creationDate index entry must survive (T2)
            ir = _plan("fx_t2_untouched_index", [
                {"name": "upd", "op": "update_prop", "vid": pv, "label": "Person",
                 "prop": "firstName", "value": "Zz_t2_pin"},
                {"name": "out", "op": "read_index_eq", "label": "Person",
                 "prop": "creationDate", "value": cdate},
            ], "out")
            py, cpp = run_pin("fx_t2_untouched_index", ir)
            check(_rows(py) == _rows(cpp), "T2 pin: untouched-index holders agree")
            check(pv in _rows(py), "T2 pin: the updated vertex STAYS in the untouched index")
            # the MULTI-column update: one row write; the untouched index keeps the vertex, and
            # every changed column reads back
            multi = {"firstName": "Zz_multi", "lastName": "Zz_ln", "browserUsed": "Zz_b"}
            ir = _plan("fx_multi_untouched_index", [
                {"name": "upd", "op": "update_prop", "vid": pv, "label": "Person", "props": multi},
                {"name": "out", "op": "read_index_eq", "label": "Person",
                 "prop": "creationDate", "value": cdate},
            ], "out")
            py, cpp = run_pin("fx_multi_untouched_index", ir)
            check(_rows(py) == _rows(cpp) and pv in _rows(py),
                  "multi update: untouched-index holders agree and keep the vertex")
            ir = _plan("fx_multi_readback", [
                {"name": "upd", "op": "update_prop", "vid": pv, "label": "Person", "props": multi},
                {"name": "out", "op": "read_vertex_props", "vids": [pv],
                 "props": sorted(multi), "label": "Person"},
            ], "out")
            py, cpp = run_pin("fx_multi_readback", ir)
            check(py == cpp and all(py[0].get(k) == v for k, v in multi.items()),
                  f"multi update: every changed column reads back, both sides ({py[0] if py else None})")
            # update_edge_prop on an EXISTING knows edge: both stored copies carry the new date
            from kvevaluator.workloads.writes import params as w_params
            e = w_params.param_rows(a.sf, "w_upd_edge")[0]
            sv, dv = person_vid(e["srcId"]), person_vid(e["dstId"])
            new_date = 1300000000123
            for side, src_, dir_, other in (("forward", sv, "out", dv), ("reverse", dv, "in", sv)):
                ir = _plan(f"fx_edge_update_{side}", [
                    {"name": "upd", "op": "update_edge_prop", "etype": "knows", "src": sv, "dst": dv,
                     "props": {"creationDate": new_date}, "src_label": "Person", "dst_label": "Person"},
                    {"name": "out", "op": "read_neighbors", "srcs": [src_], "etype": "knows",
                     "dir": dir_, "edge_props": ["creationDate"]},
                ], "out")
                py, cpp = run_pin(f"fx_edge_update_{side}", ir)
                got = {r["dst"]: r.get("creationDate") for r in py}
                check(_canon(py) == _canon(cpp) and got.get(other) == new_date,
                      f"update_edge_prop: the {side} copy carries the new date, both sides "
                      f"({got.get(other)})")
            if a.format == "arangodb":
                # the scan of a collection's documents (read_vertices_by_label with props; label_rows_one_run) AFTER a
                # write: an inserted document's docid is in the writes' own space (C++ the top bit, Python "w..."), past
                # every loaded one, so it is the run's LAST row; a deleted vertex's document leaves the run. Rows in
                # scan order and the per-step counts, both sides.
                scan = {"name": "out", "op": "read_vertices_by_label", "label": "Person", "props": ["firstName", "id"]}
                py0, cpp0 = run_pin("fx_scan_before", _plan("fx_scan_before", [scan], "out"))
                order = lambda rows: [r["_vid"] for r in rows]
                check(len(py0) > 0 and py0 == cpp0, f"document scan: {len(py0)} rows, the same rows in the same order on both sides")
                new = person_vid(987654321012345)
                check(new not in order(py0), "scan after insert: the new vid is not loaded")
                ir = _plan("fx_scan_after_insert", [
                    {"name": "ins", "op": "insert_vertex", "vid": new, "label": "Person",
                     "props": {"firstName": "Zz_scan_fx", "lastName": "Writer", "gender": "other", "birthday": 631152000000,
                               "creationDate": 1577836800000, "locationIP": "10.0.0.1", "browserUsed": "Chrome",
                               "email": ["w@example.org"], "language": ["en"]}}, scan], "out")
                py, cpp = run_pin("fx_scan_after_insert", ir)
                check(py == cpp and len(py) == len(py0) + 1, f"scan after insert: rows agree in order, one more than before ({len(py)} vs {len(cpp)})")
                check(order(py)[-1] == new == order(cpp)[-1] and py[-1].get("firstName") == "Zz_scan_fx" and order(py)[:-1] == order(py0),
                      "scan after insert: the inserted document is the run's last row, the loaded rows keep their order")
                ir = _plan("fx_scan_after_delete", [
                    {"name": "del", "op": "delete_vertex", "vid": pv, "label": "Person", "with_edges": True}, scan], "out")
                py, cpp = run_pin("fx_scan_after_delete", ir)
                check(py == cpp and len(py) == len(py0) - 1, f"scan after delete: rows agree in order, one fewer than before ({len(py)} vs {len(cpp)})")
                check(pv in order(py0) and pv not in order(py) and pv not in order(cpp), "scan after delete: the deleted vertex is in no row, on either side")
            if a.format == "pertype_csr":
                # delete_vertex of a chunk's FIRST owner whose in-neighbour's list sits in the same chunk: the write
                # rewrites that chunk twice and the second rewrite moves it to its new first owner's key. The earlier
                # put of the old key must not outlive the move (fixed in C++ by ChunkWrites.late_dels, in the Python
                # twin by _rmw's late deletes): both sides must leave the same lists, with the vertex in none.
                w = fmt._has_eprops("knows")
                pick, chunks = None, 0
                for key in sorted(k for k in base.backend._vals if isinstance(k, tuple)
                                  and k[:4] == ("t", "Person", "a", "knows") and k[4] in ("out", "in")):
                    groups = fmt._adj_groups(base.backend.get(key), w)[0]
                    chunks += 1
                    first = groups[0][0]
                    if any(first in g[1] for g in groups[1:]):          # an owner of this chunk lists its first owner
                        pick = (format(first, "016x"), [format(g[0], "016x") for g in groups[:40]], len(groups))
                        break
                check(pick is not None, f"first-owner delete: found a knows chunk whose first owner is in the list of "
                                        f"another owner of the same chunk ({chunks} chunks read, out and in; none = no cell)")
                if pick is not None:
                    gone, watch, owners = pick
                    ir = _plan("fx_del_first_owner", [
                        {"name": "del", "op": "delete_vertex", "vid": gone, "label": "Person", "with_edges": True},
                        {"name": "out", "op": "read_neighbors", "srcs": watch, "etype": "knows", "dir": "both"},
                    ], "out")
                    py, cpp = run_pin("fx_del_first_owner", ir)
                    check(_canon(py) == _canon(cpp), f"first-owner delete: the {len(watch)} first owners' knows lists agree after "
                                                     f"the delete ({len(py)} vs {len(cpp)} entries; the chunk held {owners} owners)")
                    check(gone not in _rows(py) and gone not in _rows(cpp),
                          "first-owner delete: the deleted vertex is in no list, on either side")
        if ERRS:
            print(f"\nFAIL: {len(ERRS)} cross-language write-effect check(s)")
            return 1
        print("\nCPP WRITE EFFECTS OK (rows agree; shared entries survive on both sides)")
        return 0

    # --- discover fixtures from the live store (read-only) ---------------------------------
    # a message with >= 2 likers, and two persons sharing a firstName
    raw = base.backend
    likers, msg = None, None
    if nebula_family:
        # nebula stores one key per edge: reverse likes keys (msg, "-likes", rank, liker)
        per_msg = {}
        for key in raw._vals:
            if isinstance(key, tuple) and len(key) == 4 and key[1] == "-likes":
                per_msg.setdefault(key[0], []).append(key[3])
                if len(per_msg[key[0]]) >= 3 and msg is None:
                    msg, likers = key[0], per_msg[key[0]][:3]
        assert msg is not None, "no message with >=3 likers found"
    else:
        for key in raw._vals:
            if isinstance(key, tuple) and len(key) == 3 and key[:2] == ("likes", "R"):
                entries = raw.get(key) or []
                if isinstance(entries, list) and len(entries) >= 3:
                    msg = key[2]
                    likers = [e[0] if isinstance(e, list) else e for e in entries[:3]]
                    break
        assert msg is not None, "no message with >=2 likers found"
    shared, tok = [], None
    if nebula_family:
        per_tok = {}
        for key in raw._vals:
            if isinstance(key, tuple) and len(key) == 5 and key[:3] == ("i", "Person", "firstName"):
                per_tok.setdefault(key[3], []).append(key[4])
                if len(per_tok[key[3]]) >= 3 and tok is None:
                    tok, shared = key[3], per_tok[key[3]][:3]
    else:
        for key in raw._vals:
            # the token key: predicate-global on canonical, per type (`Person.firstName`) under
            # a type-scoped index design (full_index)
            if isinstance(key, tuple) and len(key) == 3 and key[:2] in (("firstName", "I"), ("Person.firstName", "I")):
                uids = raw.get(key) or []
                if isinstance(uids, list) and len(uids) >= 3:
                    tok, shared = key[2], list(uids[:3])
                    break
    if tok is None and nebula_family:
        # index-free nebula schema: three persons via forward knows keys (v, "+knows", "0", d)
        first, first_nbrs = None, set()
        for key in raw._vals:
            if isinstance(key, tuple) and len(key) == 4 and key[1] == "+knows":
                if first is None:
                    first = key[0]
                    shared = [first, key[3]]
                if key[0] == first:
                    first_nbrs.add(key[3])
                elif key[0] not in first_nbrs and key[0] != first:
                    shared.append(key[0])
                    break
    elif tok is None:
        # index-free schema: no firstName tokens exist -- scenarios 3/4 still need three
        # persons. shared[2] gets INSERTED as a knows edge from shared[0] (the self-loop
        # scenario), so it must be a person NOT already adjacent to shared[0].
        first, first_nbrs = None, set()
        for key in raw._vals:
            if isinstance(key, tuple) and len(key) == 3 and key[:2] == ("knows", "D"):
                nbrs = [e[0] if isinstance(e, list) else e for e in (raw.get(key) or [])]
                if first is None and len(nbrs) >= 1:
                    first, first_nbrs = key[2], set(nbrs)
                    shared = [first, nbrs[0]]
                elif first is not None and key[2] != first and key[2] not in first_nbrs:
                    shared.append(key[2])
                    break
    assert len(shared) >= 3, "no shared token and no non-adjacent person pair found"

    def both_raw(name, ir, params=None):
        """(python_rows, cpp_rows) for one IR plan's output -- with the per-step OP COUNTS
        (S/G/N/P/D) checked equal along the way, so a scenario can never be rows-green
        while the two sides did different work (count-blindness)."""
        shadow.reset()
        outs, kv = run_ir(ir, fmt, store, params=params or {}, format_caps=caps,
                          caps_of_label=of_label, twin_counts="old-label-rule")
        resp = srv.run(ir, params or {})
        if resp.get("error"):
            raise SystemExit(f"cpp {name}: {resp['error']['where']}: {resp['error']['what']}")
        py_c = [(s.op, c.S, c.G, c.N, c.P, c.D) for s, _p, c in kv]
        cpp_c = [(s["op"], s["S"], s["G"], s["N"], s["P"], s["D"]) for s in resp["steps"]]
        check_counts(name, py_c, cpp_c)
        return outs[ir["output"]], resp["rows"]

    def both(name, ir, params=None):
        py, cpp = both_raw(name, ir, params)
        return _rows(py), _rows(cpp)

    with CppServer(store_dir("ldbc", a.sf, store_name), a.format, **srv_kw) as srv:
        # 1. delete one like -> other likers survive, identically on both sides
        gone, keep = likers[0], likers[1]
        ir = _plan("fx_del_edge", [
            {"name": "del", "op": "delete_edge", "etype": "likes", "src": gone, "dst": msg},
            {"name": "out", "op": "read_neighbors", "srcs": [msg], "etype": "likes", "dir": "in"},
        ], "out")
        py, cpp = both("fx_del_edge", ir)
        check(py == cpp, f"delete_edge: surviving likers agree ({len(py)} vs {len(cpp)})")
        check(str(keep) in py and str(gone) not in py,
              f"delete_edge: python keeps the co-tenant, drops the deleted ({keep} in, {gone} out)")

        # 2. indexed update -> the old token keeps its OTHER holders (both sides agree).
        # Needs the firstName index (absent on the label_only-based schemas).
        mover, stay = shared[0], shared[1]
        if ("Person", "firstName") in schema.indexed_cols:
            ir = _plan("fx_upd_indexed", [
                {"name": "upd", "op": "update_prop", "vid": mover, "label": "Person",
                 "prop": "firstName", "value": "Zz_cpp_fx"},
                {"name": "out", "op": "read_index_eq", "label": "Person",
                 "prop": "firstName", "value": tok},
            ], "out")
            py, cpp = both("fx_upd_indexed", ir)
            check(py == cpp, f"update_prop: old-token holders agree ({len(py)} vs {len(cpp)})")
            check(str(stay) in py and str(mover) not in py,
                  f"update_prop: co-holder survives, mover is out of the old token")
            # Same-value pin, ALL indexed families: the T2
            # order flip keeps the unchanged entry (nebula/arango real dels), and dgraph's
            # _merge now DEDUPS on append (a posting is a set) -- so on every family a
            # same-value update leaves the token holding BOTH persons exactly once.
            ir = _plan("fx_upd_same_value", [
            {"name": "upd", "op": "update_prop", "vid": mover, "label": "Person",
                 "prop": "firstName", "value": tok},
                {"name": "out", "op": "read_index_eq", "label": "Person",
                 "prop": "firstName", "value": tok},
            ], "out")
            py, cpp = both("fx_upd_same_value", ir)
            check(py == cpp, f"same-value update: token holders agree ({len(py)} vs {len(cpp)})")
            check(str(mover) in py and str(stay) in py,
                  "same-value update: the updated vertex AND its co-holder both stay indexed")

        # 2b. MULTI-column update: one row write carrying three columns; the old token keeps
        # its other holders, and every changed column reads back (a grouped schema puts
        # firstName + lastName in ONE group row, rewritten once)
        multi = {"firstName": "Zz_multi", "lastName": "Zz_ln", "browserUsed": "Zz_b"}
        if ("Person", "firstName") in schema.indexed_cols:
            ir = _plan("fx_upd_multi_token", [
                {"name": "upd", "op": "update_prop", "vid": mover, "label": "Person", "props": multi},
                {"name": "out", "op": "read_index_eq", "label": "Person",
                 "prop": "firstName", "value": tok},
            ], "out")
            py, cpp = both("fx_upd_multi_token", ir)
            check(py == cpp and str(stay) in py and str(mover) not in py,
                  f"multi update: old-token holders agree, the mover left it ({len(py)} vs {len(cpp)})")
        ir = _plan("fx_upd_multi_readback", [
            {"name": "upd", "op": "update_prop", "vid": mover, "label": "Person", "props": multi},
            {"name": "out", "op": "read_vertex_props", "vids": [mover], "props": sorted(multi),
             "label": "Person"},
        ], "out")
        py, cpp = both_raw("fx_upd_multi_readback", ir)
        check(py == cpp and all(py[0].get(k) == v for k, v in multi.items()),
              f"multi update: every changed column reads back, both sides ({py[0] if py else None})")

        # 2c. update_edge_prop of ONE like: the liker's copy on the message side AND on its own
        # side carry the new date; the co-likers keep theirs; a dated layout keeps its order
        # (SEQUENCE equality); an ABSENT like writes nothing and creates nothing
        new_date = 1300000000123
        ir = _plan("fx_upd_edge_like", [
            {"name": "upd", "op": "update_edge_prop", "etype": "likes", "src": gone, "dst": msg,
             "props": {"creationDate": new_date}},
            {"name": "out", "op": "read_neighbors", "srcs": [msg], "etype": "likes", "dir": "in",
             "edge_props": ["creationDate"]},
        ], "out")
        py, cpp = both_raw("fx_upd_edge_like", ir)
        dates = {str(r["dst"]): r.get("creationDate") for r in py}
        check(py == cpp, f"update_edge_prop: the message's likers agree in SEQUENCE ({len(py)} rows)")
        check(dates.get(str(gone)) == new_date and dates.get(str(keep)) not in (None, new_date),
              f"update_edge_prop: the updated like carries the new date, a co-liker keeps its own "
              f"({dates.get(str(gone))}, {dates.get(str(keep))})")
        ir = _plan("fx_upd_edge_like_fwd", [
            {"name": "upd", "op": "update_edge_prop", "etype": "likes", "src": gone, "dst": msg,
             "props": {"creationDate": new_date}},
            {"name": "out", "op": "read_neighbors", "srcs": [gone], "etype": "likes", "dir": "out",
             "edge_props": ["creationDate"]},
        ], "out")
        py, cpp = both_raw("fx_upd_edge_like_fwd", ir)
        fwd = {str(r["dst"]): r.get("creationDate") for r in py}
        check(py == cpp and fwd.get(str(msg)) == new_date,
              f"update_edge_prop: the liker's forward copy carries it too ({fwd.get(str(msg))})")
        pre = {str(x) for x in _rows(both("fx_likers_pre", _plan("fx_likers_pre", [
            {"name": "out", "op": "read_neighbors", "srcs": [msg], "etype": "likes", "dir": "in"}],
            "out"))[0])}
        absent = next(p for p in shared + likers if str(p) not in pre)
        ir = _plan("fx_upd_edge_absent", [
            {"name": "upd", "op": "update_edge_prop", "etype": "likes", "src": absent, "dst": msg,
             "props": {"creationDate": new_date}},
            {"name": "out", "op": "read_neighbors", "srcs": [msg], "etype": "likes", "dir": "in"},
        ], "out")
        py, cpp = both("fx_upd_edge_absent", ir)
        check(py == cpp and set(py) == pre, f"update_edge_prop of an absent edge: nothing created "
                                           f"({len(py)} likers, was {len(pre)})")

        # 3. delete a liker entirely -> the message's posting keeps the others
        ir = _plan("fx_del_vertex", [
            {"name": "del", "op": "delete_vertex", "vid": gone, "label": "Person",
             "with_edges": True},
            {"name": "out", "op": "read_neighbors", "srcs": [msg], "etype": "likes", "dir": "in"},
        ], "out")
        py, cpp = both("fx_del_vertex", ir)
        check(py == cpp, f"delete_vertex: surviving likers agree ({len(py)} vs {len(cpp)})")
        check(str(keep) in py and str(gone) not in py, "delete_vertex: co-tenant survives")

        # 4. self-loop: the "neighbour" key IS the own key -- the guarded branch (an entry-
        # removal put there would overwrite the own tombstone). Insert the loop + a real edge,
        # delete the vertex, read from the surviving friend's side: the deleted vertex must be
        # gone from BOTH directions, identically.
        p_self, p_friend = shared[0], shared[2]
        ir = _plan("fx_self_loop", [
            {"name": "l1", "op": "insert_edge", "etype": "knows", "src": p_self, "dst": p_self,
             "props": {}, "src_label": "Person", "dst_label": "Person"},
            {"name": "l2", "op": "insert_edge", "etype": "knows", "src": p_self, "dst": p_friend,
             "props": {}, "src_label": "Person", "dst_label": "Person"},
            {"name": "del", "op": "delete_vertex", "vid": p_self, "label": "Person",
             "with_edges": True},
            {"name": "out", "op": "read_neighbors", "srcs": [p_friend], "etype": "knows",
             "dir": "both"},
        ], "out")
        py, cpp = both("fx_self_loop", ir)
        check(py == cpp, f"self-loop delete: friend's view agrees ({len(py)} vs {len(cpp)})")
        check(str(p_self) not in py, "self-loop delete: the deleted vertex is gone from both dirs")
        # the deleted vertex's OWN postings are tombstones (the empty posting in the store's facet codec):
        # reading them back must decode as empty on both sides (a tomb written in the other codec throws)
        ir = _plan("fx_tomb_read", [
            {"name": "del", "op": "delete_vertex", "vid": p_friend, "label": "Person", "with_edges": True},
            {"name": "out", "op": "read_neighbors", "srcs": [p_friend], "etype": "knows", "dir": "both"},
        ], "out")
        py, cpp = both("fx_tomb_read", ir)
        check(py == cpp and not py, f"tomb read: a deleted vertex's own postings read back empty ({len(py)} vs {len(cpp)})")

        # 4b. a DATED insert: its creationDate entry must land in the band the range walk reads -- per type on a
        # type-scoped date design (writes.cc keys it by index_pred), the predicate-global band on canonical. The read is
        # the insert's own day on [Post], in the same plan (the shadow serves it). A design that declares no
        # Post.creationDate date index has no band to land in; the range read then returns 0 rows on both sides, so the
        # check is skipped, said out loud
        if not nebula_family and ("Post", "creationDate") not in schema.date_cols:
            print("  SKIP dated insert into the date index: this design declares no Post.creationDate date index")
        elif not nebula_family:
            from kvevaluator.schema.vids import vid as _vid
            new_post, day0 = _vid("Post", 1125899906842625), 1577836800000   # a fresh id; 2020-01-01, a day no message has
            ir = _plan("fx_ins_dated", [
                {"name": "ins", "op": "insert_vertex", "vid": new_post, "label": "Post",
                 "props": {"creationDate": day0 + 3600000, "content": "w", "length": 1}},
                {"name": "out", "op": "read_index_range", "labels": ["Post"], "prop": "creationDate",
                 "lo": day0, "hi": day0 + 86400000},
            ], "out")
            py, cpp = both("fx_ins_dated", ir)
            check(py == cpp and new_post in py, f"dated insert: its day's [Post] range holds it on both sides "
                                                f"({len(py)} vs {len(cpp)} rows)")

        # 5. shape CROSS-CHECK (secondary; DGRAPH family only: its removals are tombstone
        # REWRITES whose put bytes scale with degree -- nebula's removals are real DELETEs
        # with no put value, so every removal byte is 0 and the rank check is vacuous)
        # (exact value-byte equality is a NON-GOAL for the
        # realistic-codec formats -- different currencies): across the degree-stratified
        # params, cpp's removal write_value_bytes must RANK the params the same way python's
        # do (degree-shaped, not flat -- flat is the whole-tombstone signature).
        from kvevaluator.workloads.writes import params as w_params
        import json
        base_ir = None if nebula_family else json.load(open(os.path.join(
            str(ROOT), "python", "kvevaluator", "workloads", "writes", "microbench", "plans", "w_del_e.json")))
        py_b, cpp_b = [], []
        for args in (w_params.query_params(a.sf, "w_del_e", 5) if base_ir else []):
            shadow.reset()
            _o, kv = run_ir(base_ir, fmt, store, params=args, format_caps=caps,
                            caps_of_label=of_label, twin_counts="old-label-rule")
            py_b.append(sum(c.write_value_bytes for _s, _p, c in kv))
            resp = srv.run(base_ir, args)
            cpp_b.append(sum(s["bytes"]["write_value"] for s in resp["steps"]))
        # compare only STRICTLY-ordered python pairs (ties may legally reorder: the two
        # sides count different value currencies)
        if base_ir:
            agree = all((cpp_b[i] < cpp_b[j]) == (py_b[i] < py_b[j])
                        for i in range(len(py_b)) for j in range(len(py_b)) if py_b[i] != py_b[j])
            check(agree and len(set(cpp_b)) > 1,
                  f"shape: cpp removal bytes rank the degree strata like python "
                  f"(py {py_b} / cpp {cpp_b})")

    if ERRS:
        print(f"\nFAIL: {len(ERRS)} cross-language write-effect check(s)")
        return 1
    print("\nCPP WRITE EFFECTS OK (rows agree; shared entries survive on both sides)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
