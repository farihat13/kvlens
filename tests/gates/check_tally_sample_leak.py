#!/usr/bin/env python3
"""A sampled tally reads only its sample: change what lies outside it and the tally must not move. At sf0.1, for two
diagonals (Chunked 64 KB, Condensed), seeds 1-3, with and without the owner floor (100): the tally names its sample
(`kvevaluator tally --print-sample`); in a copy of the record stream every edge whose two ends are both outside the
sample is changed (one copy drops every other such edge; one changes each such edge's properties, an int doubled
plus one, a string written twice: writing an edge twice would be a parallel edge, which the schema refuses for a unique
edge type); a third changes the properties of every vertex outside the sample the same way (its id, label and
line kept); the sampled tally of each copy must
equal the one of the stream, row for row. Allowed to differ, named here in advance, each with its reason:
  records.*                                 the stream itself (its path, key and line count)
  owner_sample.kept.edges.<etype>.read      the edges the pass read (what entered the counts may not move)
  wall_s, provenance                        the run
A chunk family's first_id and last_chunk_value_bytes are not allowed to differ: the sample's own lowest owner, and a
last chunk assumed half full (last_chunk_assumed), read nothing outside the sample.
The vertex lines are not changed, so the floor's per-type counts (a pass over vertex lines) may not move.
The control: the tally's --plant leak_one_owner (one source outside the sample encoded whole) on one cell must FAIL.
It runs on the drop stream: the planted owner (the stream's first source outside the sample, a Person) has knows
edges whose one property is an int, whose stored size a changed value does not move, so the props stream cannot show it.
Opens no store; about two minutes (36 tallies of the changed streams, 12 of the stream, 2 planted).

    python tests/gates/check_tally_sample_leak.py [--jobs 8] [--allow-dirty NAME]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from kvevaluator.capture.binary import binary_dirty_verdict
from kvevaluator.measure.records import records_file
from kvevaluator.paths import cpp_bin
from kvcost.predict_accuracy import uniform_49_tallies as U


SF = "0.1"
SETTINGS = ("chunked", "condensed")
SEEDS = (1, 2, 3)
FLOORS = (0, 100)
CHANGES = ("drop", "props", "vprops")
PERCENT = 1
# the paths allowed to differ (see the docstring)
ALWAYS = (re.compile(r"^\.records(\.|$)"), re.compile(r"^\.wall_s$"), re.compile(r"^\.provenance(\.|$)"),
          re.compile(r"^\.owner_sample\.kept\.edges\.[^.]+\.read$"))


def differences(a, b, path=""):
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


def allowed(diff_path):
    return any(p.match(diff_path) for p in ALWAYS)


def tally(setting, records, tmp, seed, floor, plant=None, print_sample=None):
    spec_path = os.path.join(tmp, f"{setting}.json")
    if not os.path.exists(spec_path):
        with open(spec_path, "w") as f:
            json.dump(U.diagonal_spec(setting), f, indent=1, sort_keys=True)
    cmd = [str(cpp_bin()), "tally", "--schema", str(U.SCHEMA), "--spec", spec_path, "--records", records,
           "--owner-sample", str(PERCENT), "--seed", str(seed)]
    cmd += ["--owner-floor", str(floor)] if floor else []
    cmd += ["--print-sample", print_sample] if print_sample else []
    cmd += ["--plant", plant] if plant else []
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"tally {setting} seed {seed} floor {floor} on {os.path.basename(records)}: rc {r.returncode}: "
                           f"{r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")
    return json.loads(r.stdout)


def changed_stream(records, sample_path, change, out):
    """a copy of the stream: every edge whose two ends are both outside the sample dropped (every other one) or its
    properties changed; the count of changed edges"""
    with open(sample_path) as f:
        kept = {v for vs in json.load(f).values() for v in vs}
    n = k = 0
    grow = lambda d: {p: (2 * x + 1 if isinstance(x, int) and not isinstance(x, bool) else
                          x + x if isinstance(x, str) else x) for p, x in d.items()}
    with open(records) as src, open(out, "w") as dst:
        for line in src:
            if change == "vprops" and line.startswith('["v"'):
                r = json.loads(line)
                if r[1] not in kept and r[3]:
                    r[3] = grow(r[3])
                    line = json.dumps(r, separators=(",", ":")) + "\n"
                    k += 1
            elif change != "vprops" and line.startswith('["e"'):
                r = json.loads(line)
                if r[2] not in kept and r[3] not in kept:
                    n += 1
                    if change == "drop" and n % 2 == 0:
                        k += 1
                        continue
                    if change == "props" and r[4]:
                        r[4] = grow(r[4])
                        line = json.dumps(r, separators=(",", ":")) + "\n"
                        k += 1
            dst.write(line)
    return k


def compare(base, other, what):
    bad = [d for d in differences(base, other) if not allowed(d.split(": ")[0])]
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--allow-dirty", default=None, metavar="NAME")
    a = ap.parse_args(argv)
    v = json.loads(subprocess.run([str(cpp_bin()), "--version"], capture_output=True, text=True, check=True).stdout)
    print(f"check_tally_sample_leak: binary {cpp_bin()} {v['binary_git_sha'][:10]} dirty={v['binary_dirty']}")
    ok, msg = binary_dirty_verdict({"binary_git_sha": v["binary_git_sha"], "binary_dirty": v["binary_dirty"]}, a.allow_dirty)
    print(f"check_tally_sample_leak: {msg}")
    if not ok:
        return 1
    records = str(records_file("ldbc", SF))
    samples = [(s, f) for s in SEEDS for f in FLOORS]
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, ThreadPoolExecutor(a.jobs) as pool:
        # the stream's sampled tallies, and each sample's vertices
        base = {}
        sample_file = {}
        def run_base(job):
            st, s, f = job
            p = os.path.join(tmp, f"sample-s{s}-f{f}-{st}.json")
            t = tally(st, records, tmp, s, f, print_sample=p)
            return job, t, p
        for (st, s, f), t, p in pool.map(run_base, [(st, s, f) for st in SETTINGS for s, f in samples]):
            base[(st, s, f)] = t
            sample_file.setdefault((s, f), p)
        # every cell's sample is the same at one (seed, floor): the vertices, not the layout, decide it
        for (st, s, f), t in base.items():
            p = os.path.join(tmp, f"sample-s{s}-f{f}-{st}.json")
            if open(p).read() != open(sample_file[(s, f)]).read():
                print(f"  FAIL the sample of seed {s} floor {f} differs between diagonals")
                return 1
        streams = {}
        def make(job):
            (s, f), ch = job
            out = os.path.join(tmp, f"stream-s{s}-f{f}-{ch}.jsonl")
            return job, out, changed_stream(records, sample_file[(s, f)], ch, out)
        for ((s, f), ch), out, k in pool.map(make, [(sf, ch) for sf in samples for ch in CHANGES]):
            streams[(s, f, ch)] = out
            print(f"  stream seed {s} floor {f} {ch}: {k} {"vertices" if ch == "vprops" else "edges"} outside the sample changed")
            if k == 0:
                print("  FAIL a changed stream changed nothing: the test would test nothing")
                return 1
        jobs = [(st, s, f, ch) for st in SETTINGS for s, f in samples for ch in CHANGES]
        bad_cells = 0
        for (st, s, f, ch), t in zip(jobs, pool.map(lambda j: tally(j[0], streams[(j[1], j[2], j[3])], tmp, j[1], j[2]), jobs)):
            b = base[(st, s, f)]
            bad = compare(b, t, "")
            bad_cells += bool(bad)
            print(f"  {'ok  ' if not bad else 'FAIL'} {st} seed {s} floor {f} {ch}: {len(bad)} rows differ outside the named ones"
                  + (": " + "; ".join(bad[:3]) if bad else ""))
        # the control: a tally that encodes one owner outside the sample must be caught
        pb = tally("chunked", records, tmp, 1, 100, plant="leak_one_owner")
        pc = tally("chunked", streams[(1, 100, "drop")], tmp, 1, 100, plant="leak_one_owner")
        caught = bool(compare(pb, pc, ""))
        print(f"  {'ok  ' if caught else 'FAIL'} the control (--plant leak_one_owner, chunked, seed 1, floor 100, drop): "
              f"{'caught' if caught else 'NOT caught'}")
    cells = len(jobs)
    ok = bad_cells == 0 and caught and cells == len(SETTINGS) * len(samples) * len(CHANGES)
    print(f"check_tally_sample_leak: {cells} changed-stream tallies against {len(base)} of the stream "
          f"({len(SETTINGS)} diagonals x {len(SEEDS)} seeds x {len(FLOORS)} floors x {len(CHANGES)} changes), "
          f"{bad_cells} differ outside the named rows; control "
          f"{'caught' if caught else 'missed'}: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
