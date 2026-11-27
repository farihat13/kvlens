#!/usr/bin/env python3
"""A pick made from a moved input is made and marked, not refused (kvevaluator.stale), on a synthetic capture:

  1. picks.choose_cell on an LDBC capture with no held-out set and one of ic5's two candidates: ic5 is picked, marked
     pick_in_sample and candidates_unmeasured (neither refused);
  2. the control: both candidates and the in-sample rule: picked, no mark;
  3. grid_capture.check_record_bodies: a clean entry is not marked; a moved sha is body_moved; an old sha the successor
     table maps to today's is under_successor; an entry with no sha is body_moved ("no sha"); a body name HEAD no longer
     has is still refused;
  4. drifted_record_bodies (the strict form a gate reads) notes nothing;
  5. pick_record.body_for: a recorded body whose sha moved runs today's body of that name, marked body_moved;
  6. experiments/meta/check_candidate_drift.py (the layout-choice driver's step 0) exits 3 on a capture that never ran
     a candidate (the driver goes on, marked) and 0 on one that ran them all;
  7. rows with no measure_only column (a capture from before the flag): read as 0 and marked measure_only_unknown for
     a query with no measure-only body at HEAD (is1); still refused for one that has one (ic5);
  8. the logical census (experiments/logical_census/census.py head_names): a captured body whose sha moved is
     labelled with that sha and marked body_moved; today's sha is not; a body matching no HEAD candidate is refused.

    python3 tests/unit/check_choose_cell_marks.py
"""
import contextlib
import csv
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator import stale
from kvevaluator.paths import ROOT
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import Capture
from kvevaluator.plan_choice import candidates as C, pick_record
from kvcost.layout_cost import picks

CELL, Q = "props_split_adj_split", "ic5"
FIELDS = ["sf", "cell", "query", "param_idx", "body", "measure_only", "ok", "error", "S", "G", "N", *C.READ_LANES,
          "read_bytes", "rows", "result_hash", "answer_key", "body_sha", "run_key", "wall_ns", "measured_as"]
failed = []


def case(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
    if not ok:
        failed.append(name)


def fixture(root, bodies):
    """one interactive cell at sf0.1, ic5 at two params, run by `bodies` only; no held-out set"""
    out = os.path.join(root, "ldbc", "sf0.1", "captures", "grid", "interactive", "reported")
    cap, shas = Capture(out), C.candidate_bodies("interactive", Q)
    os.makedirs(os.path.dirname(cap.path_of("counts", CELL)), exist_ok=True)
    with open(cap.path_of("counts", CELL), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", restval="0")
        w.writeheader()
        for i, body in [(i, b) for i in (1, 2) for b in bodies]:
            w.writerow({"sf": "0.1", "cell": CELL, "query": Q, "param_idx": i, "body": body, "ok": 1, "error": "",
                        "S": 10 + i, "G": 5, "N": 100, "read_bytes": 4096, "rows": 3, "result_hash": f"h{i}",
                        "answer_key": f"a{i}", "body_sha": shas[body], "run_key": "", "wall_ns": "", "measured_as": ""})
    head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    with open(cap.record(CELL), "w") as f:
        json.dump({"cell": CELL, "sf": "0.1", "workload": "interactive", "nparam": 2, "plan_pipeline": C.PLAN_PIPELINE,
                   "tree": {"git_sha": head, "git_dirty": False}}, f)
    return out


def main():
    with tempfile.TemporaryDirectory() as tmp:
        with stale.scope() as notes:
            try:
                d = picks.choose_cell(fixture(os.path.join(tmp, "one"), ["ic5"]), CELL, rule=picks.PlanRule(record=None))[Q]
                case("1 no held-out set, a candidate never run: picked and marked", d["body"] == "ic5" and
                     d.get("stale") == ["candidates_unmeasured", "pick_in_sample"], f"{d['body']} {d.get('stale')}")
            except (Exception, SystemExit) as e:
                case("1 no held-out set, a candidate never run: picked and marked", False, f"{type(e).__name__}: {e}")
        case("1b the cell's notes", sorted({n["kind"] for n in notes}) == ["candidates_unmeasured", "pick_in_sample"],
             str([n["kind"] for n in notes]))
        d = picks.choose_cell(fixture(os.path.join(tmp, "both"), ["ic5", "ic5_listjoin(ic5)"]), CELL,
                              rule=picks.PlanRule(record=None, in_sample=True))[Q]
        case("2 the control: every candidate, in-sample by the rule: no mark", "stale" not in d, str(d.get("stale")))
        spec = importlib.util.spec_from_file_location("ccd", ROOT / "experiments" / "meta" / "check_candidate_drift.py")
        ccd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ccd)
        rcs = []
        for run in ("one", "both"):
            with contextlib.redirect_stdout(io.StringIO()):
                rcs.append(ccd.main(["--sf", "0.1", "--grid", os.path.join(tmp, run, "ldbc", "sf0.1", "captures", "grid"),
                                     "--families", "interactive"]))
        case("6 the drift check: a candidate never run exits 3, none exits 0", rcs == [3, 0], str(rcs))
    bi3 = C.candidate_bodies("bi", "bi3")
    with stale.scope() as notes:
        ent = {"bi3": {"body": "hand~a4eca1", "body_sha": bi3["hand~a4eca1"]}}
        clean = grid_capture.check_record_bodies("cellX", ent, "bi")
        moved = grid_capture.check_record_bodies("cellX", {"bi3": {"body": "hand~a4eca1", "body_sha": "deadbeef0000"}}, "bi")
        succ = grid_capture.check_record_bodies("cellX", {"bi3": {"body": "hand~a4eca1", "body_sha": "2bb1f0ebfdd8"}}, "bi")
        nosha = grid_capture.check_record_bodies("cellX", {"bi3": {"body": "hand~a4eca1"}}, "bi")
        try:
            grid_capture.check_record_bodies("cellX", {"bi3": {"body": "no-such-body", "body_sha": "deadbeef0000"}}, "bi")
            gone = "taken"
        except SystemExit:
            gone = "refused"
    case("3 the record's bodies: clean, moved, successor, no sha, a gone name",
         (clean, moved, succ, nosha, gone) == ({}, {"bi3": "body_moved"}, {"bi3": "under_successor"},
                                               {"bi3": "body_moved"}, "refused") and len(notes) == 3,
         f"{clean} {moved} {succ} {nosha} {gone}, {len(notes)} notes")
    with stale.scope() as notes:
        strict = grid_capture.drifted_record_bodies({"bi3": {"body": "hand~a4eca1", "body_sha": "deadbeef0000"}}, "bi")
    case("4 the strict form returns the drift and notes nothing", list(strict) == ["bi3"] and notes == [], str(strict))
    ir, cands = C.candidates("interactive", Q)
    real_entry, real_legal = pick_record.entry, pick_record.legal_bodies
    pick_record.entry = lambda *a, **k: {"body": "ic5", "body_sha": "deadbeef0000"}
    pick_record.legal_bodies = lambda *a, **k: ["ic5", "ic5_listjoin(ic5)"]
    try:
        with stale.scope() as notes:
            got = pick_record.body_for("interactive", CELL, Q, ir, set(), lambda label: set())
        case("5 body_for: a moved body runs today's, marked", got == "ic5" and [n["kind"] for n in notes] == ["body_moved"],
             f"{got} {[n['kind'] for n in notes]}")
    except (Exception, SystemExit) as e:
        case("5 body_for: a moved body runs today's, marked", False, f"{type(e).__name__}: {e}")
    finally:
        pick_record.entry, pick_record.legal_bodies = real_entry, real_legal
    def rows(q, body):
        return [{"query": q, "param_idx": str(i), "body": body, "ok": "1", "error": "", "answer_key": f"a{i}",
                 "result_hash": f"h{i}", **dict.fromkeys(C.READ_LANES, "1"), "S": "1", "G": "1", "N": "1",
                 "read_bytes": "10"} for i in (1, 2)]
    with stale.scope() as notes:
        try:
            C._valid_vectors(rows("is1", "is1"))
            is1 = "read as 0"
        except KeyError:
            is1 = "refused"
    try:
        C._valid_vectors(rows("ic5", "ic5"))
        ic5 = "read as 0"
    except KeyError:
        ic5 = "refused"
    case("7 no measure_only column: is1 read as 0 and marked, ic5 refused",
         (is1, ic5, [n["kind"] for n in notes]) == ("read as 0", "refused", ["measure_only_unknown"]),
         f"{is1} {ic5} {[n['kind'] for n in notes]}")
    spec = importlib.util.spec_from_file_location("census", ROOT / "experiments" / "logical_census" / "census.py")
    census = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(census)
    with tempfile.TemporaryDirectory() as tmp:
        def capture(body, sha):
            cap = Capture(tmp)
            os.makedirs(os.path.dirname(cap.path_of("counts", CELL)), exist_ok=True)
            with open(cap.path_of("counts", CELL), "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n", restval="0")
                w.writeheader()
                w.writerow({"query": "bi3", "param_idx": "1", "body": body, "ok": "1", "body_sha": sha})
        capture("hand~a4eca1", "deadbeef0000")
        with stale.scope() as notes:
            names, labels = census.head_names("bi", {CELL: tmp})
        capture("hand~a4eca1", bi3["hand~a4eca1"])
        clean = census.head_names("bi", {CELL: tmp})
        capture("gone-body", "deadbeef0000")
        try:
            census.head_names("bi", {CELL: tmp})
            gone = "taken"
        except SystemExit:
            gone = "refused"
    key = ("bi3", "hand~a4eca1")
    case("8 the census: a moved body labelled and marked, today's clean, a gone one refused",
         names[key] == "hand~a4eca1" and "deadbeef0000" in labels.get(key, "") and [n["kind"] for n in notes] ==
         ["body_moved"] and clean == ({key: "hand~a4eca1"}, {}) and gone == "refused",
         f"{labels} {clean} {gone}")
    print(f"check_choose_cell_marks: 10 cases, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
