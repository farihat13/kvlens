#!/usr/bin/env python3
"""The SF30 rule (measure/grid_bodies.py sf30_admission, close_at_sf30) on synthetic SF3 / SF30 rows: no store.

  1. a named store: the SF3 pick runs, a body within T = 10x of it runs, one over T is dropped with its ratio, a body
     with no SF3 row runs (new), an SF3 cost of 0 divides by nothing;
  2. a measure-only body runs the first time an SF30 capture sees it; after that the 10x rule applies (a not-run row
     counts as seen);
  3. the diagonal is the 7 cells the composed layouts read; a diagonal cell runs every body;
  4. condition (ii): a rival within 10% of the pick on SF30's rows gives the reason to run the rest; one 2x dearer
     does not, nor a measure-only one; a pick with no ok run at SF30 does;
  5. Choose reads a not-run body as unmeasured: not priced, not rejected;
  6. LDBC's two SF30 captures find each other's paths (sibling_capture; what the reported one takes from the held-out
     one is tested live in tests/gates/meta/check_sf30_reuse.py).

    python3 tests/unit/check_sf30_rule.py
"""
import sys

from kvevaluator.measure import grid_bodies as GV
from kvevaluator.capture.spec_sets import format_spec
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plan_choice import pick_record

W, Q, STORE = "interactive", "ic8", "dgraph"


def row(variant, cost, ok=1, error="", q=Q, idx=1):
    """a run row whose wops_bytes4k score is `cost` (G only)"""
    r = {"query": q, "param_idx": str(idx), "body": variant, "ok": str(ok), "error": error,
         "S": "0", "G": str(cost), "N": "0", "measure_only": "0", "result_hash": "h",
         "answer_key": "a"}
    r.update({k: "0" for k in C.READ_LANES})
    return r


def main():
    bad = 0

    def check(what, got, want):
        nonlocal bad
        ok = got == want
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}: {got!r}" + ("" if ok else f" (want {want!r})"))

    spec = format_spec(STORE)
    caps, of_label = GV._store_caps(spec)
    ir, cands = C.candidates(W, Q)
    names = [n for n, _ in cands]
    pick = pick_record.body_for(W, STORE, Q, ir, caps, of_label, "3")
    invalid = GV.invalid_on_cell(W, spec).get(Q, {})
    others = [n for n in names if n != pick and n not in invalid]
    if len(others) < 2:
        print(f"FAIL precondition: {Q} on {STORE} needs two valid bodies besides the pick ({names})")
        return 1
    near, far = others[0], others[1]
    sf3 = [row(pick, 100), row(near, 900), row(far, 1500)]
    adm = GV.sf30_admission(W, STORE, spec, [Q], sf3, [])[Q]
    check("1a the SF3 pick runs", adm[pick][0], True)
    check("1b a body at 9x the pick runs", adm[near][0], True)
    check("1c a body at 15x the pick is dropped", (adm[far][0], "15.00x" in adm[far][1]), (False, True))
    adm_new = GV.sf30_admission(W, STORE, spec, [Q], [row(pick, 100), row(near, 900)], [])[Q]
    check("1d a body with no SF3 row runs (new)", (adm_new[far][0], adm_new[far][1].startswith("new")), (True, True))

    adm_zero = GV.sf30_admission(W, STORE, spec, [Q], [row(pick, 0), row(near, 0), row(far, 5)], [])[Q]
    check("1f a pick with SF3 cost 0: no division error; equal cost runs, dearer is dropped",
          (adm_zero[near][0], adm_zero[far][0]), (True, False))

    real = GV.measure_only_bodies
    GV.measure_only_bodies = lambda w, q: {far}                    # `far` is measure-only for this check
    try:
        first = GV.sf30_admission(W, STORE, spec, [Q], sf3, [])[Q][far]
        later = GV.sf30_admission(W, STORE, spec, [Q], sf3, [row(far, 1500)])[Q][far]
        after_drop = GV.sf30_admission(W, STORE, spec, [Q], sf3, [row(far, 0, ok=0, error=GV.NOT_RUN + "15.00x")])[Q][far]
    finally:
        GV.measure_only_bodies = real
    check("2a a measure-only body runs the first time", first[0], True)
    check("2b ... then the 10x rule applies (dropped at 15x)", later[0], False)
    check("2c ... and a capture that only holds its not-run rows has seen it (no flip back)", after_drop[0], False)

    check("3a the diagonal is Split, Condensed and Chunked at the composed layouts' chunk sizes",
          sorted(GV.DIAGONAL_SF30), sorted(["props_split_adj_split", "props_condensed_adj_condensed",
                                            "props_chunked_adj_chunked", "props_chunked_c1k_adj_chunked_c1k",
                                            "props_chunked_c4k_adj_chunked_c4k", "props_chunked_c16k_adj_chunked_c16k",
                                            "props_chunked_c256k_adj_chunked_c256k"]))
    from kvcost.layout_cost import settings as K
    check("3b ... the sizes the composed layouts price (kvcost settings)", K.CHUNK_SIZES_KB, GV.grid_capture.CHUNK_SIZES_KB)
    diag = GV.sf30_admission(W, "props_split_adj_split", f"{GV.GRID}/props_split_adj_split.json", [Q], sf3, [])[Q]
    check("3 a diagonal cell runs every body", all(r for r, _w in diag.values()), True)

    check("4a a rival within 10% at SF30: run the rest",
          GV.close_at_sf30([row(pick, 100), row(near, 105)], Q, adm) is not None, True)
    check("4b a rival 2x dearer at SF30: no", GV.close_at_sf30([row(pick, 100), row(near, 200)], Q, adm), None)

    check("4c the pick has no ok run at SF30: run the rest",
          GV.close_at_sf30([row(pick, 0, ok=0, error="timeout"), row(near, 200)], Q, adm) is not None, True)
    check("4d a measure-only rival within 10% does not count",
          GV.close_at_sf30([row(pick, 100), row(near, 105)], Q, adm, mo={near}), None)
    check("6 the sibling capture: <workload>/reported <-> <workload>/held-out",
          [GV.sibling_capture(d) for d in ("/r/interactive/reported", "/r/interactive/held-out/")],
          ["/r/interactive/held-out", "/r/interactive/reported"])

    rs = [row(pick, 100), row(near, 105), row(far, 0, ok=0, error=GV.NOT_RUN + "15.00x")]
    for r in rs:
        r["measure_only"] = "0"
    _planner, _params, _by_v, costs, rejected, *_ = C._valid_vectors(rs)
    check("5 Choose: the not-run body is neither priced nor rejected", (far in costs, far in rejected), (False, False))
    print(f"check_sf30_rule: {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
