#!/usr/bin/env python3
"""kvcost predict on Chunked/Chunked at another size (Ch4k-Ch4k beside the nine) and the predicted any-size search, end to end at sf0.1 on IC7 + IC2 (the walkthrough's two
queries, as a workload file of its own). The layouts it predicts are the preflight's list for this gate
(tests/gates/gate_preflight.py, GATE_TALLIES), which builds the tally library's entries they are assembled from: a run
from another tree may not build one (tally_library.check_writable). The predicted sets go where kvcost
predict writes them, predicted/ldbc/sf0.1/<set>/ (Tree.predicted_dir), in a scratch tree of the check's own (predict
with KVEVAL_OUTPUT_DIR, its stores and stats the shared ones: output_tree.own_tree; search-mix --from), so the output tree's
predicted sets are never rewritten with these two queries. No store
opened; needs the sf0.1 CSVs (the record stream, the statistics and the tallies are made from them). About a minute.

    python tests/gates/check_predict_sizes.py

Checks (each prints what it covered; 0 covered fails):
  1. the nine layouts' predicted captures are byte-identical with and without Ch4k-Ch4k named beside them (the control);
  2. Ch4k-Ch4k adds props_chunked_c4k_adj_chunked_c4k to every predicted set and family the run wrote: the sets are the
     ones the check's own first predict call wrote, so a capture an earlier call left in the predicted place (an anchor
     set from before the anchor level was off by default) is not one; a planted old anchor capture shows it;
  3. search-mix --count-source <the default tier's set> takes the run's predicted size by itself (its result row names chunked_c4k);
  4. a size the run did not predict (--size 16k) is refused, naming the kvcost predict call that makes it;
  5. a family call (not a run) that names no layouts is refused.
Exit 1 when one fails."""
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

from gate_preflight import GATE_TALLIES
from kvcost.cli.predict import TIER_SETS, default_set
from kvevaluator.predict.tier1 import DEFAULT_TIER
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree, own_tree
from kvcost.layout_search import results as R

NAME = "check_predict_sizes"
LAYOUTS = GATE_TALLIES["predict-sizes"]   # every layout this check predicts: the one list, which the preflight builds from
KV = [sys.executable, "-m", "kvcost.cli.cli"]
BAD = []


def check(name, ok, got=""):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {got}"), flush=True)
    if not ok:
        BAD.append(name)


def kv(*args, out=None):
    """(rc, output) of a kvcost call; out: the output tree it writes into (own_tree: the stores and stats shared)"""
    r = subprocess.run([*KV, *args], cwd=ROOT, capture_output=True, text=True, env=own_tree(out) if out else None)
    return r.returncode, r.stdout + r.stderr


def set_capture(tree, name):
    """a predicted set's capture of IC7 + IC2 in the tree: predicted/ldbc/sf0.1/<set>/interactive/reported"""
    return Tree(tree).predicted("interactive", "0.1", name)


def shas(tree):
    """{path below the tree's predicted/ldbc/sf0.1 (<set>/interactive/...): sha256} of every predicted capture csv, every set"""
    place = Tree(tree).place_dir("predicted", "ldbc", "0.1")
    out = {}
    for d in sorted(glob.glob(os.path.join(place, "*", ""))):
        for p in sorted(glob.glob(os.path.join(set_capture(tree, os.path.basename(os.path.dirname(d))).path, "*", "*.csv"))):
            out[os.path.relpath(p, place)] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


PLANT_SET = TIER_SETS[DEFAULT_TIER][1]   # the default set's anchor twin: a set this check's predict calls do not write (no --anchor)


def plant_old_capture(tree):
    """an old capture in a set the run does not write, dated a day back: its path"""
    path = set_capture(tree, PLANT_SET).path_of("counts", f"{NAME}_plant")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("planted by tests/gates/check_predict_sizes.py: an old capture the run does not write\n")
    old = time.time() - 86400
    os.utime(path, (old, old))
    return path


def main():
    with tempfile.TemporaryDirectory() as tree:  # the predicted sets' tree: the check's own
        return checks(tree, plant_old_capture(tree))


def checks(tree, planted):
    place = str(Tree(tree).place_dir("predicted", "ldbc", "0.1"))
    with tempfile.TemporaryDirectory() as tmp:
        wl = os.path.join(tmp, f"{NAME}.json")
        json.dump({"id": NAME, "name": NAME, "dataset": "ldbc", "scale": "0.1",
                   "title": "IC7 and IC2 (tests/gates/check_predict_sizes.py)",
                   "items": [{"family": "interactive", "query": q, "frequency": 1, "params": None} for q in ("ic7", "ic2")]},
                  open(wl, "w"), indent=1)
        t_first = time.time()
        for step in (("generate-plans", "--workload", wl, "--dataset", "ldbc=0.1"), ("predict", "--workload", wl, "--dataset", "ldbc=0.1")):
            rc, out = kv(*step, out=tree)
            if rc:
                print(out[-2000:])
                print(f"check_predict_sizes FAIL: kvcost {step[0]} rc={rc}")
                return 1
        before = shas(tree)
        t0 = time.time()
        rc, out = kv("predict", "--workload", wl, "--dataset", "ldbc=0.1", *LAYOUTS, out=tree)
        if rc:
            print(out[-2000:])
            print(f"check_predict_sizes FAIL: kvcost predict <the nine> Ch4k-Ch4k rc={rc}")
            return 1
        after = shas(tree)
        same = sum(after.get(p) == s for p, s in before.items())
        check(f"1 the nine layouts' captures unchanged by Ch4k-Ch4k beside them: {same} of {len(before)} files equal",
              before and same == len(before), sorted(p for p, s in before.items() if after.get(p) != s)[:3])
        # written by this call: new, or rewritten (an earlier run of this check leaves its 4k files in the run dir)
        new = sorted(p for p in after if p not in before or os.path.getmtime(os.path.join(place, p)) >= t0)
        # the sets the first predict call wrote (a run passes --fresh: each capture it makes is rewritten)
        sets = sorted({p.split(os.sep)[0] for p in before
                       if os.path.getmtime(os.path.join(place, p)) >= t_first})
        old = sorted({p.split(os.sep)[0] for p in before} - set(sets))
        check(f"2a an old capture left in the predicted place is not a set of this run (left: {', '.join(old) or 'none'}; "
              f"planted: {'yes' if planted else 'no'})", sets and PLANT_SET in old, sets)
        want = {os.path.relpath(set_capture(tree, s).path_of(k, "props_chunked_c4k_adj_chunked_c4k"), place)
                for s in sets for k in ("counts", "parts", "steps")}
        check(f"2 the 4k cell in every set ({', '.join(sets)}): {len(want & set(new))} of {len(want)} files, {len(new)} written",
              want and want <= set(new), sorted(want - set(new))[:3])
        s4 = os.path.join(tmp, "s4")
        rc, out = kv("search-mix", "--workload", wl, "--dataset", "ldbc=0.1", "--from", tree, "--cost-model", "wops_bytes4k",
                     "--count-source", default_set(), "--settings", "Sp,Co,Ch4k,Ch64k", out=s4)   # any size: asked (Sp,Co,Ch64k is the default)
        rows = [p for p in [Tree(s4).summary("ldbc", "0.1", "mix-search", f"{NAME}-{default_set()}", R.stem("per-type-any-size", ["4k"]) + ".json")]
                if p.exists()]     # a predicted set's rows: <workload>-<set>
        sizes = [x[0] for r in rows for x in (json.load(open(r))["rows"][-1].get("sizes") or [])]
        check(f"3 the predicted search takes the run's size: rc={rc}, sizes {sizes}", rc == 0 and sizes == ["chunked_c4k"],
              out[-500:])
        rc, out = kv("search-mix", "--workload", wl, "--dataset", "ldbc=0.1", "--from", tree, "--cost-model", "wops_bytes4k",
                     "--count-source", default_set(),
                     "--settings", "Sp,Co,Ch16k,Ch64k", out=os.path.join(tmp, "s16"))
        check("4 a size not predicted is refused, naming the predict call", rc != 0 and "Ch16k-Ch16k" in out, out[-300:])
        rc, out = kv("predict", "--family", "interactive", "--dataset", "ldbc=0.1", "--queries", "ic7", "--tally-dir", tmp)
        check("5 a family call that names no layouts is refused", rc != 0 and "and the layouts" in out, out[-300:])
    print(f"check_predict_sizes {'FAIL' if BAD else 'OK'}: 6 checks, {len(BAD)} failed" + (f": {BAD}" if BAD else ""))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
