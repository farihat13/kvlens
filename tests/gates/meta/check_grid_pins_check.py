#!/usr/bin/env python3
"""grid_pick_record.py --check catches a wrong entry in every section of the pick record (owner: experiments/meta).

On a record written at the deciding scale and the captures it was written from, --check must pass, and each plant below
(one per check; each a copy of the record with one entry changed, passed with --file) must fail:
  1. a microbench decided entry swapped to another body                       (the in-sample section vs the chooser)
  2. a microbench decided entry removed                                       (a hole in a decided store)
  3. a carried BI entry's body sha changed                                    (a carried body still the one at HEAD)
  2b. a microbench decided store given an entry for a query it does not decide (a stale entry)
  4. a carried JOB entry swapped to another body that ran ok, not the selector's (the selector's body, planner_pick)
  5. a re-decided LDBC entry's recorded_before changed                        (--before: the previous record's anchor)
  5b. the same entry's recorded_before removed                               (a re-decision with no incumbent)
  6. the same record checked without --before                                 (a re-decided record's anchors not trusted)
  7. with --scales 0.1: nebula's sf0.1 held-out fingerprint changed           (a named store at a smaller scale), on a
     record whose sf0.1 fingerprints are first set to the sf0.1 capture's own (so only the plant moves it)

    python3 tests/gates/meta/check_grid_pins_check.py --before <previous record> -- <grid_pick_record.py's roots: --sf 3
        [--grid <captures/ldbc/sf3>] --family microbench=<its capture> --family bi=<its capture> --family job=...>
--record (default the tracked inputs/picks/picks.csv); --script runs another grid_pick_record.py (a control: the code
without the checks must fail this gate). Exit 0 = the record passes and every plant is caught: --check fails with a line naming the planted entry.
"""
import argparse
import copy
import os
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

import importlib.util

_spec = importlib.util.spec_from_file_location("grid_pins", os.path.join(str(ROOT), "python", "kvcost", "layout_cost", "grid_pick_record.py"))
grid_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grid_pins)
from kvevaluator.output_tree import Capture, Tree, capture_in


def run(script, roots, record, extra=()):
    """grid_pins --check on `record` (a writer's dict), written as the three files into a temp dir (pick_record.dump):
    (rc, the whole output's lines)"""
    from kvevaluator.plan_choice import pick_record
    with tempfile.TemporaryDirectory() as d:
        table = os.path.join(d, "picks.csv")
        pick_record.dump(record, table)
        got = subprocess.run([sys.executable, script, *roots, "--check", "--file", table, *extra],
                             capture_output=True, text=True, cwd=str(ROOT))
    return got.returncode, (got.stdout + got.stderr).strip().splitlines()


def caught(rc, out, *names):
    """a plant is caught when --check exits 1 AND one output line holds every name (the planted entry and the check's own
    words): not a crash, an argparse error or another check's failure"""
    return rc == 1 and any(all(n in line for n in names) for line in out)


def ran_not_selected(roots, wl, key, q, e):
    """another body of q that ran ok on the store's capture but is not the one the selector ran (planner_pick): (name, sha)"""
    import csv
    args = grid_pins_args(roots)
    for spec in args.workload or []:
        w, root = spec.split("=", 1)
        if w != wl:
            continue
        for k, stem in grid_pins.stores_in(root):
            if k != key:
                continue
            rows = [x for x in csv.DictReader(open(Capture(root).path_of("counts", stem))) if x["query"] == q]
            ok = {x["body_sha"] for x in rows if str(x["ok"]) == "1"}
            ok -= {x["body_sha"] for x in rows if str(x.get("planner_pick")) == "1"} | {e.get("body_sha")}
            from kvevaluator.plan_choice import candidates as C
            return next(((n, h) for n, h in sorted(C.candidate_bodies(wl, q).items()) if h in ok), (None, None))
    return None, None


def first(entries, test):
    for key, qs in sorted(entries.items()):
        for q, e in sorted(qs.items()):
            if isinstance(e, dict) and test(key, q, e):
                return key, q, e
    raise SystemExit("no entry to plant in: the record has none of this kind")


def other_body(wl, q, e):
    """another candidate of q at HEAD (name, sha), not the entry's"""
    from kvevaluator.plan_choice import candidates as C
    return next(((n, h) for n, h in sorted(C.candidate_bodies(wl, q).items()) if h != e.get("body_sha")), (None, None))


def main():
    from kvevaluator.plan_choice import pick_record
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--record", default=grid_capture_file())
    ap.add_argument("--before", required=True)
    ap.add_argument("--script", default=os.path.join(str(ROOT), "python", "kvcost", "layout_cost", "grid_pick_record.py"))
    ap.add_argument("roots", nargs=argparse.REMAINDER, help="-- then grid_pick_record.py's root flags")
    a = ap.parse_args()
    roots = a.roots[1:] if a.roots[:1] == ["--"] else a.roots
    rec = pick_record.load_full(a.record)
    before = ["--before", a.before]
    results = []

    rc, tail = run(a.script, roots, rec, before)
    results.append(("the record as written passes", rc == 0, tail))

    mb = rec["workloads"]["microbench"]
    decided = {k: v for k, v in mb["cells"].items() if mb["fingerprints"].get(k) is not None}
    key, q, e = first(decided, lambda k, q, e: other_body("microbench", q, e)[0] is not None)
    p = copy.deepcopy(rec); n, h = other_body("microbench", q, e)
    p["workloads"]["microbench"]["cells"][key][q].update(body=n, body_sha=h)
    rc, tail = run(a.script, roots, p, before)
    results.append((f"1. microbench {key} {q} -> {n}", caught(rc, tail, key, q), tail))

    p = copy.deepcopy(rec); del p["workloads"]["microbench"]["cells"][key][q]
    rc, tail = run(a.script, roots, p, before)
    results.append((f"2. microbench {key} {q} removed", caught(rc, tail, key, q), tail))

    key, q, e = first(rec["workloads"]["bi"]["cells"], lambda k, q, e: True)
    p = copy.deepcopy(rec); p["workloads"]["bi"]["cells"][key][q]["body_sha"] = "000000000000"
    rc, tail = run(a.script, roots, p, before)
    results.append((f"3. bi {key} {q} body sha changed", caught(rc, tail, key, q, "plan files moved under it"), tail))

    job = rec["workloads"]["job"]["cells"]
    key, q, e = first({k: v for k, v in job.items() if k.startswith("props_")},
                      lambda k, q, e: ran_not_selected(roots, "job", k, q, e)[0] is not None)
    p = copy.deepcopy(rec); n, h = ran_not_selected(roots, "job", key, q, e)
    p["workloads"]["job"]["cells"][key][q].update(body=n, body_sha=h)
    rc, tail = run(a.script, roots, p, before)
    results.append((f"4. job {key} {q} -> {n} (ran ok, not the selector's)", caught(rc, tail, key, q, "not the one the selector ran"), tail))

    kept = set(rec.get("by_reference", {}))
    key, q, e = first({k: v for k, v in rec["cells"].items() if k not in kept},
                      lambda k, q, e: e.get("recorded_before") is not None)
    p = copy.deepcopy(rec); p["cells"][key][q]["recorded_before"] = "not-the-previous-pick"
    rc, tail = run(a.script, roots, p, before)
    results.append((f"5. ldbc {key} {q} recorded_before changed", caught(rc, tail, key, q, "the previous record's anchor"), tail))

    p = copy.deepcopy(rec); del p["cells"][key][q]["recorded_before"]
    rc, tail = run(a.script, roots, p, before)
    results.append((f"5b. ldbc {key} {q} recorded_before removed", caught(rc, tail, key, q, "no recorded_before"), tail))

    key, q, e = first(decided, lambda k, q, e: True)
    p = copy.deepcopy(rec); p["workloads"]["microbench"]["cells"][key]["not_a_query"] = dict(e)
    rc, tail = run(a.script, roots, p, before)
    results.append((f"2b. microbench {key}: an entry for a query the capture does not decide",
                    caught(rc, tail, key, "not_a_query", "does not decide"), tail))

    rc, tail = run(a.script, roots, rec)
    results.append(("6. the re-decided record without --before", caught(rc, tail, "give --before"), tail))

    # 7: the sf0.1 fingerprints set to the sf0.1 capture's own, so they hold; then nebula's changed
    args = grid_pins_args(roots)
    cap = capture_in(args.grid or Tree().place_dir("captures", "ldbc", args.sf), "interactive").path
    p = copy.deepcopy(rec)
    held = {}
    for k, stem in grid_pins.stores_in(cap):
        hr = cap.replace(f"sf{args.sf}", "sf0.1")
        if os.path.exists(Capture(hr).held_out().record(stem)):
            held[k] = grid_pins.fingerprints(hr, stem)["held_out"]
    p["scale_fingerprints"] = {"0.1": held}
    rc, tail = run(a.script, roots, p, before + ["--scales", "0.1"])
    results.append((f"7a. sf0.1 fingerprints as captured ({len(held)} stores, nebula in: {'nebula' in held}) pass", rc == 0, tail))
    q7 = copy.deepcopy(p); q7["scale_fingerprints"]["0.1"]["nebula"] = dict(held.get("nebula") or {}, inputs="planted")
    rc, tail = run(a.script, roots, q7, before + ["--scales", "0.1"])
    results.append(("7b. nebula's sf0.1 fingerprint changed", "nebula" in held and caught(rc, tail, "nebula", "0.1"), tail))

    for what, ok, tail in results:
        print(f"{'PASS' if ok else 'FAIL'} {what}")
        if not ok:
            print("      " + "\n      ".join(tail[-3:]))
    good = all(ok for _w, ok, _t in results)
    print(f"check_grid_pins_check: {'OK' if good else 'FAILED'} ({sum(ok for _w, ok, _t in results)} of {len(results)} cases)")
    return 0 if good else 1


def grid_capture_file():
    from kvevaluator.measure import grid_capture
    return grid_capture.PICKS_FILE


def grid_pins_args(roots):
    """grid_pick_record.py's own parse of the root flags (the units a --check covers)"""
    import argparse as _a
    ap = _a.ArgumentParser()
    ap.add_argument("--sf"); ap.add_argument("--grid")
    ap.add_argument("--family", dest="workload", action="append", default=None)
    return ap.parse_known_args(roots)[0]


if __name__ == "__main__":
    sys.exit(main())
