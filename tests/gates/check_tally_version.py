#!/usr/bin/env python3
"""A tally change needs a tally version bump: the seven diagonal tallies at sf0.1 (kvevaluator tally over the sf0.1
record stream; seconds) must equal the tracked ones in tests/fixtures/golden/tally_sf0.1.json while the binary's
kTallyVersion (cli/tally_version.h) equals the fixture's. The tally library reuses an entry of the same version whatever
binary made it (python/kvevaluator/predict/tally_library.py), so a counting change made without a bump would serve stale
tallies; this gate fails it. Compared, per diagonal: the sha256 of every group, chunk family, locality row, total, the
spec and the stream's line count (wall time, provenance and the stream's path aside), and its totals and row counts
in the clear, so a failure names what moved. A planted wrong value must be caught (--plant).

    python tests/gates/check_tally_version.py                   # needs the sf0.1 CSVs (`just fetch-dataset ldbc=0.1`)
    python tests/gates/check_tally_version.py --update          # in the commit that bumps kTallyVersion: re-make it
    python tests/gates/check_tally_version.py --plant           # the control: one stored value off by one: must FAIL
    python tests/gates/check_tally_version.py --allow-dirty NAME   # a named control: the result is not a gate
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from kvevaluator.measure import records_key
from kvevaluator.capture.binary import binary_dirty_verdict
from kvevaluator.measure.records import records_file
from kvevaluator.paths import ROOT, cpp_bin
from kvevaluator.predict import tally_library as TL
from kvcost.predict_accuracy import uniform_49_tallies as U

# the seven diagonal specs, from the study that owns them (loaded by file: experiments/ is not a package)

FIXTURE = ROOT / "tests" / "fixtures" / "golden" / "tally_sf0.1.json"
SF = "0.1"
KEPT = ("format", "spec", "groups", "chunks", "locality", "totals")


def schema_moved(have, now):
    """the schema (schema.json in the stream's key) differs between the fixture's key and this one"""
    def schema(k):
        return next((h for h, paths in k.get("python", {}).items() if any(p.endswith(records_key._SCHEMA_JSON) for p in paths)), None)
    return schema(have) != schema(now)


def what_moved(have, now):
    """which input of the tallies moved between the fixture's stream key and this one: the stream's bytes, the schema
    (schema.json in the key), or neither (a code change only: a tally that then differs is a counting change)"""
    hb, nb = have.get(records_key.CONTENT), now.get(records_key.CONTENT)
    nbytes = "unknown (the fixture's key names no bytes)" if not (hb and nb) else ("moved" if hb != nb else "the same")
    nschema = "moved" if schema_moved(have, now) else "the same"
    tail = (" -- neither moved: if a tally now differs, that is a counting change: bump kTallyVersion first"
            if nbytes == "the same" and nschema == "the same" else "")
    return f"the stream's bytes {nbytes}, the schema {nschema}{tail}"


def comparable(t):
    """what a tally counts, as the fixture keeps it: the sha256 of its rows, totals, spec and the stream's line count,
    and beside it the totals and row counts in the clear"""
    kept = {k: t[k] for k in KEPT}
    kept["records_lines"] = t["records"]["lines"]
    text = json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "totals": t["totals"], "records_lines": kept["records_lines"],
            "rows": {"groups": len(t["groups"]), "chunks": len(t["chunks"]), "locality": len(t["locality"])}}


def tally(setting, records, tmp):
    spec_path = os.path.join(tmp, f"{setting}.json")
    with open(spec_path, "w") as f:
        json.dump(U.diagonal_spec(setting), f, indent=1, sort_keys=True)
    r = subprocess.run([str(cpp_bin()), "tally", "--schema", str(U.SCHEMA), "--spec", spec_path, "--records", records],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)


def differences(a, b, path=""):
    """the paths where two JSON values differ"""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for k in sorted(set(a) | set(b)):
            out += differences(a.get(k), b.get(k), f"{path}.{k}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: {len(a)} vs {len(b)} entries"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += differences(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [f"{path}: {a!r} vs {b!r}"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--update", action="store_true", help="write the fixture from this binary (after a version bump)")
    ap.add_argument("--plant", action="store_true", help="the control: one stored value off by one (must exit 1)")
    ap.add_argument("--allow-dirty", default=None, metavar="NAME",
                    help="run a dirty binary as the named control (not a gate result; binary.binary_dirty_verdict)")
    a = ap.parse_args(argv)
    v = json.loads(subprocess.run([str(cpp_bin()), "--version"], capture_output=True, text=True, check=True).stdout)
    version, sha, dirty = v.get("tally_version"), v["binary_git_sha"], v["binary_dirty"]
    print(f"check_tally_version: binary {cpp_bin()} {sha[:10]} dirty={dirty}; tally version {version}")
    ok, msg = binary_dirty_verdict({"binary_git_sha": sha, "binary_dirty": dirty}, a.allow_dirty)
    print(f"check_tally_version: {msg}")
    if not ok:
        sys.exit(1)
    if version is None:
        sys.exit("check_tally_version: the binary prints no tally_version (built before versions): FAIL")
    records = records_file("ldbc", SF)
    with open(f"{records}.key.json") as f:
        stream_key = json.load(f)
    with tempfile.TemporaryDirectory() as tmp, ThreadPoolExecutor(len(U.SETTINGS)) as pool:
        got = dict(zip(U.SETTINGS, pool.map(lambda s: comparable(tally(s, records, tmp)), U.SETTINGS)))
    if a.update:
        with open(FIXTURE, "w") as f:
            json.dump({"tally_version": version, "sf": SF, "records_key": stream_key, "tallies": got}, f,
                      indent=1, sort_keys=True)
            f.write("\n")
        print(f"check_tally_version: wrote {FIXTURE} (tally version {version}, {len(got)} diagonals)")
        return 0
    with open(FIXTURE) as f:
        want = json.load(f)
    moved = TL.stream_moved(want["records_key"], stream_key)   # by the bytes, else by sources: the same bytes are the same stream
    if moved:
        sys.exit(f"check_tally_version: the sf0.1 record stream's key moved ({'; '.join(moved)}); {what_moved(want['records_key'], stream_key)}: "
                 f"re-make the fixture (--update) and check its diff: FAIL")
    if want["tally_version"] != version:
        sys.exit(f"check_tally_version: the binary is at tally version {version}, the fixture at {want['tally_version']}: "
                 f"re-make it (--update) in the commit that bumps kTallyVersion: FAIL")
    if a.plant:
        want["tallies"]["chunked"]["totals"]["keys"] += 1
        want["tallies"]["chunked"]["sha256"] = "0" * 64
        print("[control] chunked's stored totals: keys + 1 (and its sha256)")
    bad = 0
    for s in U.SETTINGS:
        diff = differences(want["tallies"][s], got[s])
        bad += bool(diff)
        n = sum(got[s]["rows"].values())
        print(f"   {s}: {n} rows, {len(diff)} differ{': ' + '; '.join(diff[:2]) if diff else ''}")
    ok = bad == 0 and len(got) == 7
    # the stream's bytes are the same here (else it stopped above): a schema change moves the tallies with no counting
    # change, so the fixture is re-made; with the schema the same too, the counting changed and the version is bumped
    advice = (" (the schema moved, not the counting: re-make the fixture (--update) and check its diff)"
              if schema_moved(want["records_key"], stream_key) else
              " (the tally changed: bump kTallyVersion in cpp/kvevaluator/src/cli/tally_version.h, then --update)")
    print(f"check_tally_version: {len(got)} diagonals at sf{SF}, tally version {version}: {bad} differ: "
          f"{'PASS' if ok else 'FAIL'}" + (advice if bad and not a.plant else "")
          + (" [control: must FAIL]" if a.plant else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
