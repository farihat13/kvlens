#!/usr/bin/env python3
"""A sampled tally's chunk ends against the full tally's (sf0.1, the seven diagonals, seeds 1-3, floors 0 and 100). The
sample reads only its sample: in every chunk family the sample marks last_chunk_assumed, last_chunk_value_bytes is the
family's pack target x inputs/contract/capabilities.json last_chunk_fill (an assumption, not a bound) and first_id is the
sample's lowest owner, at or above the full tally's; a family without the mark (kept whole by the floor) carries the
full tally's own two values. Prints the assumed last chunk over the full tally's (median, max) per chunk size. Opens no
store; about a minute (7 full and 42 sampled tallies).

    python tests/gates/check_tally_range_ends.py [--jobs 8] [--allow-dirty NAME]
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from kvevaluator import contract
from kvevaluator.capture.binary import binary_dirty_verdict
from kvevaluator.measure.records import records_file
from kvevaluator.paths import cpp_bin
from kvcost.predict_accuracy import uniform_49_tallies as U


SF = "0.1"
SEEDS = (1, 2, 3)
FLOORS = (0, 100)
PERCENT = 1


def tally(setting, records, tmp, sample=None):
    spec_path = os.path.join(tmp, f"{setting}.json")
    if not os.path.exists(spec_path):
        with open(spec_path, "w") as f:
            json.dump(U.diagonal_spec(setting), f, indent=1, sort_keys=True)
    cmd = [str(cpp_bin()), "tally", "--schema", str(U.SCHEMA), "--spec", spec_path, "--records", records]
    if sample:
        seed, floor = sample
        cmd += ["--owner-sample", str(PERCENT), "--seed", str(seed)] + (["--owner-floor", str(floor)] if floor else [])
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"tally {setting} {sample}: rc {r.returncode}: {r.stderr.strip().splitlines()[-1:]}")
    return json.loads(r.stdout)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--allow-dirty", default=None, metavar="NAME")
    a = ap.parse_args(argv)
    v = json.loads(subprocess.run([str(cpp_bin()), "--version"], capture_output=True, text=True, check=True).stdout)
    print(f"check_tally_range_ends: binary {cpp_bin()} {v['binary_git_sha'][:10]} dirty={v['binary_dirty']}")
    ok, msg = binary_dirty_verdict({"binary_git_sha": v["binary_git_sha"], "binary_dirty": v["binary_dirty"]}, a.allow_dirty)
    print(f"check_tally_range_ends: {msg}")
    if not ok:
        return 1
    records = str(records_file("ldbc", SF))
    jobs = [(st, None) for st in U.SETTINGS] + [(st, (s, f)) for st in U.SETTINGS for s in SEEDS for f in FLOORS]
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp, ThreadPoolExecutor(a.jobs) as pool:
        out = dict(zip(jobs, pool.map(lambda j: tally(j[0], records, tmp, j[1]), jobs)))
    fill = contract.last_chunk_fill()
    bad, assumed, whole = [], 0, 0
    for st in U.SETTINGS:
        full = out[(st, None)]["chunks"]
        spec = U.diagonal_spec(st)
        ratios = []
        for s in SEEDS:
            for f in FLOORS:
                for fam, c in out[(st, (s, f))]["chunks"].items():
                    want = full.get(fam)
                    where = f"{st} seed {s} floor {f} {fam}"
                    if want is None:
                        bad.append(f"{where}: no such family in the full tally")
                        continue
                    if c.get("last_chunk_assumed"):
                        assumed += 1
                        target = spec["vertex_props" if fam.startswith("props:") else "adjacency"]["owners"]["pack"]
                        b, x = c["last_chunk_value_bytes"], want["last_chunk_value_bytes"]
                        ratios.append(b / x if x else float("inf"))
                        if b != round(target * fill):
                            bad.append(f"{where}: last chunk {b}, not the pack target {target} x {fill}")
                        if int(c["first_id"], 16) < int(want["first_id"], 16):
                            bad.append(f"{where}: first_id {c['first_id']} below the full tally's {want['first_id']}")
                    else:
                        whole += 1
                        for k in ("first_id", "last_chunk_value_bytes"):
                            if c.get(k) != want.get(k):
                                bad.append(f"{where}: kept whole, {k} {c.get(k)} vs {want.get(k)}")
        fin = [r for r in ratios if r != float("inf")]
        print(f"  {st}: {len(ratios)} assumed rows; assumed / the full last chunk median {statistics.median(fin):.2f}x, "
              f"min {min(fin):.2f}x, max {max(fin):.1f}x" if fin else f"  {st}: no assumed row")
    for b in bad[:10]:
        print(f"  FAIL {b}")
    ok = not bad and assumed > 0 and whole > 0
    print(f"check_tally_range_ends: {len(jobs) - len(U.SETTINGS)} sampled tallies against {len(U.SETTINGS)} full "
          f"(7 diagonals x {len(SEEDS)} seeds x {len(FLOORS)} floors); {assumed} assumed rows, {whole} kept-whole rows, "
          f"{len(bad)} wrong: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
