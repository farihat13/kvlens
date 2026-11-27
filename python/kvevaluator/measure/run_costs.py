#!/usr/bin/env python3
"""Recorded run times (owner: python/kvevaluator/measure/, neutral infra). Two modes:

- default: the LIVE run records (<output>/**/runs/*.json: `wall_s`) and store manifests (`ingest_wall_s`) of this
  machine, median (max) per kind of run, for a README's "Expected run time" table. A new machine starts empty.
- --paper-results: the run times recorded in paper-results/ (`just fetch-results`), one row per timed file, into the
  output tree's paper/run-times/ (run_times.csv and run_times_summary.csv; paper-results/ is read, never written).
  Opens no store, re-runs nothing. Checked by tests/gates/check_run_costs_paper_results.py.

    python3 -m kvevaluator.measure.run_costs                       # this machine's live records
    python3 -m kvevaluator.measure.run_costs --paper-results       # paper-results/: one row per timed file (CSV)
"""
import argparse
import collections
import csv
import glob
import json
import os
import re
import statistics
from kvevaluator.paths import paper_results_dir, refuse_results
from kvevaluator.output_tree import Tree



def human(s):
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.1f} min" if s < 7200 else f"{s / 3600:.1f} h"


def show(title, groups, label):
    print(f"\n## {title}")
    for key in sorted(groups, key=str):
        v = sorted(groups[key])
        print(f"  {label(key):60s} n={len(v):3d}  median {human(statistics.median(v)):>9s}  max {human(v[-1]):>9s}")


def add_cell_cost(grid, r):
    """a grid record's wall_s, as a whole cell's cost: left out when it took runs from a prior capture
    (grid_bodies.reused_record; the SF30 rule's reused_runs), as a reused kvstats run is"""
    if not ((r.get("reused_runs") or {}).get("runs") or (r.get("sf30_rule") or {}).get("reused_runs")):
        grid[(r["sf"], r["workload"], r.get("nparam"))].append(r["wall_s"])


def main():
    whole_runs, grid, ingest = collections.defaultdict(list), collections.defaultdict(list), collections.defaultdict(list)
    records = sorted(glob.glob(os.path.join(str(Tree().root), "captures", "**", "counts", "*.json"), recursive=True))
    for path in records:
        with open(path) as f:
            r = json.load(f)
        if "wall_s" not in r or r.get("status", "ok") != "ok" or r.get("composed_from"):
            continue                                      # a composed cell (compose_cells) carries its source's wall_s: not a run
        if "cell" in r and "workload" not in r:           # a writes record from before the fold: workload sits beside it
            sib = re.sub(r"\.json$", ".csv.meta.json", path)
            if not os.path.exists(sib):
                raise SystemExit(f"{path}: a run record with no workload and no {os.path.basename(sib)} beside it -- REFUSED")
            with open(sib) as f:
                r = {**json.load(f), **r}
        if "cell" in r:                                   # a meta grid cell (kvcost measure)
            add_cell_cost(grid, r)
        elif r.get("queries") == "suite" and not r.get("reused"):   # a whole run-kvstats suite, all measured
            whole_runs[(r["sf"], r["workload"], "+".join(r.get("metrics", [])), len(r.get("stores", [])))].append(r["wall_s"])
    from kvevaluator.measure.stores import cpp_stores_root
    for path in glob.glob(os.path.join(cpp_stores_root(), "**", "manifest.json"), recursive=True):
        if ".stale" in path:
            continue
        with open(path) as f:
            m = json.load(f)
        if m.get("ingest_wall_s") is None:
            continue
        rel = os.path.relpath(path, cpp_stores_root()).split(os.sep)
        scale = "/".join(rel[:-2])                          # the scale dir below the stores root: sf3, linkbench/n1m
        ingest[(scale, "meta cell" if rel[-2].startswith("meta-") else "engine format")].append(m["ingest_wall_s"])

    show("ingest, per store", ingest, lambda k: f"{k[0]} {k[1]}")
    show("run/kvstats.sh, whole suite, nothing reused", whole_runs, lambda k: f"sf{k[0]} {k[1]} {k[2]}, {k[3]} format(s)")
    show("kvcost measure, per cell", grid, lambda k: f"{k[0]} {k[1]}, nparam {k[2]}")


# ---------------------------------------------------------------------------------------------------------------------
# --paper-results: the run times recorded in paper-results/ (the paper's runs, `just fetch-results`), one row per timed
# file, into the output tree's paper/run-times/ (run_times.csv and run_times_summary.csv). Opens no store and re-runs nothing. A file's identity is its
# place in the tree (<kind>/<dataset>/<scale>/<the rest>, a study's tree under studies/<name>/); its time is its wall_s,
# or ingest_wall_s for a store's manifest. Only the run kinds count: a summary/ file (a composed cell carries its
# source's wall_s) is analysis, not a run.
RUN_KINDS = ("captures", "stats", "stores", "timings")
COLUMNS = ["kind", "dataset", "scale", "what", "store", "wall_s", "binary_sha", "binary_dirty", "host_cpu", "path"]


def provenance_of(d):
    """the binary's block of a timed file: `binary` (a run record) or `provenance` (a stats, store or timing meta)"""
    return d["binary"] if isinstance(d.get("binary"), dict) else d.get("provenance") or {}


def identity(rel):
    """(kind, dataset, scale, what, store) of a timed file from its path in the tree; None when not a run kind"""
    parts = rel.split("/")
    study = ""
    if parts[0] == "studies":
        study, parts = parts[1], parts[2:]
    if parts[0] not in RUN_KINDS or len(parts) < 4:
        return None
    kind, dataset, scale, *rest = parts
    metas = [p for p in rest[:-1] if p.startswith("meta-")]
    store = metas[-1][len("meta-"):] if metas else rest[-1].split(".")[0]
    what = "/".join(p for p in rest[:-1] if not p.startswith("meta-")) or "-"
    return kind, dataset, scale, f"{study}: {what}" if study else what, store


def paper_results_rows(root):
    """one row per timed file of the run kinds below root (wall_s, or a store manifest's ingest_wall_s)"""
    rows = []
    for path in sorted(glob.glob(os.path.join(root, "**", "*.json"), recursive=True)):
        rel = os.path.relpath(path, root)
        ident = identity(rel)
        if ident is None or ident[0] == "timings" and not rel.endswith(".meta.json"):
            continue                       # a timing's raw files (<query>/p<n>/*.json) are its runs' parts, not runs
        with open(path) as f:
            d = json.load(f)
        if not isinstance(d, dict) or "composed_from" in d or d.get("status", "ok") != "ok":
            continue
        wall = d.get("ingest_wall_s") if ident[0] == "stores" else d.get("wall_s")
        if wall is None:
            continue
        if not isinstance(wall, (int, float)):
            raise SystemExit(f"FAIL: {rel}: its time {wall!r} is not a number")
        prov = provenance_of(d)
        rows.append(dict(zip(COLUMNS, (*ident, round(float(wall), 1), str(prov.get("binary_git_sha", ""))[:9],
                                       prov.get("binary_dirty", ""), (prov.get("host") or {}).get("cpu", ""), rel))))
    return rows


def paper_results(root=None, out_dir=None):
    """--paper-results: write run_times.csv (one row per timed file) and run_times_summary.csv (per kind, dataset,
    scale and what: runs, median, max, total) from paper-results/ (root: another tree, a check's) into the output
    tree's paper/run-times/ (out_dir: another dir, a check's; refused under paper-results/); fail when the tree holds
    no timed file"""
    root = str(root or paper_results_dir())
    out_dir = refuse_results(out_dir) if out_dir else str(Tree().paper("run-times"))
    rows = paper_results_rows(root)
    if not rows:
        raise SystemExit(f"FAIL: no timed file under {root}: run `just fetch-results`")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "run_times.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["kind"], r["dataset"], r["scale"], r["what"])].append(r["wall_s"])
    with open(os.path.join(out_dir, "run_times_summary.csv"), "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["kind", "dataset", "scale", "what", "runs", "median_wall_s", "max_wall_s", "total_wall_s"])
        for key in sorted(groups):
            v = sorted(groups[key])
            w.writerow([*key, len(v), round(statistics.median(v), 1), round(v[-1], 1), round(sum(v), 1)])
    by_kind = collections.Counter(r["kind"] for r in rows)
    print(f"wrote {out_dir}/run_times.csv ({len(rows)} rows: " + ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items()))
          + f") and run_times_summary.csv ({len(groups)} groups)")
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--paper-results", action="store_true",
                    help="read paper-results/ only; write run_times.csv and run_times_summary.csv into the output "
                         "tree's paper/run-times/")
    args = ap.parse_args()
    paper_results() if args.paper_results else main()
