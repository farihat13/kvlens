#!/usr/bin/env python3
"""A cell's capture whose key moved takes its unchanged runs from its prior capture, at every scale, and a converter
brings an older capture to the same rows (grid_bodies.taken_from_prior, prior_runs, measure_runs, reused_record,
rows_offered_today; run/convert-paper-results.py step 15). Store-free: a planted prior capture of JOB's job3a on
Sp-Sp at sample1pct, a runner that records what it is asked to run, no binary.

The planted prior holds a row of every candidate of job3a, then:
  1. an unchanged valid body: taken, not run;
  2. a changed param row (its run_key made with other params): run;
  3. a changed body (its run_key made with another body sha): run;
  4. a body that is no candidate today: no row;
  5. a body invalid on the cell today (the plain scans): its invalid row, not run, not taken;
  6. a prior measured by a dirty binary, or marked binary_unclean, or holding runs a dirty binary measured: nothing
     taken, every valid body run; --fresh takes nothing;
  7. the record: reused_runs lists the binaries that measured the runs taken, across reuses, and the prior record's
     stale and adopted marks are carried;
  8. step 15 on a tree with the same prior: the removed body's rows gone (counts, steps), the plain scans' rows
     invalid and their steps gone, a composed record's sha of the rewritten file updated; a second run touches nothing.
  9. a capture written again with every run taken counts as reused in kvcost measure's tally (run_measure), and a
     record with runs taken is no cell's cost in run_costs.

    python3 tests/unit/check_prior_runs_reuse.py [--control reuse|clean-binary|step15]
--control: one rule removed; the check must fail on its cases: reuse (no reuse below SF30: case 1), clean-binary (a
prior of any binary taken: case 6), step15 (step 15 changes no row: case 8).
"""
import argparse
import csv
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from kvevaluator.measure import grid_bodies as GV
from kvevaluator.measure.grid_capture import invalid_on_cell
from kvevaluator.output_tree import Capture
from kvevaluator.paths import ROOT
from kvevaluator.plan_choice import candidates as C

W, Q, SF, CELL = "job", "job3a", "sample1pct", "props_split_adj_split"
SPEC = str(ROOT / "inputs" / "specs" / "grid" / f"{CELL}.json")
CHANGED_PARAM, CHANGED_BODY, GONE = "enter_mi_by_index", "enter_k_by_label_run", "enter_gone_by_index"
CLEAN = {"binary_git_sha": "a" * 40, "binary_dirty": False}
results = []


def case(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


class Runner:
    """records the body each request runs; answers one row"""
    def __init__(self):
        self.ran = []

    def call(self, req):
        self.ran.append(next(iter(req["plan"]["plans"])))
        return {"steps": [], "rows": [{"x": 1}], "provenance": {"body_sha": "run"}, "totals": {"wall_ns": 1}}, ""


def prior_rows():
    """the planted prior's counts, steps, parts and row-hash rows: every candidate of Q, the plants of the docstring"""
    _ir, cands = C.candidates(W, Q)
    p = {}                                                   # JOB's queries take no param
    counts, steps, parts, hashes = [], [], [], []
    for name, ir in cands + [(GONE, None)]:
        key = GV.body_key(ir) if ir is not None else json.dumps(["ffffffffffff", {"params": []}])
        sha, level = json.loads(key)
        if name == CHANGED_BODY:
            sha = "000000000000"
        rk = GV.row_run_key(json.dumps([sha, level], sort_keys=True), {"planted": 1} if name == CHANGED_PARAM else p)
        counts.append({"sf": SF, "cell": CELL, "query": Q, "param_idx": "1", "body": name, "measure_only": "0", "ok": "1",
                       "error": "", "S": "7", "G": "7", "N": "7", "read_bytes": "70", "rows": "1", "result_hash": "h",
                       "answer_key": "a", "body_sha": sha, "run_key": rk, "wall_ns": "5", "measured_as": ""})
        steps.append({"query": Q, "param_idx": "1", "body": name, "step_idx": "0", "step": "s", "op": "read_vertex_props"})
        parts.append({"query": Q, "param_idx": "1", "body": name, "step_idx": "0", "step": "s", "op": "read_vertex_props",
                      "part": "props"})
        hashes.append({"workload": W, "query": Q, "param_idx": "1", "body": name, "step_idx": "0", "step": "s",
                       "op": "read_vertex_props", "rows": "1", "rows_digest": "d", "rows_set_digest": "e"})
    return counts, steps, parts, hashes


def write_csv(path, cols, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="", extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def plant(cap_dir, record):
    counts, steps, parts, hashes = prior_rows()
    cap = Capture(cap_dir)
    write_csv(cap.path_of("counts", CELL), GV.RUN_COLS, counts)
    write_csv(cap.path_of("steps", CELL), GV.STEP_COLS, steps)
    write_csv(cap.path_of("parts", CELL), GV.PART_COLS, parts)
    write_csv(cap.path_of("row-hashes", CELL), GV.DIGEST_COLS, hashes)
    with open(cap.record(CELL), "w") as f:
        json.dump({"cell": CELL, "complete": True, "measurement_version": GV.CAPTURE_SHAPE, **record}, f)


def run(cap_dir, record, fresh=False):
    """(bodies run, rows emitted by body, tally) of one job3a cell over the planted prior"""
    plant(cap_dir, record)
    reuse, _prior, _rec = GV.taken_from_prior(cap_dir, CELL, W, SF, None, SPEC, None, fresh_start=fresh)
    runner, rows, tally = Runner(), [], {}
    GV.measure_runs(CELL, W, SF, 1, 1, runner, None, None, SPEC, rows, [], [], time.time(), slice_=([Q], None),
                    reuse=reuse, tally=tally)
    return runner.ran, {r["body"]: r for r in rows}, tally


def check_reuse(tmp):
    print("the below-SF30 reuse (taken_from_prior, measure_runs):")
    names = [n for n, _ in C.candidates(W, Q)[1]]
    invalid = set(invalid_on_cell(W, SPEC).get(Q, {}))
    valid = [n for n in names if n not in invalid]
    unchanged = [n for n in valid if n not in (CHANGED_PARAM, CHANGED_BODY)]
    cap_dir = os.path.join(tmp, "captures", "imdb-rows", SF, W, "reported")
    ran, rows, tally = run(cap_dir, {"binary": CLEAN})
    case(f"1 the {len(unchanged)} unchanged valid bodies are taken, not run",
         unchanged and not set(unchanged) & set(ran) and all(rows[n]["S"] == "7" for n in unchanged), f"ran {ran}")
    case("2 the changed param row is run", CHANGED_PARAM in ran, f"ran {ran}")
    case("3 the changed body is run", CHANGED_BODY in ran, f"ran {ran}")
    case("4 the body that is no candidate has no row", GONE not in rows and GONE not in ran)
    case(f"5 the {len(invalid)} bodies invalid now get invalid rows, neither run nor taken",
         invalid and not invalid & set(ran)
         and all(rows[n]["ok"] == 0 and rows[n]["error"].startswith(GV.INVALID) for n in invalid), f"ran {ran}")
    case("  the tally: 2 measured, the unchanged taken", tally.get("measured") == 2 and tally.get("reused") == len(unchanged),
         tally)
    for label, rec in (("a dirty binary", {"binary": {**CLEAN, "binary_dirty": True}}),
                       ("an unstamped binary", {"binary": {}}),
                       ("a binary_unclean mark", {"binary": CLEAN, "stale": [{"kind": "binary_unclean", "detail": "x"}]}),
                       ("runs a dirty binary measured, reused", {"binary": CLEAN, "reused_runs": {
                           "runs": 3, "measured": 0, "measured_by": [{**CLEAN, "binary_dirty": True}]}})):
        ran, _rows, _t = run(cap_dir, rec)
        case(f"6 a prior with {label}: nothing taken, every valid body run", sorted(ran) == sorted(valid), f"ran {ran}")
    ran, _rows, _t = run(cap_dir, {"binary": CLEAN}, fresh=True)
    case("6 --fresh: nothing taken", sorted(ran) == sorted(valid), f"ran {ran}")


def check_record():
    print("the record (reused_record):")
    other = {"binary_git_sha": "b" * 40, "binary_dirty": False}
    note = {"kind": "under_successor", "detail": "x", "at": "t"}
    took = {"kind": "adopted", "detail": "y", "rows": ["r1"], "at": "t"}
    rr, marks = GV.reused_record({"binary": CLEAN, "stale": [note], "adopted": [took]}, 5, 2)
    case("7 a prior that measured its runs: its binary listed, its marks carried",
         rr == {"runs": 5, "measured": 2, "measured_by": [CLEAN]} and marks == [note, took], (rr, marks))
    rr, _ = GV.reused_record({"binary": other, "reused_runs": {"runs": 4, "measured": 1, "measured_by": [CLEAN]}}, 5, 0)
    case("7 across two reuses: both binaries listed", rr["measured_by"] == [CLEAN, other], rr)
    rr, _ = GV.reused_record({"binary": other, "reused_runs": {"runs": 4, "measured": 0, "measured_by": [CLEAN]}}, 5, 0)
    case("7 a prior that measured nothing: only the binaries it took from", rr["measured_by"] == [CLEAN], rr)
    case("7 nothing taken: no reused_runs, no marks", GV.reused_record({"binary": CLEAN}, 0, 9) == (None, []))


def check_step15(tmp):
    print("converter step 15 (run/convert-paper-results.py):")
    spec = importlib.util.spec_from_file_location("convert_paper_results", ROOT / "run" / "convert-paper-results.py")
    conv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)
    tree = Path(tmp) / "tree"
    cap_dir = tree / "captures" / "imdb-rows" / SF / W / "reported"
    plant(str(cap_dir), {"binary": CLEAN})
    rel = f"captures/imdb-rows/{SF}/{W}/reported/counts/{CELL}.csv"
    comp = tree / "composed" / "imdb-rows" / SF / W / "reported" / "counts" / "props_split_adj_condensed.json"
    comp.parent.mkdir(parents=True)
    comp.write_text(json.dumps({"composed_from_sha": {rel: "old"}}, indent=1, sort_keys=True) + "\n")
    c = conv.Converter(tree, Path(tmp) / "aside", False)
    conv.bodies_offered(c)
    invalid = set(invalid_on_cell(W, SPEC).get(Q, {}))
    rows = {r["body"]: r for r in csv.DictReader(open(cap_dir / "counts" / f"{CELL}.csv"))}
    steps = {r["body"] for r in csv.DictReader(open(cap_dir / "steps" / f"{CELL}.csv"))}
    case("8 the removed body's rows are gone (counts, steps)", GONE not in rows and GONE not in steps)
    case("8 the plain scans' rows are invalid, their steps gone",
         invalid and all(rows[n]["ok"] == "0" and rows[n]["error"].startswith(GV.INVALID) for n in invalid)
         and not invalid & steps)
    case("8 every other row as it was", all(r["ok"] == "1" and r["S"] == "7" for n, r in rows.items() if n not in invalid))
    sha = hashlib.sha256((cap_dir / "counts" / f"{CELL}.csv").read_bytes()).hexdigest()
    case("8 the composed record's sha of the rewritten counts is the new file's",
         json.loads(comp.read_text())["composed_from_sha"][rel] == sha)
    c = conv.Converter(tree, Path(tmp) / "aside", False)
    conv.bodies_offered(c)
    case("8 a second run touches nothing", c.touched == 0, c.touched)


def check_readers(tmp):
    print("the readers (run_measure's tally, run_costs):")
    import subprocess
    from kvevaluator.measure import run_measure as M
    from kvevaluator.output_tree import Tree
    out = os.path.join(tmp, "out")
    call = ("reads job Sp-Sp", [sys.executable, "-m", "kvevaluator.measure.grid_bodies", "--sf", SF, "--family", W,
                                "--cells", CELL, "--root", out])
    rec_path = Tree(out).capture(W, SF).record(CELL)
    os.makedirs(os.path.dirname(rec_path), exist_ok=True)
    got = {}
    for label, took in (("every run taken", {"runs": 9, "measured": 0}), ("2 measured", {"runs": 7, "measured": 2}),
                        ("no reused_runs", None)):
        with open(rec_path, "w") as f:
            json.dump({"cell": CELL, "sf": SF, "workload": W, "nparam": 1, "wall_s": 5.0,
                       **({"reused_runs": took} if took else {})}, f)
        got[label] = M._nothing_measured(call, CELL)
    case("9 kvcost measure's tally: a capture written again with every run taken is reused, else measured",
         got == {"every run taken": True, "2 measured": False, "no reused_runs": False}, got)
    cap = Tree(out).capture(W, SF)
    for cell, took, wall in ((CELL, {"runs": 9, "measured": 0}, 5.0), ("props_condensed_adj_condensed", None, 100.0)):
        with open(cap.record(cell), "w") as f:
            json.dump({"cell": cell, "sf": SF, "workload": W, "nparam": 1, "wall_s": wall,
                       **({"reused_runs": took} if took else {})}, f)
    res = subprocess.run([sys.executable, "-m", "kvevaluator.measure.run_costs"], capture_output=True, text=True,
                         env=dict(os.environ, KVEVAL_OUTPUT_DIR=out))
    line = next((x for x in res.stdout.splitlines() if f"{SF} {W}" in x), "")
    case("9 run_costs: the record with runs taken is no cell's cost (n=1, the measured one's 100 s)",
         res.returncode == 0 and "n=  1" in line and "100 s" in line, line or res.stdout[-300:] + res.stderr[-300:])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--control", choices=("reuse", "clean-binary", "step15"), help="one rule removed: must fail")
    a = ap.parse_args()
    GV.same_store = lambda *x, **k: True                   # the store's identity is not what this checks
    GV.counted_fresh = lambda *x, **k: True
    if a.control == "reuse":
        orig = GV.taken_from_prior
        GV.taken_from_prior = lambda out_dir, cell, workload, sf, *x, **k: (
            orig(out_dir, cell, workload, sf, *x, **k) if str(sf) == GV.SF30_TAG else ({}, [], None))
    elif a.control == "clean-binary":
        GV.clean_binary = lambda rec: True
    elif a.control == "step15":
        GV.rows_offered_today = lambda workload, spec_path, rows: (list(rows), {})
    if a.control:
        print(f"CONTROL {a.control}: that rule removed")
    with tempfile.TemporaryDirectory(prefix="prior-runs-") as tmp:
        check_reuse(tmp)
        check_record()
        check_step15(tmp)
        check_readers(tmp)
    bad = results.count(False)
    print(f"check_prior_runs_reuse {'OK' if not bad else 'FAIL'}: {len(results)} cases, {bad} failed"
          + (" (the control: it must fail)" if a.control else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
