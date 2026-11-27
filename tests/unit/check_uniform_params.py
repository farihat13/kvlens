#!/usr/bin/env python3
"""The uniform param set's wiring (the param set as an identity axis: kvevaluator.datasets.ldbc.params PARAM_SETS,
output_tree.params_dir, --param-set), on the tracked sf0.1 files, with no data and no store:

  1. the default set, every IC/IS query reads LDBC's file; param_set="uniform", the uniform file of the same name, a
     borrow (IS1-3, params_like) included; the forum anchors and an unknown set are refused; an explicit source
     ("hosted") ignores the set; "ldbc" names the default; a workload with no second set refuses one;
  2. the uniform files keep LDBC's rows 1-n except the bound id: every other column equal, the id changed, each query
     its own draw (more than n persons over the IC files); every file's sha256 is its meta's;
  3. a capture at param set uniform sits in <workload>/uniform, whose held-out capture is LDBC's held-out;
     grid_bodies refuses --params with --param-set (the held-out rows are LDBC's);
     no tool reads a param set from the environment any more, and a leftover LDBC_PARAMS is refused;
  4. the accuracy join refuses a measured capture of another param set.

    python3 tests/unit/check_uniform_params.py
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT
from kvevaluator.datasets.ldbc import params as P
from kvevaluator import output_tree as T
from kvevaluator.measure import workload_plans as W

SF = "0.1"
IC = [f"ic{n}" for n in range(1, 13)]
IS = [f"is{n}" for n in range(1, 8)]
fails, checks = [], 0


def check(ok, what):
    global checks
    checks += 1
    if not ok:
        fails.append(what)


def refused(fn):
    try:
        fn()
    except (SystemExit, ValueError):
        return True
    return False


def part1():
    udir = P.uniform_dir(SF)
    for q in IC + IS:
        ldbc = P.param_file(SF, q)
        uni = P.param_file(SF, q, param_set="uniform")
        check(os.path.dirname(ldbc) != udir, f"{q}: the default set reads {ldbc}")
        check(uni == os.path.join(udir, os.path.basename(ldbc)), f"{q}: uniform reads {uni}")
        check(P.param_file(SF, q, source="hosted", param_set="uniform") == P.param_file(SF, q, source="hosted"),
              f"{q}: an explicit source does not ignore the set")
    check(refused(lambda: P.param_rows(SF, "forum_board_render", param_set="uniform")), "forum anchors not refused")
    check(refused(lambda: P.param_file(SF, "ic1", param_set="bogus")), "param set bogus not refused")
    check(P.param_file(SF, "ic7", param_set="ldbc") == P.param_file(SF, "ic7"), "ldbc != the default")
    check(W.query_params("interactive", SF, "ic7", 2, "uniform") == P.query_params(SF, "ic7", 2, param_set="uniform"),
          "workload_plans.query_params does not pass the set")
    check(refused(lambda: W.query_params("bi", SF, "bi1", 2, "uniform")), "a workload with one set takes uniform")
    check(T.params_dir("interactive", "ldbc") == "reported" and T.params_dir("interactive", None) == "reported",
          "the default set's dir is not reported")
    check(refused(lambda: T.params_dir("bi", "uniform")), "bi has a uniform dir")
    # LinkBench's param sets are the same argument: its first set (anchors) is `reported`, the others their own names;
    # None is its one default, the whole request log, everywhere
    from kvevaluator.datasets.linkbench import params as LP
    from kvcost.cli.workloads import linkbench as LA
    check(LA.DEFAULT_PARAM_SET == LP.DEFAULT_PARAM_SET == T.default_param_set("linkbench") == LP.check_param_set(None)
          == "log", "LinkBench: None is not the one default (log) in the params module, the adapter and the tree")
    check(all(T.params_dir("linkbench", s) == (s if s != "anchors" else "reported") for s in LP.PARAM_SETS)
          and T.params_dir("linkbench", None) == "log", "LinkBench's sets do not name their dirs, or None is not log/")
    check([T.checked_param_set("linkbench", s) for s in (None, "anchors", "log")] == [None, "anchors", "log"]
          and [T.Capture(f"/x/linkbench/{d}").param_set() for d in ("reported", "log")] == ["anchors", "log"]
          and T.checked_param_set("interactive", "ldbc") is None and T.Capture("/x/interactive/reported").param_set()
          is None, "a named set does not keep its name (LinkBench's anchors read as the default)")
    check(refused(lambda: T.params_dir("linkbench", "uniform")) and refused(lambda: LP.check_param_set("C")),
          "an unknown LinkBench set is not refused")
    check(refused(lambda: W.query_params("linkbench", "n1m", "getnode", 1, "uniform")), "linkbench takes uniform")
    # LinkBench's writes at a set sit beside its reads at that set
    check(all(T.writes_in("/x", "linkbench", s).params() == T.capture_in("/x", "linkbench", s).params()
              for s in LP.PARAM_SETS), "a LinkBench set's writes are not its reads' set")


def part2():
    persons = None
    for q in IC + ["is4", "is5", "is6", "is7"]:
        ldbc = P.param_rows(SF, q)
        uni = P.param_rows(SF, q, param_set="uniform")
        idcol = "personId" if q in IC else "messageId"
        check(len(uni) >= 1 and len(ldbc) >= len(uni), f"{q}: {len(uni)} uniform rows, {len(ldbc)} LDBC rows")
        for a, b in zip(ldbc, uni):
            check(list(a) == list(b), f"{q}: columns {list(a)} vs {list(b)}")
            keep = [c for c in a if c not in (idcol, "kind", "depth", "replies")]
            check(all(a[c] == b[c] for c in keep), f"{q}: a column other than the bound id changed")
        check(any(a[idcol] != b[idcol] for a, b in zip(ldbc, uni)), f"{q}: no {idcol} changed")
        if q in IC:
            persons = (persons or set()) | {r["personId"] for r in uni}
        args = P.query_params(SF, q, 1, param_set="uniform")[0]
        check(str(args.get("person", args.get("message"))) == uni[0][idcol], f"{q}: the args bind {args}")
    check(len(persons) > 5, f"the IC files bind {len(persons)} persons: one draw per query expected")
    meta = json.load(open(os.path.join(P.uniform_dir(SF), "uniform_params.meta.json")))
    for name, m in meta["files"].items():
        path = os.path.join(P.uniform_dir(SF), name)
        check(hashlib.sha256(open(path, "rb").read()).hexdigest() == m["sha256"], f"{name}: sha256 is not the meta's")


def part3():
    rep = T.Tree("/x").capture("interactive", SF)
    uni = T.Tree("/x").capture("interactive", SF, "uniform")
    check(rep.path.endswith(os.path.join("interactive", "reported")), f"default: {rep.path}")
    check(uni.path.endswith(os.path.join("interactive", "uniform")) and uni.params() == "uniform", f"uniform: {uni.path}")
    check(uni.held_out().path == rep.held_out().path, f"uniform's held-out is not LDBC's: {uni.held_out().path}")
    check(T.writes_in("/x", "ldbc").path.endswith(os.path.join("writes", "reported")), "writes: not reported")
    # a spec dir that does not exist: without the refusal it fails at once on that, not after a measurement
    r = subprocess.run([sys.executable, "-m", "kvevaluator.measure.grid_bodies", "--sf", SF, "--params", "11,12",
                        "--param-set", "uniform", "--root", "/nonexistent", "--spec-dir", "/nonexistent"], cwd=ROOT,
                       capture_output=True, text=True)
    check(r.returncode != 0 and "--param-set uniform" in r.stderr + r.stdout, f"grid_bodies --params: rc {r.returncode}")
    # the set is a flag: the old environment variable is named only where it is refused (python/, run/, experiments/)
    r = subprocess.run(["grep", "-rlI", "--include=*.py", "--include=*.sh", "LDBC_PARAMS", "python", "run", "experiments"],
                       cwd=ROOT, capture_output=True, text=True)
    check(r.stdout.split() == ["python/kvevaluator/datasets/ldbc/params.py"], f"LDBC_PARAMS named: {r.stdout.split()}")
    os.environ["LDBC_PARAMS"] = "uniform"
    try:
        check(refused(lambda: P.query_params(SF, "ic1", 1)), "LDBC_PARAMS set: not refused")
        check(refused(lambda: P.query_params(SF, "ic1", 1, param_set="uniform")), "LDBC_PARAMS set, a set given")
    finally:
        del os.environ["LDBC_PARAMS"]
    check(len(P.query_params(SF, "ic1", 1)) == 1, "LDBC_PARAMS unset: the default set read again")


def part4():
    """the accuracy join refuses a measured capture of the other param set"""
    from kvcost.predict_accuracy import join as J
    with tempfile.TemporaryDirectory() as t:
        set_dir = os.path.join(t, "captures", "ldbc", "sf0.1")
        for rows in ("reported", "uniform"):
            os.makedirs(os.path.join(set_dir, "interactive", rows))
        check(J.measured_capture(set_dir, "interactive", "uniform").params() == "uniform", "join: place dir")
        check(J.measured_capture(set_dir, "interactive").params() == "reported", "join: place dir, the default set")
        rep, uni = (os.path.join(set_dir, "interactive", r) for r in ("reported", "uniform"))
        check(refused(lambda: J.measured_capture(rep, "interactive", "uniform")), "join: LDBC rows for uniform")
        check(refused(lambda: J.measured_capture(uni, "interactive")), "join: uniform rows for the default set")


def main():
    part1()
    part2()
    part3()
    part4()
    for f in fails[:10]:
        print("  FAIL", f)
    print(f"check_uniform_params {'FAIL' if fails else 'OK'}: {checks} checks, {len(fails)} failed")
    return 1 if fails or not checks else 0


if __name__ == "__main__":
    sys.exit(main())
