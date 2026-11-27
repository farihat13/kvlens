#!/usr/bin/env python3
"""kvcost.latency.steady_table.stores_of and steady_share's regime lookup on a synthetic workload's timings in the output
tree's layout (<timings>/meta-<store>/<regime>/steps.csv), and steady_table.ratios_by_root (seconds, no data):
  1. stores_of: each store's steps.csv of the one regime (another regime beside it, a KV-timer one, not read);
  2. wall_regime: the one steady-<MB>mb-p2 dir among the stores' regimes (cache_mb its MB; a -kvtimer sibling not
     counted); 0 such dirs, or 2 (307 and 3072), refused; write_regime: the one after-steady-warmup-<MB>mb-p1-noflush
     dir (another run's dir beside it not counted);
  3. ratios_by_root over two runs (two trees, each with its own Sp-Co): each store over its own run's Sp-Co (wall 2
     over 1 in run a, 30 over 10 in run b: 2 and 3, never 30), Sp-Co's row from the first run; a store timed in both
     runs, or a run without Sp-Co, refused.

    python3 tests/unit/check_latency_steady_layout.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

from kvcost.latency import steady_share, steady_table
from kvevaluator.paths import ROOT


def load(name):
    """a paper/derive script as a module, by its file (its sibling `snapshots` loaded first, as the script imports it)"""
    spec = importlib.util.spec_from_file_location(name, ROOT / "paper" / "derive" / f"{name}.py")
    mod = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


load("snapshots")
L = load("latency_steady_ldbc")

FAILED = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


REGIME = "steady-307mb-p2"


def refused(name, call, words):
    """check that call() exits with a message holding `words`"""
    try:
        check(name, False, f"read as {call()}")
    except SystemExit as e:
        check(name, words in str(e), str(e))


def timings(root, stores, regime=REGIME):
    """a workload's timings under root: one empty steps.csv per store in `regime`"""
    for store in stores:
        p = root / f"meta-{store}" / regime / "steps.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    return root


def main():
    with tempfile.TemporaryDirectory() as d:
        root = timings(Path(d) / "one", ["a", "b"])
        timings(root, ["a"], "steady-307mb-kvtimer")
        got = steady_table.stores_of(root, REGIME)
        check("1 one steps.csv per store, of the one regime", got == {s: root / f"meta-{s}" / REGIME / "steps.csv"
                                                                      for s in ("a", "b")}, got)
        got = steady_share.wall_regime(root)
        check("2a the one steady-<MB>mb-p2 regime and its MB", (got, steady_share.cache_mb(got)) == (REGIME, 307), got)
        root = timings(Path(d) / "two", ["a"], "steady-307mb-kvtimer")
        refused("2b no steady-<MB>mb-p2 regime refused", lambda: steady_share.wall_regime(root), "0 regime dirs")
        root = timings(Path(d) / "three", ["a"])
        timings(root, ["b"], "steady-3072mb-p2")
        refused("2c two steady-<MB>mb-p2 regimes refused", lambda: steady_share.wall_regime(root), "2 regime dirs")
        writes = Path(d) / "four"
        for name in ("after-steady-warmup-3072mb-p1-noflush", "steady-settings-3072mb-4leg-p1-noflush"):
            (writes / "meta-a" / name).mkdir(parents=True)
        got = steady_share.write_regime(writes)
        check("2d the one run B write regime and its MB", (got, steady_share.cache_mb(got)) ==
              ("after-steady-warmup-3072mb-p1-noflush", 3072), got)
    ref = L.REFERENCE
    one = {"q1": 1.0, "q2": 3.0}
    run = lambda wall: {q: (wall * k, wall * k / 2, wall * k * 4, wall * k, wall * k / 2) for q, k in (("q1", 1), ("q2", 2))}
    host_a, host_b = {ref: run(1.0), "a": run(2.0)}, {ref: run(10.0), "b": run(30.0)}
    got = {store: (i, round(r["wall_ratio"], 9), round(r["cost_ratio"], 9))
           for store, (i, r) in steady_table.ratios_by_root([host_a, host_b], ref, one).items()}
    check("3a each store over its own run's Sp-Co; Sp-Co's row the first run's",
          got == {ref: (0, 1.0, 1.0), "a": (0, 2.0, 2.0), "b": (1, 3.0, 3.0)}, got)
    refused("3b a store timed in two runs refused",
            lambda: steady_table.ratios_by_root([host_a, {ref: run(10.0), "a": run(30.0)}], ref, one), "one row per store")
    refused("3c a run without Sp-Co refused", lambda: steady_table.ratios_by_root([host_a, {"b": run(30.0)}], ref, one),
            "is not among its stores")
    print(f"check_latency_steady_layout: 8 cases, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
