#!/usr/bin/env python3
"""kvcost's check 4: the negative controls. Each plants one defect in a scratch copy of an sf0.1 capture (never the
capture itself) and asserts kvcost catches it; exit 1 when any control is not caught.

  1 changed count     one parts row of the props source (Co-Co) gets S + 1: composing Co-Ch64k (the composer,
                      kvcost.layout_cost.compose_cells, as kvcost compose runs it; the grid measured Co-Ch64k, so the
                      composer checks it) must exit non-zero
  2 one-source body   a body is made ok=0 on the props source (Co-Co), and Co-Ch64k's own files are removed from the
                      copy (as never measured). 2a, a body neither picked nor the planner's: the composed cell's rows
                      (kvcost.cli.views.cell_rows, the pick with its exact mark) must mark that query `unknown` (a
                      held-out LDBC read), and the totals too. 2b, the picked body: the pricing must refuse naming the
                      query (the pick record names that body) or mark it `unknown`
  3 planner not ok    the planner's row of one query on one param is made ok=0 on a measured cell: the pricing refuses
  4 assignment        kvcost cost-layouts --mix with a unit the capture does not have, and with a string that leaves
                      units out and no --mix-base: refused
  5 missing source    the adjacency source's parts file removed from the copy: the composer refuses, naming it

    python3 tests/gates/check_kvcost_controls.py --sf 0.1 [--grid <the nine cells' capture root>] [--composer PATH]
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from kvevaluator.output_tree import HELD_OUT, Tree, capture_in, own_tree, tree_of

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.cli import views as V
from kvevaluator.capture import capture_layout

EXP = V.EXP
KV = [sys.executable, "-m", "kvcost.cli.cli"]   # the CLI as `just` runs it, in this interpreter
COCH = "props_condensed_adj_chunked"


def rewrite(path, fn):
    """rewrite a CSV in place: fn(rows) changes them, returns how many it changed"""
    with open(path) as f:
        rd = capture_layout.reader(f)
        head, rows = rd.fieldnames, list(rd)
    n = fn(rows)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=head, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return n


def plant_first(rows, match, change):
    """change the first row that matches; how many were changed (0 or 1)"""
    for r in rows:
        if match(r):
            change(r)
            return 1
    return 0


def run(args, env):
    p = subprocess.run(KV + args, cwd=EXP, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def compose(a, grid, out):
    """Co-Ch64k composed from the copy's Co-Co and Ch64k-Ch64k by the composer (a branch's copy with --composer), checked against
    the copy's own Co-Ch64k when it holds one (as kvcost compose runs it); (rc, its output)"""
    if os.path.exists(out):
        shutil.rmtree(out)                   # our own scratch (a run stopped half-way leaves it): the composer refuses one
    cmd = [sys.executable] + ([a.composer] if a.composer else ["-m", "kvcost.layout_cost.compose_cells"])
    cmd += ["--grid", grid, "--dataset", "ldbc", "--cells", COCH, "--out", out, "--whole-grid"]
    if os.path.exists(capture_in(grid, "interactive").path_of("counts", COCH)):
        cmd.append("--check")
    p = subprocess.run(cmd, cwd=EXP, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def fresh_copy(src, dst):
    """the measured <dataset>/<scale> dir (its layouts' files, no stores) copied to a scratch dir; returns the copy"""
    if os.path.exists(dst):
        shutil.rmtree(dst)                   # our own scratch, rebuilt from the capture in seconds
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.png", "steps", "row-hashes"))   # kinds nothing here reads
    return dst


def control_one_source(a, env, grid, scratch, out, q, body, name, expect):
    """one body of q made ok=0 on the props source (Co-Co, param 1) in a copy without Co-Ch64k's own files, then
    Co-Ch64k composed and priced: the mark of q and of the totals, or the pricing's refusal that names q"""
    g = fresh_copy(grid, os.path.join(scratch, "g-" + os.path.basename(out)))
    n = rewrite(capture_in(g, "interactive").path_of("counts", "props_condensed_adj_condensed"), lambda rows: plant_first(
        rows, lambda r: r["query"] == q and r["body"] == body and r["param_idx"] == "1", lambda r: r.update(ok="0")))
    for dp, _, fs in os.walk(g):
        for f in fs:
            if "props_condensed_adj_chunked" in f:
                os.remove(os.path.join(dp, f))          # the scratch copy's files only
    rc, out_text = compose(a, g, out)
    marked = tot = None
    if rc != 0:
        got = f"the composer failed (rc {rc}), not the pricing"
    else:
        try:
            rows = V.cell_rows("ldbc", out, COCH, rule=V.picks.PlanRule())
            marked = next((r["exact"] for r in rows if r["query"] == q), None)
            tot = next((t["exact"] for t in V.totals(rows) if t["scope"] == "all families"), None)
            got = "unknown" if marked == "unknown" and tot == "unknown" else f"marked {marked!r}, totals {tot!r}"
        except (SystemExit, KeyError) as e:     # the pick's refusals: SystemExit, or KeyError for a recorded body
            # a refusal is the pricing's own (naming the query), not any failure whose message happens to name it
            m = str(e)
            got = ("refused" if q in m and ("no plan picked" in m or "pick" in m and "record" in m or "planner" in m)
                   else f"refused without naming the query: {m[:120]}")
    ok = n == 1 and got in expect
    return (name, ok, f"{q} body {body[:40]}... ok=0 on Co-Co ({n} row): composer rc {rc}, {got}")


# the read captures whose params the composed layouts mix: (workload, param set; None = the default)
SOURCE_CAPTURES = (("interactive", None), ("interactive", HELD_OUT), ("microbench", None), ("bi", None))


def mixed_params(grid):
    """the read captures whose cells were measured on different params ([(capture, cell, its params, the first cell,
    its params)]): a cell composed from two sources needs both on the same params, so compose refuses such a grid
    before any control's path is reached; a cell absent from a capture is skipped (control 5 removes one on purpose)"""
    out = []
    for wl, ps in SOURCE_CAPTURES:
        cap = capture_in(grid, wl, ps)
        seen = {}
        for cell in cap.stores():
            if not cell.startswith("props_"):
                continue
            with open(cap.path_of("counts", cell)) as f:
                seen[cell] = sorted({(r["query"], r["param_idx"]) for r in capture_layout.reader(f)})
        if seen:
            first = min(seen)
            out += [(f"{wl} {cap.params()}", c, len(p), first, len(seen[first])) for c, p in sorted(seen.items()) if p != seen[first]]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--composer", default=None, help="another copy of compose_cells.py to run (a branch's, before its merge)")
    ap.add_argument("--grid", default=None, help="the measured <dataset>/<scale> dir (default: the output tree's; a gate round "
                                                 "passes its own)")
    a = ap.parse_args()
    env = dict(os.environ)
    grid = os.path.abspath(a.grid) if a.grid else str(Tree().place_dir("captures", "ldbc", a.sf))
    mixed = mixed_params(grid)
    if mixed:                                # not a control's failure: the controls cannot run on this grid
        for capture, cell, n, first, n_first in mixed[:10]:
            print(f"  {capture}: {cell} has {n} (query, param) rows, {first} has {n_first}")
        print(f"[check_kvcost_controls] precondition: mixed captures ({len(mixed)} cells measured on other params than "
              f"the first cell in their capture): re-measure the grid on one set of params; no control was run")
        return 2
    scratch = str(Tree().summary("ldbc", a.sf, "kvcost-controls"))
    out_dir = os.path.join(scratch, "out")      # every control's views: never the working ones
    results = []

    # 1: a changed count in the props source -> the composer's check against the measured Co-Ch64k fails
    g = fresh_copy(grid, os.path.join(scratch, "g1"))
    n = rewrite(capture_in(g, "interactive").path_of("parts", "props_condensed_adj_condensed"), lambda rows: plant_first(
        rows, lambda r: r["part"].startswith("props:") and float(r["S"]) > 0, lambda r: r.update(S=str(int(float(r["S"])) + 1))))
    rc, out = compose(a, g, out_dir + "1")
    # caught = the composer's own check found the planted difference (an rc != 0 for any other reason is not)
    d = [int(x.replace(",", "")) for x in re.findall(r"rows compared, ([\d,]+) differ", out)]
    results.append(("1 changed count", n == 1 and rc != 0 and any(x > 0 for x in d),
                    f"planted {n} row(s); composer rc {rc}; its check: {d or 'no differ line'} rows differ"))

    # 2: a body ok=0 on the props source, the target never measured (the use case: no --check)
    meas = {(r["family"], r["query"]): r for r in V.cell_rows("ldbc", grid, "props_condensed_adj_chunked", rule=V.picks.PlanRule())}
    runs = V.grid_capture.load_runs(capture_in(grid, "interactive").path, "props_condensed_adj_chunked")   # a capture dir
    planner = {(r["query"], r["body"]) for r in runs if r.get("planner_pick") == "1"}   # none on a capture
    # 2a: a body neither picked nor the planner's -> the composed view prices the query and marks it `unknown`
    q, body = next((r["query"], r["body"]) for r in runs
                   if r["query"].startswith("ic") and r["body"] != meas[("interactive", r["query"])]["body"]
                   and (r["query"], r["body"]) not in planner and r["ok"] == "1")
    results.append(control_one_source(a, env, grid, scratch, out_dir + "2a", q, body, "2a one-source body (not picked)",
                                      expect=("unknown",)))
    # 2b: the picked body (not the planner's) -> refused, the query named, or marked `unknown`; never priced as exact
    q, body = next((k[1], r["body"]) for k, r in sorted(meas.items())
                   if k[0] == "interactive" and (k[1], r["body"]) not in planner)
    results.append(control_one_source(a, env, grid, scratch, out_dir + "2b", q, body, "2b one-source body (picked)",
                                      expect=("unknown", "refused")))

    # 3: the picked body's row not ok on one param (is1: one body) -> the view refuses
    g = fresh_copy(grid, os.path.join(scratch, "g3"))
    n = rewrite(capture_in(g, "interactive").path_of("counts", "props_split_adj_split"), lambda rows: plant_first(
        rows, lambda r: r["query"] == "is1" and r.get("planner_pick", "1") == "1" and r["param_idx"] == "2", lambda r: r.update(ok="0")))
    try:
        V.cell_rows("ldbc", g, "props_split_adj_split", rule=V.picks.PlanRule())
        caught, why = False, "no refusal"
    except SystemExit as e:
        caught, why = any(m in str(e) for m in ("planner", "no plan picked", "the picked body ran ok")), str(e)[:150]
    results.append(("3 planner not ok", n == 1 and caught, f"{n} row; {why}"))

    # 4: assignments
    # read from the grid's tree, written into the controls' own output tree (both are refused before writing)
    cost = ["cost-layouts", "--dataset", f"ldbc={a.sf}", "--from", str(tree_of(grid)), "--workload", "i01",
            "--cost-model", "wops", "--mix-name", "ctl"]
    rc1, o1 = run(cost + ["--mix", "nosuch lists=Ch64k", "--mix-base", "Co-Sp"], own_tree(out_dir + "4"))
    rc2, o2 = run(cost + ["--mix", "knows lists=Ch64k"], own_tree(out_dir + "4"))
    results.append(("4 assignment", rc1 != 0 and "not in this capture" in o1 and rc2 != 0 and "leaves" in o2,
                    f"unknown unit rc {rc1}; units left out rc {rc2}"))

    # 5: a missing source file
    g = fresh_copy(grid, os.path.join(scratch, "g5"))
    os.remove(capture_in(g, "interactive").path_of("parts", "props_chunked_adj_chunked"))
    rc, out = compose(a, g, os.path.join(scratch, "out5"))
    results.append(("5 missing source", rc != 0 and "props_chunked_adj_chunked" in out, f"composer rc {rc}"))

    shutil.rmtree(scratch)                   # our own scratch: the copies and the controls' views
    for name, ok, detail in results:
        print(f"  {'caught' if ok else 'NOT CAUGHT'}: {name}: {detail}")
    bad = [r for r in results if not r[1]]
    print(f"[check_kvcost_controls] {len(results)} controls, {len(results) - len(bad)} caught" + ("" if bad else ": PASS"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
