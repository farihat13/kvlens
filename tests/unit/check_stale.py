#!/usr/bin/env python3
"""kvevaluator.stale, the one marker scheme: a kind outside KINDS raises; a note warns once per (kind, detail) and is
kept with where it was made; fold adds the process's notes and a shell step's (<dir>/stale.jsonl) to a record, once
each, and leaves a clean record without `stale`; a CSV cell is the kinds sorted and joined; the shell form notes into a
directory and refuses anything else; a scope's record folds the notes made inside it; a unit's notes (a cell's) are
its record's only; note_into warns of one note once in a process. Store-free.

    python3 tests/unit/check_stale.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator import stale


def raises(fn, *a):
    try:
        fn(*a)
    except (ValueError, SystemExit):
        return True
    return False


def main():
    cases = []
    cases.append(("1 a kind outside KINDS raises", raises(stale.note, "no_such_kind", "x")))
    cases.append(("2 a clean record gets no stale field", "stale" not in stale.fold({"a": 1})))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        stale.note("body_moved", "bi3 hand: 2bb1f0 -> a4eca1")
        stale.note("body_moved", "bi3 hand: 2bb1f0 -> a4eca1")
        stale.note("pick_in_sample", "ic5 on props_split_adj_split")
    cases.append(("3 one warning per (kind, detail), kept once", err.getvalue().count("[stale] WARNING") == 2
                  and len(stale.notes()) == 2 and stale.notes()[0]["at"] == "__main__.main"))
    cases.append(("4 a CSV cell: sorted, once each, empty when clean",
                  stale.column(["pick_in_sample", "body_moved", "body_moved"]) == "body_moved;pick_in_sample"
                  and stale.column([]) == "" and raises(stale.column, ["typo"])))
    with tempfile.TemporaryDirectory() as d:
        cmd = [sys.executable, "-m", "kvevaluator.stale", "note", "--into"]
        r1 = subprocess.run(cmd + [d, "binary_unclean", "cpp/x.cc"], capture_output=True, text=True)
        r2 = subprocess.run(cmd + [os.path.join(d, "rec.json"), "binary_unclean", "y"], capture_output=True,
                            text=True)
        r3 = subprocess.run(cmd + [d, "typo", "z"], capture_output=True, text=True)
        cases.append(("5 the shell form: into a dir; a file or a typo refused",
                      r1.returncode == 0 and r2.returncode != 0 and r3.returncode != 0))
        rec = stale.fold({"stale": [{"kind": "body_moved", "detail": "bi3 hand: 2bb1f0 -> a4eca1", "at": "earlier"}]}, d)
        r4 = subprocess.run([sys.executable, "-m", "kvevaluator.stale", "start", "--into", d], capture_output=True, text=True)
        cases.append(("8 start: an earlier run's notes moved aside, so the next record does not fold them",
                      r4.returncode == 0 and not os.path.exists(os.path.join(d, stale.STALE_FILE))
                      and any(f.startswith("stale.before-") for f in os.listdir(d))))
        stale.note_into(d, "binary_unclean", "the dir's")
        stale.note_into(d, "foreign_load", "cell A's", unit="A")
        stale.note_into(d, "foreign_load", "cell B's", unit="B")
        stale.start(d, unit="B")                       # B's next run: only B's notes move aside
        with stale.scope():
            a_rec, b_rec = stale.fold({}, d, unit="A"), stale.fold({}, d, unit="B")
        cases.append(("9 a unit's notes: its record folds the dir's and its own; another unit's start leaves them",
                      [n["detail"] for n in a_rec["stale"]] == ["the dir's", "cell A's"]
                      and [n["detail"] for n in b_rec["stale"]] == ["the dir's"]))
        got = [(n["kind"], n["detail"]) for n in rec["stale"]]
        cases.append(("6 fold: the record's own, then this process's and the dir's, each once", got == [
            ("body_moved", "bi3 hand: 2bb1f0 -> a4eca1"), ("pick_in_sample", "ic5 on props_split_adj_split"),
            ("binary_unclean", "cpp/x.cc")]))
    with stale.scope():
        stale.note("table_stale", "cell A: ic5")
        with stale.scope():
            stale.note("body_moved", "bi3 hand: 2bb1f0 -> a4eca1")   # noted before, in this process: kept here too
            inner = stale.fold({})
        outer = stale.fold({})
    cases.append(("7 a scope's record folds the notes made inside it only",
                  [n["kind"] for n in inner["stale"]] == ["body_moved"]
                  and [n["kind"] for n in outer["stale"]] == ["table_stale", "body_moved"]))
    with tempfile.TemporaryDirectory() as d:
        stale.note_into(d, "binary_unclean", "a driver's")          # the dir's own notes: a driver's, left alone
        with stale.scope():
            stale.note("table_stale", "bi: 3 queries")
            marked = stale.mark_dir(d, "bi-round")
        mine = os.path.join(d, "stale-bi-round.jsonl")
        first = os.path.exists(mine) and [json.loads(x)["kind"] for x in open(mine)] == ["table_stale"]
        with stale.scope():
            cleared = stale.mark_dir(d, "bi-round")                  # a clean re-run
        cases.append(("10 mark_dir: a step's notes beside its tables; a clean re-run clears its own, not the driver's",
                      marked == ["table_stale"] and first and cleared == [] and not os.path.exists(mine)
                      and os.path.exists(os.path.join(d, stale.STALE_FILE))))
    with stale.scope():
        two = stale.fold({"stale": [{"kind": "body_moved", "detail": "a", "at": "x"}]},
                         extra=[{"kind": "body_moved", "detail": "a", "at": "y"}, {"kind": "binaries_differ", "detail": "b", "at": "y"}])
    cases.append(("11 fold extra: a second source's notes, each (kind, detail) once",
                  [(n["kind"], n["detail"]) for n in two["stale"]] == [("body_moved", "a"), ("binaries_differ", "b")]))
    err = io.StringIO()
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(err):
        for unit in ("A", "B", "C"):                                # one note into three stores' files
            stale.note_into(d, "binary_behind_head", "count code moved", unit=unit)
        written = all(os.path.exists(stale.notes_file(d, u)) for u in ("A", "B", "C"))
    cases.append(("12 note_into: the same note into several units' files, warned once",
                  written and err.getvalue().count("[stale] WARNING binary_behind_head") == 1))
    fails = 0
    for label, ok in cases:
        print(f"{'PASS' if ok else 'FAIL'} {label}")
        fails += not ok
    print(f"check_stale: {len(cases)} cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
