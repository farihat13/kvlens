"""Validate the writes workload's parameterized plans: each template (builder -> Plan) serialized
to neutral IR, JSON round-tripped, then bound must give the SAME concrete Step list as production
plan_for. Writes are single-op plans with literal vids -- no store, no per-format override -- so a
structural step comparison is exact (identical steps => identical execution).

Run: python3 tests/unit/check_writes_planir.py
"""
import os
import sys
import json

from kvevaluator.plans.plan import Plan
from kvevaluator.plans.ir import plan_to_ir, bind_ir
from kvevaluator.workloads.writes.queries import plan_for, REGISTRY

PAIR = {"w_ins_e_plain", "w_ins_e_facet", "w_del_e", "w_upd_edge", "iu8"}


def _steps(steps):
    return [[s.out, s.op, json.dumps(s.args, default=str, sort_keys=True)] for s in steps]


def main():
    npass = nmis = 0
    for q in sorted(REGISTRY):
        args = {"src": "933", "dst": "42"} if q in PAIR else {"person": "933"}
        tags = {f"tag{i}": str(10 + i) for i in range(1, 4)}                     # typical targets (param-driven)
        if q == "iu7":
            args = {**args, "replyTo": "7", "country": "3", "tag1": "11", "tag2": "12"}
        if q == "iu1":
            args = {**args, **{f"friend{i}": str(40 + i) for i in range(1, 5)},
                    **{f"interest{i}": str(20 + i) for i in range(1, 4)},
                    "city": "5", "university1": "6", "company1": "9"}
        if q == "iu4":
            args = {**args, **tags}
        if q == "iu5":
            args = {**args, "forum": "4"}
        if q == "iu6":
            args = {**args, **tags, "forum": "4", "country": "3"}
        if q == "iu2":
            args = {**args, "post": "7"}          # a real post to like (param-driven)
        if q == "iu3":
            args = {**args, "comment": "8"}       # a real comment to like (param-driven)
        ref = plan_for(q, "dgraph", args)                     # production: bind
        raw = REGISTRY[q]({})                                 # the template, pre-bind
        assert isinstance(raw, Plan), f"{q} is not parameterized"
        ir = json.loads(json.dumps(plan_to_ir(raw, name=q)))  # neutral IR, JSON round-trip
        rt = bind_ir(ir, args)
        if _steps(ref) == _steps(rt):
            npass += 1
            print(f"  OK       {q:16s} ({len(ref)} steps)")
        else:
            nmis += 1
            print(f"  MISMATCH {q}")
            for a, b in zip(_steps(ref), _steps(rt)):
                if a != b:
                    print(f"      {a}\n   vs {b}")
    print(f"\n  writes: {npass} OK, {nmis} mismatch")
    print("\n" + ("WRITES PLAN IR OK" if nmis == 0 and npass > 0 else "FAILED"))
    return 0 if nmis == 0 and npass > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
