#!/usr/bin/env python3
"""compose.load joins another chunk size's capture to the grid's Chunked/Chunked by (query, body_sha, param), not by name
(BI's size captures ran three bodies under new names). Analysis only: opens no store.

    python3 tests/unit/check_chunk_size_body_sha.py
    python3 tests/unit/check_chunk_size_body_sha.py --grid <captures/ldbc/sf3> --size 4k \\
        [--size ...] --before <a compose.py from before the rule>

Cases on compose.size_names (synthetic run rows):
  1. same name, same body: kept;  2. a renamed body (a name the grid lacks, one grid name with its sha): the grid's name;
  3. same name, another body, the query not listed: refused;  4. the same, listed in drift_ok: kept;
  5. a capture without body_sha: no map (names kept, as before the rule);
  6. a body two grid names share: no map (the runs check then refuses it by name).
With --grid: the control. The LDBC load with the sizes gives the same tables (every cost and cost_h vector, valid set)
under this compose and under --before's. Exit 1 when a case goes the wrong way."""
import argparse
import hashlib
import json
import os
import sys
import types

from kvevaluator.paths import ROOT

LC = str(ROOT / "experiments" / "layout_choice")       # where an old layout_choice/compose.py sat


def load_compose(source):
    """the composer from `source` (default: this tree's python/kvcost/layout_cost/compose.py). A source at an older commit may be a
    layout_choice/compose.py: run where it sat (its paths), its `from price import` given the moved loaders"""
    if source is None:
        from kvcost.layout_cost import compose
        return compose
    from kvcost.layout_cost import compose
    sys.modules.setdefault("price", compose)
    m = types.ModuleType("compose")
    m.__file__ = os.path.join(LC, "compose.py")
    sys.modules["compose"] = m
    exec(compile(open(source).read(), source, "exec"), m.__dict__)
    return m


def run(q, v, sha, p="1"):
    return {"query": q, "body": v, "body_sha": sha, "param_idx": p}


def cases(compose):
    bad = 0
    grid = [run("q1", "a", "s1"), run("q1", "a", "s1", "2"), run("q2", "old", "s2"), run("q3", "c", "s3"),
            run("q4", "x", "s4"), run("q4", "y", "s4")]

    def names(mine, drift=()):
        return compose.size_names("opt", "fam", mine, grid, frozenset(drift), set())

    def refused(fn):
        try:
            fn()
            return False
        except SystemExit:
            return True
    checks = [
        ("1 same name, same body kept", names([run("q1", "a", "s1")]) == {("q1", "a"): "a"}),
        ("2 renamed body takes the grid's name", names([run("q2", "new", "s2")]) == {("q2", "new"): "old"}),
        ("3 same name, other body, not listed: refused", refused(lambda: names([run("q3", "c", "s9")]))),
        ("4 same name, other body, listed: kept", names([run("q3", "c", "s9")], drift=("q3",)) == {("q3", "c"): "c"}),
        ("5 no body_sha: no map", names([{**run("q2", "new", ""), "body_sha": ""}]) == {}),
        ("6 a body two grid names share: no map", names([run("q4", "z", "s4")]) == {}),
        # a body rewritten count-equal between the size capture and the grid (the successor table): bi3's, renamed
        ("7 an old body against its count-equal successor: the grid's name",
         compose.size_names("opt", "bi", [run("bi3", "hand~2bb1f0", "2bb1f0ebfdd8")],
                            [run("bi3", "hand~a4eca1", "a4eca18cff92")], frozenset(), set()) == {("bi3", "hand~2bb1f0"): "hand~a4eca1"}),
        ("8 the same name, an old body against its successor: kept, not refused",
         compose.size_names("opt", "bi", [run("bi17", "hand", "67302ee60fd3")],
                            [run("bi17", "hand", "44b050c52ec5")], frozenset(), set()) == {("bi17", "hand"): "hand"}),
        ("9 the control: the same name, a sha outside the table: refused",
         refused(lambda: compose.size_names("opt", "bi", [run("bi17", "hand", "000000000000")],
                                            [run("bi17", "hand", "44b050c52ec5")], frozenset(), set()))),
    ]
    for what, ok in checks:
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {what}")
    return len(checks), bad


def digest(compose, grid, sizes):
    """the sha256 of the load's tables (valid, cost, cost_h) and how many vectors they hold"""
    from kvcost.layout_cost import benchmarks, picks, settings
    from kvcost.layout_cost import compose as tree   # the tree's own module, as layout_scale_check.load used (not the `compose` argument)
    data = tree.load(grid, True, composer=tree.Composer(criterion=None, families=benchmarks.families("ldbc", drop=("microbench",)), sizes=settings.parse_sizes(sizes, grid),
                                                           plan_rule=picks.PlanRule(record=None)))
    h, n = hashlib.sha256(), 0
    for key in sorted(data, key=str):
        d = data[key]
        if not isinstance(d, dict):
            continue
        h.update(json.dumps([str(key), sorted((c, sorted(v)) for c, v in d.get("valid", {}).items())]).encode())
        for ck in ("cost", "cost_h"):
            for k in sorted(d.get(ck, {}), key=str):
                for u in sorted(d[ck][k], key=str):
                    h.update(json.dumps([ck, str(k), str(u), sorted(d[ck][k][u].items())]).encode())
                    n += 1
    return h.hexdigest(), n, tuple(data.sized)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid")
    ap.add_argument("--size", action="append", default=[], metavar="K", help="another chunk size (1k): its Chunked/Chunked in --grid's place; repeatable")
    ap.add_argument("--before", help="the compose.py source to compare against (the control)")
    ap.add_argument("--side", choices=("this", "before"), help=argparse.SUPPRESS)   # one load per process
    a = ap.parse_args()
    sizes = list(a.size)
    if a.side:
        print(json.dumps(digest(load_compose(a.before if a.side == "before" else None), a.grid, sizes)))
        return 0
    n, bad = cases(load_compose(None))
    if a.grid:
        if not (a.before and sizes):
            sys.exit("the control takes --grid, --before and one --size or more")
        import subprocess
        got = {}
        for side in ("before", "this"):
            cmd = [sys.executable, __file__, "--grid", a.grid, "--before", a.before, "--side", side] + [f"--size={s}" for s in a.size]
            out = subprocess.run(cmd, capture_output=True, text=True)
            if out.returncode:
                sys.exit(f"the {side} load failed:\n{out.stderr[-2000:]}")
            got[side] = json.loads(out.stdout.strip().splitlines()[-1])
            print(f"   {side}: {got[side][1]:,} vectors, sizes {got[side][2]}, sha256 {got[side][0][:16]}")
        ok = got["before"] == got["this"] and got["this"][1] > 0
        n += 1; bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} control: the LDBC load with {len(sizes)} sizes gives the same tables before and after")
    print(f"{n} cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
