"""kvcost's views: per (family, query) the picked body's mean cost over its params, from a measured or composed cell
(picks.choose_cell + the runs/ rows, as compose.load_reads) or a mix priced from parts (compose.query_pick).

One table shape for all three, so a measured, a composed and a priced view of one layout compare column by column.
"""
import csv
import hashlib
import json
import os
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from kvevaluator.paths import ROOT

EXP = str(ROOT)
from kvcost.layout_cost import cost_models as CM  # (the three reported models: one definition)
from kvevaluator.measure import grid_capture  # (choose_cell: THE pick of a cell)
from kvcost.layout_cost import picks  # (choose_cell: THE pick of a cell)
from kvcost.layout_cost import compose  # (read_cost / load_writes: the reported measures)
from kvcost.layout_cost import benchmarks
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import HELD_OUT, Capture, layout_capture, writes_layout_capture

COUNTS = ("S", "G", "N", "P", "D", "S_miss", "G_miss", "N_miss")
LANES = CM.READ_LANES + CM.WRITE_LANES
COLS = ("family", "query", "kind", "body", "nparam") + COUNTS + LANES + compose.MEASURES + CM.MODELS + ("pick_score", "exact")
# exact: yes = measured on the store, or composed from two sources that both ran it and agree;
# miss-key bytes approximate = priced from parts: every op and every hit byte exact, the miss keys' size taken from the
# cell the part came from (the miss-key rule of kvcost.layout_cost.composed_vs_measured, bounded 0.5x-2x); upper bound / unknown = composed with a body
# that ran on one source only (in-sample families: the pick's score can only rise; held-out: either way); no pick = the
# composed planner row failed on some param, or a LinkBench request lost its one body: such a query is refused, by name
MARKS = ("yes", "miss-key bytes approximate", "upper bound", "unknown", "no pick")


def _mean(vals, keys):
    """the mean over params, summed in param order (as compose.item_cost: the same float sums)"""
    idx = sorted(vals, key=int)
    return {k: sum(float(vals[i].get(k) or 0.0) for i in idx) / len(idx) for k in keys}


def pick_score(row, criterion):
    """the plan pick rule's own score of a row's mean vector (the 7 read lanes, no P / D): None when the rule is not linear"""
    from kvevaluator.plan_choice.candidates import READ_LANES
    from kvevaluator.plan_choice.optimizer.criteria import linear_score
    w = linear_score(criterion)
    if not w:
        return None
    return (w["S"] * row["S"] + w["G"] * row["G"] + w["N"] * row["N"]
            + w["total_bytes"] * sum(row[k] for k in READ_LANES))


def _finish(row, criterion):
    row.update(CM.of_measures(row))
    row["pick_score"] = "" if row["kind"] == "write" else pick_score(row, criterion)
    return row


def cell_rows(dataset, root, cell, criterion=None, spec_path=None, families=None, *, rule, param_set=None):
    """the view of one measured (or composed) cell: every family of the suite, every query the cell priced.
    SystemExit on a query with no pick, or one whose planner body did not run ok on every param. families: the
    adapter's (None: the suite's); rule: the PlanRule; param_set: the captures' param set (output_tree.params_dir)"""
    rows = []
    for fam, kind in sorted((dict(families) if families is not None else benchmarks.FAMILIES[dataset]).items()):
        if kind == "read":
            rdir = layout_capture(root, fam, cell, param_set).path     # measured itself, else composed
            if not os.path.exists(Capture(rdir).path_of("counts", cell)):
                raise SystemExit(f"{fam}: no capture of {cell} at {os.path.relpath(Capture(rdir).dir('counts'), EXP)}/")
            chosen = picks.choose_cell(rdir, cell, criterion, rule=rule, spec_path=spec_path)
            by = defaultdict(lambda: defaultdict(dict))         # query -> variant -> param -> run row
            reported = grid_capture.load_runs(rdir, cell)
            for r in reported:
                by[r["query"]][r["body"]][r["param_idx"]] = r
            # a capture is composed when any of its rows is, reported or held-out (the pick is made on the held-out
            # rows): every query then takes the composer's mark, never a plain "yes"
            composed = any((r.get("measured_as") or "") == "composed"
                           for r in list(reported) + (grid_capture.load_held_out(rdir, cell) or []))
            for q in sorted(by):
                c = chosen.get(q)
                if not c or not c.get("body"):
                    raise SystemExit(f"{fam} {q} on {cell}: no plan picked (every body rejected: "
                                     f"{(c or {}).get('rejected')}); a total would leave it out")
                params = set().union(*(set(v) for v in by[q].values()))
                pick_ok = {i for i, r in by[q].get(c["body"], {}).items() if r["ok"] == "1"}
                if pick_ok != params:
                    raise SystemExit(f"{fam} {q} on {cell}: the picked body ran ok on params {sorted(pick_ok, key=int)} "
                                     f"of {sorted(params, key=int)}: it would be scored on missing rows")
                runs = by[q][c["body"]]
                per = {i: dict(r, **compose.read_cost(r), P=0, D=0) for i, r in runs.items()}
                row = {"family": fam, "query": q, "kind": "read", "body": c["body"], "nparam": len(per)}
                row.update(_mean(per, COUNTS + CM.READ_LANES + ("read_ops", "read_bytes", "miss_key_bytes")))
                row.update(dict.fromkeys(CM.WRITE_LANES + ("write_bytes", "write_ops", "write_rmw_ops", "write_rmw_bytes",
                                                           "write_rmw_miss_key_bytes"), 0.0))
                row["exact"] = mark_of(rdir, cell, q) if composed else "yes"
                if row["exact"] == "no pick":
                    raise SystemExit(f"{fam} {q} on {cell}: the composer marks it 'no pick' (its planner row failed on "
                                     f"some param, or its one body was lost): a total would leave it out")
                rows.append(_finish(row, criterion))
        else:
            wcap = writes_layout_capture(root, dataset, cell, param_set)
            path = wcap.path_of("counts", cell)
            if not os.path.exists(path):
                raise SystemExit(f"{fam}: no writes capture of {cell} at {os.path.relpath(path, EXP)}")
            # the cell view is the capture as measured: its raw lanes below come from the same rows, unchanged
            meas = compose.load_writes(root, cell, "measured", dataset, param_set)
            # the raw counts and lanes from the per-part rows: each (query, param)'s parts summed, in file order
            raw = defaultdict(lambda: defaultdict(lambda: dict.fromkeys(COUNTS + LANES, 0.0)))
            for r in capture_layout.write_rows(wcap.path_of("parts", cell), cell=cell)[2]:
                t = raw[r["query"]][r["param_idx"]]
                for k in COUNTS + LANES:
                    t[k] += float(r[k] or 0)
            gaps = sorted((q, i) for q in meas for i in meas[q] if i not in raw.get(q, {}))
            if gaps:
                raise SystemExit(f"{fam} on {cell}: {len(gaps)} (query, param) rows of writes/{cell}.csv have no per-part "
                                 f"rows (e.g. {gaps[:3]}): their lanes would read 0")
            for q in sorted(meas):        # a write plan has one body: the view names it "write"
                per = {i: dict(raw[q][i], **meas[q][i]) for i in meas[q]}
                row = {"family": fam, "query": q, "kind": "write", "body": "write",
                       "nparam": len(per)}
                row.update(_mean(per, COUNTS + LANES + ("write_bytes", "write_ops", "write_rmw_ops", "write_rmw_bytes",
                                                        "write_rmw_miss_key_bytes")))
                row.update(dict.fromkeys(("read_ops", "read_bytes", "miss_key_bytes"), 0.0))
                row["exact"] = "yes"
                rows.append(_finish(row, criterion))
    return rows


_MARKS = {}


def mark_of(rdir, cell, q):
    """a composed query's mark: the weakest over its rows in the composer's exact/<cell>.csv, reported and held-out
    (compose_cells.py writes one row per runs row). No exact file under a composed capture: refused."""
    if (rdir, cell) not in _MARKS:
        got = {}
        held_out = Capture(os.path.join(os.path.dirname(os.path.normpath(rdir)), HELD_OUT))   # beside it, as composed
        for held, cap in ((False, Capture(rdir)), (True, held_out)):
            path = cap.path_of("exact", cell)
            if not os.path.exists(path):
                if held and not os.path.exists(cap.path_of("counts", cell)):
                    continue
                raise SystemExit(f"{os.path.relpath(path, EXP)}: a composed capture with no exact marks (the composer's output)")
            with open(path) as f:
                for r in capture_layout.reader(f):
                    if r["exact"] not in MARKS:
                        raise SystemExit(f"{path}: unknown mark {r['exact']!r}")
                    got[r["query"]] = max(got.get(r["query"], 0), MARKS.index(r["exact"]))
        _MARKS[(rdir, cell)] = got
    if q not in _MARKS[(rdir, cell)]:
        raise SystemExit(f"{os.path.relpath(rdir, EXP)}/exact/{cell}.csv: no mark for {q} (a composed query is never "
                         f"taken as exact by default)")
    return MARKS[_MARKS[(rdir, cell)][q]]


def mix_rows(dataset, data, assign, criterion=None, mode="option"):
    """the view of a mix priced from parts: compose.query_pick per (family, query) under `assign` (unit -> layout), in
    query_pick's `mode` ("store" prices a measured store as itself: the record's anchored, pinned row)"""
    rows = []
    for (fam, q) in sorted(data):
        d = data[(fam, q)]
        units = {u for per in d["cost"].values() for u in per if u is not None}
        best, tots, _ = compose.query_pick(d, compose.params_of(d, None), assign, units, mode)
        if best is None:
            raise SystemExit(f"{fam} {q}: no plan usable under this mix: a total would leave it out")
        vec = tots[best]
        kind = benchmarks.kind_of(fam)
        row = {"family": fam, "query": q, "kind": kind, "body": best, "nparam": len(compose.params_of(d, None))}
        row.update({k: vec.get(k, 0.0) for k in ("S", "G", "N", "P", "D") + LANES + compose.MEASURES})
        row.update(dict.fromkeys(("S_miss", "G_miss", "N_miss"), ""))      # parts carry no miss counts
        row["exact"] = "miss-key bytes approximate"
        rows.append(_finish(row, criterion))
    return rows


def totals(rows, weighted=None):
    """once each per family and over all: every query once, summed in row order; `weighted`:
    [(scope, measures dict, items)] from compose.workload_cost / compose.cost_mix (a workload's query frequencies) appended, each
    with the weakest mark of the (family, query) items it prices"""
    keys = COUNTS[:5] + LANES + compose.MEASURES + CM.MODELS
    out = []
    for scope in sorted({r["family"] for r in rows}) + ["all families"]:
        rs = [r for r in rows if scope in ("all families", r["family"])]
        t = {k: sum(float(r[k] or 0) for r in rs) for k in keys}
        worst = max((MARKS.index(r["exact"]) for r in rs if r["exact"] in MARKS), default=0)
        out.append(dict(t, scope=scope, weighting="once each", queries=len(rs), exact=MARKS[worst]))
    mark = {(r["family"], r["query"]): MARKS.index(r["exact"]) for r in rows}
    for scope, m, items in weighted or []:
        missing = sorted(k for k in items if k not in mark)
        if missing:
            raise SystemExit(f"{scope}: items {missing[:3]} have no row in this view: their mark is unknown")
        out.append(dict({k: m.get(k, "") for k in keys if k in compose.MEASURES}, **CM.of_measures(m), scope=scope,
                        weighting="query frequencies", queries=len(items),
                        exact=MARKS[max((mark[k] for k in items), default=0)]))
    return out


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def key_of(meta):
    """what decides a view's numbers: the run records' identity, every file read (by sha256), the mix's assignment, the
    spec's and the composer's sha256. A view is rewritten in place only when all of these are the same."""
    v = meta.get("view", {})
    return {"identity": meta.get("identity"), "files": meta.get("files"), "assignment": meta.get("assignment"),
            "spec_sha256": v.get("spec_sha256"), "composer_sha256": v.get("composer_sha256"),
            "historical": v.get("historical")}


def aside_path(path):
    """<path>.aside-<UTC time>, with a counter when that name is taken (two moves in one second)"""
    base = f"{path}.aside-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out, n = base, 1
    while os.path.exists(out):
        out, n = f"{base}-{n}", n + 1
    return out


def _fmt(v):
    return "" if v == "" or v is None else (repr(round(v, 6)) if isinstance(v, float) else str(v))


def write_view(out, rows, tots, meta, readme):
    """costs.csv, totals.csv, meta.json, README.md under `out`. A view whose inputs' identity changed is moved aside
    (never deleted) before the new one is written; the same identity is rewritten in place (byte-identical)."""
    mj = os.path.join(out, "meta.json")
    if os.path.exists(mj):
        old = json.load(open(mj))
        if key_of(old) != key_of(meta):
            aside = aside_path(out)
            shutil.move(out, aside)
            print(f"kvcost: the inputs changed since the view at {os.path.relpath(out, EXP)}: the old view moved to "
                  f"{os.path.relpath(aside, EXP)}")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "costs.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLS)
        for r in rows:
            w.writerow([_fmt(r.get(c, "")) for c in COLS])
    tcols = ("scope", "weighting", "queries") + COUNTS[:5] + LANES + compose.MEASURES + CM.MODELS + ("exact",)
    with open(os.path.join(out, "totals.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(tcols)
        for t in tots:
            w.writerow([_fmt(t.get(c, "")) for c in tcols])
    with open(mj, "w") as f:
        json.dump(meta, f, indent=1, sort_keys=True)
        f.write("\n")
    with open(os.path.join(out, "README.md"), "w") as f:
        f.write(readme)


def weighted_totals(ad, sf, data_fn, left_out):
    """every workload of the suite (W01-W15, I01, I02 for ldbc; LinkBench's and JOB's own) at its query frequencies: [(scope, its
    priced total, its queries)]; a missing workload file is said in left_out, not refused"""
    out = []
    if not ad.WEIGHTED:                         # a benchmark with no query frequencies (BI): once each only
        return out
    try:
        ws = benchmarks.workloads(ad.DATASET, sf)
    except FileNotFoundError as e:              # no tracked workload file at this scale (LinkBench: only n1m's are tracked)
        left_out.append(f"the weighted totals: their query frequencies {e.filename} are not here")
        return out
    for name, w in ws.items():
        m = data_fn(w)
        if m is None:
            raise SystemExit(f"{w['id']}: an item has no usable plan: the workload is not priced")
        out.append((f"{w['id']} ({name})", m, sorted({(fam, q) for fam, q, _often, _params in w["items"]})))
    return out


def cell_view(ad, root, cell, sf, out, *, rule, criterion=None, spec_path=None, param_set=None):
    """one captured cell's per-query costs, with their exact marks, and its totals (once each, and every workload of
    the suite at its frequencies), written as costs.csv and totals.csv into `out`: the pricing reference that
    check_golden_costs holds to the freeze's bytes. Returns (rows, totals)."""
    rows = cell_rows(ad.DATASET, root, cell, criterion, spec_path=spec_path, families=ad.FAMILIES, rule=rule,
                     param_set=param_set)
    body = {(r["family"], r["query"]): r["body"] for r in rows if r["kind"] == "read"}

    def costed(w):
        # compose.load_reads' per-param costs, at the body each per-query row picked (one pick for both tables)
        data = {}
        for fam, kind in dict(ad.FAMILIES).items():
            if kind == "write":
                data[fam] = compose.load_writes(root, cell, "measured")
                continue
            per = defaultdict(dict)
            for r in grid_capture.load_runs(layout_capture(root, fam, cell, param_set).path, cell):
                if body.get((fam, r["query"])) == r["body"]:
                    per[r["query"]][r["param_idx"]] = compose.read_cost(r)
            data[fam] = dict(per)
        return compose.cost_or_refuse(w, data, f"{root} {cell}")
    left_out = []
    tots = totals(rows, weighted_totals(ad, sf, costed, left_out))
    meta = {"view": {"workload": ad.view_name(), "scale": str(sf), "cell": cell, "capture_root": root,
                     "criterion": benchmarks.criterion_name(criterion)}, "left_out": left_out, "models": CM.FORMULAS}
    write_view(out, rows, tots, meta, f"> {cell}'s per-query costs and totals (views.cell_view).\n")
    return rows, tots
