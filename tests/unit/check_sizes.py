#!/usr/bin/env python3
"""python/kvevaluator/sizes.py reads one --scale value the same way everywhere: each dataset's scale, dataset=scale (a
dataset named twice: its main scale, then a further one), the paper's by default; a value that names no dataset, or a
scale its dataset's manifest does not list, is refused; a dataset the run leaves out has no scale. And no driver, derive or registry line pairs a tag
dataset's size with an LDBC scale again (the old --job-scale / --linkbench-scale / TAGS= / "beside" forms). Under a second.

Run:  python3 tests/unit/check_sizes.py              (passes)
      python3 tests/unit/check_sizes.py --control    (a bare scale accepted as LDBC's: the refusal cases must fail)
"""
import argparse
import re
import sys

from kvevaluator import sizes
from kvevaluator.paths import ROOT

CASES = [
    (None, {"ldbc": ["3"], "linkbench": ["n1m"], "imdb": ["full"]}),             # the paper's (PAPER): no SF30
    ("ldbc=0.1,ldbc=0.3,imdb=sample1pct", {"ldbc": ["0.1", "0.3"], "imdb": ["sample1pct"]}),
    ("ldbc=0.1", {"ldbc": ["0.1"]}),
    ("ldbc=0.3,imdb=sample1pct", {"ldbc": ["0.3"], "imdb": ["sample1pct"]}),
    ("imdb=full,ldbc=3,ldbc=30", {"ldbc": ["3", "30"], "imdb": ["full"]}),   # the datasets' order, whatever the map's
]
REFUSED = ["ldbc=2", "imdb=n1m", "0.1", "paper", "small", "ldbc=0.1,ldbc=0.1", "job=full"]
OLD_FORMS = re.compile(r"--job-scale|--linkbench-scale|\bTAGS=|scale_beside|\"beside\"|--scale (small|paper)\b|sizes\.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--control", action="store_true")
    a = ap.parse_args()
    if a.control:
        bare = sizes.resolve
        sizes.resolve = lambda v: ({"ldbc": [v]} if re.fullmatch(r"[0-9.]+", str(v)) else bare(v))
    bad, n = [], 0
    for value, want in CASES:
        n += 1
        try:
            got = sizes.resolve(value)
        except SystemExit as e:
            got = f"refused: {e}"
        if got != want or list(got) != list(want):
            bad.append(f"{value}: {got} (want {want})")
    for value in REFUSED:
        n += 1
        try:
            sizes.resolve(value)
            bad.append(f"{value}: accepted (want refused)")
        except SystemExit:
            pass
    run = sizes.resolve("ldbc=0.1,ldbc=0.3,imdb=sample1pct")
    n += 3
    if sizes.scale(run, "linkbench") is not None or sizes.larger(run, "ldbc") != ["0.3"] or \
            sizes.text(run) != "ldbc=0.1,ldbc=0.3,imdb=sample1pct":
        bad.append(f"ldbc=0.1,ldbc=0.3,imdb=sample1pct: scale/larger/text {sizes.scale(run, 'linkbench')}, {sizes.larger(run, 'ldbc')}, {sizes.text(run)}")
    files = [p for d in ("run", "paper/derive", "experiments") for p in (ROOT / d).rglob("*") if p.suffix in (".py", ".sh")]
    for p in files:
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if OLD_FORMS.search(line):
                bad.append(f"{p.relative_to(ROOT)}:{i}: an old pairing form: {line.strip()[:100]}")
    print(f"check_sizes: {n} cases, {len(files)} files scanned for the old pairing forms; {len(bad)} failed")
    for b in bad:
        print("  " + b)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
