"""LinkBench getlink answers every id of its request (the `id2s` list param), on the Python oracle and on C++.

The tiny LinkBench fixture of check_linkbench_blob_bytes.py ("high" data: 12 nodes, each linked to the next three on
both link types). Cases: every node x link type x four id lists (the next 1, 2 and 5 nodes, and every node, which holds
id1 itself and nodes it has no link to). The expected answer is read from links.jsonl directly, not from a plan: the
links from id1 of that type to any listed id, dst ascending. Per case the (src, dst) rows must equal it. Per (node, type)
the KV counts (S, G, N, P, D) must be the same for all four lists: the list moves the answer, not the reads.

--plans-rev REV runs REV's getlink plans instead (fed as REV's generator wrote params: id2a, id2b = the first two ids
when the plan declares them). At the commit before the list param it FAILS on the 5-id and every-node lists: the
control; it then also runs this tree's plans and reports whether the KV counts of the two are equal per case (they
must be: only the in-memory filter changed). Backends: Python (dgraph, nebula, arangodb: the formats with Python read operators) and C++ (those three and
the nine meta cells). Prints the cases per backend and format; 0 cases is a failure.

Run: python3 tests/gates/check_linkbench_getlink_ids.py [--backend python|cpp|both] [--formats ...] [--plans-rev REV]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
import check_linkbench_blob_bytes as fixture
from kvevaluator.capture import binary
from kvevaluator.output_tree import Tree
from kvevaluator.measure import server, stores
from kvevaluator.schema import vids

TAG = f"{fixture.SCALE}-high"
TYPES = {"link1": fixture.LINK1, "link2": fixture.LINK2}
PLANS = os.path.join(fixture.EXP, "python", "kvevaluator", "workloads", "linkbench", "plans")


def plan(q, rev):
    """the getlink plan of this tree, or of commit `rev`"""
    if not rev:
        return json.load(open(os.path.join(PLANS, q + ".json")))
    from kvevaluator.workloads import paths_in_old_trees
    top = subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    for path in paths_in_old_trees(os.path.relpath(os.path.join(PLANS, q + ".json"), top)):
        got = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True)
        if got.returncode == 0:
            return json.loads(got.stdout)
    raise SystemExit(f"{q}: no plan file at {rev}")


def args_for(ir, node, ids):
    """the params row the plan's generator writes: the list, or (before it) the first two ids"""
    if "id2s" in ir.get("params", {}):
        return {"node": node, "id2s": ids}
    return {"node": node, "id2a": ids[0], "id2b": ids[1] if len(ids) > 1 else ids[0]}


def cases():
    every = list(range(1, fixture.N_NODES + 1))
    for n in every:
        nxt = [(n + i) % fixture.N_NODES + 1 for i in range(5)]
        for t in TYPES:
            for name, ids in (("1 id", nxt[:1]), ("2 ids", nxt[:2]), ("5 ids", nxt), ("every node", every)):
                yield n, t, name, ids


def expected(data_dir):
    """(node, type) -> {dst} from links.jsonl"""
    out = defaultdict(set)
    rev = {v: k for k, v in TYPES.items()}
    with open(os.path.join(data_dir, "links.jsonl")) as f:
        for line in f:
            r = json.loads(line)
            out[(r["id1"], rev[r["link_type"]])].add(r["id2"])
    return out


def rows_of(rows):
    return [(r["src"], r["dst"]) for r in rows]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", choices=("python", "cpp", "both"), default="both")
    ap.add_argument("--formats", nargs="*", default=None, help="dgraph, nebula, arangodb, meta:<grid spec stem>")
    ap.add_argument("--plans-rev", default="", help="run this commit's getlink plans (the control)")
    a = ap.parse_args()
    data_dir = str(Tree().check("linkbench-getlink-ids", "linkbench", fixture.SCALE, "high", "data"))
    fixture.write_data(data_dir, "high")
    links = expected(data_dir)
    irs = {q: plan(q, a.plans_rev) for q in ("getlink_link1", "getlink_link2")}
    now = {q: plan(q, "") for q in irs} if a.plans_rev else None
    print(f"plans: {'this tree' if not a.plans_rev else a.plans_rev}; params: "
          f"{'id2s' if 'id2s' in irs['getlink_link1']['params'] else 'id2a, id2b (the first two ids)'}")
    grid = sorted(glob.glob(os.path.join(fixture.EXP, "inputs", "specs", "grid", "props_*_adj_*.json")))
    py_formats = ["dgraph", "nebula", "arangodb"]
    cpp_formats = py_formats + [f"meta:{os.path.basename(g)[:-5]}" for g in grid]
    runs = []
    if a.backend in ("python", "both"):
        runs += [("python", f) for f in (a.formats or py_formats) if ":" not in f]
    if a.backend in ("cpp", "both"):
        prov = binary.binary_provenance()
        print(f"binary {binary.binary_path()} sha {prov.get('binary_git_sha', '?')[:12]} dirty {prov.get('binary_dirty')}")
        runs += [("cpp", f) for f in (a.formats or cpp_formats)]
    total = bad = 0
    for backend, f in runs:
        db, _, spec = f.partition(":")
        run = runner(backend, db, spec, data_dir)
        n = wrong = moved = errors = same = compared = 0
        counts = defaultdict(set)
        by_list = defaultdict(lambda: [0, 0])
        for node, t, name, ids in cases():
            ir = irs[f"getlink_{t}"]
            try:
                rows, kv = run(ir, args_for(ir, node, ids))
            except Exception as e:                           # an unbound param, a refused op: a failed case
                errors += 1; n += 1; by_list[name][1] += 1
                if errors <= 2:
                    print(f"  {backend}/{f} getlink_{t}({node}, {name}): ERROR {str(e)[:200]}")
                continue
            want = [(vids.vid("Node", node), vids.vid("Node", d)) for d in sorted(links[(node, t)] & set(ids))]
            n += 1
            ok = rows_of(rows) == want
            wrong += not ok
            by_list[name][0 if ok else 1] += 1
            counts[(node, t)].add(kv)
            if now:                                          # the control: this tree's plan on the same case
                compared += 1
                same += run(now[f"getlink_{t}"], args_for(now[f"getlink_{t}"], node, ids))[1] == kv
        moved = sum(len(v) > 1 for v in counts.values())
        detail = ", ".join(f"{k} {ok}/{ok + no}" for k, (ok, no) in by_list.items())
        verdict = "ok  " if n and not wrong and not errors and not moved else "FAIL"
        print(f"  {verdict} {backend}/{f}: {n} cases ({detail}); {wrong} wrong, {errors} errors; "
              f"counts differ across the lists on {moved} of {len(counts)} (node, type)"
              + (f"; KV counts equal to this tree's plans on {same} of {compared}" if now else ""))
        total += n
        bad += verdict == "FAIL"
    print(f"{total} cases over {len(runs)} (backend, format) runs; {bad} failed")
    if total == 0:
        print("FAIL: 0 cases")
        return 1
    return 1 if bad else 0


def runner(backend, db, spec, data_dir):
    """a function (ir, args) -> (rows, (S, G, N, P, D)) for one backend and format"""
    if backend == "python":
        from kvevaluator.formats import make_format
        from kvevaluator.oracle.interp import run_ir
        from kvevaluator.formats.caps import caps_for, caps_resolver
        from kvevaluator.oracle.run import open_store
        from kvevaluator.datasets.linkbench import loader
        from kvevaluator.datasets.linkbench.schema import get_schema
        schema = get_schema("canonical")
        fmt = make_format(db, schema, True)
        store = open_store(fmt, "abstract", TAG, lambda: loader.load(data_dir))
        caps, of_label = caps_for(db, schema), caps_resolver(db, schema)

        def run(ir, args):
            outs, kv = run_ir(ir, fmt, store, params=args, format_caps=caps, caps_of_label=of_label, scale=TAG)
            tot = tuple(sum(getattr(c, x) for _s, _p, c in kv) for x in ("S", "G", "N", "P", "D"))
            return outs[ir["output"]], tot
        return run
    spec_file = os.path.join(fixture.EXP, "inputs", "specs", "grid", spec + ".json") if spec else None
    store = stores.ensure_store("linkbench", TAG, db, data=data_dir, spec=spec_file)

    def run(ir, args):
        req = server.build_request(ir, args, store, db, stores.schema_artifact("linkbench"))
        p = subprocess.run([binary.binary_path(), "run", "-"], input=json.dumps(req).encode(), capture_output=True)
        if not p.stdout.strip():
            raise RuntimeError(p.stderr.decode()[-300:])
        resp = json.loads(p.stdout.decode().strip().split("\n")[-1], object_hook=fixture.object_hook)
        if resp.get("error"):
            raise RuntimeError(str(resp["error"])[:300])
        t = resp.get("totals") or {}
        return resp["rows"], tuple(t.get(x) for x in ("S", "G", "N", "P", "D"))
    return run


if __name__ == "__main__":
    sys.exit(main())
