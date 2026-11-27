#!/usr/bin/env python3
"""A record stream's content_sha256 (records_key.py) on a real dumped file (check_predict_int_range's 10-person
fixture), through the C++ reader and the tally library:
  reader    graph-stats and tally copy the key's content_sha256 into their records block; a tally adds the sha of the
            schema it encodes with (schema_sha256)
  schema    an entry made under one schema is refused under another, though the stream's bytes are the same (one index's
            width changed in a copy of schema.json)
  adopt     an entry made before the bytes were recorded (no content, no schema) is kept after a re-dump of the same
            bytes (same_content_as, built by same_content_ids from the stream set aside): its key and its tally's records
            block are stamped with the content and the schema; under another schema, or when the set-aside bytes
            differ, it is refused
No store. Seconds (two graph-stats and a few tallies on 70 records).

    python tests/unit/check_record_stream_content.py
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

import check_predict_int_range as F  # (its 10-person fixture and stream writer)
import check_predict_rules as R  # (the schema and a spec)
from kvevaluator.measure import records_key as K
from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.predict import tally_library as TL

fails, cases = [], 0


def case(what, ok, got=""):
    global cases
    cases += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + ("" if ok else f": {str(got)[:200]}"))
    if not ok:
        fails.append(what)


def outcome(fn):
    try:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            fn()
        return "read"
    except SystemExit as e:
        return str(e)


def old_key(schema_sha=None):
    """a key of the form before content_sha256: its python part names schema.json by its sha (`schema_sha`: another) and
    its path in this tree (adoption compares the sources, paths included)"""
    return {"version": 3, "sf": "fixture", "schema": "s",
            "python": {schema_sha or K.file_sha256(R.SCHEMA): [os.path.relpath(R.SCHEMA, ROOT)]},
            "cpp": {"binary_git_sha": "a" * 40, "binary_dirty": False, "files": {}}, "raw": {}}


def library(path, records, schema=R.SCHEMA, root=None):
    with contextlib.redirect_stderr(io.StringIO()):
        return TL.Library(path, records, schema=schema, quiet=True, root=root or os.path.dirname(path))


def main():
    spec = {"version": 1, "vertex_props": {"group": "item"}}
    with tempfile.TemporaryDirectory() as t:
        rec = os.path.join(t, "records.jsonl")
        F.write_stream(rec, F.fixture())
        content = K.file_sha256(rec)
        K.write(rec, {**old_key(), K.CONTENT: content})

        # reader: the C++ side copies the key's content (and a tally its schema) into its records block
        stats = os.path.join(t, "stats.json")
        out = subprocess.run([str(cpp_bin()), "graph-stats", "--schema", R.SCHEMA, "--records", rec],
                             capture_output=True, text=True, check=True).stdout
        open(stats, "w").write(out)
        got = json.load(open(stats))["records"]
        case("reader: graph-stats names the stream's content_sha256", got.get("content_sha256") == content, got)
        tally = subprocess.run([str(cpp_bin()), "tally", "--schema", R.SCHEMA, "--spec", R.SPEC, "--records", rec],
                               capture_output=True, text=True, check=True).stdout
        got = json.loads(tally)["records"]
        case("reader: tally names the content and its schema's sha",
             got.get("content_sha256") == content and got.get("schema_sha256") == K.file_sha256(R.SCHEMA), got)

        # schema: one index's width changed, the same stream bytes
        lib = library(os.path.join(t, "lib"), rec)
        if lib.binary["dirty"]:
            print("  (the library cases need a clean binary: a dirty binary's entry is always built again)")
        else:
            outcome(lambda: lib.entry(spec))
            other = os.path.join(t, "schema-other.json")
            sch = json.load(open(R.SCHEMA))
            idx = next(i for i in sch["indexes"] if i.get("width"))
            idx["width"] += 1
            json.dump(sch, open(other, "w"))
            got = outcome(lambda: library(os.path.join(t, "lib"), rec, schema=other).entry(spec))
            case("schema: an entry made under another schema is refused, the bytes the same", "the schema changed" in got,
                 got)
            got = outcome(lambda: library(os.path.join(t, "lib"), rec).entry(spec))
            case("schema: under its own schema it is read (the control)", got == "read", got)

            # adopt: an entry from before content keys, a re-dump of the same bytes
            def made_before(d):
                """an entry as a library before content_sha256 made it: no content, no schema, in key or tally"""
                lib_old = library(d, rec)
                outcome(lambda: lib_old.entry(spec))
                stem = os.path.join(d, f"setting-{TL.sha256_bytes(TL.canonical(spec).encode())[:12]}")
                k, tl = json.load(open(stem + ".key.json")), json.load(open(stem + ".json"))
                for x in ("records_content_sha256", "schema_sha256"):
                    k.pop(x, None)
                for x in ("content_sha256", "schema_sha256"):
                    tl["records"].pop(x, None)
                json.dump(k, open(stem + ".key.json", "w"))
                json.dump(tl, open(stem + ".json", "w"))
                return stem

            def redump(aside_bytes_same=True, schema_sha=None):
                """the stream set aside under the old key (its bytes the same or not), the new key listing it"""
                aside = os.path.join(t, "records.aside.jsonl")
                shutil.copy(rec, aside)
                if not aside_bytes_same:
                    with open(aside, "ab") as f:
                        f.write(b"\n")
                K.write(aside, old_key(schema_sha))
                same = K.same_content_ids(aside, content)
                new = {**old_key(), "raw": {"x": [1, 2]}, K.CONTENT: content}     # the key moved: another dump
                if same:
                    new[K.SAME_CONTENT] = same
                K.write(rec, new)

            K.write(rec, old_key())
            stem = made_before(os.path.join(t, "lib-old"))
            redump()
            got = outcome(lambda: library(os.path.join(t, "lib-old"), rec).entry(spec))
            k, tl = json.load(open(stem + ".key.json")), json.load(open(stem + ".json"))["records"]
            case("adopt: an older entry is read after a re-dump of the same bytes", got == "read", got)
            case("adopt: its key and its tally are stamped with the content and the schema",
                 k.get("records_content_sha256") == content and tl.get("content_sha256") == content
                 and tl.get("schema_sha256") == K.file_sha256(R.SCHEMA) and k.get("schema_sha256"), (k, tl))
            case("adopt: the stamped tally and new statistics name one stream (same_records)",
                 TL.same_records(tl, {**json.load(open(stats))["records"], "lines": tl.get("lines")}), tl)

            K.write(rec, old_key())
            made_before(os.path.join(t, "lib-old2"))
            redump(aside_bytes_same=False)
            got = outcome(lambda: library(os.path.join(t, "lib-old2"), rec).entry(spec))
            case("adopt: when the set-aside bytes differ, the older entry is refused", "another record stream" in got, got)

            K.write(rec, old_key("9" * 64))      # made beside another schema.json: same sources as its own stream
            made_before(os.path.join(t, "lib-old3"))
            redump(schema_sha="9" * 64)
            got = outcome(lambda: library(os.path.join(t, "lib-old3"), rec).entry(spec))
            case("adopt: an older entry from a dump beside another schema.json is refused", "another record stream" in got,
                 got)
    print(f"check_record_stream_content: {'PASS' if not fails else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 1 if fails or cases == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
