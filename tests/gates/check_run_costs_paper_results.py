#!/usr/bin/env python3
"""Checks `run_costs.py --paper-results` (python/kvevaluator/measure/): on a planted tree, a run record, a store's
manifest and a study's timing each give one row with the identity their place in the tree gives, and a composed cell
and a summary/ file give none; an empty tree and a time that is not a number fail. On paper-results/ (when fetched):
one row per timed file of each run kind, counted here independently. An out dir under paper-results/ is refused, and
with none the tables go to the output tree's paper/run-times/. Writes to temp dirs only.

    uv run --locked python tests/gates/check_run_costs_paper_results.py        # exit 1 on a failed check
"""
import glob
import json
import os
import sys
import tempfile
from pathlib import Path

from kvevaluator.measure import run_costs
from kvevaluator.paths import paper_results_dir

FAILED = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


def plant(root, rel, d):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


def fails(root, words):
    """True when paper_results on root exits with `words`"""
    with tempfile.TemporaryDirectory() as out:
        try:
            run_costs.paper_results(root, out)
            return False
        except SystemExit as e:
            return words in str(e)


def main():
    binary = {"binary_git_sha": "abcdef1234567", "binary_dirty": False, "host": {"cpu": "planted"}}
    with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as out:
        plant(root, "captures/ldbc/sf3/interactive/reported/counts/props_split.json", {"wall_s": 12.0, "binary": binary})
        plant(root, "captures/ldbc/sf3/interactive/reported/counts/props_mix.json", {"wall_s": 5.0, "composed_from": {}})
        plant(root, "stores/ldbc/sf3/meta-props_split/manifest.json", {"ingest_wall_s": 40.0, "provenance": binary})
        plant(root, "studies/lat/timings/ldbc/sf3/bi/meta-props_split/steady-307mb-p2/steps.csv.meta.json",
              {"wall_s": 7.5, "provenance": binary})
        plant(root, "summary/ldbc/sf3/report/counts/props_split.json", {"wall_s": 3.0})
        got = sorted((r["kind"], r["what"], r["store"], r["wall_s"], r["binary_sha"])
                     for r in run_costs.paper_results(root, out))
        want = [("captures", "interactive/reported/counts", "props_split", 12.0, "abcdef123"),
                ("stores", "-", "props_split", 40.0, "abcdef123"),
                ("timings", "lat: bi/steady-307mb-p2", "props_split", 7.5, "abcdef123")]
        check("1 one row per run with its tree identity; a composed cell and a summary/ file left out", got == want, got)
        plant(root, "captures/ldbc/sf3/interactive/reported/counts/x.json", {"wall_s": "slow"})
        check("2 a time that is not a number fails", fails(root, "is not a number"))
    with tempfile.TemporaryDirectory() as root:
        plant(root, "captures/ldbc/sf3/interactive/reported/counts/props_split.json", {"wall_s": 12.0, "binary": binary})
        before = sorted(str(p) for p in paper_results_dir().rglob("*")) if paper_results_dir().is_dir() else None
        try:
            run_costs.paper_results(root, str(paper_results_dir() / "run-times"))
            refused = False
        except SystemExit as e:
            refused = "refused" in str(e)
        after = sorted(str(p) for p in paper_results_dir().rglob("*")) if paper_results_dir().is_dir() else None
        check("2a an out dir under paper-results/ is refused, nothing written there", refused and before == after)
        with tempfile.TemporaryDirectory() as tree:              # no out dir: the output tree's paper/run-times/
            saved, run_costs.Tree = run_costs.Tree, lambda: saved(tree)
            try:
                run_costs.paper_results(root)
            finally:
                run_costs.Tree = saved
            check("2b with no out dir the tables go to <output tree>/paper/run-times/",
                  sorted(os.listdir(os.path.join(tree, "paper", "run-times"))) == ["run_times.csv", "run_times_summary.csv"])
    with tempfile.TemporaryDirectory() as empty:
        check("3 a tree with no timed file fails", fails(empty, "no timed file"))
    root = paper_results_dir()
    if not root.is_dir():
        print(f"SKIP 4 paper-results/: not fetched ({root}; `just fetch-results`)")
    else:
        want = {}
        for path in glob.glob(str(root / "**" / "*.json"), recursive=True):
            rel = os.path.relpath(path, root).split("/")
            kind = rel[2] if rel[0] == "studies" else rel[0]
            if kind == "timings" and not path.endswith(".meta.json"):
                continue                   # a raw per-query file of a timing (its passes), not a run
            d = json.load(open(path))
            key = "ingest_wall_s" if kind == "stores" else "wall_s"
            if kind in run_costs.RUN_KINDS and isinstance(d, dict) and key in d and "composed_from" not in d:
                want[kind] = want.get(kind, 0) + 1
        with tempfile.TemporaryDirectory() as out:
            rows = run_costs.paper_results(root, out)
        got = {}
        for r in rows:
            got[r["kind"]] = got.get(r["kind"], 0) + 1
        check(f"4 paper-results/: one row per timed file of each run kind ({sum(want.values())} files)",
              got == want and set(want) == set(run_costs.RUN_KINDS), (got, want))
    print(f"check_run_costs_paper_results: {6 - (not root.is_dir())} checks, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
