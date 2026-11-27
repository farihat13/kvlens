#!/usr/bin/env python3
"""Write plans/<query>.json (one bundle per query: every body) from the specs.

    python3 -m kvevaluator.workloads.job.dump_plans [--check]        (from the repo root)
--check: exit 1 when a committed plan file differs from what the generator gives now (the drift guard).
"""
import json
import os
import sys

from kvevaluator import workloads
from kvevaluator.workloads.job import queries
from kvevaluator.plans.ir.bundle import with_property_index_requires
from kvevaluator.paths import ROOT

PLANS = os.path.join(str(ROOT), "python", "kvevaluator", "workloads", "job", "plans")


def main():
    check = "--check" in sys.argv
    drift, bodies = [], 0
    for q in queries.QUERIES:
        b = with_property_index_requires(queries.bundle(q))   # the store cap an index-reading body needs
        b = workloads.with_query_fields("job", q, b)            # the query's question, after `plan`
        bodies += len(b["plans"])
        text = json.dumps(b, indent=1, ensure_ascii=False) + "\n"
        path = os.path.join(PLANS, f"{q}.json")
        if check:
            if not os.path.exists(path) or open(path).read() != text:
                drift.append(q)
        else:
            with open(path, "w") as f:
                f.write(text)
    extra = sorted(set(os.listdir(PLANS)) - {f"{q}.json" for q in queries.QUERIES}) if os.path.isdir(PLANS) else []
    if check:
        if drift or extra:
            sys.exit(f"job plans drift: {len(drift)} files differ from the generator ({drift[:5]}), {len(extra)} files of no query ({extra[:5]})")
        print(f"job plans match the generator: {len(queries.QUERIES)} queries, {bodies} bodies")
    else:
        print(f"wrote {len(queries.QUERIES)} plan files, {bodies} bodies")


if __name__ == "__main__":
    main()
