#!/usr/bin/env python3
"""paper/derive/run_time.py's mix rows (mix_rows), on planted rows files (no store, no search): one fewest row gives
find (its two-step times) and verify (its solve, plus its cost cache build when it used one: a row without the cost
cache, search-mix's default, gives its solve alone); a file holding a proven row and a two-step
row (search-mix --search proven, then --search two-step) gives find from the two-step row and verify from the proven
one; two rows of one search are refused. And the search's size (search_size): a body not offered on the layout (its row
written, never run) and a measure-only body are no candidate plans; measure's own lines count the runs that ran apart
from the bodies not offered (grid_bodies.say_runs).

Run:  python3 tests/unit/check_run_time_mix_rows.py
"""
import importlib.util
import json
import os
import sys
import tempfile

from kvevaluator.paths import ROOT

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def row(search, solve, a, b, solver="cost-cache", proven=True):
    return {"question": "fewest", "model": "wops_bytes4k", "limit": None, "search": search, "solver": solver,
            "proven": proven, "times": {"solve_s": solve, "cost_cache_s": 10.0, "two_step_a_s": a, "two_step_b_s": b,
                                        "two_step_solver": solver}}


def load(name):
    """a paper/derive script as a module, by its file (its sibling `snapshots` loaded first, as the script imports it)"""
    spec = importlib.util.spec_from_file_location(name, ROOT / "paper" / "derive" / f"{name}.py")
    mod = sys.modules[name] = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    load("snapshots")
    RT = load("run_time")
    from kvevaluator.output_tree import Tree
    tmp = tempfile.mkdtemp(prefix="run-time-mix-")
    tree = Tree(tmp)
    d = tree.root / "summary" / "ldbc" / "sf0.1" / "mix-search" / "i01"
    os.makedirs(d)

    def rows_of(*rows):
        json.dump({"rows": list(rows)}, open(d / RT.MIX_ROWS, "w"))
        return {r["step"]: r for r in RT.mix_rows(tree, d, "Interactive")}

    got = rows_of(row("proven", 2.0, 0.5, 0.25))
    case("one row: find from its two-step times, verify its solve plus its cost cache",
         got["find, each part alone"]["wall_s"] == 0.5 and got["find, one-part moves"]["wall_s"] == 0.25
         and got["verify"]["wall_s"] == 12.0 and got["verify"]["proven"] == "yes", got)
    got = rows_of(row("proven", 2.0, 0.001, 0.001), row("two-step", 0.0, 3.0, 4.0, solver="no-cost-cache", proven=False))
    case("a proven and a two-step row: find from the two-step row (its own solver's names), verify from the proven row",
         got["find, fixed plan"]["wall_s"] == 3.0 and got["find, plans re-picked"]["wall_s"] == 4.0
         and got["verify"]["wall_s"] == 12.0 and got["verify"]["proven"] == "yes"
         and "find, each part alone" not in got, got)
    lone = row("proven", 2.0, 0.5, 0.25, solver="no-cost-cache")     # as best_mix_no_cost_cache writes it: no cost cache time
    lone["times"] = {k: v for k, v in lone["times"].items() if k not in ("cost_cache_s", "two_step_solver")}
    got = rows_of(lone)
    case("one proven row without the cost cache (search-mix's default): find under its solver's names, verify its solve alone",
         got["find, fixed plan"]["wall_s"] == 0.5 and got["find, plans re-picked"]["wall_s"] == 0.25
         and got["verify"]["wall_s"] == 2.0 and got["verify"]["item"] == "branch and bound"
         and got["verify"]["proven"] == "yes", got)
    try:
        rows_of(row("proven", 2.0, 0.5, 0.25), row("proven", 3.0, 0.5, 0.25))
        refused = None
    except SystemExit as e:
        refused = str(e)
    case("two proven rows: refused, the rows by search named", refused is not None and "'proven': 2" in refused, refused)
    # the candidate plans of a workload: its queries' bodies, less the measure-only ones and the ones not offered
    from pathlib import Path
    from kvevaluator.measure import grid_bodies as GB
    bi = sorted({i["query"] for i in json.load(open(ROOT / RT.WORKLOAD_FILES["BI"]))["items"]})
    rec = Path(tmp) / "props_split_adj_split.json"
    counts = [(q, "a", "0", "1", "") for q in bi] + [(bi[0], "b", "0", "0", GB.invalid_error(["x"])),
                                                      (bi[0], "c", "1", "1", ""), (bi[1], "d", "0", "0", "timeout"),
                                                      ("not_in_the_workload", "a", "0", "1", "")]
    with open(rec.with_suffix(".csv"), "w") as f:
        f.write("query,body,measure_only,ok,error\n" + "".join(",".join(r) + "\n" for r in counts))
    q, n, _ = RT.search_size("BI", [rec])
    case("search_size: a body not offered on the layout and a measure-only body are left out; a failed run's body counts",
         (q, n) == (len(bi), len(bi) + 1), (q, n, len(bi)))
    rows = [{"ok": o, "error": e} for _, _, _, o, e in counts[:len(bi) + 3]]
    case("say_runs: the runs that ran, and the bodies not offered apart",
         GB.ran_and_not_offered(rows) == (len(bi) + 2, 1)
         and GB.say_runs(rows) == f"{len(bi) + 1} of {len(bi) + 2} runs ok, 1 not offered on this layout"
         and GB.say_runs(rows[:2]) == "2 of 2 runs ok", GB.say_runs(rows))
    n_ok = sum(results)
    print(f"check_run_time_mix_rows {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
