"""LDBC BI on the LDBC store: its 17 read queries, 5 params each, priced in-sample by policy (python/kvcost/layout_cost/picks.py,
IN_SAMPLE_WORKLOADS). Captured under <LDBC grid root>/bi by experiments/meta/run-bi-cells.sh --stage counts."""

from kvevaluator import fetch_dataset
from kvcost.layout_cost import benchmarks

NAME = "bi"
DATASET = "ldbc"              # BI is a read family of the LDBC store's suite, priced as its only one
FAMILIES = benchmarks.BI        # the families its loads read (compose.Composer.families)
SCALE_HELP = "the LDBC scale factor (0.1, 0.3, 3)"
WEIGHTED = False            # BI defines no query frequencies: once each only


def prepare(a=None):
    pass


def view_name(a=None):
    return NAME


def data_missing(sf):
    return fetch_dataset.missing_message(DATASET, sf)
