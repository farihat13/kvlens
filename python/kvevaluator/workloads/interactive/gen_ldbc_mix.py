#!/usr/bin/env python3
"""LDBC SNB Interactive's own operation mix, per scale factor, from LDBC's files -> inputs/frequencies/ldbc/sf<SF>/mix.json.

  complex reads IC1-IC12  the driver's prescribed frequencies (int_driver_v1 .../interactive/sf<SF>.properties): IC_i
                          runs once per freq_i updates -- the benchmark's intent. The driver itself paces by time
                          (interleave_i = freq_i x update_interleave ms, 1542 at sf3), and the shipped sf3 stream's
                          mean gap is ~954 ms, so a real run issues ~0.62x these reads per update. IC13/IC14 are out
                          of scope as queries (not in our plans); their share is recorded (their short reads count).
  short reads IS1-IS7     the driver's random walk after each complex read (LdbcSnbShortReadGenerator): a round-robin
                          starts the person chain (IS1-IS3) or the message chain (IS4-IS7); only a chain's first query
                          is a coin toss (continue with probability = state); state starts at 1 and drops by the
                          dissipation (0.2) after each chain; chains alternate. Expected runs of EACH IS per complex
                          read = (expected chains of its kind) -- computed below. Assumes the walk always has ids.
  updates IU1-IU8         the shares of each update type in the SF's update stream (CsvBasic-LongDateFormatter).

    python3 python/kvevaluator/workloads/interactive/gen_ldbc_mix.py --sf 3 [--dissipation 0.2]
"""
import argparse
import collections
import glob
import json
import os
import sys
from kvevaluator.paths import ROOT, datasets_dir, ldbc_dir
from kvevaluator.datasets import fetch_hint, frequencies_in

EXP = str(ROOT)
# LDBC's driver frequencies per SF: inputs/frequencies/ldbc/sf<SF>/driver.properties (int_driver_v1's
# configuration/ldbc/snb/interactive/sf<SF>.properties, as tracked in the source repository, tag artifact-freeze-20261026)
IN_SCOPE = [f"ic{i}" for i in range(1, 13)]


def frequencies(sf):
    freq = {}
    with open(frequencies_in("ldbc", sf) / "driver.properties") as f:
        for line in f:
            line = line.strip()
            if line.startswith("ldbc.snb.interactive.LdbcQuery") and "_freq=" in line:
                name, val = line.split("=", 1)
                n = int(name.split("LdbcQuery", 1)[1].split("_", 1)[0])
                freq[f"ic{n}"] = int(val)
    return freq


def short_reads_per_complex(dissipation):
    """expected person chains and message chains per complex read (the driver's walk, 50/50 start)"""
    reach, k, probs = 1.0, 0, []
    while reach > 1e-12:
        probs.append(reach)                        # chain k+1 runs with this probability
        state = 1.0 - (k + 1) * dissipation        # state after k+1 finished chains
        reach *= max(state, 0.0)
        k += 1
    first = sum(p for i, p in enumerate(probs) if i % 2 == 0)     # chains of the starting kind
    second = sum(p for i, p in enumerate(probs) if i % 2 == 1)
    return (first + second) / 2, len(probs)       # per kind, averaged over the 50/50 start


def update_shares(sf):
    c = collections.Counter()
    paths = glob.glob(os.path.join(ldbc_dir(sf), "CsvBasic-LongDateFormatter", "updateStream_*_*.csv"))
    if not paths:      # no stream, no update shares: refuse rather than write a mix with 0 updates
        raise SystemExit(f"sf{sf}: no update stream under {ldbc_dir(sf)} ({fetch_hint('ldbc', sf)}) -- REFUSED")
    for path in paths:
        with open(path) as f:
            for line in f:
                parts = line.split("|", 3)
                if len(parts) > 2:
                    c[f"iu{parts[2]}"] += 1
    return c


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--dissipation", type=float, default=0.2, help="short_read_dissipation (the impls use 0.2)")
    a = ap.parse_args()
    freq = frequencies(a.sf)
    per_kind, chains = short_reads_per_complex(a.dissipation)
    counts = update_shares(a.sf)
    total_updates = sum(counts.values())
    ic_per_update = {q: 1 / freq[q] for q in IN_SCOPE}
    out = {
        "sf": a.sf,
        "sources": {"frequencies": os.path.relpath(frequencies_in("ldbc", a.sf) / "driver.properties", EXP),
                    "update_stream": "<data dir>/datasets/" + os.path.relpath(
                        os.path.join(ldbc_dir(a.sf), "CsvBasic-LongDateFormatter"), datasets_dir()),
                    "short_reads": "int_driver_v1 LdbcSnbShortReadGenerator (walk model, see gen_ldbc_mix.py)"},
        "frequency": freq,
        "complex_per_update": ic_per_update,
        "complex_per_update_out_of_scope": {q: 1 / f for q, f in freq.items() if q not in IN_SCOPE},
        "short_read_dissipation": a.dissipation,
        "short_reads_per_complex_each": per_kind,
        "update_counts": dict(sorted(counts.items())),
        "update_share": {q: n / total_updates for q, n in sorted(counts.items())},
    }
    dst = frequencies_in("ldbc", a.sf) / "mix.json"
    with open(dst, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
        f.write("\n")
    ic = sum(ic_per_update.values())
    print(f"sf{a.sf}: {total_updates:,} updates; {ic:.3f} complex reads (IC1-12) per update; each IS "
          f"{per_kind:.4f} per complex read ({chains} chains max) -> {dst}")


if __name__ == "__main__":
    main()
