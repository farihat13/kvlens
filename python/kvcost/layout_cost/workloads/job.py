"""The JOB workload's texts (suite `job`). The workload (J01: the Join Order Benchmark's 113 queries on IMDb, each once;
python/kvevaluator/workloads/job/) is inputs/workloads/imdb/j01.json, written by make_workloads.py. Reads only."""

JOB = "job"
PARAMS = {JOB: "one run per query: a JOB query has no parameter"}
QUERIES = {}       # the per-query texts live with the workload (workloads/job/); the solve does not need them
