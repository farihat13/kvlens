#!/usr/bin/env python3
"""JOB's data step on one IMDb data tag (its manifest's data_step): the reference answers on the tag's tables
(reference.py: the 113 answers, the alias and entry counts), made only if missing. `just fetch-dataset imdb=<tag>` runs
it after the dataset's own step (kvevaluator.datasets.imdb.fetch: the downloads and the tables); every store is checked
against these answers. sample1pct: ~4 s, full: ~16 s.

The data step's calls (kvevaluator.fetch_dataset): missing(tag), ensure(tag), check(tag).
"""
import os
import subprocess
import sys
import time

from kvevaluator.datasets.imdb import paths

HERE = os.path.dirname(os.path.abspath(__file__))
REFERENCE_FILES = ("reference_answers.csv", "alias_counts.csv", "entry_counts.csv")    # reference.py writes these, in order


def missing(tag):
    """[the reference files not in place] of the tag"""
    return [os.path.join(paths.tag_dir(tag), f) for f in REFERENCE_FILES
            if not os.path.exists(os.path.join(paths.tag_dir(tag), f))]


def ensure(tag):
    """the tag's reference answers, made when missing (the tag's tables must be built); [problem]"""
    if not missing(tag):
        print(f"[job] {tag}: reference answers in place", flush=True)
        return []
    t0 = time.monotonic()
    print(f"[job] reference --tag {tag}", flush=True)
    subprocess.run([sys.executable, os.path.join(HERE, "reference.py"), "--tag", tag], check=True)
    print(f"[job] reference --tag {tag}: done ({time.monotonic() - t0:.0f} s)", flush=True)
    return []


def check(tag):
    """the reference files are in place; ([problem], files checked). They have no tracked hash: reference.py computes
    them from the tables, whose dump the dataset's check holds to its pin"""
    bad = [f"{p}: missing" for p in missing(tag)]
    for b in bad:
        print(f"  BAD job {tag}: {b}")
    n = len(REFERENCE_FILES) - len(bad)
    print(f"[job] {tag}: {n} of {len(REFERENCE_FILES)} reference files in place (no tracked hash)", flush=True)
    return bad, n
