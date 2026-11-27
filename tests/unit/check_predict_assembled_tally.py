#!/usr/bin/env python3
"""An assembled tally equals the direct tally of its spec, its tally version included (no store, seconds):

  tally_for_spec   the tally library's assembly (python/kvevaluator/predict/tally_library.py) of an off-diagonal layout
                   (Chunked props, Split adjacency) over a small fixture graph, against `kvevaluator tally` on that spec:
                   A.differences must be empty (every field but wall time and provenance), and the tally_version must
                   be the binary's
  assemble         assemble_tally.assemble of the two one-sided tallies carries their version too; of two sampled
                   ones (tally --owner-sample) their owner_sample, and two samples, or a sampled and a full, refused
  control          the assembled tally with its tally_version dropped differs from the direct one
  binaries         two tallies with no version by other binaries are assembled, marked binaries_differ, both named
                   (control: one binary, no mark); a stream with no freshness key is keyed on its content sha, marked
  stamped          a library made before versions, stamped (its keys say version 1, its tally files say none): an entry
                   is read at version 1, and a diagonal assembled from it equals that entry (check_assembled_tallies'
                   reference for a diagonal); a binary at a later version does not reuse it. A dirty binary reuses no
                   entry (tally_library.reusable), so its two reuse cases are skipped by name, as with no .git

    python3 tests/unit/check_predict_assembled_tally.py
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile

import check_predict_rules as R  # (its record helpers)
from kvevaluator import stale
from kvevaluator.layouts import cell_id
from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.predict import assemble_tally as A
from kvevaluator.predict import tally_library as TL

SPEC = ROOT / "inputs" / "specs" / "grid" / "props_chunked_adj_split.json"
fails, cases = [], 0


HISTORY = (ROOT / ".git").exists()
NO_HISTORY = "no .git: stamp() compares the binary with version 1's commit in the history"


DIRTY = "a dirty binary reuses no tally entry (tally_library.reusable)"
binary_dirty = False                       # set by main() from the binary's --version


def case(what, ok, got="", history=False, clean=False):
    """one case; history=True: it reads the repo's history (TL.stamp), skipped by name with no .git (a release archive);
    clean=True: it needs the binary's reuse, skipped by name under a dirty binary (`just check` on an uncommitted tree)"""
    if history and not HISTORY:
        print(f"  skip {what} ({NO_HISTORY})")
        return
    if clean and binary_dirty:
        print(f"  skip {what} ({DIRTY})")
        return
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got else ""))
    if not ok:
        fails.append(what)


def fixture():
    """three persons who know each other, 12 posts and 20 comments spread over them"""
    lines = [R.person(i) for i in range(3)] + [R.knows(0, 1), R.knows(1, 2), R.knows(2, 0)]
    lines += [R.post(i) for i in range(12)] + [R.comment(i) for i in range(12, 32)]
    lines += [R.has_creator(i, i % 3, "Post" if i < 12 else "Comment") for i in range(32)]
    return lines


def direct(spec_path, records):
    r = subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", str(spec_path), "--records", records],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def main():
    global binary_dirty
    about = json.loads(subprocess.run([str(cpp_bin()), "--version"], capture_output=True, text=True, check=True).stdout)
    version, binary_dirty = about.get("tally_version"), bool(about.get("binary_dirty"))
    with tempfile.TemporaryDirectory() as t:
        records = os.path.join(t, "records.jsonl")
        with open(records, "w") as f:   # vertices before edges, as the record stream has them
            for x in sorted(fixture(), key=lambda x: x[0] != "v"):
                f.write(json.dumps(x) + "\n")
        json.dump({"v": 1}, open(f"{records}.key.json", "w"))   # a freshness key: the library keys its entries on it
        spec = json.load(open(SPEC))
        want = direct(SPEC, records)
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            lib = TL.Library(os.path.join(t, "lib"), records, quiet=True, root=t)
            got = TL.tally_for_spec(spec, lib)
        diff = A.differences(got, want)
        case("tally_for_spec equals the direct tally (Chunked props, Split adjacency)", diff == [], diff[:2])
        case(f"its tally_version is the binary's ({version})", got.get("tally_version") == version == want.get("tally_version"),
             got.get("tally_version"))
        bare = {k: v for k, v in got.items() if k != "tally_version"}
        case("control: without its tally_version it differs", A.differences(bare, want) != [], "")
        linked = {**want, "records": {**want["records"], "path": "/elsewhere/records.jsonl"}}   # one stream linked into a run root
        case("the same stream at another path does not differ", A.differences(linked, want) == [], A.differences(linked, want)[:1])
        other = {**want, "records": {**want["records"], "lines": want["records"].get("lines", 0) + 1}}
        case("control: another stream (one more line) differs", A.differences(other, want) != [], "")

        split = direct(SPEC.parent / "props_split_adj_split.json", records)   # the adjacency side: Split
        two = A.assemble(want, split)
        case("assemble carries its inputs' tally_version", two.get("tally_version") == version, two.get("tally_version"))
        case("assemble equals the direct tally too", A.differences(two, want) == [], A.differences(two, want)[:2])
        # sampled tallies (tally --owner-sample): the assembled one carries their owner_sample; two samples refused
        def sampled(spec_path, seed):
            r = subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", str(spec_path), "--records", records,
                                "--owner-sample", "50", "--seed", str(seed)], capture_output=True, text=True, check=True)
            return json.loads(r.stdout)
        s1, s1_split = sampled(SPEC, 1), sampled(SPEC.parent / "props_split_adj_split.json", 1)
        st = A.assemble(s1, s1_split)
        case("sampled: the assembled tally carries its inputs' owner_sample",
             "owner_sample" in s1 and st.get("owner_sample") == s1["owner_sample"], str(st.get("owner_sample", "none"))[:80])
        case("full: no owner_sample in the assembled tally", "owner_sample" not in two, "")
        for what, x, y in (("two seeds", s1, sampled(SPEC.parent / "props_split_adj_split.json", 2)),
                           ("a sampled and a full", s1, split)):
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    A.assemble(x, y)
                refused = False
            except SystemExit:
                refused = True
            case(f"sampled: {what} refused", refused, "")
        # two tallies with no version, by other binaries: assembled, warned and marked, both binaries named
        unv = lambda t, sha: {**{k: v for k, v in t.items() if k != "tally_version"},
                              "provenance": {**t["provenance"], "binary_git_sha": sha}}
        with stale.scope() as marks, contextlib.redirect_stderr(io.StringIO()):
            mixed = A.assemble(unv(want, "a" * 40), unv(split, "b" * 40))
        frm = mixed["provenance"]["assembled_from"]
        case("two unversioned tallies by other binaries: assembled, marked binaries_differ, both binaries named",
             [n["kind"] for n in marks] == ["binaries_differ"] and frm["props"]["binary_git_sha"] == "a" * 40
             and frm["adjacency"]["binary_git_sha"] == "b" * 40 and A.differences(mixed, bare) == [], marks)
        with stale.scope() as marks:
            A.assemble(unv(want, "a" * 40), unv(split, "a" * 40))
        case("control: by one binary, no mark", marks == [], marks)
        # a record stream with no freshness key: the library keys its tallies on the stream's bytes, marked
        nokey = os.path.join(t, "nokey.jsonl")
        with open(nokey, "w") as f:
            f.write(open(records).read())
        with stale.scope() as marks, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            lib2 = TL.Library(os.path.join(t, "lib-nokey"), nokey, quiet=True, root=t)
        from kvevaluator.measure import records_key
        case("a stream with no key: the library keys on its content sha, marked stream_keyed_by_content",
             lib2.stream_content == records_key.file_sha256(nokey) and [n["kind"] for n in marks] == ["stream_keyed_by_content"],
             (lib2.stream_content, marks))
        # a stamped library: the seeds' entries as an older binary left them (no version in the tally or its key),
        # then stamped as version 1's (stamp() writes the key only)
        seeds = [json.load(open(SPEC.parent / (cell_id(x, x) + ".json"))) for x in ("chunked", "split")]
        sdir = os.path.join(t, "stamped")
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            TL.Library(sdir, records, seeds=seeds, quiet=True, root=t).entry(seeds[0])
        for k in os.listdir(sdir):
            if k.endswith(".key.json"):
                key = json.load(open(os.path.join(sdir, k)))
                key.pop("tally_version", None)
                key["binary"] = {"git_sha": TL.V1_CPP_COMMIT, "dirty": False}
                json.dump(key, open(os.path.join(sdir, k), "w"))
                tp = os.path.join(sdir, k.replace(".key.json", ".json"))
                tt = json.load(open(tp))
                tt.pop("tally_version", None)
                json.dump(tt, open(tp, "w"))
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            stamped, _ = TL.stamp(sdir)
            slib = TL.Library(sdir, records, seeds=seeds, quiet=True, root=t)
            slib.version = 1   # read as a version-1 binary would: the binary's own version may be later
            _, entry = slib.entry(seeds[0])
            diag = TL.tally_for_spec(seeds[0], slib)
        case("stamped: the entry is reused and read at version 1", slib.reused and entry.get("tally_version") == 1,
             f"{len(stamped)} stamped, reused {slib.reused}, version {entry.get('tally_version')}", history=True, clean=True)
        diff = A.differences(diag, entry)
        case("stamped: the diagonal assembled from it equals the entry", diff == [], diff[:2], history=True, clean=True)
        case(f"stamped: at the binary's version ({version}) the entry is reused only if that is 1",
             TL.reusable(slib.binary, 1, slib.binary, version) == (version == 1), "")
    ok = not fails
    print(f"predict assembled tally {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok and cases else 1


if __name__ == "__main__":
    sys.exit(main())
