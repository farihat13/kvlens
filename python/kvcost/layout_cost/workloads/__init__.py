"""The texts behind the workloads the layout pricer prices (what each query does, what its params are) and
make_workloads.py, which writes the workload files a recipe computes. The workloads themselves are files:
inputs/workloads/<benchmark>/<id>.json (W01-W15, I01, I02 on LDBC SNB; L01-L04 on LinkBench, per scale; J01 the Join Order
Benchmark), read by benchmarks.workloads(). Not the plans: those are python/kvevaluator/workloads/<name>/plans/."""
