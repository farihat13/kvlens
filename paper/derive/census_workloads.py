#!/usr/bin/env python3
"""Derive census-workloads' tables: the elements each workload's plans touch, by part (adjlist, vertex props, index;
read and written), and the KV items and ops Sp-Sp, Co-Co and Ch64k-Ch64k read and write for the same plans.

Method: experiments/logical_census/README.md, "The algorithm". A KV item is an op that lands on an item:
(S - S_miss) + (G - G_miss) + (N - N_miss) read, P + D written. Split stores one KV item per element, so Sp-Sp's items
must equal the elements (checked per workload within 0.1% at the paper's scale, LDBC SF3; said, not refused, at another). JOB takes Sp-Sp's cheapest ok body (least
wops_bytes4k; ties on the first); LinkBench sums its request log per operation.

Reads, in the tree --from names at the run's scales (--dataset): the logical census of LDBC (summary/ldbc/<scale>/
logical-census/: experiments/logical_census/run-logical-census.sh), each workload's captures on Sp-Sp, Co-Co and
Ch64k-Ch64k (LDBC Interactive and its writes, BI, JOB, LinkBench's log), and I01's cost on the 49 (kvcost
cost-layouts). A dataset the run leaves out (JOB, LinkBench) has no rows and no keys.

Writes <tables>/census-workloads.csv (per kind and query, LDBC Interactive and BI), census-workloads-all.csv
(per query of every workload, with its frequency and equal share), census-workloads-ops.csv (per query, Sp-Sp's and
Co-Co's KV ops by kind), census-workloads-wops-bytes.csv (each LDBC Interactive query's wops and bytes by part at each
of the 7 settings; the parts checked to sum to the total), and <tables>/census_workloads.tex.

    uv run python paper/derive/census_workloads.py [--from paper-results|output|<tree>] [--dataset ldbc=3,...] [--tables DIR]
"""
import argparse
import csv
import importlib.util
import math
from collections import defaultdict

from pathlib import Path

from snapshots import add_tree_args, scale_in, tree_of, write_numbers, write_table
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost import compose
from kvcost.layout_cost import layout_costs as LC
from kvcost.layout_cost import query_weighting as QW
from kvevaluator import sizes
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture, capture_in, writes_in
from kvevaluator.paths import ROOT
from kvevaluator.layouts import DIAGONALS, cell_id

_spec = importlib.util.spec_from_file_location("uniform_49_layouts_workloads",
                                               ROOT / "paper/derive/uniform_49_layouts_workloads.py")
U49 = importlib.util.module_from_spec(_spec)      # its ldbc-sf3-interactive workload and loader: the 49's own inputs
_spec.loader.exec_module(U49)

# the places read, set by places() from --from and --dataset: LDBC's captures, BI's capture, the logical census, JOB's
# capture and LinkBench's captures (None: the run leaves the dataset out), the tree and the run (for I01's costed 49),
# and where the tables go
GRID = BI_COUNTS = CENSUS = JOB = LINKBENCH = TREE = RUN = TABLES = None


def places(a):
    """set the places from the derive's arguments (snapshots.add_tree_args)"""
    global GRID, BI_COUNTS, CENSUS, JOB, LINKBENCH, TREE, RUN, TABLES
    TREE, RUN, TABLES = tree_of(a.tree), a.run, a.tables
    ldbc, imdb, linkbench = (scale_in(a, ds) for ds in ("ldbc", "imdb", "linkbench"))
    if ldbc is None:
        raise SystemExit("census-touched reads LDBC's census: the run (--dataset) leaves ldbc out")
    GRID = TREE.place_dir("captures", "ldbc", ldbc)
    BI_COUNTS = Path(capture_in(GRID, "bi").path)
    CENSUS = Path(TREE.summary("ldbc", ldbc, "logical-census"))
    JOB = Path(capture_in(TREE.place_dir("captures", "imdb", imdb), "job").path) if imdb else None
    LINKBENCH = TREE.place_dir("captures", "linkbench", linkbench) if linkbench else None
    for ds, there in (("imdb", JOB), ("linkbench", LINKBENCH)):
        if there is None:
            print(f"{ds}: not in this run (--dataset): its rows and keys are left out")


# each family's weights: (frequency, equal share), each a workload id; None: the column's rule in the docstring
WEIGHTS = {"ldbc": ("i01", "i01"), "bi": ("b01", "b01"), "job": ("j01", "j01"), "linkbench": (None, None)}
ALL_COLUMNS = ("family", "query", "body", "requests") + ("adj_r", "adj_w", "v_r", "v_w", "ix_r", "ix_w") + \
    ("frequency", "equal_share")
SEGMENTS = ("adj_r", "adj_w", "v_r", "v_w", "ix_r", "ix_w")
SPLIT = "props_split_adj_split"
CELLS = ("props_condensed_adj_condensed", "props_chunked_adj_chunked")
READ_OF = {"adj": "adj_r", "props": "v_r", "index": "ix_r"}
WRITE_OF = {"adj": "adj_w", "props": "v_w", "index": "ix_w"}


def f(r, k):
    return float(r.get(k) or 0)          # a read capture has no P / D columns


def zero():
    return dict.fromkeys(SEGMENTS, 0.0)


def family(suite):
    return "bi" if suite == "bi" else "ldbc"        # LDBC Interactive = its reads (IC, IS) and its updates (IU)


def elements():
    """{(family, query, body): elements by segment}, one run of the census' chosen body"""
    out = {}
    for r in capture_layout.reader(open(CENSUS / "census.csv")):
        if r["chosen"] == "1":
            e = zero()
            # a direct edge probe reads an edge item too: drawn with the adjlist reads (only IS7 probes, 4.8 per run:
            # 0.00004% of Interactive's elements once, 0.5% every query equal; none in BI)
            e.update(adj_r=f(r, "edges_list") + f(r, "edges_probe"), adj_w=f(r, "written_edge_entries"),
                     v_r=f(r, "vertex_props"), v_w=f(r, "written_vertex_props"), ix_r=f(r, "index_entries"),
                     ix_w=f(r, "written_index_entries"))
            out[(family(r["suite"]), r["query"], r["body"])] = e
    return out


def layout_items(cell, el):
    """{(family, query, body): KV items by segment} on one layout: the census' body, the mean over the census' params"""
    params = defaultdict(set)
    for r in capture_layout.reader(open(CENSUS / "census_steps.csv")):
        params[(family(r["suite"]), r["query"], r["body"])].add(r["param_idx"])
    acc, seen = defaultdict(zero), defaultdict(set)
    sources = (("ldbc", capture_in(GRID, "interactive").path_of("parts", cell), True),
               ("bi", Capture(BI_COUNTS).path_of("parts", cell), True),
               ("ldbc", writes_in(GRID, "ldbc").path_of("parts", cell), False))
    for fam, path, is_read in sources:
        for r in (capture_layout.reader(open(path)) if is_read else capture_layout.write_rows(path, cell=cell)[2]):
            key = (fam, r["query"], r["body"] if is_read else r["query"])
            if key not in el or (is_read and r["param_idx"] not in params[key]):
                continue
            part = r["part"].split(":")[0]
            if part not in READ_OF:
                raise SystemExit(f"{cell}: part {r['part']!r} has no place")
            acc[key][READ_OF[part]] += f(r, "S") - f(r, "S_miss") + f(r, "G") - f(r, "G_miss") + f(r, "N") - f(r, "N_miss")
            acc[key][WRITE_OF[part]] += f(r, "P") + f(r, "D")
            seen[key].add(r["param_idx"])
    missing = [k for k in el if k not in acc]
    if missing:
        raise SystemExit(f"{cell}: no part rows for {missing[:5]}")
    return {k: {s: v / len(seen[k]) for s, v in acc[k].items()} for k in el}


def shares(rows, el, fam):
    """every query once: the rows' counts summed over the family's queries, over the elements' sum"""
    keys = [k for k in el if k[0] == fam]
    n = sum(sum(el[k].values()) for k in keys)
    return {s: sum(rows[k][s] for k in keys) / n for s in zero()}


def items(rows):
    """{(query, body, param_idx): KV items by segment} of parts rows (a read capture's, or write_rows'), in the rows' order"""
    out = defaultdict(zero)
    for r in rows:
        kind = r["part"].split(":")[0]
        if kind not in READ_OF:
            raise SystemExit(f"part {r['part']!r} has no segment")
        seg = out[(r["query"], r["body"] if "body" in r else r["query"], r["param_idx"])]
        seg[READ_OF[kind]] += f(r, "S") - f(r, "S_miss") + f(r, "G") - f(r, "G_miss") + f(r, "N") - f(r, "N_miss")
        seg[WRITE_OF[kind]] += f(r, "P") + f(r, "D")
    return out


def job_rows():
    """JOB: Sp-Sp's cheapest body per query, its elements"""
    best = {}
    runs = [r for r in capture_layout.reader(open(JOB / "counts" / f"{SPLIT}.csv")) if r["ok"] == "1"]
    for r in sorted(runs, key=lambda r: (r["query"], r["body"])):
        cost = f(r, "G") + 1.1 * f(r, "S") + 0.01 * f(r, "N") + f(r, "read_bytes") / 4096
        if r["query"] not in best or cost < best[r["query"]][1]:
            best[r["query"]] = (r["body"], cost)
    chosen = {(q, b) for q, (b, _) in best.items()}
    el = items(r for r in capture_layout.reader(open(JOB / "parts" / f"{SPLIT}.csv")) if (r["query"], r["body"]) in chosen)
    if {(q, b) for q, b, _ in el} != chosen:
        raise SystemExit(f"JOB: {len(chosen)} chosen bodies, part rows for {len({(q, b) for q, b, _ in el})}")
    return [{"family": "job", "query": q, "body": b, "requests": 1, **{s: round(v, 3) for s, v in seg.items()}}
            for (q, b, _), seg in sorted(el.items())]


def linkbench_rows():
    """LinkBench: every request of the log, summed per operation"""
    agg, requests = defaultdict(zero), defaultdict(set)
    for part in ("linkbench", "linkbench_writes"):
        path = LINKBENCH / part / "log" / "parts" / f"{SPLIT}.csv"
        for (q, _, p), seg in items(capture_layout.reader(open(path))).items():
            op = q.split("_")[0]
            for s, v in seg.items():
                agg[op][s] += v
            requests[op].add((p, part, q))
    print(f"linkbench: {len(agg)} operations, {sum(len(v) for v in requests.values())} requests")
    return [{"family": "linkbench", "query": op, "body": "", "requests": len(requests[op]),
             **{s: round(v, 3) for s, v in agg[op].items()}} for op in sorted(agg)]


# census-workloads-ops: per query, the KV operations each kind costs on Sp-Sp (items: Split keeps one item per KV pair) and
# on Co-Co (owners: Condensed keeps one owner per KV pair), measured, misses included
OPS_CELLS = (("sp", SPLIT), ("co", "props_condensed_adj_condensed"))
OPS_KINDS = ("adj", "adj_ep", "ep", "props", "index", "written")


def ops_sources():
    """{family: [(capture dir, keep a query)]} of the run's datasets; LDBC's updates are its IU1-8 (the writes grid has more)"""
    out = {"ldbc": [(GRID / "interactive/reported", None), (GRID / "writes/reported", lambda q: q.startswith("iu"))],
           "bi": [(BI_COUNTS, None)]}
    if JOB is not None:
        out["job"] = [(JOB, None)]
    if LINKBENCH is not None:
        out["linkbench"] = [(LINKBENCH / "linkbench/log", None), (LINKBENCH / "linkbench_writes/log", None)]
    return out


def edge_prop_steps():
    """{(plan file, body, step name): the step asks for edge properties}, from the plans: LDBC's and BI's bodies by the
    census' step rows (they name the bodies as the captures do), IU1-8, JOB and LinkBench from their plan files"""
    import json
    from pathlib import Path
    from kvevaluator.plan_choice import candidates as C
    out = {(r["query"], r["body"], r["step"]): float(r["n_edge_props"]) > 0
           for r in capture_layout.reader(open(CENSUS / "census_steps.csv"))}
    single = [Path(C.plans_dir("linkbench")), Path(C.PLANS_ROOT) / "writes" / "ldbc" / "plans"]
    for d in single:                                     # one body per file, named like the file
        for p in sorted(d.glob("*.json")):
            for s in json.load(open(p)).get("steps", []):
                out[(p.stem, p.stem, s["name"])] = bool(s.get("edge_props"))
    for p in sorted(Path(C.plans_dir("job")).glob("*.json")):  # a bundle of bodies per query
        for body, b in (json.load(open(p)).get("plans") or {}).items():
            for s in b.get("steps", []):
                out[(p.stem, body, s["name"])] = bool(s.get("edge_props"))
    return out


def capture_rows(fam, sources, want, cell):
    """(query key, "parts" or "steps", row) of one cell's captures for a family: the queries `want` names (LinkBench: an
    operation, the query's name before "_"), on the body it names (LinkBench: every body), the sources' keep filters"""
    for d, keep in sources:
        for kind_file in ("parts", "steps"):
            for r in capture_layout.reader(open(d / kind_file / f"{cell}.csv")):
                q = r["query"]
                key = q.split("_")[0] if fam == "linkbench" else q
                if (keep and not keep(q)) or (fam, key) not in want:
                    continue
                if fam != "linkbench" and r["body"] != want[(fam, key)]["body"]:
                    continue
                yield key, kind_file, r


def ops_rows(all_):
    """census-workloads-ops' rows: per (family, query) of the SF3 workloads (the body census-workloads-all names; LinkBench
    every request of the log, summed per operation), the KV ops of each kind on Sp-Sp and Co-Co: S + G + N on the part the
    capture labels (adj, props, index; an adjacency step that asks for edge props is adj_ep, a single-edge probe that asks
    for them ep), P + D written; the mean over the params (LinkBench: the sum)"""
    ep = edge_prop_steps()
    want = {(r["family"], r["query"]): r for r in all_ if r["family"] in ops_sources()}
    out = []
    for fam, sources in ops_sources().items():
        tot = {c: defaultdict(lambda: dict.fromkeys(OPS_KINDS, 0.0)) for c, _ in OPS_CELLS}
        params = defaultdict(set)
        for short, cell in OPS_CELLS:
            for key, kind_file, r in capture_rows(fam, sources, want, cell):
                q = r["query"]
                params[key].add((q, r["param_idx"]))
                t = tot[short][key]
                if kind_file == "steps":           # the writes: a part row has no P / D
                    t["written"] += f(r, "P") + f(r, "D")
                    continue
                ops = f(r, "S") + f(r, "G") + f(r, "N")
                part = r["part"].split(":")[0]
                if part != "adj":
                    t["props" if part == "props" else "index"] += ops
                    continue
                flag = ep.get((q, r["body"], r["step"]))
                if flag is None:
                    raise SystemExit(f"census-workloads-ops: {fam} {q} {r['body']} step {r['step']}: not in the plans")
                t[("ep" if r["op"] == "read_edge_exists" else "adj_ep") if flag else "adj"] += ops
        if {k for (fa, k) in want if fa == fam} != set(tot["sp"]):
            raise SystemExit(f"census-workloads-ops: {fam}: {len(want)} queries wanted, {len(tot['sp'])} measured")
        for (fa, key), r in sorted(want.items()):
            if fa != fam:
                continue
            n = 1 if fam == "linkbench" else len(params[key])     # the params it ran
            row = {"family": fam, "query": key, "body": r["body"], "requests": r["requests"],
                   "frequency": r["frequency"], "equal_share": r["equal_share"]}
            for short, _ in OPS_CELLS:
                row.update({f"{short}_{k}": round(v / n, 3) for k, v in tot[short][key].items()})
            out.append(row)
    return out


def weights():
    """{family: ({query: frequency}, {query: equal share})} from WEIGHTS: a workload's own frequencies, or its equal-share
    file where it defines none"""
    out = {}
    for fam, (own, share) in WEIGHTS.items():
        if own is None:
            out[fam] = (None, None)
            continue
        es = QW.equal_share_frequencies(share)
        how = QW.equal_share(share)["equal_share"]     # the workload's own frequencies at the file's dataset and scale
        freq = es if own in QW.EQUAL_SHARE_ONLY else \
            {q: fr for _, q, fr, _ in next(iter(benchmarks.select(how["dataset"], how["scale"], own).values()))["items"]}
        out[fam] = (freq, es)
    return out


def all_rows(el):
    """census-workloads-all's rows (the docstring's order and columns)"""
    w = weights()
    sf3 = [{"family": fam, "query": q, "body": b, "requests": 1, **seg} for (fam, q, b), seg in el.items()]
    out = []
    for r in sf3 + (job_rows() if JOB is not None else []) + (linkbench_rows() if LINKBENCH is not None else []):
        freq, es = w[r["family"]]
        out.append({**r, "frequency": freq[r["query"]] if freq else 1, "equal_share": es[r["query"]] if es else ""})
    return out


BIG_STEP = 100     # a step that does at least this many KV ops on one part (adjacency lists or vertex properties)


def big_step_shares(all_):
    """{(family, part): the share of a workload's KV ops on Co-Co (one owner per KV pair, so an op is about one owner,
    misses included) on adjacency lists (adj) or vertex properties (props) that fall in a plan step doing >= BIG_STEP
    ops on that part}, each SF3 workload as census-workloads-ops weighs it (Interactive and LinkBench: their own
    frequencies, BI and JOB: the equal share), the mean over the params (LinkBench: the log's sum); reads S + G + N,
    the read side of the writes included"""
    want = {(r["family"], r["query"]): r for r in all_ if r["family"] in ops_sources()}
    out = {}
    for fam, sources in ops_sources().items():
        step_ops, params = defaultdict(float), defaultdict(set)
        for key, kind_file, r in capture_rows(fam, sources, want, "props_condensed_adj_condensed"):
            params[key].add((r["query"], r["param_idx"]))
            if kind_file != "parts":            # a steps row: the writes' P + D, not reads
                continue
            part = r["part"].split(":")[0]
            if part in ("adj", "props"):
                step_ops[(key, r["query"], r["param_idx"], r["step"], part)] += f(r, "S") + f(r, "G") + f(r, "N")
        how = "frequency" if fam in ("ldbc", "linkbench") else "equal_share"
        tot, big = defaultdict(float), defaultdict(float)
        for (key, _, _, _, part), ops in step_ops.items():
            n = 1 if fam == "linkbench" else len(params[key])
            x = float(want[(fam, key)][how]) * ops / n
            tot[part] += x
            if ops >= BIG_STEP:
                big[part] += x
        out.update({(fam, part): big[part] / tot[part] for part in sorted(tot)})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add_tree_args(ap)
    places(ap.parse_args())
    el = elements()
    split = layout_items(SPLIT, el)
    for fam in ("ldbc", "bi"):                   # the elements are Sp-Sp's KV items
        tot = sum(shares(split, el, fam).values())
        print(f"{fam}: {sum(k[0] == fam for k in el)} queries; Sp-Sp items / elements {tot:.4f}")
        if abs(tot - 1) > 0.001:    # the paper's claim is at its scale: there it stops; at another scale it is said
            why = f"{fam}: Sp-Sp's items are not the elements within 0.1% ({tot:.4f}) at {GRID.name}"
            if GRID.name == TREE.place_dir("captures", "ldbc", sizes.scale(sizes.resolve(sizes.PAPER), "ldbc")).name:
                raise SystemExit(why)
            print(f"WARNING {why}: the claim is checked at the paper's scale, and only said here")
    kinds = {"elements": el, SPLIT: split, **{cell: layout_items(cell, el) for cell in CELLS}}
    rows = [{"kind": kind, "order": f"{i:03d}", "family": k[0], "query": k[1], "body": k[2], **data[k]}
            for kind, data in kinds.items() for i, k in enumerate(el)]
    write_table("census-workloads", ("kind", "order", "family", "query", "body") + SEGMENTS, rows, key=("kind", "order"),
                out_dir=TABLES)
    rows = all_rows(el)
    print(f"census-workloads-all: {len(rows)} rows, " +
          ", ".join(f"{fam} {sum(r['family'] == fam for r in rows)}" for fam in dict.fromkeys(r["family"] for r in rows)))
    write_table("census-workloads-all", ALL_COLUMNS, rows, key=None, out_dir=TABLES)
    big = big_step_shares(rows)
    for (fam, kind), s in big.items():
        print(f"{fam} {kind}: {100 * s:.2f}% of Co-Co's KV ops in steps of >= {BIG_STEP}")
    # the text says "at least": a share is cut down to a whole percent, never rounded up
    keys = {f"census.big-steps.{fam}.{kind}": f"{math.floor(100 * s)}" for (fam, kind), s in big.items()}
    # the text's "at least": the three workloads of graph traversals (LinkBench reads one owner a request: 0)
    if JOB is not None:
        keys["census.big-steps.ldbc-bi-job.min"] = \
            f"{math.floor(100 * min(s for (fam, _), s in big.items() if fam in ('ldbc', 'bi', 'job')))}"
    keys["census.big-steps.size"] = f"{BIG_STEP}"
    write_numbers("census_workloads", keys, f"Co-Co's parts captures of census-workloads-ops ({GRID.name}), each "
                  "workload at its weights there", out_dir=TABLES)
    ops = ops_rows(rows)
    print(f"census-workloads-ops: {len(ops)} rows, " +
          ", ".join(f"{fam} {sum(r['family'] == fam for r in ops)}" for fam in ops_sources()))
    write_table("census-workloads-ops", ("family", "query", "body", "requests", "frequency", "equal_share") +
                tuple(f"{c}_{k}" for c, _ in OPS_CELLS for k in OPS_KINDS), ops, key=None, out_dir=TABLES)
    rows = wops_bytes_rows()
    print(f"census-workloads-wops-bytes: {len(rows)} rows, {len({r['query'] for r in rows})} queries")
    write_table("census-workloads-wops-bytes", ("query", "frequency", "part", "setting", "wops", "bytes"), rows, key=None,
                out_dir=TABLES)


WB_SETTINGS = DIAGONALS


def wops_bytes_rows():
    """census-workloads-wops-bytes' rows: per LDBC Interactive query at SF3, part (adj, props, index, total) and setting,
    its wops and bytes for one run, in the workload's query order: read from kvcost cost-layouts Sp-Sp Sp-Co ... Ch256k-Ch256k's
    query_parts.csv (each part kind's measures; their currencies here) and query_costs.csv (the total)"""
    spec = [s for s in U49.workloads(TREE, RUN) if s[0] == "ldbc-sf3-interactive"][0]
    _name, dataset, sf, wid, _form, costed, *_ = spec
    (_, w), = benchmarks.select(dataset, sf, wid).items()
    parts = {}
    for r in LC.read_query_parts(costed):          # the half view: the combined model's
        v = compose.currencies({k: float(r[k]) for k in compose.MEASURES})
        parts[(r["query"], r["layout"], r["part"])] = [v["ops"], v["bytes"]]
    _, totals = LC.read_query_costs(costed)
    acc, order, freq = {}, [], {}
    for fam, q, often, _ in w["items"]:
        freq[q] = often
        order.append(q)
        for s in WB_SETTINGS:
            cell = cell_id(s, s)
            t = totals[(fam, q)][cell]
            acc[(q, "total", s)] = [float(t["wops"]), float(t["bytes"])]
            for kind in ("adj", "props", "index"):
                acc[(q, kind, s)] = parts[(q, cell, kind)]
    for q in order:                                  # the parts sum to the total, per query and setting
        for s in WB_SETTINGS:
            t = sum(acc.get((q, k, s), [0.0, 0.0])[0] for k in ("adj", "props", "index"))
            if abs(t - acc[(q, "total", s)][0]) > 1e-9 * max(1.0, t):
                raise SystemExit(f"census-workloads-wops-bytes: {q} {s}: the parts sum to {t}, the total is {acc[(q, 'total', s)][0]}")
    return [{"query": q, "frequency": freq[q], "part": kind, "setting": s, "wops": acc.get((q, kind, s), [0.0, 0.0])[0],
             "bytes": acc.get((q, kind, s), [0.0, 0.0])[1]}
            for q in order for kind in ("adj", "props", "index", "total") for s in WB_SETTINGS]


if __name__ == "__main__":
    main()
