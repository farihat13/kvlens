#!/usr/bin/env python3
"""Every paper figure's chain (run/reproduce.py FIGURES) prints and holds only the walkthrough's commands: for each
figure, `results` and `figure`, at the paper's datasets and at a small set, `--dry-run` exits 0, and every step it prints
is a kvcost command, a study script the registry names (STUDY_SCRIPTS), a derive or draw script, or a line naming a
dataset the run leaves out. Any other script fails it. An archive-only item (`results`: None) is checked as
`figure` with no --dataset (its derive and draw steps), and its `results` and `figure --dataset` must be refused. It checks the kind of each step only, not that a chain runs or
makes what its derive reads. Opens no store; seconds.

Run:  python3 tests/unit/check_registry.py              (passes)
      python3 tests/unit/check_registry.py --control    (one chain given a driver step: it must fail)
"""
import argparse
import importlib.util
import shlex
import subprocess
import sys

from kvevaluator import sizes
from kvevaluator.paths import ROOT

DATASETS = (sizes.PAPER, "ldbc=0.1,ldbc=0.3,imdb=sample1pct")
PLANT = "experiments/planted/run-planted-driver.sh"   # a driver no chain may run (the control)


def allowed(argv, study):
    """why a printed step is allowed, or None"""
    words = [w for w in argv if not w.endswith(("python", "python3"))]
    while words and (words[0] in ("env", "bash") or ("=" in words[0] and words[0].split("=")[0].isupper())):
        words = words[1:]           # `env VAR=value bash <script>`: the script decides
    if not words:
        return None
    head = words[0] if words[0] != "-m" else words[1]
    if head == "echo":
        return "a line"
    if head in ("kvcost", "kvcost.cli.cli") or (head == "uv" and "kvcost" in words):
        return "kvcost"
    if head.startswith(("paper/derive/", "paper/draw/")):
        return "derive or draw"
    if head in study:
        return "a named study script"
    return None


def steps(figure, action, dataset, plant):
    """the commands one --dry-run prints (lines starting `$ `); the planted control adds a driver step"""
    r = subprocess.run([sys.executable, "run/reproduce.py", action, figure,
                        *(["--dataset", dataset] if dataset else []), "--dry-run"], cwd=ROOT, capture_output=True, text=True)
    out = [shlex.split(line[2:]) for line in r.stdout.splitlines() if line.startswith("$ ")]
    if plant:
        out.append([PLANT, "--dataset", dataset])
    return r.returncode, r.stderr.strip().splitlines()[-1:] if r.returncode else [], out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--control", action="store_true")
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("reproduce", ROOT / "run" / "reproduce.py")
    reproduce = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reproduce)
    study = set(getattr(reproduce, "STUDY_SCRIPTS", ()))
    bad, n = [], 0
    archive = [f for f, x in reproduce.FIGURES.items() if x["results"] is None]
    for figure in archive:
        for action, dataset in (("results", None), ("results", sizes.PAPER), ("figure", sizes.PAPER)):
            if steps(figure, action, dataset, False)[0] == 0:
                bad.append(f"{figure} {action}{' --dataset ' + dataset if dataset else ''}: an archive-only item, not refused")
        rc, err, cmds = steps(figure, "figure", None, False)
        if rc:
            bad.append(f"{figure} figure: --dry-run exit {rc} {err}")
        for argv in cmds:
            n += 1
            if allowed(argv, study) is None:
                bad.append(f"{figure} figure: not a derive or draw script: {shlex.join(argv)[:150]}")
    for i, figure in enumerate(f for f in reproduce.FIGURES if f not in archive):
        for action in ("results", "figure"):
            # an item with scales of its own (its registry flags name another --dataset than the paper's) is checked
            # at those, where the others are checked at the paper's settings typed out
            own = reproduce.FIGURES[figure]["flags"].get("dataset", sizes.PAPER)
            own = own if own != sizes.PAPER else None
            for dataset in [own or DATASETS[0], *DATASETS[1:]]:
                rc, err, cmds = steps(figure, action, dataset, a.control and i == 0 and action == "results")
                if rc:
                    bad.append(f"{figure} {action} --dataset {dataset}: --dry-run exit {rc} {err}")
                for argv in cmds:
                    n += 1
                    if allowed(argv, study) is None:
                        bad.append(f"{figure} {action}: not a kvcost command or a named script: {shlex.join(argv)[:150]}")
    print(f"check_registry: {len(reproduce.FIGURES) - len(archive)} figures x 2 actions x {len(DATASETS)} dataset sets "
          f"and {len(archive)} archive-only items, {n} steps; "
          f"{len(bad)} failed")
    for b in sorted(set(bad))[:40]:
        print("  " + b)
    sys.exit(1 if bad or n == 0 else 0)


if __name__ == "__main__":
    main()
