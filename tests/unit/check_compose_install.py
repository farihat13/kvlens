#!/usr/bin/env python3
"""A composed capture is composed again on every run, never reused by a key on its sources (compose_cells.install):
its value also depends on our own code, which no such key covers. Store-free, on the golden 2-cell capture
(tests/fixtures/golden/kvcost_capture.tar.gz); seconds.

  1-4. install() on small trees: a new dst is installed; a byte-identical one is kept (no aside, tmp removed); a
       changed file is moved aside and the new one installed; a difference in sources.json alone is kept.
  4a-c. a query set composed for the first time is added with no aside, the files there untouched; a file of a query
       set this compose wrote that it no longer writes is moved aside, another query set's files stay; with
       whole=False (--cells) the other layouts stay.
  5-6. compose_cells --replace: a second run is kept; the swap-sources control (another output from the same sources,
       as a code change gives) replaces it, the old grid moved aside.
  7.   the bug's case: the capture in place differs from what today's code composes, its sources unchanged (a file
       edited by hand stands in for output of older code): composing again (--replace, as kvcost compose runs
       the composer on every run) replaces it and moves the old one aside.
  8.   an --out not beside --grid, or --chunked-from, without --whole-grid is refused.

    python3 tests/unit/check_compose_install.py
"""
import glob
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from kvevaluator.paths import ROOT
from kvcost.layout_cost.compose_cells import install
from kvevaluator.output_tree import capture_in

FIXTURE = ROOT / "tests" / "fixtures" / "golden" / "kvcost_capture.tar.gz"
fails = cases = 0


def check(cond, what):
    global fails, cases
    cases += 1
    print(("PASS " if cond else "FAIL ") + what)
    fails += 0 if cond else 1


def tree(root, files):
    for rel, text in files.items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return str(root)


def asides(path):
    return sorted(glob.glob(f"{path}.aside-*"))


def run(cmd):
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def main():
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        # 1-4: install() on small trees
        dst = t / "a" / "dst"
        v, _ = install(tree(t / "a" / "t1", {"runs/x.csv": "1\n"}), str(dst))
        check(v == "new" and (dst / "runs/x.csv").read_text() == "1\n", "1 a new dst is installed")
        (dst / "sources.json").write_text("{}\n")
        mtime = (dst / "runs/x.csv").stat().st_mtime_ns
        tmp = tree(t / "a" / "t2", {"runs/x.csv": "1\n"})
        v, _ = install(tmp, str(dst))
        check(v == "kept" and not os.path.exists(tmp) and not asides(dst) and (dst / "runs/x.csv").stat().st_mtime_ns == mtime,
              "2 a byte-identical dst is kept (tmp removed, no aside, the file untouched)")
        v, aside = install(tree(t / "a" / "t3", {"runs/x.csv": "2\n"}), str(dst))
        check(v == "replaced" and (dst / "runs/x.csv").read_text() == "2\n" and aside and
              (Path(aside) / "runs/x.csv").read_text() == "1\n", "3 a changed file is moved aside, the new one installed")
        v, _ = install(tree(t / "a" / "t4", {"runs/x.csv": "2\n", "sources.json": "{\"other\": 1}\n"}), str(dst))
        check(v == "kept", "4 a difference in sources.json alone (provenance) is kept")
        n_aside, mtime = len(asides(dst)), (dst / "runs/x.csv").stat().st_mtime_ns
        v, aside = install(tree(t / "a" / "t5", {"runs/x.csv": "2\n", "bi/y.csv": "7\n"}), str(dst))
        check(v == "added" and aside is None and len(asides(dst)) == n_aside and (dst / "bi/y.csv").read_text() == "7\n"
              and (dst / "runs/x.csv").stat().st_mtime_ns == mtime,
              "4a a query set composed for the first time is added: no aside, the files there untouched")
        (dst / "bi/old.csv").write_text("9\n")
        v, aside = install(tree(t / "a" / "t6", {"bi/y.csv": "7\n"}), str(dst))
        check(v == "replaced" and (Path(aside) / "bi/old.csv").read_text() == "9\n" and not (dst / "bi/old.csv").exists()
              and (dst / "runs/x.csv").read_text() == "2\n" and sorted(os.listdir(aside)) == ["bi"],
              "4b a file this compose no longer writes in a query set it wrote is moved aside; the other query set stays")
        (dst / "bi/old.csv").write_text("9\n")
        v, aside = install(tree(t / "a" / "t7", {"bi/y.csv": "7\n"}), str(dst), whole=False)
        check(v == "kept" and aside is None and (dst / "bi/old.csv").exists(),
              "4c whole=False (named layouts): the other layouts' files stay")

        cap = t / "capture"
        with tarfile.open(FIXTURE) as tf:
            tf.extractall(cap, filter="data")
        # 5-6: compose_cells --replace
        out = t / "grid"
        base = [sys.executable, "-m", "kvcost.layout_cost.compose_cells", "--grid", str(cap), "--dataset", "ldbc",
                "--cells", "props_split_adj_condensed", "--out", str(out), "--replace", "--whole-grid"]
        rc1, _ = run(base)
        rc2, o2 = run(base)
        check(rc1 == 0 and rc2 == 0 and "kept" in o2.splitlines()[-1] and not asides(out), "5 compose_cells --replace, run again: kept")
        rc3, o3 = run(base + ["--control", "swap-sources"])
        check(rc3 == 0 and "moved aside" in o3.splitlines()[-1] and len(asides(out)) == 1,
              "6 another output from the same sources (the swap-sources control) replaces it, the old one aside")

        # 7: the bug's case, as kvcost compose runs the composer (--replace on every run): the capture in place differs
        # from what today's code composes, its sources unchanged (a file edited by hand stands in for older code's output)
        run(base)                                  # today's output back in place (case 6 left the swapped one)
        runs = sorted(Path(capture_in(out, "interactive").dir("counts")).glob("props_split_adj_condensed*.csv"))
        good = runs[0].read_text()
        runs[0].write_text(good.replace(",", ";", 1))
        n_aside = len(asides(out))
        rc6, o6 = run(base)
        check(rc6 == 0 and "moved aside" in o6.splitlines()[-1] and runs[0].read_text() == good
              and len(asides(out)) == n_aside + 1,
              "7 a capture in place that today's code would not compose (sources unchanged) is replaced, the old one aside")

        # 8: the two forms are named, never guessed: an --out that is not the composed place beside --grid needs
        # --whole-grid, and --chunked-from does too
        rc8, o8 = run([x for x in base if x != "--whole-grid"])
        check(rc8 != 0 and "--whole-grid" in o8, "8 an --out not beside --grid, without --whole-grid: refused")
        rc9, o9 = run([x for x in base if x != "--whole-grid"] + ["--chunked-from", str(cap), "props_chunked_adj_chunked"])
        check(rc9 != 0 and "--whole-grid" in o9, "8 --chunked-from without --whole-grid: refused")
    print(f"{cases} cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
