#!/usr/bin/env python3
"""LinkBench's fetch step (its manifest's fetch_module; `just fetch-dataset linkbench=<scale>`): LinkBench's data is
generated, not downloaded. Two steps, each only if missing: the data (generate.sh, beside this file: build the LinkBench
tool, its LOAD phase to nodes.jsonl / links.jsonl, its seeded REQUEST phase logged to requests.jsonl, the data's
manifest.json, the anchors), then the params (gen_linkbench_params.py: the request log, its samples and mix.json, each
file checked against the tracked inputs/params/linkbench/<scale>/linkbench/params.sha256.txt).

What is checked against tracked hashes: the params (params.sha256.txt, every scale); n1m's three data files (the
recorded run's lines and sha256, inputs/data-sources/linkbench/n1m/clock-20261012.csv.json). A scale with no recorded
run (tiny) has its data files checked against the data dir's own manifest.json only: no tracked hash.

The fetch step's four calls (kvevaluator.fetch_dataset): plan(scale), missing(scale), ensure(scale), check(scale).
"""
import json
import os
import subprocess
import sys
import time

from kvevaluator.datasets import data_in, params_in
from kvevaluator.datasets.linkbench import data_manifest
from kvevaluator.paths import INPUTS_DIR

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = INPUTS_DIR / "data-sources" / "linkbench"
EXPECTED = json.load(open(SOURCES / "expected.json"))   # per scale: disk bytes and recorded seconds of each step
RECORDED = {"n1m": SOURCES / "n1m" / "clock-20261012.csv.json"}   # a scale's recorded run: its files' lines and sha256


def data_dir(scale):
    return data_in("linkbench", scale) / "data"


def _params_dir(scale):
    return data_in("linkbench", scale) / "params"


def steps(scale):
    """[(step name, done?)] in order: the data (the three JSONL files, their manifest, the anchors), the params"""
    d = data_dir(scale)
    data = all((d / f).is_file() for f in (*data_manifest.FILES, "manifest.json")) \
        and (data_in("linkbench", scale) / "anchors.txt").is_file()
    return [("data", data), ("params", (_params_dir(scale) / "request_log.jsonl").is_file())]


def missing(scale):
    """[what is not in place] of the scale (existence only, nothing hashed)"""
    where = {"data": data_dir(scale), "params": _params_dir(scale)}
    return [f"{name} ({where[name]})" for name, done in steps(scale) if not done]


def plan(scale):
    """what ensure(scale) would do: {present, to_fetch, download_bytes, disk_bytes, seconds, date}, from
    inputs/data-sources/linkbench/expected.json (the download is the tool's clone and its Maven packages, once)"""
    st, rec = steps(scale), EXPECTED["scales"].get(scale, {})
    todo = [n for n, done in st if not done]
    return {"present": [n for n, done in st if done], "to_fetch": todo,
            "download_bytes": rec.get("download_bytes") if "data" in todo else 0,   # None: not recorded
            "disk_bytes": sum(rec.get(n, {}).get("disk_bytes", 0) for n in todo),
            "seconds": sum(rec.get(n, {}).get("seconds", 0) for n in todo) if rec else None, "date": rec.get("date")}


def _run(what, argv):
    t0 = time.monotonic()
    print(f"[linkbench] {what}: {' '.join(str(x) for x in argv)}", flush=True)
    rc = subprocess.run([str(x) for x in argv]).returncode
    print(f"[linkbench] {what}: rc={rc} ({time.monotonic() - t0:.0f} s)", flush=True)
    return [] if rc == 0 else [f"{what} failed (rc={rc})"]


def ensure(scale):
    """each step only if missing, each printed with its time; [problem] (a failed step stops the next)"""
    for name, done in steps(scale):
        if done:
            print(f"[linkbench] {scale}: {name}: in place", flush=True)
            continue
        if name == "data":
            bad = _run("data", ["bash", os.path.join(HERE, "generate.sh"), "--scale", scale])
        else:
            bad = _run("params", [sys.executable, os.path.join(HERE, "gen_linkbench_params.py"), "--scale", scale])
        if bad:
            return bad
    return []


def _check_data(scale):
    """the three data files against the recorded run's hashes (n1m) or the data dir's own manifest; ([problem], files)"""
    bad, n = [], 0
    d = data_dir(scale)
    mpath = d / "manifest.json"
    own = json.load(open(mpath))["files"] if mpath.is_file() else {}
    recorded = json.load(open(RECORDED[scale]))["files"] if scale in RECORDED else None
    want = recorded or own
    for f in data_manifest.FILES:
        if not (d / f).is_file():
            bad.append(f"{d / f}: missing")
            continue
        lines, sha = data_manifest.digest(d / f)
        n += 1
        if f not in want:
            bad.append(f"{d / f}: not in {RECORDED[scale] if recorded else mpath}")
        elif (want[f]["lines"], want[f]["sha256"]) != (lines, sha):
            bad.append(f"{d / f}: {lines} lines, sha256 {sha}; want {want[f]['lines']}, {want[f]['sha256']}")
    if recorded is None:
        print(f"[linkbench] {scale}: no recorded run: the data files are checked against {mpath} (no tracked hash)")
    return bad, n


def _check_params(scale):
    """every params file against the tracked params.sha256.txt; ([problem], files)"""
    bad, n = [], 0
    listing = params_in("linkbench", scale, "linkbench") / "params.sha256.txt"
    p = _params_dir(scale)
    if not listing.is_file():
        print(f"[linkbench] {scale}: no tracked {listing}: the params are not checked")
        return bad, n
    for line in open(listing):
        if not line.strip():
            continue
        h, name = line.split()
        if not (p / name).is_file():
            bad.append(f"{p / name}: missing")
            continue
        n += 1
        if data_manifest.digest(p / name)[1] != h:
            bad.append(f"{p / name}: sha256 differs from {listing}")
    return bad, n


def check(scale):
    """the scale's data and params against their hashes, generating nothing; ([problem], files checked)"""
    (bad, n), (bad2, n2) = _check_data(scale), _check_params(scale)
    bad, n = bad + bad2, n + n2
    for b in bad[:20]:
        print(f"  BAD linkbench {scale}: {b}")
    print(f"[linkbench] {scale}: {n} files checked: {'OK' if not bad else f'{len(bad)} problems'}", flush=True)
    return bad, n
