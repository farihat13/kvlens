#!/usr/bin/env python3
"""candidates.captured_bodies: a run row's own body_sha wins over its name resolved at the capture's commit.

A composed capture keeps its base's record (and so its base's tree) while some of its rows ran at a newer tree; a body
whose sha changed with no count moved (bi10's by-id read named its label) is then measured at HEAD, and only
the rows say so. Four cases on HEAD's own BI plan files (no capture read); the older tree is bi10.json before that
change, read from tests/fixtures/bi10_plan_before_label.json (the artifact's history starts after it). Each must hold:
  1. rows carry HEAD's sha, the record's tree is older: bi10 is measured, and the override is recorded (row_sha_wins);
  2. rows carry a stale sha: bi10 is still unmeasured;
  3. rows carry no body_sha column: as before, the name resolved at the tree decides (at HEAD measured; at the older
     tree unmeasured);
  4. the rows of one variant carry two shas that are not count-equal successors: refused (ValueError);
  5. the capture marker (candidates.capture_plan_pipeline): a capture of every v1 body of ic9 is complete under
     pipeline 1 (v1's set), and with no marker given (None) every body generated since is unmeasured;
  6. the rows of one variant carry an older body and its count-equal successor (rows reused under the successor
     beside rows re-run on it): one body, the successor.
On the code before the rule, case 1 fails, and before 6's rule, case 6: run it there to see the check bite.

    python3 tests/unit/check_captured_bodies_row_sha.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.plan_choice import candidates as C

OLD_TREE = "older-tree"          # a capture's commit at which bi10.json is the fixture below
FIXTURE = os.path.join(HERE, "..", "fixtures", "bi10_plan_before_label.json")
_git_plan_file_at = C._plan_file_at


def _plan_file_at(path, tree):
    """the older tree's bi10.json from the fixture; every other (path, tree) as git has it"""
    if tree == OLD_TREE and os.path.basename(path) == "bi10.json":
        return json.load(open(FIXTURE))
    return _git_plan_file_at(path, tree)


C._plan_file_at = _plan_file_at


def main():
    head = C.candidate_bodies("bi", "bi10")["hand"]     # bi10's one body, named "hand" in its generated file
    row = lambda sha: {"query": "bi10", "body": "bi10", **({} if sha is None else {"body_sha": sha})}
    cases = [
        ("1 HEAD's sha in the rows, older tree", lambda: C.unmeasured_candidates([row(head)] * 5, "bi", "bi10", OLD_TREE), []),
        ("2 a stale sha in the rows", lambda: C.unmeasured_candidates([row("000000000000")] * 5, "bi", "bi10", OLD_TREE), ["hand"]),
        ("3a no body_sha column, at HEAD", lambda: C.unmeasured_candidates([row(None)] * 5, "bi", "bi10", None), []),
        ("3b no body_sha column, older tree", lambda: C.unmeasured_candidates([row(None)] * 5, "bi", "bi10", OLD_TREE), ["hand"]),
        ("3c an empty body_sha, older tree", lambda: C.unmeasured_candidates([row("")] * 5, "bi", "bi10", OLD_TREE), ["hand"]),
    ]
    v1 = sorted(C._v1_candidates("interactive", "ic9"))
    new = sorted(n for n, h in C.candidate_bodies("interactive", "ic9").items() if h not in v1)
    rows9 = [{"query": "ic9", "body": f"v{i}", "body_sha": h} for i, h in enumerate(v1)]
    cases += [("5a every v1 body, pipeline 1", lambda: C.unmeasured_candidates(rows9, "interactive", "ic9", None, 1), []),
              ("5b every v1 body, no marker given", lambda: C.unmeasured_candidates(rows9, "interactive", "ic9", None), new)]
    if not new:
        print("FAIL 5: ic9 has no body generated since v1: the case proves nothing")
        return 1
    bad = 0
    for name, fn, want in cases:
        got = fn()
        ok = got == want
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {name}: unmeasured {got} (want {want})")
    wins = getattr(C, "row_sha_wins", lambda: {})()
    ok = ("bi", "bi10", "bi10", OLD_TREE) in wins and wins[("bi", "bi10", "bi10", OLD_TREE)][0] == head
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} 1 the override recorded: {wins.get(('bi', 'bi10', 'bi10', OLD_TREE))}")
    try:
        C.captured_bodies([row(head), row("000000000000")], "bi", "bi10", OLD_TREE)
        ok = False
    except ValueError as e:
        ok = "2 bodies" in str(e)
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} 4 two shas under one variant refused")
    # 6: rows of an older body reused under its count-equal successor (inputs/adoptions.json), beside rows of the
    # successor (an SF30 re-run of one param): one body, today's. bi17's hand body: 67302ee60fd3 -> 44b050c52ec5.
    old17, new17 = "67302ee60fd3", C.candidate_bodies("bi", "bi17")["hand"]
    if C.successor_sha("bi", "bi17", old17) != new17:
        print(f"FAIL 6: bi17 {old17} has no successor {new17} in the ledger: the case proves nothing")
        return 1
    rows17 = [{"query": "bi17", "body": "hand", "body_sha": old17}] * 4 + [{"query": "bi17", "body": "hand", "body_sha": new17}]
    try:
        got = C.captured_bodies(rows17, "bi", "bi17", None)
    except ValueError as e:
        got = f"ValueError: {e}"
    ok = got == {"hand": new17}
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} 6 an old body's rows beside its successor's: one body {got} (want {{'hand': {new17!r}}})")
    print(f"{len(cases) + 3} cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
