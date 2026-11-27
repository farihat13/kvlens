#!/usr/bin/env python3
"""What the chunk size of the Chunked setting does at one scale: the sizes given (64 KB and each --composed) side by side, for I01 and I02
(LDBC Interactive, LDBC frequencies; reported params). Analysis only: opens no store. Each size is a nine-cell grid:
the measured one (64 KB) or one composed from the grid's diagonal cells and that size's measured Chunked/Chunked cell
(compose_cells.py --chunked-from). Every grid is priced by its own held-out picks (no pick record), as the
scale check does. Four tables:
    uniform_cells.csv       the five uniform layouts with a Chunked side, the reference (Sp-Co, python/kvcost/layout_cost/settings.py; B's unit too) and Co-Sp: ops,
                            bytes, ops + bytes / 4 KB, every raw channel; the Chunked/Chunked store's size on disk
    owners_per_chunk.csv    I01's adjacency parts on Chunked lists: owners probed, chunks read, bytes (the computation
                            of scale_check_owners_per_chunk.py, run here for a third size)
    mixed_layouts.csv       the exact mixed layout per type and direction: fewest ops + bytes / 4 KB, fewest ops within
                            B x Sp-Co's bytes (cells.B_UNIT; B = 1, 2, 3, 5); how many parts take Chunked
    tables.md               the four tables as text (I01's writes per size among them)

    python3 experiments/mix_benefit/chunk_size_report.py --measured <captures dir> --composed 16384=<dir> --composed 4096=<dir> \\
        --composed 1024=<dir> [--captures 16384=<dir>] [--captures 1024=<dir>] --results 65536=<dir> ... --out DIR
<captures dir> (a captures/ldbc/<scale> dir) holds the measured cells at 64 KB (the diagonal's steps are read) and each
size's Chunked/Chunked cell; --captures names another captures dir for a size whose cell is elsewhere (a snapshot has no
1 KB cell).
The `cell` column is relative to the row's `chunk_bytes` (kvevaluator.layouts.family_name): ChK-ChK on a 4096 row is
Chunked at 4 KB on both sides."""
import argparse
import csv
import json
import multiprocessing
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
import layout_scale_check as L  # (load a grid by its own picks; read the chosen mixes from result rows)
import scale_check_common as C
import scale_check_owners_per_chunk as O
from kvcost.layout_search.search import uniform
from kvevaluator.layouts import cell_id, family_name
from kvcost.layout_cost.cost_models import cost_of
from kvcost.layout_cost.mixes import assignment_label

compose = C.compose
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import settings as cells  # (the reference layout, B's unit and the cells' short names)
from kvevaluator.output_tree import capture_in
# chunk bytes -> its Chunked/Chunked cell, and the captures dir it is read from when not --measured (--captures); filled
# in main() from the flags
SIZES = {65536: "props_chunked_adj_chunked"}
ROOTS = {}


def size_entry(b):
    """the Chunked/Chunked cell of a chunk size in bytes"""
    kb = b // 1024
    return "props_chunked_adj_chunked" if b == 65536 else cell_id(f"chunked_c{kb}k", f"chunked_c{kb}k")


def NAME_OF(b):
    return f"{b // 1024} KB"


class _Names(dict):
    def __missing__(self, b):
        return NAME_OF(b)


NAME = _Names()
REF = family_name(*cells.settings(cells.REF))      # the layout Table 1 is x against (--ref), named as the rows' cells are
CELLS = (("split", "condensed"), ("condensed", "split"), ("chunked", "chunked"), ("chunked", "split"), ("chunked", "condensed"),
         ("split", "chunked"), ("condensed", "chunked"))  # the reference (Sp-Co, B's unit), Co-Sp, then the five with a Chunked side
PARTS = ("hasTag out", "replyOf in", "replyOf out", "hasCreator in", "knows out")   # the scale check's five
WORKLOADS = ("i01", "i02")
RAW = list(compose.MEASURES) + list(compose.CHANNELS)


def cell_name(p, a):
    """'Co-Sp' of ('condensed', 'split'), 'Co-ChK' of ('condensed', 'chunked'): the row's chunk size is K"""
    return family_name(p, a)


def workloads(data):
    return sorted(((n, w) for n, w in benchmarks.select("ldbc", data.sf, None).items() if w["id"] in WORKLOADS), key=lambda t: t[1]["id"])


def cost_row(t):
    cc = compose.currencies(t)
    return {"total_ops": round(cc["ops"], 3), "total_bytes": round(cc["bytes"], 3), "ops_plus_bytes_per_4kb": round(cost_of(t, "sum"), 3),
            **{x: round(t[x], 3) for x in RAW}}


def store_of(measured, size):
    """(sst bytes, keys) of the size's Chunked/Chunked store, from the manifest its run record carries"""
    with open(capture_in(ROOTS.get(size, measured), "interactive").record(SIZES[size])) as f:
        m = json.load(f)["store_manifest"]
    return m["sst_bytes"], m["keys"]


def uniform_cells(grids, measured):
    rows = []
    for size in sorted(grids, reverse=True):
        data = L.load(grids[size], True, record=False)
        sst, keys = store_of(measured, size)
        for n, w in workloads(data):
            units = compose.workload_units(w, data)
            for p, a in CELLS:
                rows.append({"chunk_bytes": size, "workload_id": w["id"], "cell": cell_name(p, a), **cost_row(compose.cost_mix(w, data, uniform(units, p, a))),
                             "chunked_chunked_store_sst_bytes": sst if (p, a) == ("chunked", "chunked") else "",
                             "chunked_chunked_store_keys": keys if (p, a) == ("chunked", "chunked") else ""})
    return rows


def owners(grids, measured):
    """scale_check_owners_per_chunk.one for Split and each chunk size; its CELLS map gets the sizes of this report"""
    O.CELLS = {("split", ""): "props_split_adj_split", **{("chunked", str(s)): SIZES[s] for s in SIZES}}
    rows, missing = [], 0
    for (setting, chunk) in O.CELLS:
        if chunk and int(chunk) not in grids:
            continue
        root = ROOTS.get(int(chunk), measured) if chunk else measured
        _, tot, m = O.one(root, grids[int(chunk)] if chunk else grids[65536], setting, chunk)
        missing += m
        for part in sorted(tot):
            rows.append({"workload_id": "i01", "part": " ".join(part), "lists": setting, "chunk_bytes": chunk,
                         **{c: round(tot[part][c], 3) for c in O.SUMS}})
    if missing:
        sys.exit(f"owners per chunk: {missing} (query, param) whose plan the measured cell did not run")
    return rows


def _mixed_one(task):
    """one (size, workload)'s mixed layouts; a SystemExit inside is raised as a RuntimeError, which the pool passes back
    (a SystemExit kills the worker, its result is lost and Pool.map waits for ever)"""
    try:
        return _mixed_rows(task)
    except SystemExit as e:
        raise RuntimeError(f"mixed layouts {task[:1] + task[2:]}: {e}") from None


def _mixed_rows(task):
    """one (size, workload)'s mixed layouts, read from the size's search-mix result rows (layout_scale_check.chosen:
    nothing is solved here), priced on its grid"""
    size, grid, wid, results = task
    data = L.load(grid, True, record=False)
    n, w = next((n, w) for n, w in workloads(data) if w["id"] == wid)
    solved, units, ref = L.chosen(w, data, results, "per-type-and-direction", grid)
    rows = []
    for q in sorted(solved):
        assign, fits = solved[q], True          # chosen() refuses a named limit no mix fits
        t = compose.cost_mix(w, data, assign)
        ch = [u for u in assign if assign[u] == "chunked"]
        rows.append({"chunk_bytes": size, "workload_id": wid, "question": q, "fits": "yes" if fits else "no", **cost_row(t),
                     "reference_bytes": round(ref, 3), "parts": len(assign), "parts_chunked": len(ch),
                     "props_chunked": sum(u[0] == "props" for u in ch), "lists_chunked": sum(u[0] != "props" for u in ch),
                     "chunked_parts": "; ".join(sorted(C.part_name(u) for u in ch)), "assignment": assignment_label(assign)})
    return rows


def mixed(grids, results, jobs):
    missing = sorted(set(grids) - set(results))
    if missing:
        raise SystemExit(f"--results: none for {', '.join(map(str, missing))} bytes (each size's search-mix rows: its output tree's summary/<dataset>/<scale>/)")
    tasks = [(s, grids[s], wid, results[s]) for s in sorted(grids, reverse=True) for wid in WORKLOADS]
    with multiprocessing.get_context("fork").Pool(min(jobs, len(tasks))) as pool:
        return [r for rows in pool.map(_mixed_one, tasks) for r in rows]


def write_csv(path, rows):
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        wr.writeheader()
        wr.writerows(rows)


def human(v):
    for unit, d in (("G", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= d:
            return f"{v / d:,.2f} {unit}"
    return f"{v:,.0f}"


def tables(sizes, uni, own, mix, sf):
    names = [NAME[s] for s in sizes]
    L_ = [f"# Chunked at {', '.join(names)}: SF{sf}, I01 and I02", "",
          "> Generated by "
          "`experiments/mix_benefit/chunk_size_report.py`; working output, not snapshotted.", "",
          "A workload's cost = the sum over its queries of frequency x the mean over the reported params (1-5). Weighted ops = "
          "GET + 1.1 SEEK + 0.01 NEXT on reads; a write adds its own reads, PUTs and DELETEs. Bytes = read + read by writes + written. "
          f"Each chunk size is a nine-cell grid priced by its own held-out picks. x = against {REF}, the reference layout, the same store at every size. "
          + cells.NAMES_NOTE + " "
          "Counts are deterministic.", ""]
    at = {(r["chunk_bytes"], r["workload_id"], r["cell"]): r for r in uni}
    CC = cell_name("chunked", "chunked")
    L_ += ["## Table 1: the uniform layouts with a Chunked side (properties-adjacency)", "",
           "| workload | layout | " + " | ".join(f"{n}: ops (x)" for n in names) + " | " + " | ".join(f"{n}: bytes (x)" for n in names) + " | "
           + " | ".join(f"{n}: ops + bytes / 4 KB (x)" for n in names) + " |", "|---|---|" + "---|" * (3 * len(sizes))]
    for wid in WORKLOADS:
        for p, a in CELLS[2:]:
            c = cell_name(p, a)
            f = lambda col: " | ".join(f"{human(at[(s, wid, c)][col])} ({at[(s, wid, c)][col] / at[(s, wid, REF)][col]:.2f}x)" for s in sizes)
            L_.append(f"| {wid.upper()} | {c} | {f('total_ops')} | {f('total_bytes')} | {f('ops_plus_bytes_per_4kb')} |")
    ref = at[(sizes[0], "i01", REF)]
    L_ += ["", f"{REF}, I01: {human(ref['total_ops'])} ops, {human(ref['total_bytes'])} bytes, {human(ref['ops_plus_bytes_per_4kb'])} ops + bytes / 4 KB. "
           "The Chunked/Chunked store on disk: " + ", ".join(
               f"{NAME[s]} {at[(s, 'i01', CC)]['chunked_chunked_store_sst_bytes'] / 1e9:.2f} GB "
               f"({at[(s, 'i01', CC)]['chunked_chunked_store_keys']:,} keys)" for s in sizes) + ".", ""]
    by = {(r["part"], r["lists"], r["chunk_bytes"]): r for r in own}
    L_ += ["## Table 2: why (I01's list parts on Chunked lists)", "",
           "Owners probed = rows into the steps that read the part. On Chunked lists one SEEK reads one chunk, so owners per chunk read = "
           "owners probed / SEEKs. Measured Chunked/Chunked cells; the plan is the one the layout (Condensed properties, every list Chunked) picks.", "",
           "| part | owners probed | " + " | ".join(f"{n}: owners per chunk read" for n in names) + " | "
           + " | ".join(f"{n}: bytes per owner" for n in names) + " | Split: bytes per owner | " + " | ".join(f"{n}: chunks read" for n in names) + " | "
           + " | ".join(f"{n}: bytes per chunk read" for n in names) + " |", "|---|---|" + "---|" * (4 * len(sizes) + 1)]
    for part in PARTS:
        s = by[(part, "split", "")]
        rs = [by[(part, "chunked", str(z))] for z in sizes]
        L_.append(f"| {part} | {human(s['owners_probed'])} | " + " | ".join(f"{r['owners_probed'] / max(r['S'], 1):,.2f}" for r in rs) + " | "
                  + " | ".join(f"{r['total_bytes'] / r['owners_probed']:,.0f}" for r in rs) + f" | {s['total_bytes'] / s['owners_probed']:,.0f} | "
                  + " | ".join(human(r["S"]) for r in rs) + " | " + " | ".join(f"{r['total_bytes'] / max(r['S'], 1):,.0f}" for r in rs) + " |")
    mx = {(r["chunk_bytes"], r["workload_id"], r["question"]): r for r in mix}
    L_ += ["", "## Table 3: the mixed layout, per type and direction (read from search-mix's result rows)", "",
           f"B = the byte limit as a multiple of {cells.name_of(cells.B_UNIT)}'s bytes. Chunked parts = how many of the layout's parts take Chunked (properties + lists).", "",
           "| workload | question | " + " | ".join(f"{n}: ops" for n in names) + " | " + " | ".join(f"{n}: bytes (x reference)" for n in names) + " | "
           + " | ".join(f"{n}: ops + bytes / 4 KB" for n in names) + " | " + " | ".join(f"{n}: Chunked parts" for n in names) + " |",
           "|---|---|" + "---|" * (4 * len(sizes))]
    for wid in WORKLOADS:
        for q in sorted({r["question"] for r in mix}):
            rs = [mx[(s, wid, q)] for s in sizes]
            L_.append(f"| {wid.upper()} | {q} | " + " | ".join(human(r["total_ops"]) + ("" if r["fits"] == "yes" else " (over the limit)") for r in rs) + " | "
                      + " | ".join(f"{human(r['total_bytes'])} ({r['total_bytes'] / r['reference_bytes']:.2f}x)" for r in rs) + " | "
                      + " | ".join(human(r["ops_plus_bytes_per_4kb"]) for r in rs) + " | "
                      + " | ".join(f"{r['parts_chunked']} of {r['parts']} ({r['props_chunked']} + {r['lists_chunked']})" for r in rs) + " |")
    L_ += ["", "## Table 4: I01's writes per chunk size", "",
           "| layout | " + " | ".join(f"{n}: write ops (PUT + DELETE)" for n in names) + " | " + " | ".join(f"{n}: bytes written" for n in names) + " | "
           + " | ".join(f"{n}: ops the writes read first" for n in names) + " | " + " | ".join(f"{n}: bytes the writes read first" for n in names) + " |",
           "|---|" + "---|" * (4 * len(sizes))]
    for p, a in CELLS:
        c = cell_name(p, a)
        f = lambda col: " | ".join(human(at[(s, "i01", c)][col]) for s in sizes)
        L_.append(f"| {c} | {f('write_ops')} | {f('write_bytes')} | {f('write_rmw_ops')} | {f('write_rmw_bytes')} |")
    return "\n".join(L_) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--measured", required=True, help="a captures/ldbc/<scale> dir: the cells at 64 KB (the diagonal's steps) and each size's Chunked/Chunked cell")
    ap.add_argument("--composed", action="append", default=[], metavar="BYTES=DIR", help="a size's composed nine-cell grid; repeatable "
                    "(65536=DIR: the 64 KB grid priced, when --measured holds only the diagonal)")
    ap.add_argument("--captures", action="append", default=[], metavar="BYTES=DIR", help="another captures dir holding a size's "
                    "Chunked/Chunked cell, when --measured does not; repeatable")
    ap.add_argument("--results", action="append", default=[], metavar="BYTES=DIR", help="a size's search-mix result dir "
                    "(the summary/<dataset>/<scale>/ of the output tree search-mix and search-mix --byte-limit "
                    "--limits named wrote into, I01 and I02, per type and direction, on that size's grid); one per size")
    ap.add_argument("--out", required=True)
    ap.add_argument("--ref", default=cells.REF, choices=cells.CELLS, help="the layout Table 1's x is against (default Sp-Co)")
    ap.add_argument("--jobs", type=int, default=8, help="the mixed layouts' solves at a time (one per size and workload)")
    ap.add_argument("--only", default="", help="uniform,owners,mixed: a part of the report (the tables need all three)")
    a = ap.parse_args()
    global REF
    REF = family_name(*cells.settings(a.ref))
    if tuple(cells.settings(a.ref)) not in CELLS:
        raise SystemExit(f"--ref {a.ref}: not one of the report's cells")
    for k, v in (x.split("=", 1) for x in a.captures):
        ROOTS[int(k)] = os.path.abspath(v)
    grids = {65536: a.measured, **{int(k): v for k, v in (x.split("=", 1) for x in a.composed)}}
    for b in grids:
        SIZES.setdefault(b, size_entry(b))
    sizes = sorted(grids, reverse=True)
    only = set(a.only.split(",")) if a.only else {"uniform", "owners", "mixed"}
    os.makedirs(a.out, exist_ok=True)
    out = {}
    for name, fn in (("mixed", lambda: mixed(grids, {int(k): os.path.abspath(v) for k, v in (x.split("=", 1) for x in a.results)}, a.jobs)), ("uniform", lambda: uniform_cells(grids, a.measured)),
                     ("owners", lambda: owners(grids, a.measured))):
        if name in only:
            out[name] = fn()
            path = os.path.join(a.out, {"uniform": "uniform_cells.csv", "owners": "owners_per_chunk.csv", "mixed": "mixed_layouts.csv"}[name])
            write_csv(path, out[name])
            print(f"wrote {path} ({len(out[name])} rows)", flush=True)
    if len(out) == 3:
        sf = L.load(grids[65536], True, record=False).sf
        text = tables(sizes, out["uniform"], out["owners"], out["mixed"], sf)
        with open(os.path.join(a.out, "tables.md"), "w") as f:
            f.write(text)
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
