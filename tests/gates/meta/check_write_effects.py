#!/usr/bin/env python3
"""Does every meta layout leave the SAME store after the writes workload? The store-contents check meta's writes had
no gate for (write-gate's cross-language effects cover the named formats only).

For one binary: ingest each meta cell (the 9 grid cells + the own-placement cells) and a nebula store fresh into
--root; apply the whole writes sequence on a checkpoint arena of each (measure/cppwrite.py: the fixed cell order, shadow
then apply, cross-checked); then read back, through the ops, every adjacency list the store keeps (out, and in where
the schema declares the reverse; with the edge props) and every vertex row of the vids the writes named, then the
lists and rows of their neighbours. Required:
  - every meta cell holds the same graph as nebula after the same writes (rows of the lists and of the vertices);
  - in each store: an out entry u -> v has its in entry v <- u (same props) where the reverse is kept; no list holds
    a deleted vertex, and a deleted vertex has no list and no row;
  - every far-end label a meta cell stores (adjacency far_label: census --far-labels) is its neighbour's label (the
    records', or the label its insert_vertex step gave): a write that stores a wrong label is caught.
--control BINARY runs the same check on a binary known to write a wrong store; it must FAIL with its own signature
("control caught"), named by its file name (the patch stem, CONTROL_SIGNATURES). A crash, a throw, or a failure without
that signature is "CONTROL VOID" and fails the check. Give it once per control: the tested binary's pass runs once,
then one pass per control.
The stores of every pass run side by side, --jobs at a time: each store has its own dir, arena and server, so no two
jobs open one RocksDB. The verdict is taken after all of them, in the fixed store order (nebula, then the cells by name).
--cells a,b checks a slice (nebula is always there): for a quick check while editing, never a gate result.

  python3 tests/gates/meta/check_write_effects.py [--binary B] [--specs-at REV] [--sf 0.1] [--nparam 5]
        [--root DIR] [--control B]... [--jobs 12] [--cells a,b] [--spec-dir DIR]...
--spec-dir DIR adds another spec set (e.g. inputs/specs/grid_index) to this run; the default set is unchanged.
Exit 0 = every store agrees and every control was caught; prints the cells and the lists / rows compared.
"""
import argparse
import concurrent.futures
import glob
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.measure import records, server
from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.cppwrite import run_cell, sequence

SCHEMA = schema_path("ldbc")
# the grid, the own-placement cells, and the per-direction mixes (an edge's two copies in two layouts)
SPEC_DIRS = ("inputs/specs/grid", "inputs/specs/grid_chunked_own", "inputs/specs/mixes_per_direction")
from kvevaluator.plans.ir.capabilities import KV_WRITE_OPS as WRITE_OPS  # (the op table's writes)
HEX16 = re.compile(r"[0-9a-f]{16}")
LABEL_NOT_RUN = "the far-end label check did not run"   # a failure that never makes a control "caught"
# each planted control (controls/<stem>.patch), by its binary's file name: the failure text its defect must produce
CONTROL_SIGNATURES = {
    "write_effects_empty_list_kept": "stored values hold nothing after the writes",
    "write_effects_far_label_zero": "a wrong far-end label after the writes",
    "write_effects_dangling_view": "holds the deleted",
}


def components_of(spec):
    """every layout component of a spec, each with whether it is an adjacency: the defaults and all per-type /
    per-direction overrides (vertex_props_by_label, adjacency_by_etype, adjacency_by_etype_dir)"""
    out = [(spec[k], k == "adjacency") for k in ("vertex_props", "adjacency") if k in spec]
    out += [(c, False) for c in spec.get("vertex_props_by_label", {}).values()]
    out += [(c, True) for c in spec.get("adjacency_by_etype", {}).values()]
    out += [(c, True) for dirs in spec.get("adjacency_by_etype_dir", {}).values() for c in dirs.values()]
    return out


def sizes_of(spec):
    """the pack / split sizes a spec states, over every component"""
    return sorted({c["owners"]["pack"] for c, _ in components_of(spec) if isinstance(c.get("owners"), dict)} |
                  {c["split"] for c, _ in components_of(spec) if isinstance(c.get("split"), int)})


def small_specs(specs, size, into):
    """the same cells with every size forced to `size` (the scale-only paths at sf0.1): a Condensed list's split, and
    the pack of every Chunked component (props and adjacency, inline and own), in the defaults AND in every per-type
    / per-direction override (a mix's Chunked edge type must not keep its 64 KB chunks)"""
    out = {}
    for cell, path in specs.items():
        spec = json.load(open(path))
        for c, adjacency in components_of(spec):
            if isinstance(c.get("owners"), dict):
                c["owners"]["pack"] = size
            elif adjacency and c.get("group") == "owner":
                c["split"] = size
        if sizes_of(spec) not in ([], [size]):   # a component the walk missed would keep its own size
            raise SystemExit(f"{cell}: sizes {sizes_of(spec)} left after forcing {size}")
        name = f"{cell}_small{size}"
        dst = os.path.join(into, f"{name}.json")
        with open(dst, "w") as f:
            json.dump(spec, f, indent=1)
        out[name] = dst
    return out


EXTRA_SPEC_DIRS = []   # --spec-dir: more spec sets for this run only (the gate's default set is SPEC_DIRS, unchanged)


def specs_at(rev, into):
    """{cell: spec file}: the grid + own specs (+ any --spec-dir) as of git revision `rev` (None = the working tree)"""
    out = {}
    for d in tuple(SPEC_DIRS) + tuple(EXTRA_SPEC_DIRS):
        if rev is None:
            files = sorted(glob.glob(os.path.join(EXP, d, "*.json")))
            out.update({os.path.basename(f)[:-5]: f for f in files})
            continue
        top = git_top()   # git from the top: ls-tree run in a subdirectory lists only that subdirectory
        rel = os.path.relpath(os.path.join(EXP, d), top)
        ls = subprocess.run(["git", "-C", top, "ls-tree", "--name-only", f"{rev}:{rel}"], capture_output=True, text=True)
        for name in ls.stdout.split():   # a dir the revision lacks (the own cells before S2) lists nothing
            if not name.endswith(".json"):
                continue
            body = subprocess.run(["git", "-C", top, "show", f"{rev}:{rel}/{name}"], capture_output=True, text=True,
                                  check=True).stdout
            path = os.path.join(into, name)
            with open(path, "w") as f:
                f.write(body)
            out[name[:-5]] = path
    return out


def git_top():
    return subprocess.run(["git", "-C", EXP, "rev-parse", "--show-toplevel"], capture_output=True, text=True,
                          check=True).stdout.strip()


def ingest(binary, fmt, out, sf, spec=None):
    cmd = [binary, "ingest", "--schema", SCHEMA, "--format", fmt, "--out", out,
           "--scale", str(sf)]
    if spec:
        cmd += ["--spec", spec]
    with open(records.records_file("ldbc", sf)) as rin:
        subprocess.run(cmd, stdin=rin, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)


class Schema:
    def __init__(self):
        s = json.load(open(SCHEMA))
        self.tag = {v["label"]: v["tag"] for v in s["vertices"]}
        self.labels_of_tag = {}
        for v in s["vertices"]:
            self.labels_of_tag.setdefault(v["tag"], []).append(v["label"])
        self.props = {v["label"]: [p["name"] if isinstance(p, dict) else p for p in v["props"]] for v in s["vertices"]}
        self.edges = s["edges"]

    def families(self, tag):
        """[(etype, dir, edge props)] a vertex of this tag's group can own (dir 'in' only where the reverse is kept)"""
        labs = set(self.labels_of_tag.get(tag, []))
        out = []
        for e in self.edges:
            eprops = [p["name"] if isinstance(p, dict) else p for p in e.get("props", [])]
            if any(s in labs for s, _ in e["endpoints"]):
                out.append((e["etype"], "out", eprops))
            if e.get("reverse") and any(d in labs for _, d in e["endpoints"]):
                out.append((e["etype"], "in", eprops))
        return out


def read_back(srv, sch, vids):
    """{('list', etype, dir, vid): sorted entries, ('row', vid): row} for the vids, through the ops"""
    by_tag = {}
    for v in sorted(vids):
        by_tag.setdefault(int(v[0], 16), []).append(v)    # the top 4 bits of the vid = its group's tag
    got = {}
    for tag, vs in sorted(by_tag.items()):
        for etype, d, eprops in sch.families(tag):
            ir = {"plan": "write_effects", "params": {}, "output": "out",
                  "steps": [{"name": "out", "op": "read_neighbors", "srcs": vs, "etype": etype, "dir": d,
                             "edge_props": eprops, "pairs": True}]}
            r = srv.run(ir, {})
            if r.get("error"):
                raise SystemExit(f"read {etype} {d}: {r['error']}")
            lists = {v: [] for v in vs}
            for row in r["rows"]:
                lists[row["src"]].append(json.dumps(row, sort_keys=True))
            for v, items in lists.items():
                got[("list", etype, d, v)] = sorted(items)
        labs = sch.labels_of_tag.get(tag, [])
        props = sorted({p for lab in labs for p in sch.props[lab]})
        ir = {"plan": "write_effects", "params": {}, "output": "out",
              "steps": [{"name": "out", "op": "read_vertex_props", "vids": vs, "props": props, "label": labs}]}
        r = srv.run(ir, {})
        if r.get("error"):
            raise SystemExit(f"read rows of tag {tag}: {r['error']}")
        for v, row in zip(vs, r["rows"]):
            got[("row", v)] = json.dumps(row, sort_keys=True)
    return got


def one_store(binary, fmt, store, sch, seq, root, name, condensed_adj=False, vertex_labels=None):
    """apply the writes on an arena of `store`, read back the touched vids and their neighbours. The vids a write
    touches are its write steps' targets (the plans' lets resolve in C++), read by a shadow run of each write step
    before the cell applies; returns (read-back, touched, deleted, multi -- with the far-label check's counts)"""
    arena = os.path.join(root, f".arena-{name}")
    subprocess.run([binary, "checkpoint", store, arena], check=True, capture_output=True)
    run_apply = {"P": 1, "apply_writes": True, "perf_level": 1, "iterations": {"warmup": 0, "measured": 1},
                 "parts": fmt == "meta"}
    run_shadow = {k: v for k, v in run_apply.items() if k != "apply_writes"}
    touched, deleted = set(), {True: set(), False: set()}   # deleted vids by with_edges
    inserted = {}                                            # vid -> the label its insert_vertex step gave
    multi = {"list_reads": 0, "next": 0, "adj_seeks": 0}     # write steps' adjacency reads (per part, meta)
    with server.CppServer(arena, fmt) as srv:
        for cell in seq:
            wl, q, idx, args, ir = cell
            for st in ir["steps"]:
                if st.get("op") not in WRITE_OPS:
                    continue
                r = srv.run(dict(ir, output=st["name"]), args, run=run_shadow)
                if r.get("error"):
                    raise SystemExit(f"{name} {q}/p{idx} target of {st['name']}: {r['error']}")
                for row in r.get("rows") or []:
                    vids = set(HEX16.findall(str(row.get("target", ""))))
                    touched |= vids
                    if st["op"] == "delete_vertex":
                        deleted[bool(st.get("with_edges", False))] |= vids
                    if st["op"] == "insert_vertex":
                        inserted.update({v: st.get("label") for v in vids})
            applied, _ = run_cell(srv, cell, run_apply)
            for st in applied.get("steps", []):
                for part, c in (st.get("parts") or {}).items():
                    if part.startswith("adj:"):
                        multi["adj_seeks"] += c.get("S", 0)
                        # a Condensed list read in parts (N = its parts); on Split, N is a prefix scan's records
                        if condensed_adj and c.get("N", 0) > 0 and st.get("op") in WRITE_OPS:
                            multi["list_reads"] += 1
                            multi["next"] += c["N"]
        got = read_back(srv, sch, touched)
        nbrs = set()
        for k, items in got.items():
            if k[0] == "list":
                for it in items:
                    nbrs.add(json.loads(it)["dst"])
        got.update(read_back(srv, sch, nbrs - touched))
    if fmt == "meta":   # the keys the ops cannot see: a list / part / chunk left holding nothing
        # census exits 1 here by design: after the writes the arena's totals no longer equal the ingest manifest
        tsv = os.path.join(root, f".far-labels-{name}.tsv")
        r = subprocess.run([binary, "census", "--far-labels", tsv, arena], capture_output=True, text=True)
        if r.returncode == 2:   # a binary from before far_label (an older control): no label decoder, no label check
            r = subprocess.run([binary, "census", arena], capture_output=True, text=True)
            tsv = None
        if not r.stdout.strip():   # no JSON at all: say census's own reason, not a JSON parse error
            raise RuntimeError(f"census gave no output (rc {r.returncode}): {r.stderr.strip()[-400:]} "
                               f"[{binary} on {arena}]")
        cen = json.loads(r.stdout)
        multi["empty_values"] = sum(g.get("empty_values", 0) for g in cen["groups"])
        n, wrong = 0, []   # every stored far-end label vs its neighbour's label
        if tsv:
            with open(tsv) as f:
                for line in f:
                    fam, owner, other, label = line.rstrip("\n").split("\t")
                    want = inserted.get(other) or (vertex_labels or {}).get(other)
                    n += 1
                    if label != want:
                        wrong.append(f"{fam} {owner} -> {other}: stored {label}, the vertex is {want}")
            os.remove(tsv)
        multi["far_labels"], multi["far_wrong"] = (n if tsv else None), wrong
    return got, touched, deleted, multi


def invariants(got, deleted):
    """the in-store rules: every out entry has its in entry where the reverse is read; a vertex deleted with its edges
    leaves no entry, no list and no row; one deleted as a record only (w_del_v_record) leaves no row (its edges stay,
    dangling, by that plan's design)"""
    bad = []
    for k, items in got.items():
        if k[0] != "list":
            continue
        _, etype, d, v = k
        for it in items:
            e = json.loads(it)
            other = e["dst"]
            if other in deleted[True]:
                bad.append(f"{etype} {d} list of {v} holds the deleted {other}")
            twin = ("list", etype, "in" if d == "out" else "out", other)
            if twin in got:
                back = dict(e, src=other, dst=v)
                if json.dumps(back, sort_keys=True) not in got[twin]:
                    bad.append(f"{etype}: {v} -{d}-> {other} has no matching entry in {other}'s "
                               f"{'in' if d == 'out' else 'out'} list")
    for v in deleted[True]:
        if any(items for k, items in got.items() if k[0] == "list" and k[3] == v):
            bad.append(f"the deleted {v} still has a list")
    for v in deleted[True] | deleted[False]:
        row = json.loads(got.get(("row", v), "{}"))
        if any(val is not None for key, val in row.items() if key not in ("_vid", "id")):
            bad.append(f"the deleted {v} still has a row")
    return bad


_SHARED = {}   # what every store job reads, set before the pool forks: the schema, the write sequence, the labels


def shared_inputs(sf, nparam):
    """the inputs every pass and store share, loaded once: the schema, the fixed write sequence, and vid -> label from
    the records (the truth a stored far-end label is checked against)"""
    vertex_labels = {}
    with open(records.records_file("ldbc", sf)) as f:
        for line in f:
            if line.startswith('["v"'):
                r = json.loads(line)
                vertex_labels[r[1]] = r[2]
    _SHARED.update(sch=Schema(), seq=sequence(sf, None, nparam), vertex_labels=vertex_labels, sf=sf)


def store_job(binary, root, name, fmt, spec):
    """one store of one pass, in a worker: ingest it fresh, apply the writes, read back; returns one_store's result,
    with the store's ingest counts in `multi` and its wall seconds"""
    t0 = time.time()
    os.environ["KVEVAL_CPP_BIN"] = binary   # the server of this job (a worker runs one job at a time)
    path = os.path.join(root, name)
    ingest(binary, fmt, path, _SHARED["sf"], spec)
    adj = json.load(open(spec)).get("adjacency", {}) if spec else {}
    condensed = adj.get("group") == "owner" and not isinstance(adj.get("owners"), dict)
    got, t, d, multi = one_store(binary, fmt, path, _SHARED["sch"], _SHARED["seq"], root, name, condensed,
                                 _SHARED["vertex_labels"])
    if fmt == "meta":
        m = json.load(open(os.path.join(path, "manifest.json")))
        c = m.get("ingest_counts", m)
        multi.update(split_units=c.get("adjacency_split_units", 0), parts=c.get("adjacency_parts", 0))
        multi["far_on"] = adj.get("far_label", True) is not False   # the setting's default is on
    return got, t, d, multi, time.time() - t0


def start_pass(pool, binary, rev, root, small=None, cells=None):
    """submit one binary's stores to the pool; finish_pass takes the verdict. cells = a slice of the meta cells"""
    binary = os.path.abspath(binary)
    if os.path.exists(root):
        raise SystemExit(f"{root} exists: give a fresh --root (nothing is overwritten)")
    os.makedirs(root)
    specs = specs_at(rev, tempfile.mkdtemp(prefix="write-effects-specs-", dir=root))
    if cells is not None:
        unknown = sorted(set(cells) - set(specs))
        if unknown:
            raise SystemExit(f"--cells: no such cell {unknown}; the cells are {sorted(specs)}")
        specs = {c: specs[c] for c in cells}
    if small:
        specs = small_specs(specs, small, tempfile.mkdtemp(prefix="write-effects-small-", dir=root))
    stores = [("nebula", "nebula", None)] + [(c, "meta", p) for c, p in sorted(specs.items())]
    return [(name, fmt, pool.submit(store_job, binary, root, name, fmt, spec)) for name, fmt, spec in stores]


def finish_pass(jobs, verbose=True, small=None):
    """wait for a pass's stores and compare them, in the fixed store order (nebula first, then the cells by name)"""
    t0 = time.time()
    multis = {}
    deleted, touched = None, None
    results = {}
    for name, fmt, job in jobs:
        try:
            results[name], t, d, multi, secs = job.result()
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"{name}: {e}\n{(e.stderr or b'')[-2000:]}")
        if fmt == "meta":
            multis[name] = multi
        if deleted is None:
            touched, deleted = t, d
        elif (t, d) != (touched, deleted):
            raise SystemExit(f"{name}: the writes touched other vids than on nebula ({len(t)} vs {len(touched)})")
        if verbose:
            print(f"  {name}: {sum(1 for k in results[name] if k[0] == 'list')} lists, "
                  f"{sum(1 for k in results[name] if k[0] == 'row')} rows read back ({secs:.0f}s in its job)")
    ref = results["nebula"]
    fails = []
    lists = rows = 0
    for name, got in results.items():
        for msg in invariants(got, deleted):
            fails.append(f"{name}: {msg}")
        for w in multis.get(name, {}).get("far_wrong", [])[:3]:
            fails.append(f"{name}: a wrong far-end label after the writes: {w}")
        if len(multis.get(name, {}).get("far_wrong", [])) > 3:
            fails.append(f"{name}: {len(multis[name]['far_wrong'])} wrong far-end labels in all")
        if multis.get(name, {}).get("far_on") and multis[name].get("far_labels") == 0:
            fails.append(f"{name}: far_label is on, yet no stored far-end label was checked (0 compared)")
        if multis.get(name, {}).get("far_on") and multis[name].get("far_labels") is None:
            fails.append(f"{name}: {LABEL_NOT_RUN}: far_label is on and this binary's census has no --far-labels")
        if multis.get(name, {}).get("empty_values"):
            fails.append(f"{name}: {multis[name]['empty_values']} stored values hold nothing after the writes "
                         f"(an emptied list / part / chunk kept as a key)")
        if name == "nebula":
            continue
        for k in sorted(set(got) | set(ref), key=str):
            if k[0] == "list" and k not in got:
                continue    # a family nebula keeps and meta does not (nebula stores both directions)
            if k[0] == "list" and k not in ref:
                fails.append(f"{name}: {k} read on meta, not on nebula")
                continue
            if k[0] == "list":
                lists += 1
            else:
                rows += 1
            if got.get(k) != ref.get(k):
                fails.append(f"{name}: {k} differs from nebula: {str(got.get(k))[:160]} vs {str(ref.get(k))[:160]}")
    cells = len(results) - 1
    ok = not fails and cells > 0 and lists > 0 and rows > 0
    if verbose:
        print("  far-end labels checked after the writes: " + ", ".join(
            f"{n} {'(no label decoder in this binary)' if m.get('far_labels') is None else m['far_labels']}"
            for n, m in sorted(multis.items())))
    if small:
        for name, m in sorted(multis.items()):
            print(f"  {name}: split lists {m['split_units']} in {m['parts']} parts at ingest; the writes read "
                  f"{m['list_reads']} Condensed lists in parts ({m['next']} NEXT); {m['adj_seeks']} adjacency SEEKs "
                  f"(a chunk read on Chunked)")
    return ok, fails, cells, lists // max(cells, 1), rows // max(cells, 1), len(_SHARED["seq"]), \
        (len(touched), len(deleted[True]), len(deleted[False])), time.time() - t0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--binary", default=binary_path())
    ap.add_argument("--specs-at", default=None, help="git revision to read the cell specs from (default: the tree)")
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--nparam", type=int, default=5)
    ap.add_argument("--root", default=None, help="a fresh dir for the stores (default output/checks/meta-write-effects/ldbc/sf<SF>/<time>)")
    ap.add_argument("--keep", action="store_true", help="keep the stores (default: removed after the verdict; they are "
                    "disposable, every run ingests its own)")
    ap.add_argument("--control", action="append", default=[],
                    help="a binary known to write a wrong store: must FAIL (give it once per control)")
    ap.add_argument("--control-specs-at", default=None)
    ap.add_argument("--small-size", type=int, default=None,
                    help="force every chunk pack and Condensed split to this size (the multi-part write paths)")
    ap.add_argument("--jobs", type=int, default=12, help="stores handled at the same time, over every pass")
    ap.add_argument("--cells", default=None, help="a slice: only these meta cells (comma list), beside nebula")
    ap.add_argument("--spec-dir", action="append", default=[],
                    help="another spec set to check beside the default ones (relative to the experiment; repeatable)")
    a = ap.parse_args()
    EXTRA_SPEC_DIRS.extend(a.spec_dir)
    cells = [c for c in a.cells.split(",") if c] if a.cells else None
    stems = [os.path.basename(c) for c in a.control]
    if len(set(stems)) != len(stems):
        raise SystemExit(f"--control: two binaries share a file name ({stems}): each pass's dir is named by it")
    stamp = time.strftime("%Y%m%dT%H%M%S")
    root = a.root or str(Tree().check("meta-write-effects", "ldbc", a.sf, stamp))   # the check's own stores
    t0 = time.time()
    os.environ["KVEVAL_CPP_BIN"] = os.path.abspath(a.binary)   # whatever the bridge runs here is the tested binary
    shared_inputs(a.sf, a.nparam)
    # fork: the workers inherit _SHARED; every pass's stores go to ONE pool, the verdicts are taken pass by pass
    with concurrent.futures.ProcessPoolExecutor(a.jobs, mp_context=multiprocessing.get_context("fork")) as pool:
        real = start_pass(pool, a.binary, a.specs_at, os.path.join(root, "check"), a.small_size, cells)
        controls = [(c, start_pass(pool, c, a.control_specs_at or a.specs_at, os.path.join(root, f"control-{stem}"),
                                   a.small_size, cells)) for c, stem in zip(a.control, stems)]
        ok, fails, ncell, lists, rows, ncells, ndel, _ = finish_pass(real, small=a.small_size)
        for f in fails[:12]:
            print("  FAIL", f)
        print(f"[write-effects] {a.binary} (specs at {a.specs_at or 'the tree'}), sf{a.sf}: {ncell} meta cells vs nebula "
              f"after {ncells} write cells ({ndel[0]} vids written, {ndel[1]} deleted with edges, {ndel[2]} as a record): {lists} lists + {rows} rows compared per cell -> "
              f"{'OK' if ok else f'FAIL ({len(fails)})'} ({time.time() - t0:.0f}s)")
        if cells is not None:
            print(f"[write-effects] a SLICE ({ncell} cells: {','.join(cells)}): not a gate result")
        caught = []
        for (c, jobs), stem in zip(controls, stems):
            try:
                cok, cfails, *_ = finish_pass(jobs, verbose=False, small=a.small_size)
            except (SystemExit, Exception) as e:   # a crash or a throw is not the planted defect
                caught.append(False)
                print(f"[write-effects] control {c}: CONTROL VOID (its pass crashed: {str(e)[:500]})")
                continue
            bad = [f for f in cfails if LABEL_NOT_RUN not in f]   # the label check not running is not the control's bug
            sig = CONTROL_SIGNATURES.get(stem)
            hit = [f for f in bad if sig and sig in f]
            caught.append(bool(hit))
            verdict = ("control caught by its signature" if hit else "CONTROL NOT CAUGHT" if not bad else
                       f"CONTROL VOID (no expected signature for '{stem}')" if not sig else
                       f"CONTROL VOID (it failed, but without its signature '{sig}')")
            print(f"[write-effects] control {c}: {verdict}"
                  f" ({len(hit)} of {len(bad)} differences match; e.g. {(hit or bad)[0][:200] if bad else '-'}"
                  f"{'; the label check did not run on it (no decoder): not counted' if len(bad) < len(cfails) else ''})")
        if controls:
            print(f"[write-effects] controls: {sum(caught)} of {len(controls)} caught by their signature")
    print(f"[write-effects] {1 + len(controls)} passes, {a.jobs} stores at a time: {time.time() - t0:.0f}s")
    if not a.keep:
        shutil.rmtree(root, ignore_errors=True)   # ~1 GB of stores per sf0.1 run (13 stores + arenas)
    return 0 if ok and all(caught) else 1


if __name__ == "__main__":
    sys.exit(main())
