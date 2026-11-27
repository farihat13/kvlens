"""C++ against the Python oracle on LinkBench: the same LinkBench plans, run on both sides over a tiny LinkBench graph
whose data blobs hold every byte value (check_linkbench_blob_bytes.py's "high" data dir), compared by the shared rule
of check_cpp_vs_python.py (diff_cell: per-step S / G / N / P / D and misses, the byte lanes where both sides count
the same currency, then rows and their order). It is the LinkBench slice of `just cpp-diff`, which covers LDBC and
BI only: a sibling, not a branch of that script, because that one is keyed by the LDBC scale factor throughout.

Formats with Python read operators: dgraph, nebula, arangodb (the configurable format `meta` is size-only in Python;
its LinkBench check is check_linkbench_blob_bytes.py). As in `just cpp-diff` (--pack --skip-bytes): the packed twins,
and no byte lanes, because the Python byte model differs from the C++ codec by design; the bytes a blob is stored and
read at are check_linkbench_blob_bytes.py's (every format) and test_bytes.cc's (every codec).
Run: python3 tests/gates/check_cpp_vs_python_linkbench.py [--formats dgraph nebula arangodb]
"""
import argparse
import glob
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
import check_cpp_vs_python as shared
import check_linkbench_blob_bytes as fixture
from kvevaluator.capture import binary
from kvevaluator.output_tree import Tree
from kvevaluator.measure import server, stores
from kvevaluator.formats import make_format
from kvevaluator.oracle.interp import run_ir
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.oracle.run import open_store
from kvevaluator.datasets.linkbench import loader
from kvevaluator.datasets.linkbench.schema import get_schema

TAG = f"{fixture.SCALE}-high"


def reads():
    """(query, params) for every LinkBench read over every fixture node"""
    out = []
    every = list(range(1, fixture.N_NODES + 1))
    for n in every:
        out += [("getnode", {"node": n})]
        nxt = [(n + i) % fixture.N_NODES + 1 for i in range(5)]          # the next five nodes, wrapping
        for t in ("link1", "link2"):
            out += [(f"getlinklist_{t}", {"node": n}), (f"countlink_{t}", {"node": n})]
            # getlink's id list: 1, 2, 5 ids, and every node (id1 itself and ids with no link among them)
            out += [(f"getlink_{t}", {"node": n, "id2s": ids}) for ids in (nxt[:1], nxt[:2], nxt, every)]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--formats", nargs="*", default=["dgraph", "nebula", "arangodb"])
    a = ap.parse_args()
    t0 = time.time()
    data_dir = str(Tree().check("cpp-vs-python-linkbench", "linkbench", fixture.SCALE, "high", "data"))
    fixture.write_data(data_dir, "high")
    prov = binary.binary_provenance()
    print(f"binary {binary.binary_path()} sha {prov.get('binary_git_sha', '?')[:12]} dirty {prov.get('binary_dirty')}")
    schema = get_schema("canonical")
    plans = {os.path.basename(p)[:-5]: p for p in glob.glob(os.path.join(fixture.EXP, "python", "kvevaluator",
                                                                         "workloads", "linkbench", "plans", "*.json"))}
    import json
    nok = nbad = 0
    for db in a.formats:
        store_dir = stores.ensure_store("linkbench", TAG, db, data=data_dir)
        fmt = make_format(db, schema, True)                 # packed, as cpp-diff's --pack
        store = open_store(fmt, "abstract", TAG, lambda: loader.load(data_dir))
        caps, of_label = caps_for(db, schema), caps_resolver(db, schema)
        ok = bad = 0
        with server.CppServer(store_dir, db, schema=stores.schema_artifact("linkbench")) as srv:
            for q, args in reads():
                ir = json.load(open(plans[q]))
                out = shared.output_name(ir, caps, of_label, TAG)
                outs, kv = run_ir(ir, fmt, store, params=args, format_caps=caps, caps_of_label=of_label, scale=TAG)
                py_steps = [(s.out, s.op, c.S, c.G, c.N, c.P, c.D, c.S_miss, c.G_miss, c.N_miss, c.get_value_bytes,
                             c.scan_value_bytes, c.write_key_bytes, c.write_value_bytes) for s, _p, c in kv]
                reason = shared.diff_cell(py_steps, outs[out], srv.run(ir, args), check_bytes=False)
                if reason:
                    bad += 1
                    print(f"  MISMATCH {db}/{q} {args}: {reason[:300]}")
                else:
                    ok += 1
        print(f"{db}: {ok} ok, {bad} mismatch (counts, rows, order; bytes not compared)")
        nok, nbad = nok + ok, nbad + bad
    print(f"{nok} ok, {nbad} mismatch over {len(a.formats)} formats ({time.time() - t0:.0f}s)")
    if nok + nbad == 0:
        print("FAIL: 0 cells compared")
        return 1
    return 1 if nbad else 0


if __name__ == "__main__":
    sys.exit(main())
