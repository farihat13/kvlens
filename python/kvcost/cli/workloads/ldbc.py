"""LDBC SNB Interactive as layout_choice prices it: the IC / IS reads (held-out params 11-15 for the pick), the writes
(10 primitives + IU1-8), and the microbench reads. Measured by kvcost measure (--workload i01, --family microbench)."""

from kvevaluator import fetch_dataset
from kvcost.layout_cost import benchmarks

NAME = "ldbc"
DATASET = "ldbc"
FAMILIES = benchmarks.families(DATASET)     # the families its loads read (compose.Composer.families)
SCALE_HELP = "the LDBC scale factor (0.1, 0.3, 3, 30)"


WEIGHTED = True


def prepare(a=None):
    pass


def view_name(a=None):
    return NAME


def data_missing(sf):
    return fetch_dataset.missing_message(DATASET, sf)
