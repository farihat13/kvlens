#!/usr/bin/env python3
"""--reuse-stale keeps a result whose freshness key moved, and marks it reused_stale (kvevaluator.stale); what cannot
stand for the run is still re-measured or refused. Each family with a control:
  grid_bodies   reused(): a fresh cell is kept unmarked; a moved key is re-measured without the flag and kept, its
                record marked with the parts, with it; a cell that cannot stand (None) is never kept. moved_parts():
                the parts named (inputs, picks), another params count is None
  latency       _moved_parts(): the identity parts that moved (store, params, plan, counting); a failed run, fewer params
                than asked or other step columns cannot stand (None); stale_of_plan marks a kept stage, not a re-measured one
  tallies       Library.entry: an entry of another record stream is refused without the flag, kept and marked with it;
                one of another tally version is never kept
Store-free and binary-free (the library's binary is planted).

    python3 tests/unit/check_reuse_stale.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

from kvevaluator import stale
from kvevaluator.measure import grid_bodies as GB
from kvevaluator.measure import latency_fresh as F
from kvevaluator.output_tree import Capture
from kvevaluator.predict import tally_library as TL


def main():
    fails = 0

    def check(ok, what):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {what}")
        fails += not ok

    # grid_bodies
    with tempfile.TemporaryDirectory() as t:
        cap = Capture(t)
        side = cap.record("c1")
        os.makedirs(os.path.dirname(side), exist_ok=True)
        rec = {"cell": "c1", "workload": "interactive", "nparam": 5, "inputs": {"bodies": "x"}, "picks": None,
               "measurement_version": GB.CAPTURE_SHAPE, "slice": {"queries": None, "params": None}, "cell_inputs": "ci"}
        json.dump(rec, open(side, "w"))
        for kind in ("counts", "parts"):
            os.makedirs(os.path.dirname(cap.path_of(kind, "c1")), exist_ok=True)
            open(cap.path_of(kind, "c1"), "w").write("query\n")
        store = os.path.join(t, "store")
        os.makedirs(store)
        open(os.path.join(store, "manifest.json"), "w").write("{}")
        saved = GB.meta_store, GB.cell_fingerprint, GB.same_store, GB.counted_fresh
        try:
            GB.meta_store = lambda *a, **k: store
            GB.cell_fingerprint = lambda *a, **k: "ci"
            GB.same_store = lambda *a, **k: True
            GB.counted_fresh = lambda *a, **k: True
            args = (t, "c1", "interactive", "0.1", 5)
            got = [GB.moved_parts(*args, {"bodies": "x"}), GB.moved_parts(*args, {"bodies": "y"}, picks={"ic1": "b"}),
                   GB.moved_parts(t, "c1", "interactive", "0.1", 3, {"bodies": "x"})]
        finally:
            GB.meta_store, GB.cell_fingerprint, GB.same_store, GB.counted_fresh = saved
        check(got[0] == [] and got[1] == ["inputs (bodies or param rows)", "picks"] and got[2] is None,
              f"1 moved_parts: fresh [], the moved parts named, another params count None {got}")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            r = [GB.reused(t, "c1", "interactive", [], False), GB.reused(t, "c1", "interactive", ["picks"], False),
                 "stale" not in json.load(open(side)), GB.reused(t, "c1", "interactive", ["picks"], True),
                 GB.reused(t, "c1", "interactive", None, True)]
        kept = json.load(open(side)).get("stale", [])
        check(r == [True, False, True, True, False] and [n["kind"] for n in kept] == ["reused_stale"] and "picks" in kept[0]["detail"],
              f"2 reused: fresh kept unmarked; moved re-measured without the flag, kept and marked with it; None never {r}")

    # latency
    ident = {"store": "s1", "nparam": 5, "params": {"ic1": "p"}, "bodies": {"ic1": "b1"}, "plans": {},
             "format": "meta", "counted": {}, "step_columns": ["S"]}
    saved = F.counting
    try:
        F.counting = type("C", (), {"counting_fresh": staticmethod(lambda s, *a: (s.get("cv") == 1,))})
        run = lambda **st: {"status": "ok", "identity": {"x": {**ident, "cv": 1, **st}}}
        got = [F._moved_parts(run(), "x", "ic1", ident), F._moved_parts(run(store="s0", cv=0), "x", "ic1", ident),
               F._moved_parts({**run(), "status": "failed"}, "x", "ic1", ident),
               F._moved_parts(run(nparam=3), "x", "ic1", ident), F._moved_parts(run(step_columns=["S", "G"]), "x", "ic1", ident)]
    finally:
        F.counting = saved
    check(got == [[], ["store", "counting"], None, None, None],
          f"3 latency _moved_parts: the parts; a failed run, fewer params, other step columns None {got}")
    got = F.stale_of_plan({"x": {"skip": ["warm"], "reused_stale": {"warm": {"ic1": "store"}, "cold": {"ic2": "plan"}}}})
    check([(e["kind"], e["detail"].split(":")[0]) for e in got] == [("reused_stale", "x warm ic1")],
          f"4 stale_of_plan: a kept stage marked, a re-measured one not {got}")

    # tallies
    with tempfile.TemporaryDirectory() as t:
        records = os.path.join(t, "r.jsonl")
        open(records, "w").write("")
        json.dump({"v": 1}, open(records + ".key.json", "w"))
        saved = TL.binary_info
        try:
            TL.binary_info = lambda: ({"git_sha": "a" * 40, "dirty": False}, 2)
            spec = {"version": 1, "vertex_props": {"group": "item"}}
            sha = TL.sha256_bytes(TL.canonical(spec).encode())
            lib_dir = os.path.join(t, "lib")
            os.makedirs(lib_dir)
            stem = os.path.join(lib_dir, f"setting-{sha[:12]}")
            json.dump({"format": "meta", "tally_version": 2}, open(stem + ".json", "w"))

            def plant(version):
                json.dump({"spec_sha256": sha, "records_key_sha256": "other", "records_content_sha256": "c" * 64,
                           "tally_version": version, "binary": {"git_sha": "a" * 40, "dirty": False}}, open(stem + ".key.json", "w"))

            def entry(reuse):
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    lib = TL.Library(lib_dir, records, quiet=True, root=t, reuse_stale=reuse)
                    try:
                        with stale.scope() as marks:
                            lib.entry(spec)
                        return "kept", [n["kind"] for n in marks]
                    except SystemExit:
                        return "refused", []
            plant(2)
            no_flag, with_flag = entry(False), entry(True)
            plant(1)
            other_version = entry(True)
        finally:
            TL.binary_info = saved
        check(no_flag == ("refused", []) and with_flag == ("kept", ["reused_stale"]),
              f"5 tallies: another stream refused without the flag, kept and marked with it {no_flag} {with_flag}")
        check(other_version[0] != "kept", f"6 tallies: another tally version is never kept {other_version}")
    print(f"check_reuse_stale: 6 cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
