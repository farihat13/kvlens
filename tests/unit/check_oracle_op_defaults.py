"""The Python oracle formats' read methods default their arguments as the op table does (inputs/contract/kv_ops.json,
read through kvevaluator.plans.ir.capabilities.KV_OPS; None stands for an empty list), and Python's own op lists
(formats.KV_BASE_OPS' reads, KV_CONCRETE_OPS) name only the table's reads. The oracle writes its defaults in its method
signatures, so this is where they can drift from the table both sides read. No binary, no store.
Run: python3 tests/unit/check_oracle_op_defaults.py [--plant oracle]   (a plant must FAIL)
"""
import argparse
import inspect
import sys

from kvevaluator import formats
from kvevaluator.plans.ir import capabilities as caps


def oracle_drift(table):
    """(format, op, arg, the method's default, the table's): each oracle format's read method whose default for an
    argument differs from the table's (None stands for an empty list: Python's idiom for a list default)"""
    out = []
    for name, cls in formats.FORMATS.items():
        for op, (kind, _, args) in table.items():
            f = getattr(cls, op, None) if kind == "read" else None
            if f is None:
                continue
            params = inspect.signature(f).parameters
            for arg, dflt in args.items():
                if dflt in (caps.REQUIRED, caps.OPTIONAL) or arg not in params:
                    continue
                py = params[arg].default
                if py is inspect.Parameter.empty or (py != dflt and not (py is None and dflt == [])):
                    out.append((name, op, arg, py, dflt))
    return out


def plant_oracle():
    """a control: nebula's oracle top-k defaulting id_order to desc"""
    base = formats.FORMATS["nebula"]

    def read_top_k_by_index(self, *args, id_order="desc", **kw):
        return base.read_top_k_by_index(self, *args, id_order=id_order, **kw)
    formats.FORMATS["nebula"] = type("NebulaPlant", (base,), {"read_top_k_by_index": read_top_k_by_index})


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plant", choices=("oracle",), help="a control: one oracle format's default drifts")
    a = ap.parse_args()
    if a.plant:
        print(f"=== CONTROL: plant {a.plant}: a FAIL is the expected verdict")
        plant_oracle()
    table = caps.KV_OPS
    problems = [f"{op}: the {fmt} oracle defaults {arg}={pyd!r}, the table {d!r}" for fmt, op, arg, pyd, d in oracle_drift(table)]
    reads = {op for op, (k, _, _) in table.items() if k == "read"}
    listed = {op for op in formats.KV_BASE_OPS if op.startswith("read_")} | set(formats.KV_CONCRETE_OPS)
    problems += [f"{op}: in Python's formats op lists, not a read of the table" for op in sorted(listed - reads)]
    ndefaults = sum(1 for _, args in ((k, v[2]) for k, v in table.items())
                    for d in args.values() if d not in (caps.REQUIRED, caps.OPTIONAL))
    print(f"[oracle-defaults] {len(table)} ops ({ndefaults} defaults) against {len(formats.FORMATS)} oracle formats; "
          f"{len(listed)} listed reads checked")
    if not table or not formats.FORMATS:
        problems.append("0 ops or 0 formats checked")
    for p in problems:
        print("  DRIFT", p)
    if problems:
        sys.exit(f"FAIL: {len(problems)} drifts")
    print("OK: the oracle's defaults equal the op table's")


if __name__ == "__main__":
    main()
