#!/usr/bin/env python3
"""The pick record and a query the record does not mention (candidates.decide): the query is picked at this run's
scale by the held-out rule, and the run says so once on stderr ("[picks] q: picked at this run's scale, not recorded");
a query whose BUNDLE has one body (LinkBench's ops) needs no pick and says nothing; a recorded query takes the record's
pick. Synthetic run rows through candidates.choose; prints what each case did.

    python3 tests/gates/meta/check_record_single_body.py        (run/meta-gates.sh runs it; exit 1 on any case failing)
"""
import contextlib
import io
import os
import sys
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvevaluator.plan_choice import candidates as C

LANES = C.READ_LANES   # the lanes a run row carries for the pick


def rows(q, variants, planner, params=(1, 2), fail=()):
    """run rows of one query: every variant on every param; `fail` = variants that did not run (rejected on this capture)"""
    out = []
    for v in variants:
        for p in params:
            ok = "0" if v in fail else "1"
            out.append({"query": q, "param_idx": str(p), "body": v, "planner_pick": "1" if v == planner else "0", "ok": ok,
                        "error": "boom" if ok == "0" else "", "result_hash": f"h{q}{p}", "S": "2", "G": "3", "N": "4",
                        **{k: "10" for k in LANES}, "measure_only": "0"})
    return out


def picked(name, q, rws, pinned, want, said):
    """choose() on the rows: the pick must be `want`, and the unrecorded line printed (said) or not"""
    C._UNRECORDED_SAID.clear()
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            out = C.choose(rws, pinned=pinned)
    except KeyError as e:
        print(f"  BAD {name}: raised ({str(e)[:70]})")
        return False
    v, rule = out[q]["body"], out[q]["rule"]
    told = f"[picks] {q}: picked at this run's scale" in err.getvalue()
    good = v == want and told == said
    print(f"  {'ok ' if good else 'BAD'} {name}: picked {v!r} by rule {rule!r} (want {want!r}); "
          f"{'said' if told else 'silent'} (want {'said' if said else 'silent'})")
    return good


def main():
    fails = 0
    # (1) an LDBC multi-body query absent from the record: picked here (the planner's body wins on equal scores), said
    fails += not picked("multi-body query missing from the record", "ic1", rows("ic1", ["a", "b"], "a"),
                        {"ic2": "a"}, "a", True)
    # (2) every body but one rejected on this capture: the one left is picked, and still said (the bundle has two)
    fails += not picked("multi-body query, one body rejected here, missing from the record", "ic1",
                        rows("ic1", ["a", "b"], "b", fail=("a",)), {"ic2": "a"}, "b", True)
    # (3) a single-body query absent from the record: its one body, and nothing said
    fails += not picked("single-body query missing from the record", "getnode", rows("getnode", ["default"], "default"),
                        {"ic1": "a"}, "default", False)
    # (4) the record binds a recorded multi-body query (the pick is the recorded variant), nothing said
    fails += not picked("recorded multi-body query", "ic1", rows("ic1", ["a", "b"], "a"), {"ic1": "b"}, "b", False)
    print(f"check_record_single_body: 4 cases, {fails} failing")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
