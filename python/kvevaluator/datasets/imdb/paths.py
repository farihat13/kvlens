"""Where the JOB workload's data lives: <data>/datasets/imdb (KVEVAL_IMDB_DIR; rebuilt by fetch.py and
build_tables.py): raw/ and job-queries/ as fetched, <mapping>/<tag>/ as built. The data tag (`full`, `sample1pct`) plays the role LDBC's scale factor plays."""
import os
import sys

from kvevaluator.datasets.imdb.mapping import MAPPING  # (a plain module: the dataset scripts run without the evaluator package)
from kvevaluator.paths import ROOT, imdb_dir

EXP = str(ROOT)
ROOT = str(imdb_dir())
TAGS = ("full", "sample1pct")


def raw_dir():
    return os.path.join(ROOT, "raw")


def queries_dir():
    return os.path.join(ROOT, "job-queries")


def tag_dir(tag):
    if tag not in TAGS:
        raise SystemExit(f"unknown IMDb data tag {tag!r} (one of {', '.join(TAGS)})")
    return os.path.join(ROOT, MAPPING, tag)      # the mapping decides the tables' merge and the files' shape


def tables_db(tag):
    return os.path.join(tag_dir(tag), "tables.duckdb")


def forced_titles():
    """the titles the sample keeps (tracked, beside this file): JOB's reference writes it (--witnesses), build_tables reads it"""
    return os.path.join(EXP, "python", "kvevaluator", "datasets", "imdb", "sample_forced_titles.csv")
