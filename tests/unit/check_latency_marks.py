#!/usr/bin/env python3
"""latency_fresh.stale_of_plan, the run record's marks (kvevaluator.stale) for latency rows kept although a body since
rewritten count-equal timed them: a stage the run reuses is marked per query, a stage it re-measures is not (its rows
are timed again), and a plan with none gives no entry; a figure set that mixes request protocols (run/kvstats.sh
notes it in every slug's plan) is one protocol_mixed entry; grid_latency --check gates a pick that moved since its
timing against the body timed and lists it (as_timed); the counts gate's verdict (gate_verdict: a measuring run whose
counts differ is marked timed_counts_differ and goes on; a cell with no counts is not gated). Store-free: synthetic plans.

    python3 tests/unit/check_latency_marks.py
"""
import sys

from kvcost.latency import grid_latency as GL
from kvevaluator.measure import latency_fresh as F

UNDER = {"warm": {"ic5": "550c76caff32"}, "cold": {"ic5": "550c76caff32"}}


def main():
    cases = []
    got = F.stale_of_plan({"props_split_adj_split": {"skip": ["warm"], "under_successor": UNDER}})
    cases.append(("1 a reused stage's old-body rows are marked; a re-measured stage's are not",
                  [(e["kind"], e["detail"].split(":")[0]) for e in got]
                  == [("under_successor", "props_split_adj_split warm ic5")]))
    got = F.stale_of_plan({"props_split_adj_split": {"skip": ["warm", "cold"], "under_successor": {}},
                           "props_chunked_adj_chunked": {"skip": [], "under_successor": UNDER}})
    cases.append(("2 nothing kept under an old body: no entry", got == []))
    why = "the figures of dgraph,nebula mix request protocols after this run: x"
    got = F.stale_of_plan({"dgraph": {"skip": [], "protocol_mixed": [why]}, "nebula": {"skip": [], "protocol_mixed": [why]}})
    cases.append(("3 a mixed figure set: one protocol_mixed entry", [(e["kind"], e["detail"]) for e in got]
                  == [("protocol_mixed", why)]))
    got = GL.as_timed({"ic1": "a", "ic2": "new", "ic3": "c"}, {"ic1": "a", "ic2": "old"})
    cases.append(("4 a moved pick is gated as timed and listed; an untimed query keeps today's",
                  got == ({"ic1": "a", "ic2": "old", "ic3": "c"}, ["ic2"])))
    cases.append(("5 never timed: nothing moved", GL.as_timed({"ic1": "a"}, None) == ({"ic1": "a"}, [])))
    got = [GL.gate_verdict(*x) for x in ((True, 85, 0, False), (True, 85, 5, False), (True, 85, 5, True),
                                         (False, 85, 0, False), (False, 85, 5, False), (True, 0, 0, False),
                                         (True, 85, 5, False, 2), (True, 0, 0, False, 0, False),
                                         (False, 0, 0, False, 0, False))]
    cases.append(("6 the counts gate: equal counts done; a measuring run whose counts differ is marked and goes on, "
                  "--check fails on it; a failed regime, nothing compared, or a cell missing from the timed run fails; "
                  "a cell no counts capture holds (timed from --picks) compares nothing and is done, unless its regime failed",
                  got == ["done", "marked", "fail", "fail", "fail", "fail", "fail", "done", "fail"]))
    from kvevaluator import stale
    cases.append(("7 the mark is a stale kind", "timed_counts_differ" in stale.KINDS))
    import glob
    import re
    from kvevaluator.paths import ROOT
    used = {}                                  # a shell driver's `mark <kind> "..."` (run/measure-preflight.sh): its kind must exist
    for p in sorted(glob.glob(str(ROOT / "run" / "*.sh")) + glob.glob(str(ROOT / "experiments" / "*" / "*.sh"))):
        for k in re.findall(r'\bmark ([a-z_]+) "', open(p).read()):
            used.setdefault(k, p)
    unknown = {k: p for k, p in used.items() if k not in stale.KINDS}
    cases.append((f"8 every kind a shell driver marks is a stale kind ({len(used)} kinds: {', '.join(sorted(used))})",
                  len(used) >= 3 and not unknown))
    fails = 0
    for label, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        fails += not ok
    print(f"check_latency_marks: {len(cases)} cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
