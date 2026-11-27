"""A drawing script never writes into the paper's runs: kvevaluator.paths' output_twin moves a path under
paper-results/ to the same place under the output dir, refuse_results refuses an --out there (also through a linked
paper-results), figure_dir combines the two, and benchmarks.plots_dir applies them to an analysis dir. Each case runs in
a fresh interpreter on a copy of kvevaluator whose root is a temp dir (its paper-results/ a link) with its own output
dir; a planted paths.py that never sees paper-results must fail.

    python tests/unit/check_figure_dir.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kvevaluator.paths import ROOT

# (name, the code, what it must print, or None: it must exit 1 with "refused"); R = paper-results, O = output, X = elsewhere
CASES = [
    ("a results path moves under the output dir", "print(P.output_twin(R + '/sf3/a/plots'))", "{O}/sf3/a/plots"),
    ("a path elsewhere stays", "print(P.output_twin(X + '/plots'))", "{X}/plots"),
    ("the linked results dir's real path is caught", "print(P.output_twin(REAL + '/sf3/a'))", "{O}/sf3/a"),
    ("an --out under the results is refused", "P.refuse_results(R + '/sf3/a')", None),
    ("an --out under the results' real path is refused", "P.refuse_results(REAL + '/sf3/a')", None),
    ("an --out elsewhere is kept", "print(P.refuse_results(X + '/out'))", "{X}/out"),
    ("figure_dir: no --out, a results default", "print(P.figure_dir(R + '/sf3/a/plots'))", "{O}/sf3/a/plots"),
    ("figure_dir: an --out under the results", "P.figure_dir(X + '/plots', R + '/b')", None),
    ("plots_dir: a results analysis dir", "print(B.plots_dir(R + '/sf3/lc/i01'))", "{O}/sf3/lc/i01/plots"),
    ("plots_dir: with --out elsewhere", "B.use_plots_out(X + '/figs'); print(B.plots_dir(R + '/sf3/lc/i01'))", "{X}/figs/i01/plots"),
    ("plots_dir: an --out under the results", "B.use_plots_out(R + '/figs')", None),
]
PRELUDE = ("import os, sys; R, O, X, REAL = sys.argv[1:5]; import kvevaluator.paths as P; "
           "from kvcost.layout_cost import benchmarks as B; ")


def run(code, d, pythonpath=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("KVEVAL_")}
    env.update(KVEVAL_DATA_DIR=str(d / "data"), KVEVAL_OUTPUT_DIR=str(d / "out"))
    if pythonpath:
        env["PYTHONPATH"] = pythonpath + os.pathsep + env.get("PYTHONPATH", "")
    args = [str(d / "paper-results"), str(d / "out"), str(d / "elsewhere"), str(d / "real-results")]
    return subprocess.run([sys.executable, "-c", PRELUDE + code, *args], capture_output=True, text=True, env=env)


def cases(d, pythonpath=None):
    """the cases' failures, [(name, why)]"""
    bad = []
    for name, code, want in CASES:
        r = run(code, d, pythonpath)
        if want is None:
            if r.returncode != 1 or "refused" not in r.stderr:
                bad.append((name, f"not refused (rc {r.returncode}): {(r.stdout + r.stderr).strip()[-200:]}"))
        else:
            want = want.format(O=d / "out", X=d / "elsewhere")
            if r.returncode != 0 or r.stdout.strip() != want:
                bad.append((name, f"rc {r.returncode}, got {(r.stdout + r.stderr).strip()[-200:]!r}, want {want!r}"))
    return bad


def main():
    with tempfile.TemporaryDirectory() as t:
        d = Path(t)
        (d / "data").mkdir()
        (d / "real-results").mkdir()
        (d / "paper-results").symlink_to(d / "real-results")    # a linked paper-results, as a copy on another disk
        (d / "out").mkdir()
        (d / "inputs").mkdir()
        (d / "inputs" / "contract").symlink_to(ROOT / "inputs" / "contract")   # the copies' root's shared data files
        copy = d / "copy"      # kvevaluator as it is, its root the temp dir (paths.py: ROOT = its file's parents[2])
        shutil.copytree(ROOT / "python" / "kvevaluator", copy / "kvevaluator", ignore=shutil.ignore_patterns("__pycache__"))
        bad = cases(d, pythonpath=str(copy))
        # the control: a paths.py that never finds a path under paper-results must fail the cases that need it
        plant = d / "plant"
        shutil.copytree(ROOT / "python" / "kvevaluator", plant / "kvevaluator", ignore=shutil.ignore_patterns("__pycache__"))
        src = (ROOT / "python" / "kvevaluator" / "paths.py").read_text()
        cut = "def _under_results(path):\n"
        assert src.count(cut) == 1, "paths.py has no _under_results: the plant does not apply"
        (plant / "kvevaluator" / "paths.py").write_text(src.replace(cut, cut + "    return None\n"))
        caught = cases(d, pythonpath=str(plant))
    must = {name for name, _, _ in CASES if "elsewhere" not in name and "kept" not in name}   # the cases that touch the results
    for name, why in bad:
        print(f"FAIL {name}: {why}")
    for name, why in caught:
        if name not in must:
            print(f"FAIL control {name}: the planted paths.py fails a case that does not touch the results: {why}")
    print(f"check_figure_dir: {len(CASES)} cases, {len(bad)} failing; the planted paths.py fails {len(caught)} of them "
          f"(must: the {len(must)} that touch the results)")
    ok = not bad and {name for name, _ in caught} == must
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
