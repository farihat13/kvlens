#!/usr/bin/env python3
"""grid_bodies stops a cell on a store or session error, not on a plan's result: infra_error on each stage the C++
session names (cli/session.cc: request, store, lsm_state stop the cell; batch, warmup_write, plan, run do not), a
timeout and an invalid body; measure_runs raises CellAbort on the first such run, with no row emitted, and on a crash
of the cell's first run only when the store does not open (store_opens; a plan's crash is a row). The live case (a held store, rc 1, no capture): tests/gates/meta/check_store_error_fails_cell.py.
Store-free: the runners here run nothing.

    python3 tests/unit/check_grid_bodies_store_errors.py
"""
import sys
import time

from kvevaluator.measure import grid_bodies as GV

SPEC = "inputs/specs/grid/props_split_adj_split.json"


class Scripted:
    """answers each call with the next (err, died) of its script; past the end, the last one"""

    def __init__(self, script):
        self.script, self.calls, self.died = script, 0, False

    def call(self, req):
        err, self.died = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return None, err


def run(script, opens=True):
    """measure_runs over is1-is3 at param 1 on the scripted runner, the store opening or not: (aborted with, runner
    calls, rows emitted)"""
    rows, runner = [], Scripted(script)
    GV.store_opens = lambda store: (opens, "" if opens else "planted")
    try:
        GV.measure_runs("props_split_adj_split", "interactive", "0.1", 1, 1, runner, None, None, SPEC, rows, [], [],
                        time.time(), slice_=(["is1", "is2", "is3"], [1]))
    except GV.CellAbort as e:
        return str(e), runner.calls, len(rows)
    return None, runner.calls, len(rows)


def main():
    fails, checked = [], 0

    def check(what, got, want):
        nonlocal checked
        checked += 1
        good = got == want
        print(f"{'PASS' if good else 'FAIL'} {what}: {got!r}{'' if good else f' (want {want!r})'}")
        if not good:
            fails.append(what)

    for where in ("request", "store", "lsm_state"):
        check(f"error ({where}) stops the cell", GV.infra_error(f"error ({where}): x"), True)
    for where in ("batch", "warmup_write", "plan", "run"):
        check(f"error ({where}) is a row", GV.infra_error(f"error ({where}): x"), False)
    check("a timeout is a row", GV.infra_error("timeout 1800s"), False)
    check("an invalid body is a row", GV.infra_error(GV.INVALID + "lacks validity caps ['x']"), False)
    check("a crash is a row by its text", GV.infra_error("serve died"), False)

    store_err = "error (store): open db: IO error: lock db/LOCK: Resource temporarily unavailable"
    check("measure_runs: a store error aborts at the first call, no row", run([(store_err, False)]), (store_err, 1, 0))
    aborted, calls, nrows = run([("serve died", True)], opens=False)
    check("measure_runs: a death on the first call, the store not opening: aborts",
          (aborted.startswith("serve died (on the cell's first run") if aborted else None, calls, nrows), (True, 1, 0))
    aborted, calls, nrows = run([("serve died", True), ("error (plan): x", False)], opens=True)
    check("measure_runs: a death on the first call, the store opening: a row (a plan's crash)",
          (aborted, calls > 1, nrows == calls), (None, True, True))
    aborted, calls, nrows = run([("error (plan): x", False), ("serve died", True)])
    check("measure_runs: a plan error, then a later death: no abort, every run a row",
          (aborted, calls > 1, nrows == calls), (None, True, True))
    aborted, calls, nrows = run([("error (run): x", False), ("error (run): x", False), (store_err, False)])
    check("measure_runs: a store error after other runs aborts there", (aborted, calls, nrows), (store_err, 3, 2))

    if fails:
        print(f"check_grid_bodies_store_errors: FAILED {len(fails)} of {checked}: {fails}")
        return 1
    print(f"check_grid_bodies_store_errors: OK ({checked} assertions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
