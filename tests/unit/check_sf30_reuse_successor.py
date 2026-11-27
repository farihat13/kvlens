#!/usr/bin/env python3
"""grid_bodies.measure_runs keeps a row of a body rewritten count-equal since (the successor table: ic5's list-join
twin, 550c76caff32) instead of running it, and lists it (under_successor) whichever way the row came: reused from a
prior SF30 capture, taken from the cell's journal after a restart, or copied to a second name of the same body; the
cell's record is marked under_successor (kvevaluator.stale). The control, a row of a sha outside the table, is run and
not listed. Store-free: the runner here runs nothing.

    python3 tests/unit/check_sf30_reuse_successor.py
"""
import json
import sys
import time
from kvevaluator import stale
from kvevaluator.measure import grid_bodies as GV
from kvevaluator.measure.grid_capture import query_params
from kvevaluator.plan_choice import candidates as C

W, Q, NAME, OLD = "interactive", "ic5", "ic5_listjoin(ic5)", "550c76caff32"
COPY = NAME + "#second-name"     # the same body under another name: measure_runs copies its row
SPEC = "inputs/specs/grid/props_split_adj_split.json"


class NoRun:
    called = 0

    def call(self, req):
        NoRun.called += 1
        return None, "not run here"


class Journal:
    """a cell's journal after a restart (grid_bodies.Journal): the rows it holds, nothing added"""
    def __init__(self, loaded):
        self.loaded = loaded

    def add(self, *a):
        pass

    def note_wall(self, s):
        pass


def prior(sha):
    ir, cands = C.candidates(W, Q)
    key = json.loads(GV.body_key(dict(cands)[NAME]))
    p = query_params(W, "0.1", Q, 1)[0]
    rk = GV.row_run_key(json.dumps([sha, key[1]], sort_keys=True), p)
    row = {"query": Q, "param_idx": "1", "body": NAME, "body_sha": sha, "run_key": rk, "ok": "1", "error": "",
           "measured_as": "", "S": "1", "G": "2", "N": "3", "result_hash": "h", "rows": "1", **{k: "0" for k in C.READ_LANES}}
    return {(Q, "1", NAME): (row, [{"query": Q, "body": NAME, "step_idx": 0}], [])}


def run(sha, how, copy=False):
    """(runner calls, the rows of the twin's names, the listed rows, the cell record's stale kinds) of one ic5 cell
    at p1"""
    NoRun.called = 0
    run_rows, under = [], []
    real = GV.candidates
    if copy:   # a second name for the twin's body
        GV.candidates = lambda w, q: (lambda ir, c: (ir, c + [(COPY, dict(c)[NAME])]))(*real(w, q))
    try:
        ir, cands = GV.candidates(W, Q)
        sf30 = {Q: {n: (n in (NAME, COPY), "test") for n, _ in cands}}
        kw = {"reuse": prior(sha) if how == "reused" else {},
              "journal": Journal(prior(sha)) if how == "journal" else None}
        with stale.scope():             # as measure_cell does: the cell's record folds its notes
            GV.measure_runs("props_split_adj_split", W, "0.1", 1, 1, NoRun(), None, None, SPEC, run_rows, [], [],
                            time.time(), slice_=([Q], [1]), sf30=sf30, under_successor=under, **kw)
            marked = sorted({n["kind"] for n in stale.fold({}).get("stale", [])})
    finally:
        GV.candidates = real
    return NoRun.called, [r for r in run_rows if r["body"] in (NAME, COPY)], under, marked


def main():
    fails = 0
    want = lambda *names: [{"query": Q, "param_idx": 1, "body": n, "old_body_sha": OLD} for n in names]
    cases = [("1 reused from a prior SF30 capture: taken, listed", OLD, "reused", False, want(NAME)),
             ("2 taken from the journal after a restart: listed", OLD, "journal", False, want(NAME)),
             ("3 copied to a second name of the body: both listed", OLD, "reused", True, want(NAME, COPY)),
             ("4 the control, a sha outside the table: run, not listed", "000000000000", "reused", False, [])]
    for label, sha, how, copy, listed in cases:
        calls, rows, under, marked = run(sha, how, copy)
        taken = calls == 0 and rows and all(r.get("body_sha") == sha for r in rows)
        ok = under == listed and bool(taken) == bool(listed) and marked == (["under_successor"] if listed else [])
        print(f"{'PASS' if ok else 'FAIL'} {label} (runner calls {calls}; listed {[u['body'] for u in under]}; "
              f"record marked {marked})")
        fails += not ok
    print(f"check_sf30_reuse_successor: {len(cases)} cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
