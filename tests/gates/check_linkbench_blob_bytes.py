"""LinkBench data blobs are stored, read, written and answered at their RAW length, in every format that runs LinkBench.

Two tiny LinkBench data dirs (made here) differ only in their blobs' CONTENT: one all ASCII, one with every byte
>= 0x80; lengths, ids and every other field are equal. Each format ingests both. A blob counted at its raw length makes
the two stores the same size and every request's byte lanes equal; a blob stored as text (the bug: latin1 then UTF-8)
makes the non-ASCII store bigger. Per format:
  1. the store's logical bytes (manifest) are equal
  2. getnode and getlinklist on every node: equal S / G / N and byte lanes; the answers' data is the raw blob; and the
     absolute size: a 1000 B blob's getnode reads 1000-1008 value bytes more than an empty blob's (a blob stored as
     text or base64 reads ~2000 or ~1333 more; skipped on Chunked props, whose one chunk holds both nodes)
  3. addnode / updatenode / addlink with a blob of each kind, equal lengths: equal write lanes
A str blob is refused where an input predates the bytes type: in the params (case 4), in a record the C++ ingest
reads, and as a records cache dumped by the old loader (case 5).
Each case prints; the exit code is the verdict. On code before the bytes type, cases 1-3 fail: the control.
Run: python3 tests/gates/check_linkbench_blob_bytes.py [--formats dgraph nebula arangodb meta:<spec stem> ...]
"""
import argparse
import base64
import glob
import json
import os
import shutil
import subprocess
import sys
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.capture import binary
from kvevaluator.measure import plan_runs, server
from kvevaluator.measure.stores import (ensure_store, scale_root, schema_artifact)
from kvevaluator.measure.records import records_cache_path, records_file

EXP = str(ROOT)
SCALE = "blobtest"
N_NODES = 12
LINK1, LINK2 = 123456789, 123456790


def object_hook(d):        # {"$bytes": base64} -> bytes (defined here so this file also runs on code without the type)
    if "$bytes" in d and len(d) == 1:
        return base64.b64decode(d["$bytes"])
    return d


def blob(node, kind, salt=0):
    """node's blob: its length depends only on the node; kind 'ascii' or 'high' decides the content"""
    n = [0, 1, 127, 128, 255, 256, 300, 1000][node % 8] + salt
    return bytes((0x41 + (i % 26)) if kind == "ascii" else (0x80 + (i * 7 + node) % 128) for i in range(n))


def write_data(d, kind):
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "nodes.jsonl"), "w") as f:
        for i in range(1, N_NODES + 1):
            f.write(json.dumps({"id": i, "type": 2048, "version": 1, "time": 1000 + i,
                                "data": base64.b64encode(blob(i, kind)).decode()}) + "\n")
    with open(os.path.join(d, "links.jsonl"), "w") as f:
        for i in range(1, N_NODES + 1):
            for j in range(1, 4):
                dst = (i + j - 1) % N_NODES + 1
                for lt in (LINK1, LINK2):
                    f.write(json.dumps({"id1": i, "link_type": lt, "id2": dst, "visibility": 1, "version": 0,
                                        "time": 2000 + i * 10 + j, "data": base64.b64encode(blob(i + j, kind)).decode()}) + "\n")


def native(q, name, b):
    """a blob param in the form the tree under test declares it: the tag for a `bytes` param, else (code before the
    bytes type) the loader's old latin1 str -- so the write case runs, and is compared, on either code"""
    decl = (plan_runs.ir_for("linkbench", q).get("params") or {}).get(name, {})
    return {"$bytes": base64.b64encode(b).decode()} if decl.get("type") == "bytes" else b.decode("latin1")


def run(srv_store, fmt, q, params):
    ir = plan_runs.ir_for("linkbench", q)
    req = server.build_request(ir, params, srv_store, fmt, schema_artifact("linkbench"))   # scale: the store's
    p = subprocess.run([binary.binary_path(), "run", "-"], input=json.dumps(req).encode(), capture_output=True)
    if not p.stdout.strip():
        return {"error": p.stderr.decode()[-400:]}
    # split on "\n" only: str.splitlines also splits at U+0085, which a latin1 blob holds on code before the bytes type
    return json.loads(p.stdout.decode().strip().split("\n")[-1], object_hook=object_hook)


def lanes(resp):
    t = resp.get("totals") or {}
    return {k: t.get(k) for k in ("S", "G", "N", "P", "D")} | dict(sorted((t.get("bytes") or {}).items()))


def data_of(row):
    d = row.get("data")
    return d.encode("latin1") if isinstance(d, str) else d


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--formats", nargs="*", default=None,
                    help="dgraph, nebula, arangodb, meta:<a inputs/specs/grid spec stem> (default: the 3 + the 9 cells)")
    a = ap.parse_args()
    grid = sorted(glob.glob(os.path.join(EXP, "inputs", "specs", "grid", "props_*_adj_*.json")))
    formats = a.formats or (["dgraph", "nebula", "arangodb"] + [f"meta:{os.path.basename(g)[:-5]}" for g in grid])
    prov = binary.binary_provenance()
    print(f"binary {binary.binary_path()} sha {prov.get('binary_git_sha', '?')[:12]} dirty {prov.get('binary_dirty')}")
    work = str(Tree().check("linkbench-blob-bytes", "linkbench", SCALE))
    results, cases = [], []

    def case(name, ok, detail=""):
        cases.append(ok)
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))

    stores = {}
    for kind in ("ascii", "high"):
        scale_dir = f"{SCALE}-{kind}"
        write_data(os.path.join(work, kind, "data"), kind)
        root = scale_root("linkbench", scale_dir)
        if os.path.isdir(root):
            shutil.rmtree(root)                         # this test's own stores: rebuilt every run
    for f in formats:
        db, _, spec = f.partition(":")
        spec_file = os.path.join(EXP, "inputs", "specs", "grid", spec + ".json") if spec else None
        print(f"{f}:")
        for kind in ("ascii", "high"):
            stores[kind] = ensure_store("linkbench", f"{SCALE}-{kind}", db,
                                                  data=os.path.join(work, kind, "data"), spec=spec_file)
        sz = {k: json.load(open(os.path.join(s, "manifest.json"))).get("logical_bytes") for k, s in stores.items()}
        case("1 store size independent of blob content", sz["ascii"] == sz["high"] and sz["ascii"] is not None,
             f"logical bytes ascii {sz['ascii']} high {sz['high']}")
        reads = diffs = wrong = 0
        links = {n: sorted(blob(n + j, "high") for j in range(1, 4)) for n in range(1, N_NODES + 1)}
        for node in range(1, N_NODES + 1):
            for q in ("getnode", "getlinklist_link1", "getlinklist_link2"):
                ra, rh = (run(stores[k], db, q, {"node": node}) for k in ("ascii", "high"))
                if ra.get("error") or rh.get("error"):
                    case(f"{q}({node}) runs", False, str(ra.get("error") or rh.get("error"))[:300]); continue
                reads += 1
                diffs += lanes(ra) != lanes(rh)
                want = [blob(node, "high")] if q == "getnode" else links[node]
                got = [data_of(r) for r in rh["rows"]]
                wrong += (got if q == "getnode" else sorted(got)) != want
        case("2 reads: byte lanes independent of blob content", reads > 0 and diffs == 0, f"{reads} reads, {diffs} differ")
        case("2 reads: getnode and getlinklist answer the raw blobs", reads > 0 and wrong == 0, f"{wrong} wrong")
        if spec.startswith("props_chunked"):    # one props chunk holds every fixture node: the absolute size is T1's
            print("  skip 2 reads: absolute size (one props chunk holds both nodes; test_bytes checks the chunk codec)")
        else:
            big, empty = (lanes(run(stores["high"], db, "getnode", {"node": n})) for n in (7, 8))   # 1000 and 0 B
            extra = sum((big.get(k) or 0) - (empty.get(k) or 0) for k in ("get_value", "scan_value"))
            case("2 reads: a 1000 B blob reads 1000-1008 value bytes more than an empty one", 1000 <= extra <= 1008,
                 f"{extra} more")
        writes = wdiff = 0
        for kind_params in [("addnode", lambda k: {"node": 100, "ntype": 1, "version": 1, "time": 5, "data": blob(5, k, 3)}),
                            ("updatenode", lambda k: {"node": 2, "version": 2, "time": 6, "data": blob(6, k, 1)}),
                            ("addlink_link1", lambda k: {"src": 3, "dst": 9, "visibility": 1, "version": 0, "time": 7,
                                                         "data": blob(7, k)})]:
            q, mk = kind_params
            ra, rh = (run(stores[k], db, q, {kk: native(q, kk, v) if isinstance(v, bytes) else v for kk, v in mk(k).items()})
                      for k in ("ascii", "high"))
            if ra.get("error") or rh.get("error"):
                case(f"{q} runs", False, str(ra.get("error") or rh.get("error"))[:300]); continue
            writes += 1
            wdiff += lanes(ra) != lanes(rh)
        case("3 writes: write lanes independent of blob content", writes > 0 and wdiff == 0, f"{writes} writes, {wdiff} differ")
        stale = run(stores["high"], db, "updatenode", {"node": 2, "version": 3, "time": 7, "data": "éé"})
        case("4 a str blob in the params is refused", bool(stale.get("error")), str(stale.get("error") or "accepted")[:160])
        results.append(f)
    # 5. stale inputs, store-free: a record holding a str blob, and a records cache with no format version
    print("stale inputs:")
    rec = json.dumps(["v", "7000000000000001", "Node", {"ntype": 1, "version": 1, "time": 1, "data": "\u00e9"}]) + "\n"
    out = os.path.join(work, "stale-ingest")
    if os.path.isdir(out):
        shutil.rmtree(out)
    p = subprocess.run([binary.binary_path(), "ingest", "--schema", schema_artifact("linkbench"),
                        "--format", "dgraph", "--out", out, "--scale", "stale"],
                       input=rec.encode(), capture_output=True)
    case("5 the C++ ingest refuses a str blob in a record", p.returncode != 0, p.stderr.decode().strip().splitlines()[-1][:140]
         if p.returncode else "ingested")
    root = os.path.dirname(records_cache_path("linkbench", f"{SCALE}-stale"))
    if os.path.isdir(root):
        shutil.rmtree(root)
    os.makedirs(root)
    open(os.path.join(root, ".records.jsonl"), "w").write(rec)
    json.dump({"data": "old", "sha256": "0"}, open(os.path.join(root, ".records.jsonl.src.json"), "w"))   # no version
    try:
        records_file("linkbench", f"{SCALE}-stale")
        case("5 a records cache with no format version is refused", False, "reused")
    except SystemExit as e:
        case("5 a records cache with no format version is refused", True, str(e)[:140])
    print(f"{len(results)} formats, {len(cases)} cases, {cases.count(False)} failed")
    return 0 if results and all(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
