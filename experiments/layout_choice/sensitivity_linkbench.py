#!/usr/bin/env python3
"""Does the LinkBench layout ranking survive the modeling choices that favor one layout? Analysis only: it
re-prices the LinkBench workloads (inputs/workloads/linkbench/<scale>/) on the 9 uniform cells from the grid capture, once as
measured and once per change below, and ranks the cells in total ops and total bytes (compose.currencies).

  -upsert_get   Split adjacency's updateLink GETs the edge first ("update if it exists"). LinkBench's updateLink
                sets every field, so a blind PUT would do: drop that GET.
  -hub          drop every request on the highest-degree node (4 of the 100,000 logged requests at n1m)
                -- how much one node decides. (At n1m: node 389121, 961,964 links = 480,982 per link type.)
  -upsert_get-hub  both.
(A Condensed list write's read of the old list is MEASURED -- 1 SEEK, plus 1 NEXT per part over the
split target, with its bytes -- so the former `+list_reads` case, which charged it by hand, is gone.)
  sample_A / _B (params source `all` only) each query's first k requests / its next k, k = min(2,000, half):
                the two samples the heavy-tail check compares (reported as not measured when the capture holds
                fewer requests, e.g. a smoke run's --nparam 20).

Mixed layouts are not re-priced: on LinkBench the best mixed layout is a uniform one (python/kvcost/layout_cost/compose.py).
Output: <the suite's layout-choice dir>/sensitivity.csv (long: workload x cell x case) + a printed table.

    experiments/layout_choice/run-layout-choice.sh --dataset linkbench --scale n1m --params log      # runs it as its last step
"""
import argparse
import collections
import copy
import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from kvcost.layout_cost import benchmarks
from kvevaluator.output_tree import Tree
from kvevaluator.datasets.linkbench.params import check_param_set, request_params_path
from kvevaluator.datasets import data_in
from kvcost.layout_cost.labels import display_title
from kvcost.layout_cost import compose
from kvcost.layout_cost.compose import currencies

DATASET, READS, WRITES = "linkbench", "linkbench", "linkbench_writes"
SAMPLE = 2000        # python/kvevaluator/datasets/linkbench/gen_linkbench_params.py --per-query default: the A / B sample size


def params_rows(scale, source, query):
    with open(request_params_path(scale, source, query)) as f:
        return [json.loads(line) for line in f]


def hub_node(scale):
    """the node with the most links (out-lists only, both link types)"""
    deg = collections.Counter()
    with open(data_in("linkbench", scale) / "data" / "links.jsonl") as f:
        for line in f:
            deg[json.loads(line)["id1"]] += 1
    node, n = deg.most_common(1)[0]
    return node, n


def without_upsert_get(data):
    """-upsert_get on every Split-adjacency cell: updateLink's only read is the edge GET (checked); drop it"""
    out = copy.deepcopy(data)
    for cell in compose.CELLS:
        if not cell.endswith("_adj_split"):
            continue
        for q, per in out[cell][WRITES].items():
            if not q.startswith("updatelink_"):
                continue
            for p, v in per.items():
                if v["write_rmw_ops"] > 1.0 + 1e-9:
                    raise SystemExit(f"{cell} {q} p{p}: expected one GET before the write, got {v['write_rmw_ops']}")
                v["write_rmw_ops"] = v["write_rmw_bytes"] = 0.0
    return out


def restrict(workload, keep, scale):
    """the workload with each item's params restricted to keep[query] (1-based indices).
    scale=True: how often shrinks by the kept share -- with params `all` (every logged request), mean x count over
    the kept ones = their exact sum (dropping requests). scale=False: how often stays -- the kept requests' mean
    stands for all of the query's requests (a sample's estimate of the whole workload)."""
    w = dict(workload)
    items = []
    for fam, q, often, params in workload["items"]:
        idx, n = keep[q]
        items.append((fam, q, often * len(idx) / n if scale else often, idx))
    w["items"] = items
    return w


def ranks(costs, currency):
    order = sorted(costs, key=lambda c: (costs[c][currency], c))
    return {c: i + 1 for i, c in enumerate(order)}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--scale", required=True)
    benchmarks.add_workload_arg(ap)
    benchmarks.add_pick_by_arg(ap)
    benchmarks.add_params_arg(ap)
    benchmarks.add_chunk_view_arg(ap)
    a = ap.parse_args()
    source = check_param_set(a.param_set)
    root = str(Tree().place_dir("captures", DATASET, a.scale))
    base = compose.load_measured(DATASET, root, "ops", rule=compose.PlanRule(), param_set=a.param_set, view=compose.resolve_chunk_view(a.chunk_view))
    data = {"measured": base, "-upsert_get": without_upsert_get(base)}
    hub, hub_links = hub_node(a.scale)

    rows = []
    for name, w in benchmarks.select(DATASET, a.scale, a.workload).items():
        queries = [q for _, q, _, _ in w["items"]]
        params = {q: params_rows(a.scale, source, q) for q in queries}
        cases = {k: (w, d) for k, d in data.items()}
        on_hub = lambda p: hub in (p.get("node"), p.get("src"))
        no_hub = restrict(w, {q: ([i for i, p in enumerate(ps, 1) if not on_hub(p)], len(ps))
                              for q, ps in params.items()}, scale=True)
        cases["-hub"] = (no_hub, base)
        cases["-upsert_get-hub"] = (no_hub, data["-upsert_get"])
        if source == "all":
            for s, lo in (("sample_A", 0), ("sample_B", 1)):
                keep = {}
                for q, ps in params.items():
                    k = min(SAMPLE, len(ps) // 2)
                    keep[q] = (list(range(lo * k + 1, lo * k + k + 1)), len(ps))
                cases[s] = (restrict(w, keep, scale=False), base)
        costs = {}
        for case, (wc, d) in cases.items():
            try:
                costs[case] = {cell: currencies(compose.workload_cost(wc, d[cell])) for cell in compose.CELLS}
            except compose.Unmeasured as e:   # a sample beyond what the capture measured (a smoke run)
                if not case.startswith("sample_"):
                    raise
                print(f"   {case}: not measured ({str(e)[:80]}...)")
        rk = {case: {cur: ranks(c, cur) for cur in ("ops", "bytes")} for case, c in costs.items()}
        for case, c in costs.items():
            for cell in compose.CELLS:
                rows.append({"scale": a.scale, "params": source, "workload": name, "cell": cell, "case": case,
                             "ops": round(c[cell]["ops"], 3), "bytes": round(c[cell]["bytes"], 1),
                             "rank_ops": rk[case]["ops"][cell], "rank_bytes": rk[case]["bytes"][cell]})
        # the printed view: every cell, ops and bytes per case, sorted by measured ops
        print(f"\n== {display_title(w)} (LinkBench {a.scale}, params {source}; hub = node {hub}, "
              f"{hub_links:,} links)")
        shown = [k for k in ("measured", "-upsert_get", "-hub", "-upsert_get-hub", "sample_A", "sample_B")
                 if k in costs]
        print(f"   {'layout':34s}" + "".join(f"{k:>13s}" for k in shown) + "   rank ops / bytes per case")
        for cell in sorted(compose.CELLS, key=lambda c: costs["measured"][c]["ops"]):
            label = cell.replace("props_", "P:").replace("_adj_", " A:") + (
                f" ({compose.KNOWN[cell]})" if compose.KNOWN.get(cell, "").startswith("All-") else "")
            print(f"   {label:34s}" + "".join(f"{costs[k][cell]['ops']:>13,.0f}" for k in shown) + "   "
                  + " ".join(f"{rk[k]['ops'][cell]}/{rk[k]['bytes'][cell]}" for k in shown))
            print(f"   {'':34s}" + "".join(f"{costs[k][cell]['bytes'] / 1e6:>11,.1f}MB" for k in shown))
        for cur in ("ops", "bytes"):
            print(f"   cheapest in {cur}: " + ", ".join(f"{k} {min(costs[k], key=lambda c: costs[k][c][cur])}"
                                                      for k in shown))
    out = os.path.join(benchmarks.out_root(DATASET, a.scale, a.criterion, a.param_set), benchmarks.chunk_view_name("sensitivity", a.chunk_view) + ".csv")
    if a.workload:
        print(f"\n--workload: {out} not written (it covers every workload)")
        return
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]))
        wr.writeheader()
        wr.writerows(rows)
    print(f"\nwrote {out} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
