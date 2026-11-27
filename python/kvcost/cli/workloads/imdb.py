"""The Join Order Benchmark on IMDb (a data tag: sample1pct, full): its 113 read queries, in-sample by policy, on the
nine cells and on the three named formats. Driver: experiments/job/run-job-counts-chain.sh (the cells with
--cells-only; a named format runs the chain with the formats, which measures all three: the chain has no per-format
flag)."""

from kvevaluator import fetch_dataset
from kvcost.layout_cost import benchmarks

NAME = "job"                    # the workload a view names
DATASET = "imdb"
FAMILIES = benchmarks.families(DATASET)     # the families its loads read (compose.Composer.families)
SCALE_HELP = "IMDb's data tag (sample1pct, full)"
WEIGHTED = True
NAMED_OK = True
NPARAM = {None: 1}      # each JOB query has its one param (the paper's capture: nparam 1)
TIMEOUT = 3600          # a JOB body's seconds before it is not ok (the paper's capture; at full, some bodies run long)


def prepare(a=None):
    pass


def view_name(a=None):
    return NAME


def data_missing(sf):
    return fetch_dataset.missing_message(DATASET, sf)   # the tables and JOB's reference answers
