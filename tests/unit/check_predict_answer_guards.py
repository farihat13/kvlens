#!/usr/bin/env python3
"""The guards around predict mode's answer, each with a case that must pass and one that must be refused:
  within        uniform.within: the layouts within cost_models.WITHIN (5%) of the predicted best, cheapest first; a tie
                at 12 significant digits goes to the first name
  search-mix    --count-source <set> (cli/search._predicted_setup): refuses a workload with write items on a set with no
                predicted writes capture, a --size the set has no Chunked/Chunked cell of (naming the kvcost predict Ch<k>-Ch<k>
                call), a set without the nine grid cells (naming the kvcost predict call) and a set with no predicted capture; reads records that name no workload, marked;
                reads a reads-only set
  library       tally_library.Library.entry: refuses an entry made from another record stream, and a key that holds
                another spec (a sha12 collision); reads its own entry, and one of the same tally version made by another
                commit; reusable(): the version rule (dirty binaries, unversioned entries); refuses to write a library
                whose real path is outside its run's root (a linked settings dir); stamp(): adopts an unversioned entry
                only when its clean binary has version 1's cpp/ tree; stream_moved(): a record stream another binary
                dumped from the same sources is the same stream, one whose reader changed is not
  content       the stream's bytes (records_key content_sha256): an entry and statistics of the same bytes are kept
                whatever the key, of other bytes refused by name; one made before the bytes were recorded is kept when
                the stream lists its ids in same_content_as (a re-dump of the same bytes), else refused;
                same_content_ids hashes the stream set aside and lists its ids only when the bytes are the same
No store, no measurement. The library cases call the binary once (--version). Under a few seconds.

Run:  python3 tests/unit/check_predict_answer_guards.py
"""
import contextlib
import copy
import csv
import io
import json
import os
import sys
import tempfile
import types

from kvcost.cli import predict as PC
from kvcost.cli import search
from kvevaluator import layouts as L
from kvevaluator import stale
from kvevaluator.paths import ROOT
from kvcost.layout_cost import cost_models as CM
from kvcost.predict_accuracy import uniform
from kvevaluator.measure import records_key
from kvcost.cli.predict import capture_dirs
from kvcost.predict_accuracy.predicted import PREDICTED
from kvevaluator.output_tree import Capture
from kvevaluator.predict import tally_library as TL

fails, cases = [], 0


HISTORY = (ROOT / ".git").exists()
NO_HISTORY = "no .git: stamp() compares the binary with version 1's commit in the history"


def case(what, ok, got, history=False):
    """one case; history=True: it reads the repo's history (TL.stamp), skipped by name with no .git (a release archive)"""
    if history and not HISTORY:
        print(f"  skip {what} ({NO_HISTORY})")
        return
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got}")
    if not ok:
        fails.append(what)


def outcome(fn):
    """'read' when fn returns, the refusal's text when it exits"""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fn()
        return "read"
    except SystemExit as e:
        return str(e)


def check_within():
    assert CM.WITHIN == 0.05, "this fixture is written for 5%"
    tot = {"c": 100.0, "b": 105.0, "d": 105.0000001, "a": 100.0 * (1 + 1e-14), "e": 120.0}
    got = [c for c, _ in uniform.within(tot)]
    case("within: 105 is within 5% of 100, 105.0000001 is not; a and c tie at 12 digits, a first", got == ["a", "c", "b"],
         got)


def write_set(root, workload_field=True, cells=None):
    """the default tier's predicted set (PC.default_set()) of interactive under `root`: `cells` (default the nine grid cells: a mix is composed from
    them), one query, every body"""
    cap = Capture(capture_dirs(3, "interactive", (PC.default_set(),), root)[0])
    os.makedirs(cap.dir("counts"), exist_ok=True)
    for cell in cells or L.CELLS:
        with open(cap.path_of("counts", cell), "w", newline="") as f:
            w = csv.DictWriter(f, ("query", "body", "param_idx", "ok", "S", "G", "N"))
            w.writeheader()
            w.writerow({"query": "ic2", "body": "a", "param_idx": "1", "ok": "1", "S": 0, "G": 1, "N": 0})
        rec = {"measured_as": PREDICTED, "bodies": "all"}
        if workload_field:
            rec["workload"] = "interactive"
        json.dump(rec, open(cap.record(cell), "w"))


def check_search_mix():
    ad = types.SimpleNamespace(DATASET="ldbc", FAMILIES=(("interactive", "read"), ("writes", "write")))
    rule = types.SimpleNamespace(but=lambda **kw: kw)
    with tempfile.TemporaryDirectory() as t:
        reads = f"{t}/reads.json"
        json.dump({"id": "toy-reads", "items": [{"family": "interactive", "query": "ic2", "frequency": 1}]},
                  open(reads, "w"))
        mixed = f"{t}/mixed.json"
        json.dump({"id": "toy-mixed", "items": [{"family": "interactive", "query": "ic2", "frequency": 1},
                                                {"family": "writes", "query": "add_person", "frequency": 1}]},
                  open(mixed, "w"))

        def setup(workload, root, size=()):
            a = types.SimpleNamespace(sf=3, workload=workload, source=PC.default_set(), sub="search-mix", size=list(size),
                                      from_dir=root)
            return lambda: search._predicted_setup(a, ad, rule)

        os.makedirs(f"{t}/empty")
        os.makedirs(f"{t}/old")
        write_set(f"{t}/old", workload_field=False)
        os.makedirs(f"{t}/ok")
        write_set(f"{t}/ok")
        os.makedirs(f"{t}/few")
        write_set(f"{t}/few", cells=["props_split_adj_split"])
        for what, fn, want in (
                ("a workload with write items, no predicted writes capture", setup(mixed, f"{t}/ok"),
                 "holds no predicted capture of writes"),
                ("--size 16k on a set with no 16 KB Chunked/Chunked cell", setup(reads, f"{t}/ok", ("16k",)),
                 f"{PC.predict_call(PC.default_set())} Ch16k-Ch16k"),   # the set's own call (the default tier's: no --tier)
                ("a set with no predicted capture", setup(reads, f"{t}/empty"), "holds no predicted capture"),
                ("a set without the nine grid cells", setup(reads, f"{t}/few"), f"{PC.predict_call(PC.default_set())} Sp-Co"),
                ("a reads-only workload on its own predicted set (the control)", setup(reads, f"{t}/ok"), "read")):
            got = outcome(fn)
            case(f"search-mix --count-source: {what}", (want in got) if want != "read" else got == "read", got[:140])
        with stale.scope() as marks, contextlib.redirect_stderr(io.StringIO()):
            got = outcome(setup(reads, f"{t}/old"))
        case("search-mix --count-source: records that name no workload: read, marked capture_shape_moved",
             got == "read" and [n["kind"] for n in marks] == ["capture_shape_moved"], (got[:140], marks))


def check_library():
    with tempfile.TemporaryDirectory() as t:
        records = f"{t}/r.jsonl"
        open(records, "w").write("")
        json.dump({"v": 1}, open(records_key.key_path(records), "w"))
        with contextlib.redirect_stderr(io.StringIO()):
            lib = TL.Library(f"{t}/lib", records, quiet=True, root=t)
        spec = {"version": 1, "vertex_props": {"group": "item"}}
        text = TL.canonical(spec)
        sha = TL.sha256_bytes(text.encode())
        stem = f"{t}/lib/setting-{sha[:12]}"
        own = {"spec": json.loads(text), "spec_sha256": sha, "records": lib.records, "records_key_sha256": lib.stream_key,
               "records_sources_sha256": lib.stream_sources, "tally_version": lib.version, "binary": lib.binary}

        def plant(key):
            json.dump({"rows": []}, open(f"{stem}.json", "w"))
            json.dump(key, open(f"{stem}.key.json", "w"))
            lib.loaded.clear()

        plant({**own, "records_key_sha256": "0" * 64, "records_sources_sha256": "1" * 64})
        got = outcome(lambda: lib.entry(spec))
        case("library: an entry made from another record stream is refused", "another record stream" in got, got[:140])
        old = {k: v for k, v in own.items() if k != "records_sources_sha256"}
        plant({**old, "records_key_sha256": "0" * 64})
        got = outcome(lambda: lib.entry(spec))
        case("library: an entry from before sources were keyed, of another freshness key, is refused",
             "another record stream" in got, got[:140])
        plant({**own, "spec_sha256": "f" * 64})
        got = outcome(lambda: lib.entry(spec))
        case("library: a key that holds another spec is refused", "holds another spec" in got, got[:140])
        if lib.binary["dirty"]:
            print("  (the control is skipped: a dirty binary's entry is always built again)")
        else:
            plant(own)
            got = outcome(lambda: lib.entry(spec))
            case("library: its own entry is read (the control)", got == "read", got[:140])
            plant({**own, "binary": {"git_sha": "a" * 40, "dirty": False}})
            got = outcome(lambda: lib.entry(spec))
            case("library: an entry of the same tally version made by another commit is read", got == "read", got[:140])
            plant({**own, "records_key_sha256": "0" * 64})
            got = outcome(lambda: lib.entry(spec))
            case("library: an entry from a re-dump of the same sources (another freshness key) is read", got == "read",
                 got[:140])
            plant(old)
            got = outcome(lambda: lib.entry(spec))
            case("library: an entry from before sources were keyed, of this freshness key, is read", got == "read",
                 got[:140])
        clean, other = {"git_sha": "b" * 40, "dirty": False}, {"git_sha": "c" * 40, "dirty": False}
        for what, args, want in (
                ("the same version, another commit", (other, 1, clean, 1), True),
                ("another version", (other, 2, clean, 1), False),
                ("an unversioned entry, the same commit", (clean, None, clean, 1), True),
                ("an unversioned entry, another commit", (other, None, clean, 1), False),
                ("a dirty binary reads nothing", (other, 1, {**clean, "dirty": True}, 1), False),
                ("an entry a dirty binary made", ({**other, "dirty": True}, 1, clean, 1), False)):
            got = TL.reusable(*args)
            case(f"library: reusable, {what}", got == want, got)
        os.makedirs(f"{t}/run")
        os.makedirs(f"{t}/elsewhere")
        os.symlink(f"{t}/elsewhere", f"{t}/run/settings")
        with contextlib.redirect_stderr(io.StringIO()):
            linked = TL.Library(f"{t}/run/settings", records, quiet=True, root=f"{t}/run")
        got = outcome(linked.check_writable)
        case("library: a settings dir linked from outside the run's root is not written", "outside this run's output root"
             in got, got[:140])
        with contextlib.redirect_stderr(io.StringIO()):
            inside = TL.Library(f"{t}/run/own", records, quiet=True, root=f"{t}/run")
        got = outcome(inside.check_writable)
        case("library: its own dir under the run's root is written (the control)", got == "read", got[:140])
        os.makedirs(f"{t}/stamp")
        for name, binary, version in (("a", {"git_sha": TL.V1_CPP_COMMIT, "dirty": False}, None),
                                      ("b", {"git_sha": TL.V1_CPP_COMMIT, "dirty": True}, None),
                                      ("c", {"git_sha": "d" * 40, "dirty": False}, None),
                                      ("d", {"git_sha": "e" * 40, "dirty": False}, 1)):
            json.dump({"binary": binary, **({"tally_version": version} if version else {})},
                      open(f"{t}/stamp/setting-{name * 12}.key.json", "w"))
        with contextlib.redirect_stdout(io.StringIO()):
            stamped, left = TL.stamp(f"{t}/stamp")
        got = (stamped, sorted(k for k, _ in left))
        case("library: stamp adopts only the clean version-1 binary's entry", got == (
            ["setting-aaaaaaaaaaaa.key.json"], ["setting-bbbbbbbbbbbb.key.json", "setting-cccccccccccc.key.json"]), got, history=True)
        key = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "golden",
                                          "tally_sf0.1.json")))["records_key"]
        other_bin = copy.deepcopy(key)
        other_bin["cpp"]["binary_git_sha"] = "f" * 40
        changed = copy.deepcopy(key)
        f = sorted(changed["cpp"]["files"])[0]
        changed["cpp"]["files"][f] = "0" * 64
        case("library: the same record stream (the control)", TL.stream_moved(key, copy.deepcopy(key)) == [], "[]")
        got = TL.stream_moved(key, other_bin)
        case("library: a stream another binary dumped from the same sources is the same stream", got == [], got)
        got = TL.stream_moved(key, changed)
        case("library: the same bytes from changed sources are the same stream (content first)", got == [], got)
        other_bytes = {**copy.deepcopy(key), "content_sha256": "e" * 64}
        got = TL.stream_moved(key, other_bytes)
        case("library: other bytes from the same sources have moved (content first)",
             len(got) == 1 and got[0].startswith("content:"), got)
        no_content = {k: v for k, v in key.items() if k != "content_sha256"}
        got = TL.stream_moved(no_content, {k: v for k, v in changed.items() if k != "content_sha256"})
        case("library: tallies of one stream: sources equal, keys differ", TL.same_records(
            {"lines": 1, "key_sha256": "a", "sources_sha256": "s"}, {"lines": 1, "key_sha256": "b", "sources_sha256": "s"}),
            "same")
        case("library: tallies of other sources", not TL.same_records(
            {"lines": 1, "key_sha256": "a", "sources_sha256": "s"}, {"lines": 1, "key_sha256": "a", "sources_sha256": "t"}),
            "differ")
        case("library: a stream whose reader's sources changed has moved (no bytes named)",
             len(got) == 1 and got[0].startswith("cpp:"), got)
        case("library: a stamped key says version 1 and why", json.load(open(f"{t}/stamp/setting-aaaaaaaaaaaa.key.json"))
             .get("tally_version") == 1, "tally_version 1", history=True)


def check_content():
    from kvevaluator.predict import build_stats as BS
    with tempfile.TemporaryDirectory() as t:
        records = f"{t}/r.jsonl"
        open(records, "w").write("{}\n")
        earlier = {"key_sha256": "K" * 64, "records_key_sha256": "R" * 64, "sources_sha256": "S" * 64,
                   "schema_json_sha256": records_key.file_sha256(TL.SCHEMA)}   # dumped beside this schema.json
        json.dump({"v": 2, records_key.CONTENT: "c1", records_key.SAME_CONTENT: [earlier]},
                  open(records_key.key_path(records), "w"))
        with contextlib.redirect_stderr(io.StringIO()):
            lib = TL.Library(f"{t}/lib", records, quiet=True, root=t)
        spec = {"version": 1, "vertex_props": {"group": "item"}}
        text = TL.canonical(spec)
        sha = TL.sha256_bytes(text.encode())
        stem = f"{t}/lib/setting-{sha[:12]}"
        base = {"spec": json.loads(text), "spec_sha256": sha, "records": lib.records, "records_key_sha256": "0" * 64,
                "records_sources_sha256": "1" * 64, "tally_version": lib.version, "binary": lib.binary}

        def plant(key):
            json.dump({"rows": []}, open(f"{stem}.json", "w"))
            json.dump(key, open(f"{stem}.key.json", "w"))
            lib.loaded.clear()

        plant({**base, "records_content_sha256": "c2"})
        got = outcome(lambda: lib.entry(spec))
        case("content: an entry of other bytes is refused, naming both", "content c2, now c1" in got, got[:160])
        plant({k: v for k, v in base.items()})
        got = outcome(lambda: lib.entry(spec))
        case("content: an entry from before the bytes were recorded, of an unlisted stream, is refused",
             "another record stream" in got, got[:160])
        if lib.binary["dirty"]:
            print("  (the read cases are skipped: a dirty binary's entry is always built again)")
        else:
            plant({**base, "records_content_sha256": "c1"})
            got = outcome(lambda: lib.entry(spec))
            case("content: an entry of the same bytes is read, whatever its key and sources", got == "read", got[:160])
            plant({**base, "records_sources_sha256": "S" * 64})
            got = outcome(lambda: lib.entry(spec))
            case("content: an older entry whose sources the stream lists (same_content_as) is read", got == "read",
                 got[:160])
            plant({k: v for k, v in base.items() if k != "records_sources_sha256"} | {"records_key_sha256": "R" * 64})
            got = outcome(lambda: lib.entry(spec))
            case("content: an older entry whose key the stream lists is read", got == "read", got[:160])
        case("content: tallies of the same bytes, other sources", TL.same_records(
            {"lines": 1, "content_sha256": "c", "sources_sha256": "s"}, {"lines": 1, "content_sha256": "c",
                                                                         "sources_sha256": "t"}), "same")
        case("content: tallies of other bytes, the same sources", not TL.same_records(
            {"lines": 1, "content_sha256": "c", "sources_sha256": "s"}, {"lines": 1, "content_sha256": "d",
                                                                         "sources_sha256": "s"}), "differ")
        # the statistics: _moved by the bytes, else by the key file (or an earlier one the stream lists)
        for what, have, want in (
                ("statistics of the same bytes are kept", {"content_sha256": "c1", "key_sha256": "x"}, None),
                ("statistics of other bytes are refused", {"content_sha256": "c9", "key_sha256": "x"}, "content c9"),
                ("older statistics whose key the stream lists are kept", {"key_sha256": "K" * 64}, None),
                ("older statistics of an unlisted key are refused", {"key_sha256": "x"}, "another dump")):
            json.dump({"records": have, "provenance": {}}, open(f"{t}/stats.json", "w"))
            got = BS._moved(f"{t}/stats.json", records, {})
            case(f"content: {what}", got is None if want is None else (got is not None and want in got), got)
        # a re-dump's same_content_as: the stream set aside, hashed (its key names no bytes)
        aside = f"{t}/old.jsonl"
        open(aside, "w").write("{}\n")
        json.dump({"v": 1}, open(records_key.key_path(aside), "w"))
        same = records_key.same_content_ids(aside, records_key.file_sha256(records))
        case("content: a set-aside stream of the same bytes is listed by its three ids",
             len(same) == 1 and same[0] == records_key.stream_ids(records_key.key_path(aside)), same)
        case("content: one of other bytes is not listed", records_key.same_content_ids(aside, "0" * 64) == [], "[]")


def main():
    check_within()
    check_search_mix()
    check_library()
    check_content()
    ok = not fails
    print(f"predict answer guards {'PASS' if ok else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
