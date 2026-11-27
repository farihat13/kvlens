#!/usr/bin/env python3
"""The reuse ledger (kvevaluator.adoptions, inputs/adoptions.json): a row keeps a result whose freshness key moved and
marks it adopted; with no row the check warns and marks it stale as before. The tracked ledger first (every row valid);
then each family with a control:
  ledger      covering: a chain A -> B -> C covers A -> C; another content hash, scope or key is not covered; problems():
              a judgment by an agent, a duplicate, a cycle, made == now and a non-hex key are refused; a carried-over
              judgment (why.migrated_from) is not
  marks       stale.adopted goes to the record's `adopted` list, never to `stale`; column() and kinds() leave it out
  successors  every pair of the table the body rows replaced (inputs/picks/count_equal_successors.json at the
              ledger's first commit's parent) is in the successor map
  streams     records._existing on a code-only move: covered -> adopted, no stream_keyed_by_content; not covered ->
              stream_keyed_by_content with the add line
  grid        grid_bodies.reused with the real ledger_keys: picks moved, no row -> measured, the add line names the
              keys; the row from it -> kept, adopted; control: the store moved too (identity) -> measured
  tallies     Library.entry, only the binary moved: no row -> tallied again; a row -> kept, adopted; control: another
              stream (identity) with the row -> not kept
  latency     ledger_keys: made == now when nothing moved, != for a moved body; only plan and counting adoptable;
              stale_of_plan folds an adopted stage into `adopted` with its rows
Store-free and binary-free (a temp ledger, adoptions.use; the library's binary is planted).

    python3 tests/unit/check_adoptions.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator import adoptions as A
from kvevaluator import stale
from kvevaluator.paths import ROOT

H = {c: c * 16 for c in "abcdef0123456789"}      # 16-hex keys


def row(what, made, now, kind="capture", scope=None, who="user", why=None):
    scope = scope or {"cell": "c1"}
    return {"id": A.row_id(kind, scope, what, now, made), "kind": kind, "scope": scope, "what": what,
            "made_under": made, "current_for": now, "why": why or {"judgment": "test", "moved": "none"}, "who": who,
            "when": "2026-11-05T22:00:00Z"}


def main():
    fails = 0

    def check(ok, what):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {what}")
        fails += not ok

    tracked = A.rows()
    bad = A.problems(tracked)
    check(tracked and not bad, f"0 the tracked ledger holds: {len(tracked)} rows {bad[:2]}")
    with tempfile.TemporaryDirectory() as t:
        A.use(os.path.join(t, "adoptions.json"))
        try:
            # ledger
            A._write([row(H["a"], H["1"], H["2"]), row(H["a"], H["2"], H["3"])])
            chain = A.covering("capture", H["a"], H["1"], H["3"], {"cell": "c1"})
            check(len(chain) == 2, f"1a a chain 1 -> 2 -> 3 covers 1 -> 3 ({len(chain)} rows)")
            misses = [A.covering("capture", H["b"], H["1"], H["3"], {"cell": "c1"}),
                      A.covering("capture", H["a"], H["1"], H["3"], {"cell": "c2"}),
                      A.covering("capture", H["a"], H["1"], H["4"], {"cell": "c1"}),
                      A.covering("tally", H["a"], H["1"], H["3"], {"cell": "c1"})]
            check(misses == [[], [], [], []], "1b control: another hash, scope, key or kind is not covered")
            bad = {"agent judgment": A.problems([row(H["a"], H["1"], H["2"], who="agent")]),
                   "duplicate": A.problems([row(H["a"], H["1"], H["2"])] * 2),
                   "cycle": A.problems([row(H["a"], H["1"], H["2"]), row(H["a"], H["2"], H["1"])]),
                   "made == now": A.problems([row(H["a"], H["1"], H["1"])]),
                   "not hex": A.problems([row("a path/x", H["1"], H["2"])])}
            check(all(bad.values()), f"1c refused: {[k for k, v in bad.items() if not v] or 'all five'}")
            carried = A.problems([row(H["a"], H["1"], H["2"], who="agent",
                                      why={"judgment": "kept: x", "migrated_from": "snapshot_acknowledgements.json"})])
            check(carried == [], f"1d control: a carried-over judgment by an agent holds {carried}")

            # marks
            with stale.scope(), contextlib.redirect_stderr(io.StringIO()):
                stale.adopted(["r1"], "kept by a row")
                stale.note("reused_stale", "kept by the flag")
                rec = stale.fold({})
            check([n["kind"] for n in rec.get("stale", [])] == ["reused_stale"]
                  and [n["rows"] for n in rec.get("adopted", [])] == [["r1"]],
                  f"2a adopted goes to `adopted`, the flag's note to `stale` {sorted(rec)}")
            check(stale.column(["adopted", "reused_stale"]) == "reused_stale" and "adopted" not in stale.kinds(),
                  "2b column() and kinds() leave an adoption out")
        finally:
            A.use(None)

    # successors: the body rows against the table they replaced
    first = subprocess.run(["git", "log", "--diff-filter=A", "--format=%H", "--", "inputs/adoptions.json"],
                           capture_output=True, text=True, cwd=ROOT).stdout.split()
    base = f"{first[-1]}^" if first else "HEAD"
    old = subprocess.run(["git", "show", f"{base}:inputs/picks/count_equal_successors.json"], capture_output=True,
                         text=True, cwd=ROOT)
    if old.returncode == 0:
        from kvevaluator.plan_choice import candidates as C
        C._successors.cache_clear()
        want = {(p["workload"], p["query"], p["old_body_sha"]): p["new_body_sha"] for p in json.loads(old.stdout)["pairs"]}
        got = C._successors()
        check(want.items() <= got.items() and len(want) >= 9, f"3 every pair of the old table at {base[:10]} is in the "
              f"successor map from the body rows ({len(want)} of {len(got)})")
    else:
        check(False, f"3 the old successor table at {base}: {old.stderr.strip()[:120]}")

    with tempfile.TemporaryDirectory() as t:
        A.use(os.path.join(t, "adoptions.json"))
        try:
            for family in (streams, grid, tallies):
                family(t, check)
            latency(check)
        finally:
            A.use(None)
    print(f"check_adoptions: 18 cases, {fails} failed")
    return 1 if fails else 0


def streams(t, check):
    from kvevaluator.measure import records as R
    from kvevaluator.measure import records_key as K
    path = os.path.join(t, "stream.jsonl")
    open(path, "w").write("{}\n")
    have = {"version": K.KEY_VERSION, "code": {"by": "files"}, "python": {"a": ["x.py"]}, K.CONTENT: H["c"] * 4}
    want = dict(have, python={"b": ["x.py"]})
    json.dump(have, open(K.key_path(path), "w"))
    saved = K.moved, K.upgraded
    try:
        K.moved = lambda h, w: ["python: 1 files changed in content (e.g. x.py)"]
        K.upgraded = lambda h, w: False

        def run():
            with stale.scope() as sc, contextlib.redirect_stderr(io.StringIO()) as err:
                R._existing("ldbc", "0.3", None, path, None, want, False)
            return [n["kind"] for n in sc], err.getvalue()
        before = run()
        A.add("records_stream", have[K.CONTENT], K.code_key(have), K.code_key(want),
              {"proof": "test"}, "agent", K.ledger_scope("ldbc", "0.3"))
        after = run()
    finally:
        K.moved, K.upgraded = saved
    check(before[0] == ["stream_keyed_by_content"] and "kvevaluator.adoptions add records_stream" in before[1],
          f"4a streams, no row: marked stream_keyed_by_content, the add line printed {before[0]}")
    check(after[0] == ["adopted"], f"4b streams, a row: adopted, not marked stale {after[0]}")
    return 0


STAMP = {"seam": 4, "format": 3, "write_seam": 1, "write_format": 1}


def grid(t, check):
    from kvevaluator.measure import grid_bodies as GB
    from kvevaluator.output_tree import Capture
    out, store = os.path.join(t, "grid"), os.path.join(t, "store")
    cap = Capture(out)
    os.makedirs(os.path.dirname(cap.record("c1")), exist_ok=True)
    os.makedirs(os.path.dirname(cap.path_of("counts", "c1")), exist_ok=True)
    os.makedirs(store)
    json.dump({"format": "meta"}, open(os.path.join(store, "manifest.json"), "w"))
    open(cap.path_of("counts", "c1"), "w").write("query,S\nic1,5\n")
    json.dump({"cell": "c1", "picks": {"ic1": "a"}, "counting": {"seam": 4, "format": 3}}, open(cap.record("c1"), "w"))
    saved = GB.meta_store, GB.counting.stamp
    try:
        GB.meta_store = lambda *a, **k: store
        GB.counting.stamp = lambda fmt: dict(STAMP)
        led = lambda: GB.ledger_keys(out, "c1", "interactive", "0.1", picks={"ic1": "b"})
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            none = GB.reused(out, "c1", "interactive", ["picks"], False, ledger=led)
            what, made, now = led()
            hint = f"add capture --what {what} --made {made} --now {now}" in err.getvalue()
            A.add("capture", what, made, now, {"judgment": "test", "moved": "none"}, "user",
                  {"workload": "interactive", "cell": "c1"})
            identity = GB.reused(out, "c1", "interactive", ["store", "picks"], False, ledger=led)
            kept = GB.reused(out, "c1", "interactive", ["picks"], False, ledger=led)
    finally:
        GB.meta_store, GB.counting.stamp = saved
    rec = json.load(open(cap.record("c1")))
    check(none is False and hint and made != now,
          "5a grid, picks moved, no row: measured again, the add line names the keys ledger_keys computes")
    check(kept is True and [n["kind"] for n in rec.get("adopted", [])] == ["adopted"] and "stale" not in rec,
          f"5b grid, the row from that line: kept, the record marked adopted {sorted(rec)}")
    check(identity is False, "5c control: the same row, but the store moved too (identity): measured again")


def tallies(t, check):
    from kvevaluator.measure.records_key import file_sha256
    from kvevaluator.predict import tally_library as TL
    records = os.path.join(t, "r.jsonl")
    open(records, "w").write("")
    json.dump({"v": 1}, open(records + ".key.json", "w"))
    saved = TL.binary_info, TL.Library.check_writable
    try:
        TL.binary_info = lambda: ({"git_sha": "a" * 40, "dirty": False}, 2)
        TL.Library.check_writable = lambda self: (_ for _ in ()).throw(SystemExit("would tally again"))
        spec = {"version": 1, "vertex_props": {"group": "item"}}
        sha = TL.sha256_bytes(TL.canonical(spec).encode())
        lib_dir = os.path.join(t, "lib")
        os.makedirs(lib_dir)
        stem = os.path.join(lib_dir, f"setting-{sha[:12]}")
        json.dump({"format": "meta"}, open(stem + ".json", "w"))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            lib = TL.Library(lib_dir, records, quiet=True, root=t)
        # an entry from before tally versions, tallied by another (clean) binary: only the binary moved
        have = {"spec": json.loads(TL.canonical(spec)), "spec_sha256": sha, "records_key_sha256": lib.stream_key, "records_sources_sha256": lib.stream_sources,
                "records_content_sha256": lib.stream_content, "schema_sha256": lib.schema_sha,
                "binary": {"git_sha": "b" * 40, "dirty": False}}

        def entry(key):
            json.dump(key, open(stem + ".key.json", "w"))
            with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                lib = TL.Library(lib_dir, records, quiet=True, root=t)
                try:
                    with stale.scope() as marks:
                        lib.entry(spec)
                    return "kept", [n["kind"] for n in marks]
                except SystemExit as e:
                    return "not kept", str(e) + err.getvalue()
        no_row = entry(have)
        want = {"binary": lib.binary, "tally_version": lib.version}
        A.add("tally", file_sha256(stem + ".json"), TL._ledger_key(have), TL._ledger_key(want),
              {"judgment": "test", "moved": "none"}, "user", {"spec": sha[:12]})
        with_row = entry(have)
        other_stream = entry(dict(have, records_key_sha256="other", records_sources_sha256="e" * 64))
    finally:
        TL.binary_info, TL.Library.check_writable = saved
    check(no_row[0] == "not kept" and "would tally again" in no_row[1],
          f"6a tallies, only the binary moved, no row: tallied again {no_row[0]}")
    check(with_row == ("kept", ["adopted"]), f"6b tallies, a row: kept, adopted {with_row}")
    check(other_stream[0] == "not kept", f"6c control: another record stream (identity), the same row: not kept {other_stream[0]}")


def latency(check):
    from kvevaluator.measure import latency_fresh as F
    saved = F.counting.stamp
    try:
        F.counting.stamp = lambda fmt: dict(STAMP)
        ident = {"format": "meta", "counted": "read", "store": "s", "params": {"ic1": "p"}, "bodies": {"ic1": "b1"}}
        run = {"identity": {"x": dict(ident, counting={"seam": 4, "format": 3})}}
        rows = [{"query": "ic1", "wall_ns": 5}]
        same = F.ledger_keys([run], rows, "x", "ic1", ident)
        moved = F.ledger_keys([{"identity": {"x": dict(run["identity"]["x"], bodies={"ic1": "b0"})}}], rows, "x", "ic1", ident)
    finally:
        F.counting.stamp = saved
    check(same[1] == same[2] and moved[1] != moved[2],
          "7a latency keys: a run stamped as today gives made == now; a moved body, made != now")
    check(F.ADOPTABLE_PARTS == ("plan", "counting"), f"7b latency: only the plan and counting parts adoptable {F.ADOPTABLE_PARTS}")
    keys = (H["e"] * 4, H["1"], H["2"])
    A.add("capture", *keys, {"judgment": "test", "moved": "none"}, "user", {"cell": "x", "query": "ic1", "stage": "warm"})
    plan = {"x": {"skip": ["warm"], "adopted": {"warm": {"ic1": ["plan", list(keys)]}}}}
    with stale.scope():
        rec = stale.fold({}, extra=F.stale_of_plan(plan))
    check([n["kind"] for n in rec.get("adopted", [])] == ["adopted"] and "stale" not in rec
          and rec["adopted"][0]["rows"], f"7c latency: an adopted stage folds into `adopted` with its rows {sorted(rec)}")


if __name__ == "__main__":
    sys.exit(main())
