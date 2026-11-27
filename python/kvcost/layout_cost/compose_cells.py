#!/usr/bin/env python3
"""Write the captures of uniform layouts not measured themselves from the measured diagonals (Sp-Sp, Co-Co and
Chunked/Chunked at each chunk size): the only composer that writes a layout's capture (kvcost compose runs it).

A cell props P / adjacency A takes its property and index part rows from the P/P capture and its adjacency (and edge
property) part rows from the A/A capture, per (query, plan, param, step); a run's totals are the sums of its part rows.
P and A are any settings, Chunked at any chunk size (Sp-Ch4k from Sp-Sp and Ch4k-Ch4k). So a scale where only the
diagonals were measured reads as every pair of them. Analysis only: opens no store. Each composed run record carries
the measured files it was made from by content (`composed_from_sha`: one measured again since marks the layout stale,
note_if_stale) and its own `wall_s`.

    python3 -m kvcost.layout_cost.compose_cells --grid output/captures/ldbc/sf3 --out output/composed/ldbc/sf3
        [--whole-grid]   --out is a grid read on its own (a scratch or study dir), not the composed place beside --grid
        [--chunked-from <captures/<dataset>/<scale> dir> <its cell>]   with --whole-grid: the Chunked diagonal from
                    another capture (another chunk size)
        [--check]   compare every cell written with its measured files under --grid, where they exist; exit 1 on a
                    difference, or when 0 rows were compared. With --chunked-from, a cell with a Chunked side is not
                    compared (its measured file is at the grid's own chunk size)
        [--bill]    with --check: also the bill's three forms (bill_lower, bill_upper, bill_exact), composed against
                    measured; per form the runs, cells and queries compared and the largest relative difference
        [--dataset ldbc|linkbench|imdb] [--families a,b] [--cells C1,C2] [--param-set S]
        [--control swap-sources]   a negative control: props parts from A/A, adjacency parts from P/P (--check must differ)
        [--jobs N]    layouts composed at once, a process each (default: the cores, capped by memory); same files
        [--replace]   --out may hold a composed capture: its files kept when byte-identical, new ones added, a changed
                      one moved aside (install())

Written under --out, never under --grid, as <workload>/<params>/{counts,parts,exact}/<cell>.{csv,json}. Two forms,
named, never guessed from the paths:
  - default: --out is the composed place beside --grid (a tree's composed/<dataset>/<scale> next to its
    captures/<dataset>/<scale>: kvcost compose; any other --out is refused). Only the layouts not measured themselves
    are written: a reader finds each layout by output_tree.layout_capture (measured itself, else composed), so nothing
    is copied, and an off-diagonal cell measured itself is composed and --checked against its measured files, then left
    out.
  - --whole-grid: --out is a grid read on its own, every layout composed: the diagonal cells are copied in as they
    are, and an off-diagonal cell is kept composed even where --grid measured it (after --check), so a composed-vs-
    measured check reads the composed side. The form of a study's composed-only grid, a gate's
    scratch grid, and --chunked-from's grid at another chunk size (its Chunked diagonal is a renamed copy).
A composed counts row says so in `measured_as`; a composed run record carries "composed_from". No steps files are
written.

Exactness: exact/<cell>.csv marks every runs row with its query's mark (`exact`):
  "yes"          every row of the query is ok on both sources with equal answers, and each source's parts sum to its
                 runs row (a body statically invalid on the target cell aside). Then the composed pick is the measured
                 cell's. For LDBC's reads the reported and held-out sets are one pick, so the query takes one mark over
                 the two, written in both files.
  "upper bound"  an in-sample family (microbench, bi, job) lost a non-planner body: the pick is an argmin over fewer
                 bodies, so the pick score is >= the measured one (wops or bytes alone may still drop)
  "unknown"      the same for a held-out family (LDBC IC/IS): the 10% margin rule can move the cost either way
  "no pick"      the planner's own row failed on a param (then every body is rejected), or a LinkBench request lost
                 its one body
`why` names the rows that broke "yes". Not covered by a mark: a body ok on both sources that would fail on the
measured target.
"""
import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from kvevaluator.measure.run_measure import at_a_time, at_once, own_gb
from kvevaluator.paths import ROOT, recorded_path, resolve_recorded
from kvevaluator.layouts import cell_id

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.compose import unit_of  # (the one part rule)
from kvcost.layout_cost import settings
from kvevaluator.output_tree import HELD_OUT, capture_in, place_pair, tree_root, writes_in
csv.field_size_limit(1 << 30)

LAY = settings.SETTINGS
cell_of = cell_id
from kvevaluator.capture.step_record import READ_BYTE_LANES as LANES  # (the 7 read lanes, one definition)
from kvevaluator.capture.step_record import READ_OPS as COUNTS  # (a read row's counts)
from kvevaluator.capture.step_record import WRITE_BYTE_LANES, WRITE_OPS  # (a write row's ops and lanes)
from kvevaluator.capture.step_record import (LAST_CHUNK)  # (a write row's last-chunk columns, summed like its lanes)
from kvevaluator.capture.step_record import WRITE_UNITS  # (a write part row's exact write units)
from kvevaluator.plan_choice.candidates import INVALID_HERE, candidate_bodies, current_name  # (HEAD's candidates; a captured name as named today)
from kvevaluator import stale
from kvevaluator.capture import capture_layout
from kvcost.layout_cost.picks import PlanRule  # (in-sample: the rule's test)
DATASET = ["ldbc"]                           # --dataset: whose capture layout (reads' workload, the write parts' path)
PARAM_SET = [None]                         # --param-set: whose reported rows (LinkBench's: anchors, quick, quick-next, log)
READ_SUBS = [()]                           # --workloads: the read captures the composed grid holds, (workload, rows)
WITH_WRITES = [False]                      # --workloads: does it hold the dataset's writes too


def read_subs(dataset, workloads=None):
    """((read captures: (workload, rows)), with writes?) of the dataset's workloads (`workloads`: a comma list; default
    its own, the optional ones aside: LDBC's BI is composed alone, --families bi). A workload priced held-out has a
    held-out capture beside its reported one (the workload manifests' pick_params)"""
    from kvevaluator import workloads as W
    names = workloads.split(",") if workloads else list(W.families(dataset))
    bad = [w for w in names if w not in W.names(dataset=dataset)]
    if bad:
        raise SystemExit(f"--families {','.join(bad)}: not workloads of {dataset} (have: {', '.join(W.names(dataset=dataset))})")
    reads = [w for w in names if W.get(w).kind == "read"]
    subs = tuple(s for w in reads for s in [(w, "reported")] + ([(w, "held-out")] if W.get(w).pick_params == "held-out" else []))
    return subs, any(W.get(w).kind == "write" for w in names)
MARK_ORDER = ("yes", "upper bound", "unknown", "no pick")      # weakest last
EXACT_HEADER = ["query", "body", "param_idx", "exact", "why"]


def capture(root, sub):
    """a read capture below a <dataset>/<scale> dir (sub: its (workload, rows))"""
    workload, rows = sub
    return capture_in(root, workload, HELD_OUT if rows == HELD_OUT else PARAM_SET[0])


def writes(root):
    """the dataset's write capture below a <dataset>/<scale> dir"""
    return writes_in(root, DATASET[0], PARAM_SET[0])


def label(sub):
    """a read capture's name in messages and marks: its dir below the grid root ("" for the grid's own reads)"""
    rel = os.path.relpath(capture("R", sub).path, "R")
    return "" if rel == "." else rel


def read(path):
    with open(path) as f:
        rd = capture_layout.reader(f)
        return rd.fieldnames, list(rd)


SOURCES = {}      # a diagonal's file -> what was read from it: every layout of a call reads its two diagonals' files
                  # (kept until the call ends: at --jobs 1 too, every diagonal read stays in memory)


def source(path, cell=None, sha=False):
    """a measured diagonal's file, read once per call (its rows are never changed here): read()'s (header, rows) of a
    read capture's file; with `cell`, capture_layout.write_rows' result of a writes file; with `sha`, file_sha's"""
    key = (path, cell, sha)
    if key not in SOURCES:
        SOURCES[key] = file_sha(path) if sha else read(path) if cell is None else capture_layout.write_rows(path, cell=cell)
    return SOURCES[key]


def rows_of(path, cell, is_write):
    """(header, rows) of a capture file in the grid columns: a writes file through capture_layout.write_rows (the one reader
    of either lane naming), a read file through read()"""
    if is_write:
        _, header, rows = capture_layout.write_rows(path, cell=cell)
        return header, rows
    return read(path)


def write(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=header, lineterminator="\n", extrasaction="ignore")
        wr.writeheader()
        wr.writerows(rows)


def side(part):
    """which diagonal source a part row comes from: its layout unit's (compose.unit_of); an index part (no unit: the index
    is fixed) from the props source"""
    u = unit_of(part, False)
    return "adj" if u and u[0] == "adj" else "props"


def spec_file(cell):
    """the spec file of a uniform layout at any chunk size: the grid's, a size set's, or made from its name
    (kvevaluator.layouts.resolve)"""
    from kvevaluator import layouts
    return layouts.resolve(cell).spec


def file_sha(path):
    """the sha256 of a file's bytes (a composed layout's sources, so a source measured again since is seen)"""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sources_moved(rec, tree=None):
    """[the source files of a composed run record whose bytes moved since it was composed] (its `composed_from_sha`:
    {path as recorded_path writes it, relative to the record's tree `tree`: sha256}); [] for a record of a measured
    layout or one with no shas"""
    moved = []
    for path, sha in sorted((rec.get("composed_from_sha") or {}).items()):
        full = resolve_recorded(path, tree)
        if not os.path.exists(full) or file_sha(full) != sha:
            moved.append(path)
    return moved


def note_if_stale(record_path, cell):
    """a stale note (a warning, never a refusal) when a composed layout's sources were measured again since it was
    composed; -> True when it was"""
    if not os.path.exists(record_path):
        return False
    with open(record_path) as f:
        rec = json.load(f)
    moved = sources_moved(rec, tree_root(Path(record_path).parents[3]))
    if moved:
        stale.note("composed_stale", f"{cell}: composed from {', '.join(moved)}, measured again since "
                                     f"(kvcost compose composes it again)")
    return bool(moved)


_FLAGS = {}
MARKS = {}      # (cell, sub) -> {query: mark}, for the summary line
STATE = {}      # (cell, sub) -> (runs rows, {query: why}, {query} with no pick, weak mark): write_marks reads it
_INVALID = {}


def invalid_on_target(workload, cell):
    """{query: bodies} statically invalid on the target cell (grid_capture.invalid_on_cell): the measured cell never runs
    them, so a failed source row of one of them loses nothing"""
    if (workload, cell) not in _INVALID:
        from kvevaluator.measure.grid_capture import invalid_on_cell
        _INVALID[(workload, cell)] = invalid_on_cell(workload, spec_file(cell))
    return _INVALID[(workload, cell)]


class _Flags(set):
    """the measure-only bodies of a query on a cell, and (a source with a planner_pick column) the selector's body"""
    planner = None


def plan_flags(q, cell, sf, workload, historical=False):
    """the measure-only bodies of query q on a cell, from the cell's own spec (what the capture tool records per run row:
    measure_only_bodies | measure_only_on_cell; they depend on the cell's capabilities, so on BOTH settings: a composed
    cell cannot copy them from a source), with `.planner` the selector's body where `historical` (a source capture with
    a planner_pick column; a plan file without `variants` has none). The spec is the 64 KB grid's even with
    --chunked-from: harmless while no body's validity names the chunk size."""
    if (q, cell, sf, workload, historical) not in _FLAGS:
        from kvevaluator.plan_choice.candidates import measure_only_bodies
        from kvevaluator.plan_choice.cells import cell_selection
        from kvevaluator.measure.grid_capture import measure_only_on_cell
        spec_path = spec_file(cell)
        f = _Flags(measure_only_bodies(workload, q) | measure_only_on_cell(workload, q, spec_path))
        if historical:
            try:
                f.planner = cell_selection(workload, spec_path, sf, [q])[q]
            except ValueError:                            # today's plan file has no selector
                f.planner = None
        _FLAGS[(q, cell, sf, workload, historical)] = f
    return _FLAGS[(q, cell, sf, workload, historical)]


class Source:
    """where a diagonal cell's files are read from: a captures/<dataset>/<scale> dir (--grid, or --chunked-from's)"""

    def __init__(self, root, cell):
        self.root, self.cell = root, cell

    def path(self, sub, kind, ext="csv"):
        """a read capture's file of the source's own cell (sub: (workload, rows))"""
        return capture(self.root, sub).path_of(kind, self.cell, ext)

    def writes_path(self, kind):
        """the source cell's write capture file (kind counts: the flat rows; parts: per part)"""
        return writes(self.root).path_of(kind, self.cell)


def compose_reads(sp, sa, cell, sub, out):
    """one off-diagonal cell's reads in <out>'s capture `sub` ((workload, rows)): parts, then runs summed from them"""
    t0 = time.time()
    workload = family = sub[0]
    hp, parts_p = source(sp.path(sub, "parts"))
    _, parts_a = source(sa.path(sub, "parts"))
    hr, runs_p = source(sp.path(sub, "counts"))
    _, runs_a = source(sa.path(sub, "counts"))
    key = lambda r: (r["query"], r["body"], r["param_idx"])
    by_a = {key(r): r for r in runs_a}
    parts = [r for r in parts_p if side(r["part"]) == "props"] + [r for r in parts_a if side(r["part"]) == "adj"]
    order = {key(r): i for i, r in enumerate(runs_p)}
    parts.sort(key=lambda r: (order.get(key(r), 1 << 30), int(r["step_idx"]), r["part"]))
    write(capture(out, sub).path_of("parts", cell), hp, parts)
    tot = {}
    for r in parts:
        t = tot.setdefault(key(r), dict.fromkeys(COUNTS + LANES, 0))
        for c in COUNTS + LANES:
            t[c] += int(float(r[c] or 0))
    rows, problems = [], []
    where = f"{cell} {'/'.join(sub) or 'reported'}"
    names = {}
    broke = {}                                               # query -> the rows that break "yes" (exact/<cell>.csv)
    nopick = set()                                           # queries whose planner row failed: no pick at all
    for r in runs_p:
        names.setdefault(r["query"], set()).add(r["body"])
    today_of, cands = {}, {}       # HEAD's name of a (query, body, sha) and a query's candidates: read once, not per row
    for r in runs_p:
        nk = (r["query"], r["body"], r.get("body_sha"))
        if nk not in today_of:
            today_of[nk] = current_name(workload, r["query"], r["body"], r.get("body_sha"))
        if r["query"] not in cands:
            cands[r["query"]] = candidate_bodies(workload, r["query"])
    for src, runs, prts in (("props", runs_p, parts_p), ("adjacency", runs_a, parts_a)):
        seen = set()
        for r in runs:                                       # one row per key, or a source overwrites itself
            if key(r) in seen:
                problems.append(f"{where} {key(r)}: two rows on the {src} source")
                broke.setdefault(r["query"], []).append(f"{r['body']}@{r['param_idx']} two rows on the {src} source")
            seen.add(key(r))
        whole = {}                                           # every part of the source, both sides: its runs row's total
        for r in prts:
            w = whole.setdefault(key(r), [0, 0, 0])
            for n, c in enumerate(("S", "G", "N")):
                w[n] += int(float(r[c] or 0))
        for r in runs:
            if r["ok"] == "1" and whole.get(key(r), [0, 0, 0]) != [int(float(r[c] or 0)) for c in ("S", "G", "N")]:
                problems.append(f"{where} {key(r)}: the {src} source's parts do not sum to its runs row (S, G, N "
                                f"{whole.get(key(r))} against {[r[c] for c in ('S', 'G', 'N')]})")
                broke.setdefault(r["query"], []).append(f"{r['body']}@{r['param_idx']} {src} parts do not sum to its run")
    keys_p = {key(r) for r in runs_p}
    for k in sorted(set(by_a) - keys_p):                     # a row the props source lacks: never dropped silently
        problems.append(f"{where} {k}: not run on the props source")
        broke.setdefault(k[0], []).append(f"{k[1]}@{k[2]} props source has no row")
    for r in runs_p:
        a = by_a.get(key(r))
        if a is None:
            problems.append(f"{where} {key(r)}: not run on the adjacency source")
            broke.setdefault(r["query"], []).append(f"{r['body']}@{r['param_idx']} adjacency source has no row")
            continue
        if r["ok"] == "1" and a["ok"] == "1" and r["result_hash"] != a["result_hash"]:
            problems.append(f"{where} {key(r)}: the two sources returned different answers")
            broke.setdefault(r["query"], []).append(f"{r['body']}@{r['param_idx']} different answers")
        ok = r["ok"] == "1" and a["ok"] == "1"
        today = today_of[(r["query"], r["body"], r.get("body_sha"))]     # HEAD's name for the row's body
        invalid = (today in invalid_on_target(workload, cell).get(r["query"], {})
                   or today not in cands[r["query"]])                   # a retired body: no candidate anywhere today
        t = tot.get(key(r), dict.fromkeys(COUNTS + LANES, 0))
        row = dict(r, cell=cell, ok="1" if ok else "0", wall_ns="", measured_as="composed")
        if ok and invalid:                                   # valid on each source, not on the target: never a candidate
            ok = False
            row.update(ok="0", error=f"{INVALID_HERE} (composed: valid on both sources, not on the target)")
        mo = plan_flags(r["query"], cell, str(r["sf"]), workload, historical="planner_pick" in r)
        if "planner_pick" in r:                               # a source with a planner_pick column: its selector's flag
            planner = mo.planner
            if planner is None:                               # no selector today: no reference row (the vote decides)
                row["planner_pick"] = ""
            elif planner in names[r["query"]]:                # a query with one plan keeps its source's flag
                row["planner_pick"] = int(today == planner)
        row["measure_only"] = int(today in mo)
        if not ok and not invalid:
            broke.setdefault(r["query"], []).append(f"{r['body']}@{r['param_idx']} ok props {r['ok']} adjacency {a['ok']}")
            if str(row.get("planner_pick")) == "1" or DATASET[0] == "linkbench":
                nopick.add(r["query"])
        if ok:
            row.update({c: t[c] for c in COUNTS + LANES})
            row["read_bytes"] = sum(t[c] for c in LANES)
        rows.append(row)
    write(capture(out, sub).path_of("counts", cell), hr, rows)
    held_out = (family, "held-out") in READ_SUBS[0]     # does the grid hold held-out rows of this family?
    in_sample = PlanRule().picks_in_sample(family, has_held_out=held_out)
    STATE[(cell, sub)] = (rows, broke, nopick, "upper bound" if in_sample else "unknown")
    rec = json.load(open(sp.path(sub, "counts", "json")))
    tree = tree_root(out)
    spec_path = recorded_path(spec_file(cell), tree)
    # the spec the counts are of: the props source's vertex props and index, the adjacency source's adjacency (with
    # --chunked-from, the Chunked side's chunk size is the substitute's, not the grid spec's); no store was opened
    adj_rec = json.load(open(sa.path(sub, "counts", "json")))
    spec = dict(rec["spec"], adjacency=adj_rec["spec"]["adjacency"])
    rec.update({"cell": cell, "spec": spec, "spec_path": spec_path, "store": "", "store_manifest": None, "cell_inputs": None,
                "composed_from": {"props and index parts": recorded_path(sp.path(sub, "parts"), tree),
                                  "adjacency parts": recorded_path(sa.path(sub, "parts"), tree)},
                # the measured layouts it was made from, by content (sources_moved: a source measured again since marks
                # it stale), and the wall time this layout's composition took
                "composed_from_sha": {recorded_path(x, tree): source(x, sha=True) for x in sorted(
                    {sp.path(sub, "parts"), sp.path(sub, "counts"), sa.path(sub, "parts"), sa.path(sub, "counts")})},
                "wall_s": round(time.time() - t0, 3)})
    with open(capture(out, sub).record(cell), "w") as f:   # marked: both sources' moved inputs, and this run's
        json.dump(stale.fold(rec, extra=adj_rec.get("stale", []) + adj_rec.get("adopted", [])), f, indent=1, sort_keys=True)
        f.write("\n")
    return problems


def write_marks(cell, subs, out):
    """exact/<cell>.csv per read sub, from compose_reads' STATE: a query's mark is its weakest over the subs priced as
    one pick (LDBC's reported and held-out sets), else over its own sub"""
    groups = [[s for s in subs if s[0] == "interactive"], [s for s in subs if s[0] != "interactive"]] \
        if DATASET[0] == "ldbc" else [[s] for s in subs]
    for g in groups:
        g = [s for s in g if (cell, s) in STATE]
        if not g:
            continue
        broke, nopick = {}, set()
        for s in g:
            for q, why in STATE[(cell, s)][1].items():
                broke.setdefault(q, []).extend(f"{label(s) or 'reported'}: {w}" for w in why)
            nopick |= STATE[(cell, s)][2]
        for s in g:
            rows, _, _, weak = STATE[(cell, s)]
            mark = lambda q: "no pick" if q in nopick else weak if q in broke else "yes"
            write(capture(out, s).path_of("exact", cell), EXACT_HEADER,
                  [{"query": r["query"], "body": r["body"], "param_idx": r["param_idx"], "exact": mark(r["query"]),
                    "why": "; ".join(broke.get(r["query"], []))} for r in rows])
            MARKS[(cell, s)] = {r["query"]: mark(r["query"]) for r in rows}


def compose_writes(sp, sa, cell, out):
    """the writes: the per-part rows by side, and the flat rows' counted columns summed from them, in the grid shape
    (capture_layout.write_rows reads either shape of the sources), and the run record beside them, as compose_reads
    writes one: the measured files it was made from by content and its own wall_s"""
    if not os.path.exists(sp.writes_path("parts")):
        return ["no writes capture"]
    t0 = time.time()
    _, hp, parts_p = source(sp.writes_path("parts"), sp.cell)
    _, _, parts_a = source(sa.writes_path("parts"), sa.cell)
    key = lambda r: (r["query"], r["param_idx"])
    parts = [r for r in parts_p if side(r["part"]) == "props"] + [r for r in parts_a if side(r["part"]) == "adj"]
    _, hf, flat_p = source(sp.writes_path("counts"), sp.cell)
    _, _, flat_a = source(sa.writes_path("counts"), sa.cell)
    only_a = sorted({key(r) for r in flat_a} - {key(r) for r in flat_p})
    only_p = sorted({key(r) for r in flat_p} - {key(r) for r in flat_a})
    problems = [f"{cell} writes {k}: not run on the props source" for k in only_a] + \
        [f"{cell} writes {k}: not run on the adjacency source" for k in only_p]
    order = {key(r): i for i, r in enumerate(flat_p)}
    parts.sort(key=lambda r: (order.get(key(r), 1 << 30), int(r["step_idx"]), r["part"]))
    write(writes(out).path_of("parts", cell), hp, parts)
    num = [c for c in hp if c in (*COUNTS, *LANES, *WRITE_OPS, *WRITE_BYTE_LANES, *LAST_CHUNK)]
    tot = {}
    for r in parts:
        t = tot.setdefault(key(r), dict.fromkeys(num, 0))
        for c in num:
            t[c] += int(float(r[c] or 0))
    rows = []
    for r in flat_p:
        t = tot.get(key(r), dict.fromkeys(num, 0))
        row = dict.fromkeys(hf, "")
        row.update({k: r[k] for k in ("sf", "query", "param_idx", "body", "measure_only", "ok", "error", "body_sha")})
        row.update({"cell": cell, **{c: t[c] for c in num if c in hf}})
        row["read_bytes"] = sum(t[c] for c in LANES)
        rows.append(row)
    write(writes(out).path_of("counts", cell), hf, rows)
    src_p, src_a = writes(sp.root).record(sp.cell), writes(sa.root).record(sa.cell)
    rec = json.load(open(src_p)) if os.path.exists(src_p) else {}
    adj_rec = json.load(open(src_a)) if os.path.exists(src_a) else {}
    tree = tree_root(out)
    rec.update({"cell": cell, "store": "", "store_manifest": None, "cell_inputs": None,
                "composed_from": {"props and index parts": recorded_path(sp.writes_path("parts"), tree),
                                  "adjacency parts": recorded_path(sa.writes_path("parts"), tree)},
                "composed_from_sha": {recorded_path(x, tree): source(x, sha=True) for x in sorted(
                    {sp.writes_path("parts"), sp.writes_path("counts"), sa.writes_path("parts"), sa.writes_path("counts")})},
                "wall_s": round(time.time() - t0, 3)})
    with open(writes(out).record(cell), "w") as f:     # marked: both sources' moved inputs, as a read record
        json.dump(stale.fold(rec, extra=adj_rec.get("stale", []) + adj_rec.get("adopted", [])), f, indent=1, sort_keys=True)
        f.write("\n")
    return problems


def copy_cell(src, cell, out, subs=None):
    """a cell as measured: every capture file of it (of `subs`' read captures, default all, and the writes' when
    measured), under the grid's cell name"""
    n = 0
    files = [(capture(src.root, s), capture(out, s), k, e) for s in (READ_SUBS[0] if subs is None else subs)
             for k, e in (("counts", "csv"), ("counts", "json"), ("parts", "csv"))]
    if WITH_WRITES[0] and (subs is None or os.path.exists(writes(src.root).record(src.cell))):
        files += [(writes(src.root), writes(out), k, e) for k, e in (("counts", "csv"), ("parts", "csv"))
                  + ((("counts", "json"),) if subs is not None else ())]   # an off-diagonal's record replaces the composed one
    for src_cap, out_cap, kind, ext in files:
        s = src_cap.path_of(kind, src.cell, ext)
        if os.path.exists(s):
            d = out_cap.path_of(kind, cell, ext)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            if src.cell == cell:
                shutil.copyfile(s, d)
            elif s.endswith(".json"):                # another spec set's cell under the grid's name: say where it is from
                rec = json.load(open(s))
                rec.update({"cell": cell, "named_as_the_grids_cell_from": recorded_path(s, tree_root(out))})
                json.dump(rec, open(d, "w"), indent=1, sort_keys=True)
            else:
                h, rows = read(s)
                write(d, h, [dict(r, cell=cell) if "cell" in r else r for r in rows])
            n += 1
    return n


def check_form(a):
    """refuse an --out its form does not allow: --chunked-from needs --whole-grid, and without --whole-grid --out must
    be the composed place beside --grid (output_tree.place_pair), where readers find the measured layouts"""
    if a.chunked_from and not a.whole_grid:
        raise SystemExit("--chunked-from writes a grid read on its own: give --whole-grid")
    pair = place_pair(a.out)
    if not a.whole_grid and (pair is None or os.path.abspath(pair[0]) != os.path.abspath(a.grid)):
        raise SystemExit(f"--out {a.out} is not the composed place beside --grid {a.grid} (kvcost compose writes there; "
                         f"a grid read on its own takes --whole-grid)")


def drop_cell(cell, out, subs, grid):
    """remove a composed cell's files from `out` in the query sets `subs` it was measured in (and the writes', when
    `grid` holds its measured writes): a layout measured itself is read from its captures. -> the files removed"""
    caps = [capture(out, s) for s in subs] + ([writes(out)] if WITH_WRITES[0] and os.path.exists(writes(grid).record(cell))
                                              else [])
    paths = [c.path_of(k, cell, e) for c in caps for k, e in (("counts", "csv"), ("counts", "json"), ("parts", "csv"),
                                                              ("exact", "csv"))]
    n = 0
    for p in paths:
        if os.path.exists(p):
            os.remove(p)
            n += 1
    return n


TEXT_COLS = ("result_hash", "planner_pick", "measure_only", "ok")    # compared as text; every other column as a number


def row_difference(x, y, cols):
    """the first difference of two ok rows (a measured one, its composed twin) over cols, as text, or None: the numeric
    columns first (equal as text, or as numbers: 3 and 3.0 agree), then the text columns (TEXT_COLS)"""
    d = next((f"{col}: measured {x[col]!r}, composed {y[col]!r}" for col in cols
              if col not in TEXT_COLS                              # first: a hash is not a number
              if str(x[col] or 0) != str(y[col] or 0) and float(x[col] or 0) != float(y[col] or 0)), None)
    return d or next((f"{col}: measured {x[col]!r}, composed {y.get(col)!r}" for col in TEXT_COLS
                      if col in cols and x[col] != y.get(col)), None)


def check(grid, out, cell, write_rows_compared=None):
    """a composed cell against its measured files: (rows compared, rows that differ, the first difference);
    write_rows_compared: a [rows, measured files] list the writes files' rows and files are added to"""
    cmp_cols = {"runs": ("ok", "planner_pick", "measure_only", "result_hash", "rows", "read_bytes") + COUNTS + LANES}
    n = bad = 0
    first = ""
    run_key = lambda r: (r["query"], r["body"], r["param_idx"])
    part_key = lambda r: (r["query"], r["body"], r["param_idx"], r["step_idx"], r["part"])
    write_key = lambda r: (r["query"], r["param_idx"], r["step_idx"], r["part"])
    # (the measured file, the composed one, its key), in the order the first difference is reported
    reads = READ_SUBS[0]
    order = [(s, k) for k in ("counts", "parts") for s in reads[:2]] + [(s, k) for s in reads[2:] for k in ("counts", "parts")] \
        if DATASET[0] == "ldbc" else [(s, k) for s in reads for k in ("counts", "parts")]
    files = [(capture(grid, s), capture(out, s), k, run_key if k == "counts" else part_key) for s, k in order]
    if WITH_WRITES[0]:
        files.append((writes(grid), writes(out), "parts", write_key))
    for m_cap, c_cap, kind, keyf in files:
        is_write = keyf is write_key                                     # the writes capture's file (either layout)
        sub = os.path.relpath(m_cap.dir(kind), grid).split(os.sep)     # its name in the first difference
        m, c = m_cap.path_of(kind, cell), c_cap.path_of(kind, cell)
        if not os.path.exists(m):
            continue
        if is_write and write_rows_compared is not None:
            write_rows_compared[1] += 1
        if not os.path.exists(c):                               # measured, but not composed: a hole
            bad += 1
            first = first or f"{'/'.join(sub)}: measured, not composed"
            continue
        hm, rm = rows_of(m, cell, is_write)       # both in the grid columns: a measured file of the older shape too
        _, rc = rows_of(c, cell, is_write)
        cols = [x for x in cmp_cols["runs"] if x in hm] if kind == "counts" and not is_write else \
            [x for x in hm if x in (*COUNTS, *LANES, *WRITE_OPS, *WRITE_BYTE_LANES, *LAST_CHUNK, *WRITE_UNITS)]
        dm, dc = {keyf(r): r for r in rm}, {keyf(r): r for r in rc}
        for k in sorted(set(dm) | set(dc)):
            n += 1
            if is_write and write_rows_compared is not None:
                write_rows_compared[0] += 1
            x, y = dm.get(k), dc.get(k)
            d = None if x and y else "in one file only"
            if x and y and x.get("ok", "1") != "1":            # a failed run: its ok and its flags must still agree
                d = next((f"{col}: measured {x[col]!r}, composed {y.get(col)!r}" for col in ("ok", "planner_pick", "measure_only")
                          if col in x and x[col] != y.get(col)), None)
            if x and y and x.get("ok", "1") == "1":
                d = row_difference(x, y, cols)
            if d:
                bad += 1
                first = first or f"{'/'.join(sub)} {k}: {d}"
    return n, bad, first


BILL_FORMS = ("bill_lower", "bill_upper", "bill_exact")
BILL_TOL = 1e-9          # --bill: the composed bill equals the measured one (float sums of the same units)


def bill_by_run(rows, keyf):
    """{run key: {form: the run's bill}}: compose.bill_units on each part row (sized: the exact form too), summed per run"""
    from kvcost.layout_cost import compose as C
    from kvcost.layout_cost import cost_models as CM
    acc = {}
    for r in rows:
        t = acc.setdefault(keyf(r), dict.fromkeys(C.BILL, 0.0))
        for k, x in C.bill_units(r, {c: float(r.get(c) or 0) for c in C.CHANNELS}, sized=True).items():
            t[k] += x
    return {k: {f: getattr(CM, f)(t) for f in BILL_FORMS} for k, t in acc.items()}


def bill_check(grid, out, cell, table):
    """the bill of a composed cell's runs against its measured runs' (both from their part rows), into `table`:
    {form: {"runs", "worst", "where", "cells", "queries"}}; -> the runs compared"""
    run_key = lambda r: (r["query"], r["body"], r["param_idx"])
    write_key = lambda r: (None, r["query"], r["param_idx"])  # (a write run: no body)
    files = [(capture(grid, s), capture(out, s), run_key) for s in READ_SUBS[0]]
    if WITH_WRITES[0]:
        files.append((writes(grid), writes(out), write_key))
    n = 0
    for m_cap, c_cap, keyf in files:
        m, c = m_cap.path_of("parts", cell), c_cap.path_of("parts", cell)
        if not (os.path.exists(m) and os.path.exists(c)):
            continue
        is_write = keyf is write_key
        bm, bc = bill_by_run(rows_of(m, cell, is_write)[1], keyf), bill_by_run(rows_of(c, cell, is_write)[1], keyf)
        for k in sorted(set(bm) & set(bc)):
            n += 1
            for f in BILL_FORMS:
                x, y = bm[k][f], bc[k][f]
                d = abs(y - x) / x if x else abs(y - x)
                kind = "read runs" if keyf is run_key else "write runs"
                e = table.setdefault((f, kind), {"runs": 0, "worst": 0.0, "where": "", "cells": set(), "queries": set()})
                e["runs"] += 1
                e["cells"].add(cell)
                e["queries"].add(k[0] if keyf is run_key else k[1])
                if d > e["worst"]:
                    e["worst"], e["where"] = d, f"{cell} {k}: measured {x:.6g}, composed {y:.6g}"
    return n


def print_bill_table(table):
    print("the bill, composed against measured (per run, summed over its part rows):")
    print("  form        kind         runs  cells  queries  largest relative difference")
    for f in BILL_FORMS:
        for kind in ("read runs", "write runs"):
            e = table.get((f, kind))
            if not e:
                print(f"  {f:<10}  {kind:<10}  0 runs compared")
                continue
            print(f"  {f:<10}  {kind:<10} {e['runs']:>5}  {len(e['cells']):>5}  {len(e['queries']):>7}  {e['worst']:.3g}"
                  + (f" ({e['where']})" if e["worst"] else ""))


def parse_cells(text):
    """--cells: a comma-separated list of uniform layouts (cell ids at any chunk size, or short names: Sp-Ch4k); None =
    the default (every pair of the settings whose diagonal --grid measured; with --whole-grid, the nine)"""
    if text is None:
        return None
    from kvevaluator import layouts
    want = []
    for c in (c.strip() for c in text.split(",") if c.strip()):
        try:
            cell = layouts.cell_of(c)
        except SystemExit as e:
            sys.exit(f"--cells: {e}")
        if cell not in want:
            want.append(cell)
    if not want:
        sys.exit(f"--cells: no layout in {text!r}")
    return want


def measured_settings(grid, subs):
    """the settings (split, condensed, chunked, chunked_c<K>k) whose diagonal --grid holds a measured run record of in
    its first read capture, in the diagonals' order (kvevaluator.layouts.DIAGONALS)"""
    from kvevaluator import layouts
    return [s for s in layouts.DIAGONALS if any(os.path.exists(capture(grid, x).record(cell_of(s, s))) for x in subs[:1])]


def every_pair(settings_):
    """every uniform layout of the settings: props outer, adjacency inner, in the settings' order"""
    return [cell_of(p, ad) for p in settings_ for ad in settings_]


PROVENANCE = "sources.json"     # what a caller records it composed from: provenance, never a reuse key


def tree_files(root):
    """{relative path: sha256} of every file under root, the top-level provenance file aside; a run record's sha leaves
    out its `wall_s` (a time, not a result: a layout composed again to the same bytes is kept, with its first time)"""
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            rel = os.path.relpath(os.path.join(dp, f), root)
            if rel != PROVENANCE:
                with open(os.path.join(dp, f), "rb") as fh:
                    data = fh.read()
                if f.endswith(".json") and b'"wall_s"' in data:
                    rec = json.loads(data)
                    rec.pop("wall_s", None)
                    data = json.dumps(rec, sort_keys=True).encode()
                out[rel] = hashlib.sha256(data).hexdigest()
    return out


def install(tmp, dst, whole=True):
    """Put the freshly composed capture `tmp` at `dst`, file by file. A composed capture is always composed again, never
    reused by a key: its value depends on our own code, which no key on its sources covers. A file byte-identical to
    dst's is kept as it is (tree_files' rule), a new one is added, one that differs replaces it; `whole` (every layout
    of the query sets composed, not --cells): a file dst holds in a query set this compose wrote and the compose no
    longer writes goes too. Whatever is replaced or no longer written is moved aside, beside dst, never deleted; dst's
    other query sets, and with --cells its other layouts, stay. Returns (verdict, aside or None): "new" (dst did not
    exist), "kept" (nothing to change), "added" (new files only), "replaced" (files moved aside)"""
    if not os.path.exists(dst):
        os.rename(tmp, dst)
        return "new", None
    old, new = tree_files(dst), tree_files(tmp)
    wrote = {rel.split(os.sep)[0] for rel in new}            # the query sets this compose wrote
    gone = [rel for rel in old if whole and rel not in new and rel.split(os.sep)[0] in wrote]
    from datetime import datetime, timezone
    base = aside = f"{dst}.aside-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    n = 1
    while os.path.exists(aside):      # an aside of the same second: its own dir, never merged into (nor over) it
        aside, n = f"{base}-{n}", n + 1

    def move_aside(rel):
        os.makedirs(os.path.dirname(os.path.join(aside, rel)), exist_ok=True)
        os.rename(os.path.join(dst, rel), os.path.join(aside, rel))
    moved = added = 0
    for rel in sorted(gone):
        move_aside(rel)
        moved += 1
    for rel, sha in sorted(new.items()):
        if old.get(rel) == sha:
            continue
        if rel in old:
            move_aside(rel)
            moved += 1
        os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
        os.rename(os.path.join(tmp, rel), os.path.join(dst, rel))
        added += 1
    shutil.rmtree(tmp)
    return ("replaced", aside) if moved else ("added", None) if added else ("kept", None)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--grid", required=True, help="the measured <dataset>/<scale> dir holding the diagonal cells")
    ap.add_argument("--out", required=True, help="the composed <dataset>/<scale> dir (must not hold a capture)")
    ap.add_argument("--whole-grid", action="store_true", help="--out is a grid read on its own: the diagonals copied in, "
                    "every other layout composed (see the docstring)")
    ap.add_argument("--chunked-from", nargs=2, metavar=("ROOT", "CELL"), default=None)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--bill", action="store_true", help="with --check: also the bill's three forms per run")
    ap.add_argument("--dataset", default="ldbc", choices=("ldbc", "linkbench", "imdb"))
    ap.add_argument("--families", dest="workloads", default=None, help="the dataset's workloads composed, a comma list (default its own, "
                    "the optional ones aside: LDBC's BI is composed alone, --families bi)")
    ap.add_argument("--cells", default=None, help="comma-separated target layouts at any chunk size (cell ids or short "
                    "names, Sp-Ch4k); with --replace, the composed place keeps its other layouts (default: every pair "
                    "of the settings whose diagonal --grid measured; with --whole-grid, the nine)")
    ap.add_argument("--param-set", default=None, help="the param set whose captures are composed (LinkBench: anchors, "
                    "A, B, all); default the suite's first")
    ap.add_argument("--control", choices=("swap-sources",), default=None,
                    help="a negative control: compose with the two sources swapped (--check must then differ)")
    ap.add_argument("--jobs", type=int, default=None,
                    help="layouts composed at once, a process each (default: the machine's cores, and no more than "
                         "the available memory over this process's size once it holds the sources; 1: one after "
                         "another). The files do not depend on it")
    ap.add_argument("--replace", action="store_true",
                    help="--out may hold a composed capture: a file byte-identical to the new one is kept, a new one "
                         "added, a changed one moved aside")
    a = ap.parse_args()
    check_form(a)
    if not a.replace:
        return compose(a)
    dst, a.out = a.out, f"{a.out.rstrip(os.sep)}.composing-{os.getpid()}"
    rc = compose(a)
    if rc != 0:
        if os.path.isdir(a.out):
            shutil.rmtree(a.out)            # this run's own output, rebuilt in seconds
        return rc
    verdict, aside = install(a.out, dst, whole=not a.cells)
    print({"new": f"installed at {dst}", "kept": f"byte-identical to {dst}: kept",
           "added": f"added to {dst}: new files only, the files there byte-identical and kept",
           "replaced": f"{dst} differed: its files that changed or are no longer composed moved aside to {aside}, the "
                       f"new ones installed"}[verdict])
    return 0


_RUN = {}       # compose()'s arguments, diagonals and read captures: what _compose_cell reads (set before a fork)


def _read_sources(src, subs):
    """read a diagonal's files into SOURCES: what every layout with that side reads"""
    paths = [(src.path(s, k), None) for s in subs for k in ("parts", "counts")]
    if WITH_WRITES[0] and os.path.exists(src.writes_path("parts")):
        paths += [(src.writes_path(k), src.cell) for k in ("parts", "counts")]
    for path, cell in paths:
        source(path, cell)
        source(path, sha=True)


def _compared(cell, out):
    """what the call says of a composed layout against its measured files (--check, --bill), its counts into `out`"""
    a = _RUN["a"]
    if not a.check:
        return ""
    from kvevaluator import layouts
    if a.chunked_from and "chunked" in layouts.cell_settings(cell):
        return "; not compared: its Chunked parts are --chunked-from's, its measured file the grid's own chunk size"
    wn = out["wn"]
    n, d, first = check(a.grid, a.out, cell, wn)
    out["bad"], out["compared"] = d, n
    if a.bill:
        out["bill_runs"] = bill_check(a.grid, a.out, cell, out["bill"])
    if not n:
        return "; no measured files to compare with"
    return (f"; against its measured files: {n:,} rows compared, {d} differ ({wn[0]:,} of them write rows)"
            + (f" (first: {first})" if d else ""))


def _left_out(cell):
    """a layout measured itself wins over its composed cost: in each query set it was measured
    in, its composed files are left out (after --check compared them); readers find its measured ones
    (output_tree.layout_capture). A whole grid keeps it composed. -> what the call says of it"""
    a, subs = _RUN["a"], _RUN["subs"]
    own = [x for x in subs if os.path.exists(capture(a.grid, x).record(cell))]
    if not own or a.control is not None or a.whole_grid:
        return ""
    n = drop_cell(cell, a.out, own, a.grid)
    return f"; measured itself in {', '.join(label(x) or 'reported' for x in own)}: its composed files left out ({n})"


def _compose_cell(cell):
    """one off-diagonal layout composed into the call's --out, compared with its measured files (_compared) and left
    out where it was measured itself (_left_out): {problems, lines (what the call prints of it), compared, bad, wn,
    bill_runs, bill, noted (a stale note was made meanwhile)}. In a forked process of compose() or in its own"""
    from kvevaluator import layouts
    a, diag, subs = _RUN["a"], _RUN["diag"], _RUN["subs"]
    notes = len(stale.notes())
    out = {"problems": [], "bad": 0, "compared": 0, "wn": [0, 0], "bill_runs": 0, "bill": {}}
    p, ad = layouts.cell_settings(cell)
    sp, sa = (diag[ad], diag[p]) if a.control == "swap-sources" else (diag[p], diag[ad])
    for s in subs:
        out["problems"] += compose_reads(sp, sa, cell, s, a.out)
    write_marks(cell, subs, a.out)
    if WITH_WRITES[0]:
        out["problems"] += [x for x in compose_writes(sp, sa, cell, a.out) if x != "no writes capture"]
    out["lines"] = [f"{cell}: composed (props and index parts from {sp.cell}, adjacency parts from {sa.cell})"
                    + _compared(cell, out) + _left_out(cell)]
    for s in subs:
        m = MARKS.get((cell, s), {})
        weak = sorted(q for q, v in m.items() if v != "yes")
        out["lines"].append(f"  exact ({label(s) or 'reported'}): {len(m) - len(weak)} of {len(m)} queries yes"
                            + (f"; {MARKS[(cell, s)][weak[0]]}: {', '.join(weak)}" if weak else ""))
    out["noted"] = len(stale.notes()) > notes
    return out


def compose(a):
    """compose the cells `a` names into a.out (refused when it holds a capture), --jobs layouts at once (default
    run_measure.at_once: the cores, and no more processes than the available memory over this one's size once it
    holds the sources); the exit code"""
    DATASET[0] = a.dataset
    READ_SUBS[0], WITH_WRITES[0] = read_subs(a.dataset, a.workloads)
    from kvevaluator.output_tree import checked_param_set
    PARAM_SET[0] = checked_param_set(READ_SUBS[0][0][0], a.param_set)
    targets = parse_cells(a.cells)
    from kvevaluator import layouts
    if targets is None:          # the nine of a whole grid, else every pair of the diagonals --grid measured
        targets = every_pair(LAY) if a.whole_grid else every_pair(measured_settings(a.grid, READ_SUBS[0]))
    want = targets
    if any(os.path.exists(capture(a.out, s).dir("counts")) for s in READ_SUBS[0]):
        sys.exit(f"{a.out} holds a capture: composed files are never written over one")
    # the diagonal each target side reads from: the setting's own Chunked/Chunked at its chunk size
    used = [s for s in layouts.DIAGONALS if any(s in layouts.cell_settings(c) for c in want)]
    diag = {l: Source(a.grid, cell_of(l, l)) for l in used}
    if a.chunked_from:
        diag["chunked"] = Source(*a.chunked_from)
    ref = "split" if "split" in used else used[0]
    if a.bill and not a.check:
        sys.exit("--bill goes with --check (it compares the composed bill with the measured one)")
    problems, bad, compared = [], 0, 0
    bill_table, bill_runs, write_compared, write_files = {}, 0, 0, 0
    # every capture the reference source has (Split/Split without --cells) must be on each source, and one capture with it
    subs = [s for s in READ_SUBS[0] if os.path.exists(diag[ref].path(s, "counts", "json"))]
    extra = [(diag[l].cell, s) for l in used for s in READ_SUBS[0]
             if s not in subs and os.path.exists(diag[l].path(s, "counts", "json"))]
    if extra:                                            # a capture one source has and the reference lacks: not skipped silently
        sys.exit(f"{extra[0][0]} has a {label(extra[0][1]) or 'reported'} capture that {diag[ref].cell} lacks")
    if not subs:
        sys.exit(f"{diag[ref].cell}: no {a.dataset} capture under {diag[ref].root} (looked for "
                 f"{', '.join(diag[ref].path(s, 'counts', 'json') for s in READ_SUBS[0])})")
    for l in used:
        for s in subs:
            p = diag[l].path(s, "counts", "json")
            if not os.path.exists(p):
                sys.exit(f"{diag[l].cell}: no {label(s) or 'reported'} capture at {p} ({diag[ref].cell} has one)")
            r, g = json.load(open(p)), json.load(open(diag[ref].path(s, "counts", "json")))
            diff = [k for k in ("sf", "workload", "nparam") if r.get(k) != g.get(k)]
            diff += ["params"] if (r.get("slice") or {}).get("params") != (g.get("slice") or {}).get("params") else []
            ref_name = "the grid's Split/Split" if ref == "split" else diag[ref].cell
            if diff:   # another scale, workload or param rows: not the same runs
                sys.exit(f"{diag[l].cell} ({label(s) or 'reported'}) is not one capture with {ref_name}: {diff} differ")
            shape = [k for k in ("measurement_version", "inputs") if r.get(k) != g.get(k)]
            if shape:  # the same runs, captured in another shape or from moved inputs: composed, adopted or marked
                from kvevaluator import adoptions
                with open(diag[l].path(s, "counts"), "rb") as f:
                    what = hashlib.sha256(f.read()).hexdigest()
                detail = f"{diag[l].cell} ({label(s) or 'reported'}) and {ref_name} differ in {shape}: composed as they are"
                if shape == ["measurement_version"]:   # our own capture shape only (moved inputs can be identity)
                    adoptions.adopted_or_stale(
                        "capture", what, adoptions.key_sha({"measurement_version": r.get("measurement_version")}),
                        adoptions.key_sha({"measurement_version": g.get("measurement_version")}), "capture_shape_moved",
                        detail, {"cell": diag[l].cell, "sub": label(s) or "reported"})
                else:
                    stale.note("capture_shape_moved", detail)
    for l in used:                                       # a source file missing is refused by name, before any write
        need = [diag[l].path(s, k) for s in subs for k in ("counts", "parts")]
        if WITH_WRITES[0] and os.path.exists(diag[ref].writes_path("parts")):
            need += [diag[l].writes_path("parts"), diag[l].writes_path("counts")]
        missing = [p for p in need if not os.path.exists(p)]
        if missing:
            sys.exit(f"{diag[l].cell}: a source file is missing: {missing[0]}")
    for l in used if a.whole_grid else ():         # a whole grid: its diagonals copied beside the composed cells
        if cell_of(l, l) in want:
            print(f"{cell_of(l, l)}: copied as measured ({copy_cell(diag[l], cell_of(l, l), a.out)} files from {os.path.relpath(diag[l].root, EXP)})")
    cells = [c for c in want if len(set(layouts.cell_settings(c))) == 2]
    _RUN.update(a=a, diag=diag, subs=subs)
    SOURCES.clear()                                  # this call's sources: nothing an earlier call in the process read
    jobs, why = 1, "one after another" + (": --jobs 1" if a.jobs == 1 else "")
    if a.jobs != 1 and len(cells) > 1:
        for l in used:                               # read before the processes start: each then holds the sources
            _read_sources(diag[l], subs)
        jobs, why = at_once(a.jobs, 1, own_gb(), "a process", "this process's size is not known")
    print(f"composing {len(cells)} layouts, {why}")
    done = at_a_time(_compose_cell, cells, jobs, notes=False)
    if jobs > 1 and any(d["noted"] for d in done):
        # a stale note made while a layout is composed goes into every later layout's record (the process's notes, in
        # order): composed again one after another (this process holds none of the workers' notes), so the records
        # are the ones a call with --jobs 1 writes
        print("a stale note was made while composing: composed again, one after another")
        done = [_compose_cell(c) for c in cells]
    for d in done:
        problems += d["problems"]
        bad, compared, write_compared, write_files = bad + d["bad"], compared + d["compared"], write_compared + d["wn"][0], write_files + d["wn"][1]
        bill_runs += d["bill_runs"]
        for k, e in d["bill"].items():
            t = bill_table.setdefault(k, {"runs": 0, "worst": 0.0, "where": "", "cells": set(), "queries": set()})
            t["runs"] += e["runs"]
            t["cells"] |= e["cells"]
            t["queries"] |= e["queries"]
            if e["worst"] > t["worst"]:
                t["worst"], t["where"] = e["worst"], e["where"]
        print("\n".join(d["lines"]))
    for x in problems[:10]:
        print("PROBLEM", x)
    if a.bill:
        print_bill_table(bill_table)
        if bill_runs == 0:
            print("--bill compared 0 runs")
            return 1
        if any(e["worst"] > BILL_TOL for e in bill_table.values()):
            print(f"--bill: a form differs by more than {BILL_TOL:g} relative")
            bad += 1
    print(f"composed grid at {a.out}: {len(problems)} problems" + (f", {bad} rows differ from measured files" if a.check else ""))
    if a.check and write_files and write_compared == 0:
        print(f"--check compared 0 write rows in {write_files} measured writes files: a column or key mismatch")
        return 1
    if a.check and compared == 0:
        print("--check compared 0 rows: no measured off-diagonal cell under --grid"
              + (" that reads no Chunked part" if a.chunked_from else ""))
        return 1
    return 1 if problems or bad else 0


if __name__ == "__main__":
    sys.exit(main())
