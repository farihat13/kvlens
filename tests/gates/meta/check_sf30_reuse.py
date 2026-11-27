#!/usr/bin/env python3
"""An SF30 capture decides WHICH bodies run from the current SF3 capture on every run, and reuses each run already
measured by its own key (grid_bodies.prior_runs, row_run_key: body, params, CAPTURE_SHAPE; the record: store, counting versions). Live, at
sf0.1 with the rule's tag set to sf0.1 (no SF30 store needed), one non-diagonal cell, ic8 and is7 on param 1, against
synthetic SF3 rows (the pick at 100, a rival at 2x, a third body at 50x). The held-out capture (the reported
capture's held-out sibling):

  1. the first run measures the admitted bodies and writes not-run rows for a dropped one (at least one);
  2. a second run with the same SF3 rows runs nothing and writes the same counts / steps / parts / row-hashes files;
  3. the SF3 rows change so the dropped bodies are within T: only they run, the rest is reused;
  4. the SF3 rows change so a body that ran is over T: nothing runs, it gets not-run rows (at least one);
  5. another CAPTURE_SHAPE, or (5b) a prior record counted under other counting versions: every admitted run is
     measured again (none reused);
  6. another store (same_store fails; the prior capture at this capture shape): every admitted run is measured
     again;
  7. a measure-only body over T that an earlier run measured with the same key keeps its rows (0 runs);
then the reported capture (<root>):
  8. it takes the held-out capture's condition (ii) decisions (a planted one runs here);
  9. a held-out capture is not taken when it was priced from other SF3 rows (another path; other SF3 inputs), or
     measured at another capture shape or counting versions: (ii) is decided on the reported rows;
 10. a query the held-out capture did not measure decides its (ii) on the reported rows (the others still take it).

    python3 tests/gates/meta/check_sf30_reuse.py [--cell props_split_adj_condensed]
"""
import argparse
import csv
import json
import os
import shutil
import sys
import tempfile

from kvevaluator.measure import grid_bodies as GV
from kvevaluator.paths import OUTPUT_DIR
from kvevaluator.plan_choice import candidates as C
from kvevaluator.plan_choice import pick_record
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture, capture_in

SF, QUERIES, PARAMS = "0.1", ["ic8", "is7"], [1]
FILES = ("counts", "steps", "parts", "row-hashes")    # every file a measured capture writes per cell


class CountingRunner(GV.ServeRunner):
    """the real runner, counting the bodies it is asked to run"""
    called = []

    def call(self, req):
        CountingRunner.called.append(GV.body_key(req["plan"]))
        return super().call(req)


def sf3_rows(costs):
    """synthetic SF3 held-out rows: {query: {body: cost}} as the G count (the criterion's score is then the cost)"""
    out = []
    for q, by in costs.items():
        for body, cost in by.items():
            r = {"query": q, "param_idx": "11", "body": body, "ok": "1", "error": "", "S": "0", "G": str(cost),
                 "N": "0", "measure_only": "0"}
            r.update({k: "0" for k in C.READ_LANES})
            out.append(r)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", default="props_split_adj_condensed")
    cell = ap.parse_args().cell
    if cell in GV.DIAGONAL_SF30:
        raise SystemExit(f"{cell} is a diagonal cell (every body runs): pick another")
    spec = os.path.join(GV.GRID, f"{cell}.json")
    caps, of_label = GV._store_caps(spec)
    invalid = GV.invalid_on_cell("interactive", spec)
    roles = {}                                     # query -> (pick, rival, far, {name: body key})
    for q in QUERIES:
        ir, cands = C.candidates("interactive", q)
        pick = pick_record.body_for("interactive", cell, q, ir, caps, of_label, "3")
        others = [n for n, _ in cands if n != pick and n not in invalid.get(q, {})]
        if len(others) < 2:
            raise SystemExit(f"precondition: {q} on {cell} needs three valid bodies, has {[pick] + others}")
        roles[q] = (pick, others[0], others[1], {n: GV.body_key(ci) for n, ci in cands})
    binary = GV.binary_path()
    inputs = GV.inputs_fingerprint("interactive", SF, 1, PARAMS)
    ok, covered = True, 0
    real = {"tag": GV.SF30_TAG, "ver": GV.CAPTURE_SHAPE, "same": GV.same_store, "mo": GV.measure_only_bodies}
    GV.RUNNERS["count"] = CountingRunner
    GV.SF30_TAG = SF
    tmp = tempfile.mkdtemp(dir=str(OUTPUT_DIR), prefix="check_sf30_reuse_")
    root, sf3 = capture_in(os.path.join(tmp, "sf30"), "interactive").path, os.path.join(tmp, "sf3")   # sf3: a tree
    held = Capture(root).held_out().path
    try:
        def run(costs, out=held):
            nonlocal covered
            d = GV.sf3_rows_dir("interactive", sf3)
            os.makedirs(d, exist_ok=True)
            rows = sf3_rows(costs)
            with open(os.path.join(d, f"{cell}.csv"), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
                w.writeheader(); w.writerows(rows)
            CountingRunner.called = []
            GV.measure_cell(cell, "interactive", SF, 1, 4, 600, binary, out, inputs, GV.GRID, "count", (QUERIES, PARAMS),
                            sf3_root=sf3)
            got = list(capture_layout.reader(open(Capture(out).path_of("counts", cell))))
            covered += len(got)
            return list(CountingRunner.called), got

        def check(what, good, detail=""):
            nonlocal ok
            ok &= bool(good)
            print(f"{'PASS' if good else 'FAIL'} {what}{': ' + detail if detail else ''}")

        def not_run(rows):
            return sorted((r["query"], r["body"]) for r in rows if r["error"].startswith(GV.NOT_RUN))

        def measured(rows):
            return sum(str(r["ok"]) == "1" and not r["measured_as"] for r in rows)

        def files(out):
            return {k: open(Capture(out).path_of(k, cell)).read() for k in FILES}

        base = {q: {p: 100, r: 200, f: 5000} for q, (p, r, f, _k) in roles.items()}
        called1, rows1 = run(base)
        nr1 = not_run(rows1)
        check("1 the first run measures the admitted bodies; a 50x body gets not-run rows",
              called1 and nr1 and set(nr1) <= {(q, f) for q, (_p, _r, f, _k) in roles.items()},
              f"{len(called1)} runs, not run {nr1}")
        files1 = files(held)

        called2, _rows2 = run(base)
        check("2 the same SF3 rows: nothing runs, the same files", not called2 and files(held) == files1,
              f"{len(called2)} runs")

        near = {q: {p: 100, r: 200, f: 300} for q, (p, r, f, _k) in roles.items()}
        called3, rows3 = run(near)
        want = {roles[q][3][f] for q, f in nr1}
        check("3 the dropped bodies are now within T: only they run",
              want and set(called3) == want and not not_run(rows3),
              f"{len(called3)} runs of {len(set(called3))} bodies (want {len(want)})")

        over = {q: {p: 100, r: 5000, f: 300} for q, (p, r, f, _k) in roles.items()}
        called4, rows4 = run(over)
        nr4 = not_run(rows4)
        check("4 a body that ran is now over T: nothing runs, it gets not-run rows",
              not called4 and nr4 and set(nr4) <= {(q, r) for q, (_p, r, _f, _k) in roles.items()},
              f"{len(called4)} runs, not run {nr4}")

        GV.CAPTURE_SHAPE = real["ver"] + 1000
        called5, rows5 = run(over)
        GV.CAPTURE_SHAPE = real["ver"]
        check("5 another CAPTURE_SHAPE: every admitted run measured again",
              called5 and len(called5) == measured(rows5), f"{len(called5)} runs, {measured(rows5)} measured rows")
        run(over)                                  # a prior capture at this shape again
        side5 = Capture(held).record(cell)
        rec5 = json.load(open(side5))
        rec5["counting"] = dict(rec5["counting"], format=rec5["counting"]["format"] + 1)   # counted under another rule
        json.dump(rec5, open(side5, "w"))
        called5b, rows5b = run(over)
        check("5b a prior record counted under another format version: every admitted run measured again",
              called5b and len(called5b) == measured(rows5b), f"{len(called5b)} runs, {measured(rows5b)} measured rows")
        run(over)                                  # a prior capture at this version again: 6 then differs by its store only
        called6r, _ = run(over)
        check("6 (setup) the prior capture is reusable at this version", not called6r, f"{len(called6r)} runs")
        GV.same_store = lambda *a, **k: False
        called6, rows6 = run(over)
        GV.same_store = real["same"]
        check("6 another store: every admitted run measured again",
              called6 and len(called6) == measured(rows6), f"{len(called6)} runs, {measured(rows6)} measured rows")

        mo_q, mo_body = nr4[0]                     # a body over T: measure-only from here on
        GV.measure_only_bodies = lambda w, q: ({mo_body} if q == mo_q else set()) | real["mo"](w, q)
        run(near)                                  # within T: measured
        called7, rows7 = run(over)                 # over T and seen: kept by its key
        GV.measure_only_bodies = real["mo"]
        check("7 a measure-only body over T measured before with the same key keeps its rows",
              not called7 and (mo_q, mo_body) not in not_run(rows7), f"{len(called7)} runs, not run {not_run(rows7)}")

        _c, rows_h = run(over)                     # the held-out capture as the rule leaves it, then plant a (ii)
        side = Capture(held).record(cell)
        rec = json.load(open(side))
        q8, b8 = not_run(rows_h)[0]
        rec["sf30_rule"]["admitted"][q8][b8] = {"ran": True, "why": "(ii) planted"}
        json.dump(rec, open(side, "w"))
        called8, rows8 = run(over, out=root)
        dec8 = json.load(open(Capture(root).record(cell)))["sf30_rule"]["close_decided_on"]
        check("8 the reported capture takes the held-out capture's (ii) decision",
              roles[q8][3][b8] in called8 and (q8, b8) not in not_run(rows8) and dec8.startswith("the held-out"),
              f"{len(called8)} runs, not run {not_run(rows8)}; (ii) decided on: {dec8}")
        def decided_with(change):
            planted = json.loads(json.dumps(rec))
            change(planted)
            json.dump(planted, open(side, "w"))
            run(over, out=root)
            json.dump(rec, open(side, "w"))
            return json.load(open(Capture(root).record(cell)))["sf30_rule"]
        for what, change in (("another SF3 path", lambda r: r["sf30_rule"].update(sf3_capture="/elsewhere.csv")),
                             ("other SF3 inputs", lambda r: r["sf30_rule"].update(sf3_inputs="other")),
                             ("another capture shape", lambda r: r.update(measurement_version=-1)),
                             ("other counting versions", lambda r: r.update(counting=dict(r["counting"], seam=-1)))):
            dec9 = decided_with(change)["close_decided_on"]
            check(f"9 a held-out capture on {what} is not taken", dec9.startswith("this capture"), dec9)
        rule10 = decided_with(lambda r: r["sf30_rule"]["admitted"].pop(q8))
        check("10 a query missing from the held-out capture decides (ii) on the reported rows",
              rule10["close_decided_on"].startswith("the held-out") and rule10["close_on_this_capture"] == [q8],
              f"decided on {rule10['close_decided_on']}; on this capture: {rule10['close_on_this_capture']}")
        if covered == 0:
            ok = False
        print(f"check_sf30_reuse: {'OK' if ok else 'FAILED'} (20 SF30 captures of {cell} sf{SF}, {covered} run rows, "
              f"{len(QUERIES)} queries)")
    finally:
        GV.SF30_TAG, GV.CAPTURE_SHAPE = real["tag"], real["ver"]
        GV.same_store, GV.measure_only_bodies = real["same"], real["mo"]
        GV.RUNNERS.pop("count", None)
        for out in (root, held):                   # this check's own journals and scratch
            shutil.rmtree(GV.journal_dir(out), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
