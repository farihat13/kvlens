#!/usr/bin/env python3
"""The record cache's freshness key (python/kvevaluator/measure/records_key.py) follows the compiled LDBC schema, not
the .schema text: a comment-only edit of ldbc.schema keeps the schema part, a real change (an index dropped, a property
added) moves it. At sf0.1 (the probe scale) the code is keyed by its files, by content: a move with the text unchanged
keeps the key, an edit moves it. At a larger scale the code is keyed by the probe stream's bytes: an edit to a module
or to the C++ reader with the same probe bytes keeps the key; other probe bytes, another DUMP_FORMAT_VERSION or the
other keying move it; so do other bytes for the probe tree (python/kvevaluator/datasets/ldbc/records_probe), which the dump of a planted
copy of the code shows: a docstring keeps them, a tab read as a space, trimmed whitespace or escaped non-ASCII moves
them. A key of an earlier version fresh by its files is upgraded, its old ids kept; one adopted by probe, or whose
module text or CSVs moved, is not. --adopt-by-probe takes the dumping code's bytes for both probes (a witness with its
python, cpp and schema parts, the old key's code part, an old tree) and only when they equal today's; it keeps the
dumping code. A stale probe stream is refused, not dumped again. The dump's import closure stays inside its reader.
Opens no store; reads the committed probe tree only; a few seconds.

Run:  python3 tests/unit/check_records_key.py
"""
import json
import os
import sys
import tempfile

from kvevaluator import stale
from kvevaluator.measure import records as records_mod
from kvevaluator.measure import records_key as rk
from kvevaluator.measure.records_dump import DUMP_FORMAT_VERSION
from kvevaluator.datasets.ldbc import records as ldbc_records
from kvevaluator.datasets.ldbc.schema import SCHEMAS

CANON = SCHEMAS["canonical"]
results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


# a plant appended to a copy of loader.py: every string value passes through `fix` (x) after the reader
PLANT_STREAM = """

_planted_stream = stream


def stream(*a, **k):
    for r in _planted_stream(*a, **k):
        props = r[3] if r[0] == "v" else r[4]
        props = {{p: ({fix} if isinstance(x, str) else x) for p, x in props.items()}}
        yield (r[0], r[1], r[2], props) if r[0] == "v" else (r[0], r[1], r[2], r[3], props, r[5], r[6])
"""


def copy_code(root):
    """a copy of this tree's python/ under `root`, with inputs/ linked and no binary: a checkout a plant can edit"""
    import shutil
    from kvevaluator.paths import ROOT
    shutil.copytree(os.path.join(str(ROOT), "python"), os.path.join(root, "python"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    os.symlink(os.path.join(str(ROOT), "inputs"), os.path.join(root, "inputs"))


def refused(fn):
    """the SystemExit text of fn(), else None"""
    try:
        fn()
    except SystemExit as e:
        return str(e)
    return None


def schema_sha_of(text):
    """the schema part of a temporary copy of ldbc.schema holding `text`"""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ldbc.schema")
        with open(p, "w") as f:
            f.write(text)
        return rk.schema_part("ldbc", p)


def main():
    text = open(CANON).read()
    live = rk.schema_part("ldbc")
    case("the live schema part == the same text compiled from a copy", schema_sha_of(text) == live)

    lines = text.splitlines(keepends=True)
    commented = "# a new comment line\n" + "".join(l + ("    # a note\n" if l.startswith("vertex Person") else "")
                                                  for l in lines) + "# the end\n"
    case("comments added (a header, inside a vertex block, at the end): the schema part stays", schema_sha_of(commented) == live)
    no_index = "".join(l for l in lines if l.strip() != "index Person.firstName")
    case("an index dropped (Person.firstName): the schema part moves", no_index != text and schema_sha_of(no_index) != live)
    extra_prop = text.replace("    browserUsed  str\n", "    browserUsed  str\n    nickname     str\n", 1)
    case("a property added to Person: the schema part moves", extra_prop != text and schema_sha_of(extra_prop) != live)

    py = rk.python_part("ldbc")
    names = [n for ns in py.values() for n in ns]
    case("the python part holds schema.json and no .schema file",
         any(n.endswith("ldbc/schema.json") for n in names) and not any(n.endswith(".schema") for n in names))

    # keys built by hand: the cpp and raw parts are placeholders (store-free)
    files = {"dump_format": DUMP_FORMAT_VERSION, "by": "files"}
    cpp = {"binary_git_sha": "a" * 40, "binary_dirty": False, "files": {"cli/records.cc": "b1"}}
    want = {"version": rk.KEY_VERSION, "sf": "0.1", "variant": None, "code": files, "python": py, "schema": live,
            "cpp": cpp, "raw": {"dynamic/person_0_0.csv": [10, 20]}}
    case("the same key has not moved", rk.moved(want, want) == [])
    case("a moved schema part is named", any(m.startswith("schema:") for m in rk.moved({**want, "schema": "x"}, want)))
    renamed = {k: [n.replace("kvevaluator/", "kvevaluator/elsewhere/") for n in v] for k, v in py.items()}
    case("modules moved with their text unchanged: the key has not moved", rk.moved({**want, "python": renamed}, want) == [])
    one = sorted(py)[0]
    edited = {**{k: v for k, v in py.items() if k != one}, "0" * 64: py[one]}
    case("by files: a module's text changed: the python part moves, named by its path",
         any(m.startswith("python:") and py[one][0] in m for m in rk.moved({**want, "python": edited}, want)))
    other_reader = {**cpp, "files": {"cli/records.cc": "b2"}}
    case("by files: the C++ reader's sources changed: the cpp part moves",
         any(m.startswith("cpp:") for m in rk.moved({**want, "cpp": other_reader}, want)))
    case("by files: another DUMP_FORMAT_VERSION moves the key", any("dump format" in m for m in rk.moved(
        {**want, "code": {**files, "dump_format": DUMP_FORMAT_VERSION + 1}}, want)))

    # a larger scale: keyed by the probe's bytes (the sf0.1 stream and the probe tree)
    probe = {"dump_format": DUMP_FORMAT_VERSION, "by": "probe", "probe_sf": "0.1", "probe_content_sha256": "c" * 64,
             "probe_tree": ldbc_records.PROBE_TREE, "probe_tree_sha256": "t" * 64}
    big = {**want, "sf": "30", "code": probe}
    case("by probe: the same key has not moved", rk.moved(big, big) == [])
    case("by probe: a module edited and the C++ reader changed, the same probe bytes: the key has not moved "
         "(a comment, a docstring, a refactor)", rk.moved({**big, "python": edited, "cpp": other_reader}, big) == [])
    got = rk.moved({**big, "code": {**probe, "probe_content_sha256": "d" * 64}}, big)
    case("by probe: other sf0.1 bytes move the key, named", any(m.startswith("code:") and "sf0.1" in m for m in got), got)
    got = rk.moved({**big, "code": {**probe, "probe_tree_sha256": "u" * 64}}, big)
    case("by probe: other probe-tree bytes move the key, named (a change sf0.1 cannot show: a tab, an escape)",
         any(m.startswith("code:") and "probe tree" in m for m in got), got)
    got = rk.moved({**big, "code": {**probe, "dump_format": DUMP_FORMAT_VERSION + 1}}, big)
    case("by probe: another DUMP_FORMAT_VERSION moves the key", any("dump format" in m for m in got), got)
    got = rk.moved({**big, "code": files}, big)
    case("by probe: a key by files moves, naming --adopt-by-probe", any("keyed by" in m and "--adopt-by-probe" in m
                                                                          for m in got), got)
    got = rk.moved({**big, "raw": {"dynamic/person_0_0.csv": [10, 21]}}, big)
    case("by probe: the data changed: the raw part moves", any(m.startswith("raw:") for m in got), got)
    got = rk.moved(big, {**big, "cpp": {**cpp, "binary_dirty": True}})
    case("by probe: a dirty binary is refused", any("dirty" in m for m in got), got)

    # the code part: which keying, and the probe's bytes
    case("code part at the probe scale: by files", rk.code_part("ldbc", "0.1") == files, rk.code_part("ldbc", "0.1"))
    case("code part of a dataset with no probe scale (linkbench): by files", rk.code_part("linkbench", "1m") == files)
    real_source, real_records_file, real_tree = ldbc_records.source, records_mod.records_file, rk.probe_tree_sha256
    with tempfile.TemporaryDirectory() as d:
        stream = os.path.join(d, ".records.jsonl")
        with open(stream, "w") as f:
            f.write("[]\n")
        with open(rk.key_path(stream), "w") as f:
            json.dump({rk.CONTENT: "f" * 64}, f)
        try:
            def no_data(*_a):
                raise SystemExit("no CSV files")
            ldbc_records.source = no_data
            case("code part with no probe data here: by files", rk.code_part("ldbc", "30") == files)
            ldbc_records.source = lambda *_a: (d, {"x": [1, 2]})
            records_mod.records_file = lambda *_a, **_k: stream
            rk.probe_tree_sha256 = lambda dataset, code_root=None, python=None: "t" * 64
            got = rk.code_part("ldbc", "30")
            case("code part at sf30 with the probe here: by its bytes, both probes",
                 got == {**probe, "probe_content_sha256": "f" * 64}, got)
            got = rk.code_part("ldbc", "0.1", variant="fat128fixed")
            case("code part of a data variant at the probe scale: by the probe", got.get("by") == "probe", got)
        finally:
            ldbc_records.source, records_mod.records_file, rk.probe_tree_sha256 = real_source, real_records_file, real_tree

    # earlier versions: upgraded when the key's files are the dumping code and still fresh
    v3 = {k: v for k, v in big.items() if k != "code"}
    v3["version"] = 3
    v4 = {**big, "version": 4, "code": {k: v for k, v in probe.items() if not k.startswith("probe_tree")}}
    case("a version-3 key fresh by its files is upgraded", rk.upgraded(v3, big) is big)
    case("a version-4 key fresh by its files is upgraded (its files are the dumping code)", rk.upgraded(v4, big) is big)
    adopted = {**v4, "adopted_by_probe": {"witness": "w"}}
    case("a version-4 key adopted by probe is not upgraded (its files name the adopting code)",
         rk.upgraded(adopted, big) is None)
    case("a version-3 key whose module text moved is not upgraded (and is refused)",
         rk.upgraded({**v3, "python": edited}, big) is None and rk.moved({**v3, "python": edited}, big) != [])
    case("a version-3 key whose CSVs moved is not upgraded", rk.upgraded({**v3, "raw": {"dynamic/person_0_0.csv": [10, 21]}},
                                                                          big) is None)
    case("a version-2 key is never upgraded", rk.upgraded({**v3, "version": 2}, big) is None)
    case("a version-5 key is never 'upgraded'", rk.upgraded(big, big) is None)
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, ".records.jsonl")
        with open(cache, "w") as f:
            f.write("[1]\n")
        rk.write(cache, v3)
        old_ids = rk.stream_ids(rk.key_path(cache))
        rk.upgrade_key(cache, big)
        with open(rk.key_path(cache)) as f:
            new = json.load(f)
        case("an upgraded key keeps the stream's bytes (hashed when the old key names none) and the old ids",
             new[rk.CONTENT] == rk.file_sha256(cache) and new[rk.SAME_CONTENT] == [old_ids] and new["code"] == probe, new)

    # --adopt-by-probe: the dumping code's bytes for both probes equal today's
    real_check, real_tree = rk.check_old_tree, rk.probe_tree_sha256
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.jsonl")
        with open(w, "w") as f:
            f.write("[]\n")
        today = {**big, "python": edited, "cpp": other_reader}        # the code moved since the cache was dumped
        same = {**v3, "sf": "0.1", "raw": {"other": [1, 1]}, rk.CONTENT: "c" * 64}
        try:
            rk.check_old_tree = lambda tree, code: {"commit": "a" * 40, "binary_git_sha": "a" * 40}
            rk.probe_tree_sha256 = lambda dataset, code_root=None, python=None: "t" * 64
            got = rk.probe_adoption(v3, today, [(w, same)], old_tree=d)
            case("adoption of a version-3 key: a witness with its python, cpp and schema parts, and the old tree's dump "
                 "of the probe tree, both equal today's; the dumping code is kept",
                 got["probe_content_sha256"] == "c" * 64 and got["probe_tree_sha256"] == "t" * 64
                 and got["dumped_by"]["python"] == py and got["dumped_by"]["cpp"] == cpp and bool(got.get("old_tree")), got)
            moved_paths = {**same, "python": renamed}
            case("adoption: a witness whose python files moved with their text unchanged is taken (by content)",
                 rk.probe_adoption(v3, today, [(w, moved_paths)], old_tree=d)["probe_content_sha256"] == "c" * 64)
            rebuilt = {**same, "cpp": {**cpp, "binary_git_sha": "9" * 40}}
            case("adoption: a witness whose binary is another commit of the same reader sources is taken",
                 rk.probe_adoption(v3, today, [(w, rebuilt)], old_tree=d)["probe_content_sha256"] == "c" * 64)
            for part, val in (("python", edited), ("cpp", other_reader), ("schema", "x")):
                why = refused(lambda part=part, val=val: rk.probe_adoption(v3, today, [(w, {**same, part: val})], d))
                case(f"adoption: a witness whose {part} part differs is refused, and the parts compared are named",
                     why is not None and f"{part} differs" in why and "equal" in why, why)
            why = refused(lambda: rk.probe_adoption(v3, today, [(w, {**same, rk.CONTENT: "d" * 64})], d))
            case("adoption: the dumping code wrote other sf0.1 bytes: refused", why is not None and "other bytes" in why, why)
            rk.probe_tree_sha256 = lambda dataset, code_root=None, python=None: "u" * 64
            why = refused(lambda: rk.probe_adoption(v3, today, [(w, same)], d))
            case("adoption: the dumping code writes other bytes for the probe tree (a tab, an escape): refused",
                 why is not None and "probe tree" in why and "other bytes" in why, why)
            rk.probe_tree_sha256 = lambda dataset, code_root=None, python=None: "t" * 64
            why = refused(lambda: rk.probe_adoption(v3, today, [(w, same)]))
            case("adoption: no old tree and no probe-tree bytes in the old key: refused, naming --old-tree",
                 why is not None and "--old-tree" in why, why)
            why = refused(lambda: rk.probe_adoption({**v3, "raw": {"dynamic/person_0_0.csv": [10, 21]}}, today,
                                                    [(w, same)], d))
            case("adoption: a cache whose data moved too is refused", why is not None and "raw" in why, why)
            why = refused(lambda: rk.probe_adoption(v3, {**today, "code": files}, [(w, same)], d))
            case("adoption: a key not by the probe here is refused", why is not None and "not keyed by the probe" in why,
                 why)
            why = refused(lambda: rk.probe_adoption(v3, today, [], d))
            case("adoption: no witness: refused, with how to make one", why is not None and "--witness" in why, why)
            # a key adopted earlier: the dumping code from adopted_by_probe.dumped_by, else its witness's key file
            dumping = {"python": py, "cpp": cpp, "schema": live}
            kept = {**v4, "python": edited, "cpp": other_reader, "adopted_by_probe": {"dumped_by": dumping}}
            got = rk.probe_adoption(kept, today, [], d)
            case("re-adoption: the dumping code from adopted_by_probe.dumped_by, the sf0.1 bytes from the old key, the "
                 "earlier evidence kept", got["dumped_by"] == dumping and got["probe_content_from"] == "the old key"
                 and got["earlier"] == {"dumped_by": dumping}, got)
            rk.write(w, same)
            old_form = {**v4, "python": edited, "cpp": other_reader, "adopted_by_probe": {"witness": w}}
            got = rk.probe_adoption(old_form, today, [], d)
            case("re-adoption of a key adopted before dumped_by was kept: the dumping code from its witness's key",
                 got["dumped_by"] == {"python": py, "cpp": cpp, "schema": live}, got)
            os.remove(rk.key_path(w))
            why = refused(lambda: rk.probe_adoption(old_form, today, [], d))
            case("re-adoption: no dumped_by and the witness's key gone: refused", why is not None and "names no dumping"
                 in why, why)
        finally:
            rk.check_old_tree, rk.probe_tree_sha256 = real_check, real_tree

    # a stale probe stream is refused by records_file, not dumped again; a by-files key at a larger scale names the adoption
    real_path, real_compute, real_source = records_mod.records_cache_path, rk.compute, ldbc_records.source
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, ".records.jsonl")
        with open(cache, "w") as f:
            f.write("[1]\n")
        try:
            records_mod.records_cache_path = lambda *_a, **_k: cache
            ldbc_records.source = lambda *_a: (d, want["raw"])
            rk.write(cache, {**want, "python": edited, rk.CONTENT: "c" * 64})
            rk.compute = lambda *_a, **_k: want
            why = refused(lambda: records_mod.records_file("ldbc", "0.1", fresh=False))
            case("a probe stream stale by its files is refused with its re-dump command, not dumped again",
                 why is not None and "STALE" in why and "probe stream" in why and "KVEVAL_RECORDS_FRESH=1" in why
                 and open(cache).read() == "[1]\n", why)
            rk.write(cache, {**want, "sf": "30", rk.CONTENT: "c" * 64})
            rk.compute = lambda *_a, **_k: {**big, "sf": "30"}
            got = records_mod.records_file("ldbc", "30", fresh=False)
            n = [x for x in stale.notes() if x["kind"] == "stream_keyed_by_content" and "--sf 30" in x["detail"]]
            case("a larger stream keyed by files, now by the probe: used as it is, marked, naming --adopt-by-probe",
                 got == cache and n and "--adopt-by-probe" in n[-1]["detail"] and open(cache).read() == "[1]\n",
                 n[-1]["detail"] if n else "no note")
            rk.write(cache, {**want, "sf": "30", "raw": {"dynamic/person_0_0.csv": [10, 21]}, rk.CONTENT: "c" * 64})
            rk.compute = lambda *_a, **_k: {**big, "sf": "30", "raw": {"dynamic/person_0_0.csv": [10, 22]}}
            why = refused(lambda: records_mod.records_file("ldbc", "30", fresh=False))
            case("a larger stream whose data moved (and its code): refused", why is not None and "raw" in why, why)
            os.remove(rk.key_path(cache))
            rk.compute = lambda *_a, **_k: {**big, "sf": "30"}
            got = records_mod.records_file("ldbc", "30", fresh=False)
            key = json.load(open(rk.key_path(cache)))
            case("a larger stream with no key: keyed by its content, the reason kept in its key, used",
                 got == cache and key.get(rk.KEYED_BY_CONTENT) and key.get(rk.CONTENT) == rk.file_sha256(cache), key.get(rk.KEYED_BY_CONTENT))
            with stale.scope() as later:         # the next use (a unit's own notes: the warning is said once per process)
                records_mod.records_file("ldbc", "30", fresh=False)
            case("its next use is marked again (stream_keyed_by_content, from the key)",
                 any(x["kind"] == "stream_keyed_by_content" and "no freshness key" in x["detail"] for x in later), later)
            os.remove(rk.key_path(cache))
            rk.compute = lambda *_a, **_k: want
            why = refused(lambda: records_mod.records_file("ldbc", "0.1", fresh=False))
            case("the probe stream with no key: refused (every larger key holds its bytes)",
                 why is not None and "no freshness key" in why and not os.path.exists(rk.key_path(cache)), why)
        finally:
            records_mod.records_cache_path, rk.compute, ldbc_records.source = real_path, real_compute, real_source

    # the probe tree's dump, by today's code and by a planted copy of it: a docstring keeps it, a change to how a
    # value is read or written moves it (sf0.1 holds no tab: the tab plant leaves sf0.1's bytes as they are)
    # (the frozen Python parser: the copy has no binary; both parsers give the probe tree the same bytes, checked first)
    from kvevaluator.datasets.ldbc.loader import _cpp_binary
    cpp_sha = rk.probe_tree_sha256("ldbc") if _cpp_binary() else None
    os.environ["KVEVAL_PY_PARSER"] = "1"
    rk._TREE_SHA.clear()
    today_sha = rk.probe_tree_sha256("ldbc")
    case("the probe tree dumps; the C++ reader and the frozen Python parser give it the same bytes"
         + ("" if cpp_sha else " (no binary here: the parser only)"), len(today_sha) == 64 and cpp_sha in (None, today_sha),
         (cpp_sha, today_sha))
    plants = {
        "a docstring added (control: same bytes)": ("datasets/ldbc/loader.py", '"""', '"""A planted docstring line.\n', 1),
        "a tab read as a space": ("datasets/ldbc/loader.py", None, PLANT_STREAM.format(fix='x.replace("\\t", " ")'), 0),
        "whitespace trimmed from every value": ("datasets/ldbc/loader.py", None, PLANT_STREAM.format(fix="x.strip()"), 0),
        "the writer escapes non-ASCII": ("measure/records_dump.py", "ensure_ascii=False", "ensure_ascii=True", 1),
    }
    for what, (rel, old, new, count) in plants.items():
        with tempfile.TemporaryDirectory() as root:
            copy_code(root)
            path = os.path.join(root, "python", "kvevaluator", rel)
            text = open(path).read()
            text = text + new if old is None else text.replace(old, new, count)
            with open(path, "w") as f:
                f.write(text)
            os.environ["PYTHONPATH"] = os.path.join(root, "python")      # the copy has no venv of its own
            try:
                got = rk.probe_tree_sha256("ldbc", code_root=root, python=sys.executable)
            finally:
                del os.environ["PYTHONPATH"]
        same_bytes = got == today_sha
        case(f"probe tree, planted: {what}: {'the same bytes' if same_bytes else 'other bytes'}",
             same_bytes if what.startswith("a docstring") else not same_bytes, got[:12])
    del os.environ["KVEVAL_PY_PARSER"]
    rk._TREE_SHA.clear()

    # the dump's closure stays the reader's own code: a module outside these (a format, a registry, kvcost) would make
    # every edit there re-dump the record stream (an SF30 dump is about 51 min)
    allowed = ("python/kvevaluator/measure/records_dump.py", "python/kvevaluator/jsonbytes.py",
               "python/kvevaluator/datasets/ldbc/", "inputs/schemas/ldbc/", "python/kvevaluator/schema/",
               "python/kvevaluator/paths.py",
               "python/kvevaluator/__init__.py", "python/kvevaluator/datasets/__init__.py",
               "python/kvevaluator/measure/__init__.py")
    outside = sorted(f for fs in rk.python_part("ldbc").values() for f in fs if not f.startswith(allowed))
    case("the dump's import closure stays inside its reader (records_dump, jsonbytes, datasets/ldbc, schema, paths)",
         not outside, f"outside: {outside}")

    n_ok = sum(results)
    print(f"check_records_key {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if results and n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
