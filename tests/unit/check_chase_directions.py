"""read_chase over both directions in the Python oracle (formats/base.py read_chase), the C++ rule's twin
(cpp/kvevaluator/tests/test_chase_directions.cc): a node's first neighbour is its first direction's, out then in, and a
later direction is read only when the earlier ones have none. On the demo graph (A knows B, C; D knows A), a one-hop
chase from A (out has a neighbour) costs what the out chase costs; one from B (out has none) costs the out and in
chases together and answers the in chase's vertex. Dgraph, NebulaGraph and ArangoDB (Per-type CSR's oracle needs
encoded vids, which the demo graph does not have; the C++ test covers it). Their engine twins keep the engine's plan:
every direction read, so both from A costs out + in. `--control` expects the old rule (every direction read) on the
formats: the check must then fail.

    python tests/unit/check_chase_directions.py [--control]
"""
import sys

from kvevaluator.datasets.demo import tiny, DEMO_SCHEMA
from kvevaluator.formats.arangodb import ArangodbFormat
from kvevaluator.formats.arangodb_engine import ArangodbEngineFormat
from kvevaluator.formats.dgraph import DgraphFormat
from kvevaluator.formats.dgraph_engine import DgraphEngineFormat
from kvevaluator.formats.nebula import NebulaFormat
from kvevaluator.formats.nebula_engine import NebulaEngineFormat
from kvevaluator.kvstore.kvstore import AbstractKV, CountingKV
from kvevaluator.schema.vids import person_vid


def store_of(FmtCls):
    fmt = FmtCls(DEMO_SCHEMA)
    raw = AbstractKV()
    fmt.materialize(tiny(), raw)
    store = CountingKV(raw, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes)
    fmt.warm(store)
    return fmt, store


def sgn(c):
    return (c.S, c.G, c.N)


def plus(a, b):
    return tuple(x + y for x, y in zip(a, b))


def main():
    control = "--control" in sys.argv
    ok = cases = 0
    fails = []
    for FmtCls in (DgraphFormat, NebulaFormat, ArangodbFormat,
                   DgraphEngineFormat, NebulaEngineFormat, ArangodbEngineFormat):
        twin = not FmtCls.CHASE_READS_ONCE
        fmt, store = store_of(FmtCls)
        run = {}
        for who in ("A", "B"):
            for d in ("both", "out", "in"):
                got, c = fmt.read_chase(store, [person_vid(who)], "knows", d, max_hops=1)
                run[who, d] = (got, sgn(c))
        want_a = plus(run["A", "out"][1], run["A", "in"][1]) if control or twin else run["A", "out"][1]
        checks = [
            ("both from A answers out's", run["A", "both"][0] == run["A", "out"][0]),
            (f"both from A costs {run['A', 'both'][1]}, want {want_a}", run["A", "both"][1] == want_a),
            ("both from B answers in's", run["B", "both"][0] == run["B", "in"][0]),
            ("both from B reaches A", run["B", "both"][0] == [person_vid("A")]),
            (f"both from B costs {run['B', 'both'][1]}, want out + in",
             run["B", "both"][1] == plus(run["B", "out"][1], run["B", "in"][1])),
            ("the in chase from A reads something (the case bites)", sum(run["A", "in"][1]) > 0),
        ]
        for what, good in checks:
            cases += 1
            ok += good
            if not good:
                fails.append(f"{FmtCls.__name__}: {what}")
    for f in fails:
        print("  FAIL", f)
    print(f"check_chase_directions {'OK' if not fails else 'FAIL'}: {ok}/{cases} checks over 3 formats and their engine twins")
    return 1 if fails or cases == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
