#!/usr/bin/env python3
"""The kvcost commands' walkthrough runs as written (python/kvcost/cli/README.md's main workflow, LDBC sf0.1): its
commands on inputs/workloads/ldbc/examples/ic7_ic2.json exit 0 and give the walkthrough's answers -- Co-Ch64k the fewest
wops_bytes4k at 396.59, the best mix PROVEN at 312.245, check-mix PASS, the mix and Co-Co timed steady -- and every file
they write sits in one of the output tree's kinds (python/kvevaluator/output_tree.py: no runs/ or other dir of its own,
no captures/ or composed/ nested under another kind). It runs on a fresh tree, as the walkthrough does (a new output
dir, the stores, stats and specs shared: output_tree.own_tree), so what another run measured in output/ does not
change its answers. Needs LDBC sf0.1 (`just fetch-dataset ldbc=0.1`); builds the stores it lacks (about a minute the first
time).

    python3 tests/gates/check_walkthrough.py
"""
import os
import re
import subprocess
import sys
import tempfile
import time

from kvevaluator.output_tree import own_tree
from kvevaluator.paths import ROOT

WORKLOAD = "inputs/workloads/ldbc/examples/ic7_ic2.json"
DATASET = "ldbc=0.1"
BEST = "summary/ldbc/sf0.1/mix-search/ic7_ic2/mixes/best.json"   # search-mix's mix, below the output dir
STEPS = [  # (the command after `kvcost`, what its ==> line must hold)
    ("generate-plans", None),
    ("measure Sp-Sp Co-Co Ch64k-Ch64k", None),
    ("compose", None),
    ("cost-layouts --cost-model wops_bytes4k", r"Co-Ch64k, 396\.59\b"),
    ("search-mix --cost-model wops_bytes4k", r"PROVEN: the fewest wops_bytes4k: 312\.245\b"),
    ("check-mix mix:best", r"PASS"),
    # a 1 MB cache: store_config's 10 MB at sf0.1 holds all of IC7 and IC2, and the steady rule refuses a pass with no miss
    ("measure-latency Co-Co mix:best --cache-mb 1", r"mix mix_[0-9a-f]+ [0-9.]+ ms per run of the workload"),
]
# the output tree's kinds (output_tree.py's docstring): every file a command writes is below one of them
KINDS = {"stores", "stats", "specs", "captures", "composed", "predicted", "timings", "summary", "engines", "figures",
         "paper", "studies", "logs", "checks"}
# a capture or composed tree below another kind's dir (logs/journals/ mirrors a capture's path by design: grid_bodies)
NESTED = re.compile(r"(?!logs/).+/(captures|composed)/")


def files(root):
    return {os.path.join(d, f) for d, _, fs in os.walk(root) for f in fs}


def main():
    out = tempfile.mkdtemp(prefix="walkthrough-", dir=os.environ.get("TMPDIR"))
    env = own_tree(out)
    print(f"[check_walkthrough] a fresh tree: {out}")
    before = files(out)
    bad = []
    for verb, want in STEPS:
        words = verb.format(out=out).split()
        wl = [] if words[0] == "compose" else ["--workload", WORKLOAD]     # compose works per dataset
        cmd = [sys.executable, "-m", "kvcost.cli.cli", words[0], *wl, "--dataset", DATASET, *words[1:]]
        t = time.monotonic()
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
        answer = next((ln for ln in reversed(r.stdout.splitlines()) if ln.startswith("==>")), "")
        ok = r.returncode == 0 and (want is None or re.search(want, answer))
        print(f"{'ok  ' if ok else 'FAIL'} kvcost {verb.format(out='<out>')} ({time.monotonic() - t:.0f} s): "
              f"{answer[:140] or r.stderr.strip()[-200:]}")
        if not ok:
            bad.append(verb)
    new = sorted(files(out) - before)
    nested = [os.path.relpath(p, out) for p in new if NESTED.match(os.path.relpath(p, out))]
    outside = [os.path.relpath(p, out) for p in new if os.path.relpath(p, out).split(os.sep)[0] not in KINDS]
    for p in nested[:10]:
        print(f"FAIL nested tree: {p}")
    for p in outside[:10]:
        print(f"FAIL not in a kind of the output tree: {p}")
    print(f"check_walkthrough {'OK' if not bad and not nested and not outside else 'FAIL'}: {len(STEPS)} steps "
          f"({len(bad)} failed), {len(new)} files written ({len(nested)} in a nested tree, {len(outside)} outside the "
          f"tree's kinds)")
    sys.exit(1 if bad or nested or outside else 0)


if __name__ == "__main__":
    main()
