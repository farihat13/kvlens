"""LinkBench on its own axis (a scale tag, n1m) and a params source: its reads and its writes. The published counts
are the whole request log (--params log, 100,000 requests). Measured by
kvcost measure --dataset linkbench=n1m --workload l01 (the param set: --params, default log)."""

from kvevaluator import fetch_dataset
from kvevaluator.datasets.linkbench import params as lb_params
from kvcost.layout_cost import benchmarks

NAME = "linkbench"
DATASET = "linkbench"
FAMILIES = benchmarks.families(DATASET)     # the families its loads read (compose.Composer.families)
SCALE_HELP = "LinkBench's scale tag (tiny: 1,000 nodes, the smallest, with --params quick; n1m: 1M nodes)"
WEIGHTED = True
NPARAM = {"log": 100000}    # the whole log; a sample (quick, quick-next) or the anchors take workloads.NPARAM
DEFAULT_PARAM_SET = lb_params.DEFAULT_PARAM_SET   # the whole request log, as published (a layout-costs place names
                                                  # no params level for it: layout_costs.place_of_run)
_P = [DEFAULT_PARAM_SET]


def prepare(a=None):
    """the param set (--params, read as a.param_set): part of the capture's identity (compose.Composer.from_args,
    output_tree.params_dir); default the whole request log"""
    if a is not None and getattr(a, "param_set", None):
        _P[0] = a.param_set
    if a is not None:
        a.param_set = _P[0]


# each request type is a single plan and the capture has no held-out set: the pick is in-sample, as in
# experiments/mix_benefit/per_query_disagreement.py (the planner's one body); cli.plan_rule reads it
IN_SAMPLE = True


def view_name(a=None):
    return f"{NAME}-{_P[0]}"


def data_missing(sf):
    return fetch_dataset.missing_message(DATASET, sf)   # the data, the request log and its params
