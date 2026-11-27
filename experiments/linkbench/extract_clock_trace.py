#!/usr/bin/env python3
"""Recover the clock of a LinkBench generation from its data (owner: experiments/linkbench). Opens no store.

LinkBench stamps every link and node time from the wall clock; all else in the data is seeded. So a generation made
with ONE fixed clock value C (python/kvevaluator/datasets/linkbench/generate.sh --clock C --clock-trace none) differs from a wall-clock generation only
in the time fields, and on each line   time(wall clock) - time(fixed) = the clock when that line was stamped - C.
This script reads the two dirs line by line, checks that every line is equal but for its time, and writes the clock as
steps per output file and unit:   file,unit,first_line,clock   ("from this line on (0-based), a time in <unit> was
stamped when the clock read <clock>"; a link's time is in ms, a node's in s). LinkBenchClock.java replays it.
Beside it, <out>.json: the wall-clock run's lines and sha256 per file, which a replay must reproduce
(data_manifest.py checks it at every generation).

    python3 experiments/linkbench/extract_clock_trace.py --wall-clock DIR --fixed DIR --clock MS --out FILE.csv [--note TEXT]
Exit 1 if a line differs in more than its time: then the two dirs are not the same generation and no file is written."""
import argparse
import hashlib
import json
import os
import re
import sys

FILES = ("nodes.jsonl", "links.jsonl", "requests.jsonl")
TIME = re.compile(rb'"time":(-?\d+)')


def unit_of(name, line):
    """a link's time is in ms, a node's in s (LinkBench's Link.time is a long of ms, Node.time an int of seconds)"""
    if name == "links.jsonl":
        return "ms"
    if name == "nodes.jsonl":
        return "s"
    return "ms" if b'"id1":' in line else "s"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--wall-clock", required=True, help="the data dir whose clock is recovered")
    ap.add_argument("--fixed", required=True, help="the same generation with one fixed clock value")
    ap.add_argument("--clock", required=True, type=int, help="that fixed value, ms")
    ap.add_argument("--out", required=True, help="the trace file to write (.csv); <out>.json beside it")
    ap.add_argument("--note", default="", help="what the wall-clock run is (date, where it was measured), kept in <out>.json")
    a = ap.parse_args()
    steps, files, bad = [], {}, 0
    for name in FILES:
        h, lines, stamped, last = hashlib.sha256(), 0, 0, {}
        lo = hi = None
        with open(os.path.join(a.wall_clock, name), "rb") as fw, open(os.path.join(a.fixed, name), "rb") as ff:
            for n, w in enumerate(fw):
                f = ff.readline()
                h.update(w)
                lines += 1
                mw, mf = TIME.search(w), TIME.search(f)
                if TIME.sub(b"T", w, 1) != TIME.sub(b"T", f, 1) or bool(mw) != bool(mf):
                    bad += 1
                    if bad <= 3:
                        print(f"{name} line {n}: differs in more than its time", file=sys.stderr)
                    continue
                if not mw:
                    continue
                unit = unit_of(name, w)
                clock = (a.clock if unit == "ms" else a.clock // 1000) + int(mw.group(1)) - int(mf.group(1))
                stamped += 1
                if last.get(unit) != clock:
                    steps.append((name, unit, n, clock))
                    last[unit] = clock
                ms = clock if unit == "ms" else clock * 1000
                lo, hi = (ms if lo is None else min(lo, ms)), (ms if hi is None else max(hi, ms))
            if ff.readline():
                bad += 1
                print(f"{name}: the fixed-clock file has more lines", file=sys.stderr)
        files[name] = {"lines": lines, "sha256": h.hexdigest(), "lines_with_time": stamped,
                       "steps": sum(1 for s in steps if s[0] == name), "clock_first_ms": lo, "clock_last_ms": hi}
        print(f"{name}: {lines:,} lines, {stamped:,} with a time, {files[name]['steps']:,} clock steps, "
              f"clock {lo} .. {hi} ms ({(hi - lo) / 1000 if stamped else 0:.1f} s)")
    if bad:
        print(f"FAIL: {bad} lines differ in more than their time: not the same generation; nothing written", file=sys.stderr)
        return 1
    with open(a.out, "w") as f:
        f.write("file,unit,first_line,clock\n")
        for s in steps:
            f.write(",".join(str(x) for x in s) + "\n")
    with open(a.out + ".json", "w") as f:
        json.dump({"what": "the run whose clock " + os.path.basename(a.out) + " holds: a replay must give these files",
                   "note": a.note, "fixed_clock_ms_used_for_the_extraction": a.clock, "files": files}, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote {a.out}: {len(steps):,} steps ({os.path.getsize(a.out):,} bytes), and {a.out}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
