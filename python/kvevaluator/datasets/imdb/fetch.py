#!/usr/bin/env python3
"""IMDb's fetch step (its manifest's fetch_module; `just fetch-dataset imdb=<tag>`): the IMDb dump the Join Order
Benchmark paper used and the benchmark's query texts, then the full tag's tables, then the tag's tables, each step only
if missing. JOB's reference answers on a tag's tables are JOB's own data step (kvevaluator.workloads.job.ensure), which
`just fetch-dataset` runs after this one.

Both downloads are pinned in inputs/data-sources/imdb/pins.json: the dump by its size and sha256, the queries by the
full commit sha in the URL and the sha256 of every file the workload reads. Any other bytes are refused, naming the
file and both hashes. Neither is redistributed by this repo (IMDb's data is for noncommercial use). A tag is built when
its manifest.json is there (build_tables.py writes the dir under another name and renames it last); the sample is cut
from the full tag's tables. A fresh machine, sample1pct: about 3 min, a 1.26 GB download, 11.6 GB on disk.

The fetch step's four calls (kvevaluator.fetch_dataset): plan(tag), missing(tag), ensure(tag), check(tag).
"""
import hashlib
import json
import os
import subprocess
import sys
import time

from kvevaluator.datasets.imdb import paths
from kvevaluator.paths import INPUTS_DIR

SOURCES = INPUTS_DIR / "data-sources" / "imdb"
PINS_FILE = str(SOURCES / "pins.json")
PINS = json.load(open(PINS_FILE))
EXPECTED = json.load(open(SOURCES / "expected.json"))   # per step: its disk bytes and recorded seconds
DUMP_URL, DUMP_BYTES, DUMP_SHA256 = PINS["dump"]["url"], PINS["dump"]["bytes"], PINS["dump"]["sha256"]
QUERIES_URL, QUERY_FILES = PINS["queries"]["url"], PINS["queries"]["files"]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def refuse(path, got, want):
    raise SystemExit(f"{path}: sha256 {got}, pinned {want} ({os.path.relpath(PINS_FILE)}): not the input the plans "
                     f"were checked on -- remove it and fetch again, or update the pin knowingly")


def fetch_dump(raw):
    done = os.path.join(raw, "fetched.json")
    if os.path.exists(done):                 # extracted before: its recorded hash must still be the pinned one
        got = json.load(open(done))["sha256"]
        if got != DUMP_SHA256:
            refuse(done, got, DUMP_SHA256)
        return
    os.makedirs(raw, exist_ok=True)
    tgz = os.path.join(paths.ROOT, "imdb.tgz")
    if not os.path.exists(tgz):
        subprocess.run(["curl", "-sSL", "--fail", "-o", tgz + ".part", DUMP_URL], check=True)
        os.rename(tgz + ".part", tgz)
    if os.path.getsize(tgz) != DUMP_BYTES:
        raise SystemExit(f"{tgz}: {os.path.getsize(tgz)} bytes, pinned {DUMP_BYTES} ({os.path.relpath(PINS_FILE)}): "
                         f"a truncated or other download -- remove it and fetch again")
    got = sha256(tgz)
    if got != DUMP_SHA256:
        refuse(tgz, got, DUMP_SHA256)
    subprocess.run(["tar", "-xzf", tgz, "-C", raw], check=True)
    with open(done, "w") as f:
        json.dump({"url": DUMP_URL, "sha256": got, "csv_files": sorted(x for x in os.listdir(raw) if x.endswith(".csv"))}, f, indent=1)


def query_problems(qdir):
    """[problem] of the query texts in place against their pins"""
    out = []
    for name, want in sorted(QUERY_FILES.items()):   # 114 small files, milliseconds
        path = os.path.join(qdir, name)
        if not os.path.exists(path):
            out.append(f"{path}: missing, pinned in {os.path.relpath(PINS_FILE)}")
        elif (got := sha256(path)) != want:
            out.append(f"{path}: sha256 {got}, pinned {want}")
    return out


def fetch_queries(qdir):
    if not os.path.exists(os.path.join(qdir, "schema.sql")):
        os.makedirs(qdir, exist_ok=True)
        tar = os.path.join(paths.ROOT, "job-queries.tar.gz")
        subprocess.run(["curl", "-sSL", "--fail", "-o", tar, QUERIES_URL], check=True)
        subprocess.run(["tar", "-xzf", tar, "--strip-components=1", "-C", qdir], check=True)
    bad = query_problems(qdir)
    if bad:
        raise SystemExit(f"{bad[0]} -- remove {qdir} and fetch again ({len(bad)} problems)")
    n = len([x for x in os.listdir(qdir) if x[0].isdigit() and x.endswith(".sql")])
    if n != 113:
        raise SystemExit(f"{qdir}: {n} query files, expected 113")


def tag_built(tag):
    return os.path.exists(os.path.join(paths.tag_dir(tag), "manifest.json"))


def steps(tag):
    """[(step name, done?)] of a tag, in order: the downloads, the full tag's tables, the tag's (the sample's)"""
    tags = ["full"] if tag == "full" else ["full", tag]
    fetched = os.path.exists(os.path.join(paths.raw_dir(), "fetched.json")) \
        and os.path.exists(os.path.join(paths.queries_dir(), "schema.sql"))
    # built tables need no download: a dump removed after the build is not fetched again
    return [("download", fetched or all(tag_built(t) for t in tags))] + [(f"tables {t}", tag_built(t)) for t in tags]


def missing(tag):
    """[what is not in place] of a tag (existence only, nothing hashed)"""
    return [f"{name} ({paths.tag_dir(tag) if name.startswith('tables') else paths.ROOT})" for name, done in steps(tag)
            if not done]


def plan(tag):
    """what ensure(tag) would do: {present, to_fetch (step names), download_bytes, disk_bytes, seconds, date}, from the
    pins and inputs/data-sources/imdb/expected.json; nothing is hashed"""
    st = steps(tag)
    todo = [n for n, done in st if not done]
    rec = EXPECTED["steps"]
    return {"present": [n for n, done in st if done], "to_fetch": todo,
            "download_bytes": DUMP_BYTES if "download" in todo else 0,
            "disk_bytes": sum(rec[n]["disk_bytes"] for n in todo),
            "seconds": sum(rec[n]["seconds"] for n in todo), "date": EXPECTED["date"]}


def _step(what, *argv):
    t0 = time.monotonic()
    print(f"[imdb] {what}", flush=True)
    subprocess.run([sys.executable, *argv], check=True)
    print(f"[imdb] {what}: done ({time.monotonic() - t0:.0f} s)", flush=True)


def ensure(tag):
    """each step of the tag only if missing, each printed with its time; [problem] (a refused download stops it)"""
    here = os.path.dirname(os.path.abspath(__file__))
    for name, done in steps(tag):
        if done:
            print(f"[imdb] {tag}: {name}: in place", flush=True)
        elif name == "download":
            t0 = time.monotonic()
            print(f"[imdb] download (pinned): {DUMP_URL} ({DUMP_BYTES / 1e9:.2f} GB) and the query texts", flush=True)
            fetch_dump(paths.raw_dir())
            fetch_queries(paths.queries_dir())
            print(f"[imdb] download: done, every file as pinned ({time.monotonic() - t0:.0f} s)", flush=True)
        else:
            _step(name, os.path.join(here, "build_tables.py"), "--tag", name.split()[1])
    return []


def check(tag):
    """the tag's inputs against their pins, downloading nothing: the dump's recorded sha256 (and the archive's own bytes
    when it is kept), every query text's sha256, and each tag's tables built from the pinned dump (its manifest's
    `dump`); ([problem], files checked). The built tables have no tracked hash: their manifest's dump sha is checked"""
    bad, n = [], 0
    done = os.path.join(paths.raw_dir(), "fetched.json")
    tags = ["full"] if tag == "full" else ["full", tag]
    if not os.path.exists(done):
        if not all(tag_built(t) for t in tags):      # built tables hold their dump's sha (checked below)
            bad.append(f"{done}: missing (the dump is not fetched)")
    else:
        n += 1
        if (got := json.load(open(done))["sha256"]) != DUMP_SHA256:
            bad.append(f"{done}: sha256 {got}, pinned {DUMP_SHA256}")
    tgz = os.path.join(paths.ROOT, "imdb.tgz")
    if os.path.exists(tgz):
        n += 1
        if os.path.getsize(tgz) != DUMP_BYTES or sha256(tgz) != DUMP_SHA256:
            bad.append(f"{tgz}: not the pinned dump (size or sha256)")
    q = query_problems(paths.queries_dir())
    bad += q
    n += len(QUERY_FILES) - sum(": missing" in x for x in q)
    for t in tags:
        m = os.path.join(paths.tag_dir(t), "manifest.json")
        if not os.path.exists(m):
            bad.append(f"{m}: missing (the {t} tables are not built)")
            continue
        n += 1
        if (got := json.load(open(m)).get("dump")) != DUMP_SHA256:
            bad.append(f"{m}: built from dump {got}, pinned {DUMP_SHA256}")
    for b in bad:
        print(f"  BAD imdb {tag}: {b}")
    print(f"[imdb] {tag}: {n} files checked against the pins: {'OK' if not bad else f'{len(bad)} problems'}", flush=True)
    return bad, n
