"""I01's per-query class (IC, IS, IU) and its weights: LDBC SNB Interactive's own mix, per 100,000 updates."""
from kvcost.layout_cost import benchmarks


def qclass(q):
    return "IC" if q.startswith("ic") else "IS" if q.startswith("is") else "IU" if q.startswith("iu") else "other"


def weights():
    """{query: count per 100,000 updates} of I01; IU included"""
    (i01,) = [w for w in benchmarks.workloads("ldbc", "3").values() if w["id"] == "i01"]
    return {q: n for _, q, n, _ in i01["items"]}
