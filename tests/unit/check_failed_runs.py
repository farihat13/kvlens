#!/usr/bin/env python3
"""A query whose runs failed is never priced: run_measure.failed_runs finds a workload file's query with a failed run in
the tree's captures (kvcost measure's), and say_failed names it with its first error (the pricing steps refuse by it).
kvcost measure exits 1 naming the failed runs of the stores it measured, and kvcost compose exits 1 naming the
failed runs in the captures it wrote (their calls stubbed: no store, no binary). Runs on a planted capture (one
failed run) under a temporary output dir. The controls, the same captures with every run ok, have no failed run.

Run:  python3 tests/unit/check_failed_runs.py
"""
import argparse
import csv
import json
import os
import sys
import tempfile

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def plant(run, ok_bad, other_ok=False):
    """the tree's capture: Sp-Sp's reported counts, ic7 ok and ic2 with one run failed (unless ok_bad), and is1 failed
    (a query of the set the workload file does not name: not its failure); ic7 also has a body invalid on the cell and
    ic2 one the SF30 rule did not run (results, not failures: candidates.failed_run)"""
    from kvevaluator.output_tree import Tree
    d = Tree().capture("interactive", run.scale).dir("counts")
    os.makedirs(d, exist_ok=True)
    rows = [{"query": "ic7", "param_idx": "1", "body": "edge_probe", "ok": "1", "error": ""},
            {"query": "ic7", "param_idx": "1", "body": "edge_probe_out", "ok": "0",
             "error": "invalid on this cell: lacks validity caps ['holds whole_label_scan_by_default']"},
            {"query": "ic2", "param_idx": "1", "body": "two_phase", "ok": "0", "error": "not run at SF30: (i) dearer"},
            {"query": "ic2", "param_idx": "1", "body": "index_walk", "ok": "1" if ok_bad else "0",
             "error": "" if ok_bad else "error (run): step fl (read_edge_exists): expected row, got vid"},
            {"query": "is1", "param_idx": "1", "body": "default", "ok": "1" if other_ok else "0",
             "error": "" if other_ok else "another query's error"}]
    with open(os.path.join(d, "props_split_adj_split.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def main():
    tmp = tempfile.mkdtemp(prefix="failed-runs-")
    os.environ["KVEVAL_OUTPUT_DIR"] = os.path.join(tmp, "output")
    from kvevaluator.measure import run_measure as M
    from kvcost.cli import runs
    wl = os.path.join(tmp, "w.json")
    json.dump({"id": "w", "name": "w", "items": [{"family": "interactive", "query": "ic7", "frequency": 1, "params": None},
                                                  {"family": "interactive", "query": "ic2", "frequency": 1, "params": None}]},
              open(wl, "w"))
    run = runs.load(argparse.Namespace(workload=wl, sf="0.1"))

    plant(run, ok_bad=False)
    failed = getattr(M, "failed_runs", lambda r: {})(run)      # absent on code without the rule: the control fails
    case("failed_runs: one failed run of ic2 on Sp-Sp (is1, not the run's, left out)",
         failed == {("props_split_adj_split", "ic2"): (1, "error (run): step fl (read_edge_exists): expected row, got vid")},
         failed)
    out = M.say_failed(run, failed, "REFUSED")
    case("say_failed names the query and its first error", "ic2: props_split_adj_split 1 failed runs" in out
         and "expected row, got vid" in out and "ic7:" not in out and "is1" not in out, out)

    plant(run, ok_bad=True)                              # the control: every run of the run's queries ok
    case("control: no failed run", getattr(M, "failed_runs", lambda r: {})(run) == {})

    # kvcost measure: the stores it measured (here Sp-Sp's reads; run_calls and the binary stubbed)
    import contextlib
    import io
    from kvcost.cli import cli, verbs as VB
    M.run_calls = lambda cs, jobs=1, log=print, **kw: (0, [(c[0], 0.0, 0, None) for c in cs])
    M.mark_captures = lambda cs, notes: None
    VB._binary_version = lambda: {"path": "stub", "binary_git_sha": "0" * 40, "binary_dirty": False}

    def kvcost(*argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()
    plant(run, ok_bad=False)
    rc, out = kvcost("measure", "--dataset", "ldbc=0.1", "--family", "interactive", "Sp-Sp", "--pick-on", "in-sample", "--no-check")
    case("measure: exit 1, the failed runs named (ic2 and is1 on Sp-Sp, the query set's queries)",
         rc == 1 and "ic2: props_split_adj_split 1 failed runs" in out and "is1: props_split_adj_split" in out
         and "expected row, got vid" in out, (rc, out[-600:]))
    plant(run, ok_bad=True, other_ok=True)
    rc, out = kvcost("measure", "--dataset", "ldbc=0.1", "--family", "interactive", "Sp-Sp", "--pick-on", "in-sample", "--no-check")
    case("measure control: every run ok, exit 0", rc == 0 and "runs failed" not in out, (rc, out[-400:]))

    # kvcost compose: the composed captures it wrote (compose_cells stubbed: it "writes" a failed run into composed/)
    from kvevaluator.output_tree import Tree
    composed = str(Tree().place_dir("composed", "ldbc", "0.1"))
    M.compose_call = lambda ds, sc, root=None, pset=None, cells=None, jobs=None: ([sys.executable, "-c", "pass", "--out", composed], None)

    def plant_composed(ok):
        d = os.path.join(composed, "interactive", "reported", "counts")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "props_condensed_adj_split.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["query", "param_idx", "body", "ok", "error"])
            w.writeheader()
            w.writerow({"query": "ic2", "param_idx": "1", "body": "index_walk", "ok": ok, "error": "" if ok == "1" else "boom"})
    VB.subprocess.run = lambda cmd, **kw: argparse.Namespace(returncode=0, stdout="installed at x\n", stderr="")
    plant_composed("0")
    rc, out = kvcost("compose", "--dataset", "ldbc=0.1")
    case("compose: exit 1, the failed run named", rc == 1 and "ic2: props_condensed_adj_split 1 failed runs" in out
         and "boom" in out, (rc, out[-400:]))
    plant_composed("1")
    rc, out = kvcost("compose", "--dataset", "ldbc=0.1")
    case("compose control: every run ok, exit 0", rc == 0, (rc, out[-400:]))

    n_ok = sum(results)
    print(f"check_failed_runs {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
