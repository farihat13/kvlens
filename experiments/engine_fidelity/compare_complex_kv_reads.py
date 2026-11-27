#!/usr/bin/env python3
"""Compare a fresh run of the complex-kv-reads figure (`just results complex-kv-reads`, in the output dir) with the
paper's run (paper-results/, or the tree --against names), cell by cell, and print one verdict line. Both are read
through kvengines.complex_kv_reads (the same place in the two trees).

  counts   every live cell's S, G and N of the configurations the paper's figure reads
           (complex_kv_reads.figure_configurations; each query-text variant of theirs: IC1 at its claim params, IC3,
           hop1p7_knows at every param): they must be EQUAL. A cell the fresh run lacks fails. The other configurations
           (only the figure's options draw them; `just results complex-kv-reads-other-configurations`) are said, not
           checked: their cells equal, or the run lacks them. A configuration only the fresh run holds is named as new.
  bytes    the same cells' GET, SEEK and NEXT bytes. Bytes are checked only between two runs on the same load of a
           database (the two rows' data_created equal): each lane within BYTES_TOL. Across two loads they are said,
           not checked: a load numbers Dgraph's nodes anew, and its bytes move with the numbers (seen between two
           loads: up to 1.087% on one lane). A row that names no load counts as another load.
PASS: every count equal and every same-load byte lane within BYTES_TOL. Exit 1 otherwise, and when 0 cells were compared.

A time decides nothing (it varies with the machine and the run). After the verdict the fresh run's times are printed
as they are: per panel and arm (the live engine, the engine's plan on our evaluator, our cheapest plan), each
configuration's median in ms, fastest first, and on the live arm its share of that time in its KV store, or "not
measured on this CPU" where perf records no call stacks for the engine; then the run's CPU.

    python experiments/engine_fidelity/compare_complex_kv_reads.py [--dataset ldbc=0.3] [--from output] [--against <tree>]
"""
import argparse
import csv
import sys

from kvengines import complex_kv_reads as CKR
from kvevaluator import sizes
from kvevaluator.output_tree import Tree
from kvevaluator.paths import paper_results_dir

ENGINE_NAME = {"dgraph": "Dgraph", "nebula": "NebulaGraph", "arangodb": "ArangoDB", "tugraph": "TuGraph"}
BYTES_TOL = 0.01      # a byte lane's largest allowed relative gap between two runs on the same load
ARMS = (("live", "the live engine"), ("engine", "the engine's plan on our evaluator"), ("cheapest", "our cheapest plan on our evaluator"))
BYTES = ("get_bytes", "seek_bytes", "next_bytes")


def name(db):
    """a configuration as a reader sees it: its database, and the configuration's own name where the two differ"""
    e = CKR.engine(db)
    return ENGINE_NAME.get(e, e) + ("" if db == e else f" ({db})")


def count_cells(tree, sf):
    """{(workload, query, db, param): its live row and the load it was counted on} of a run: every engine
    configuration and query-text variant the run holds (kvengines.complex_kv_reads)"""
    out = {}
    for workload in sorted({w for w, _, _ in CKR.CELLS}):
        with open(CKR.counts_file(tree, sf, workload)) as f:
            for r in csv.DictReader(f):
                if r["status"] == "ok":
                    out[(workload, r["query"], r["db"], int(r["param_idx"]))] = {
                        **{k: int(float(r[k])) for k in "SGN"}, **{k: int(float(r[k] or 0)) for k in BYTES},
                        "load": r.get("data_created", "")}
    return out


def compare_counts(paper, fresh, say):
    """(cells, cells with equal S/G/N, then per kind of byte lane, "same load" and "two loads": (lanes, equal lanes,
    {db: largest relative difference})). A cell's bytes are on the same load when both rows name one load
    (data_created, equal and not empty); a row that names none counts as another load"""
    equal = 0
    lanes = {"same load": [0, 0, {}], "two loads": [0, 0, {}]}
    for key in sorted(paper, key=str):
        p, f = paper[key], fresh.get(key)
        same = f is not None and all(p[k] == f[k] for k in "SGN")
        equal += same
        if f is not None:
            kind = lanes["same load" if p["load"] and p["load"] == f["load"] else "two loads"]
            for k in BYTES:
                kind[0] += 1
                kind[1] += p[k] == f[k]
                if p[k] != f[k]:
                    kind[2][key[2]] = max(kind[2].get(key[2], 0.0), abs(f[k] - p[k]) / max(p[k], 1))
        if not same:
            say(f"  COUNTS DIFFER {key}: paper {({k: p[k] for k in 'SGN'})}, fresh "
                f"{'not in the fresh run' if f is None else {k: f[k] for k in 'SGN'}}")
    return len(paper), equal, lanes


def cpu_name():
    """this machine's CPU model (/proc/cpuinfo)"""
    try:
        with open("/proc/cpuinfo") as f:
            return next((l.split(":", 1)[1].strip() for l in f if l.startswith("model name")), "unknown CPU")
    except OSError:
        return "unknown CPU"


def time_summary(tree, sf, say):
    """the fresh run's times as they are: per panel and arm, each database's median, fastest first; live: its KV share"""
    d = CKR.times_dir(tree, sf)
    live, ev = CKR.cells(d / "live.csv", "live"), CKR.cells(d / "evaluator.csv", "evaluator")
    host = next(iter(live.values()))[0]["host"]
    say(f"times of the fresh run (median of the timed runs, ms; one engine CPU, {CKR.REGIME}; they decide nothing):")
    for panel, ((_, q, _), (_, src)) in enumerate(zip(CKR.CELLS, CKR.QUERIES), 1):
        for arm, what in ARMS:
            cells = live if arm == "live" else ev       # the evaluator's arms: the databases with a format on it
            got = {db: CKR.summary(cells[(db, q, src, arm)]) for db in CKR.DBS if (db, q, src, arm) in cells}
            parts = []
            for db in sorted(got, key=lambda x: got[x]["median_ms"]):
                t = got[db]
                share = "" if arm != "live" else (f", {100 * t['kv_ms'] / t['median_ms']:.0f}% in its KV store" if t["kv_ms"] is not None
                                                  else ", KV share not measured on this CPU")
                parts.append(f"{name(db)} {t['median_ms']:.2f}{share}")
            say(f"  Q{panel} ({q}), {what}: " + "; ".join(parts))
    say(f"  measured on {host}: {cpu_name()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dataset", type=sizes.resolve, default=sizes.resolve(CKR.SCALE),
                    help=f"the run's scale (default the paper's: {CKR.SCALE})")
    ap.add_argument("--from", dest="tree", default="output", help="the fresh run's tree: output (default) or a path")
    ap.add_argument("--against", default="paper-results", help="the tree of the run compared against: paper-results "
                    "(default: the paper's run) or a path (a converted copy of it, another output tree)")
    a = ap.parse_args()
    sf = sizes.scale(a.dataset, "ldbc")
    fresh_tree = Tree() if a.tree == "output" else Tree(a.tree)
    paper_tree = Tree(paper_results_dir() if a.against == "paper-results" else a.against)
    if not CKR.run_dir(paper_tree, sf).is_dir():
        sys.exit(f"compare complex-kv-reads: the paper's run is at {CKR.SCALE}; {a.against} holds none at ldbc={sf}: "
                 f"nothing to compare (0 cells)")
    for tree in (paper_tree, fresh_tree):
        CKR.check_shape(CKR.run_dir(tree, sf))
    say = print
    paper, fresh = count_cells(paper_tree, sf), count_cells(fresh_tree, sf)
    held = {k[2] for k in paper}
    shown = set(CKR.figure_configurations())
    for db in CKR.other_configurations():                   # said, not checked: the paper's figure does not read them
        cells = [k for k in paper if k[2] == db]
        if cells:
            have = [k for k in cells if k in fresh]
            same = sum(all(paper[k][c] == fresh[k][c] for c in "SGN") for k in have)
            say(f"not checked: {name(db)}, which only the figure's options draw: " + (
                f"S/G/N equal on {same} of {len(have)} cells" if have else
                f"not in the fresh run ({len(cells)} cells in the paper's; just results complex-kv-reads-other-configurations)"))
    paper = {k: v for k, v in paper.items() if k[2] in shown}
    for db in CKR.DBS:
        new = [k for k in fresh if k[2] == db and db not in held]
        if new:
            say(f"new: {name(db)}, which the paper's run does not hold: {len(new)} cells in the fresh run, not compared")
    n, eq, lanes = compare_counts(paper, fresh, say)
    say(f"counts: S/G/N equal on {eq} of {n} cells (every configuration the paper's figure reads, and its query variants)")
    gaps = lambda worst: ", ".join(f"{name(db)} {100 * w:.3f}%" for db, w in sorted(worst.items())) or "none"
    total, same_eq, worst = lanes["same load"]
    say(f"bytes, the two runs on the same load: equal on {same_eq} of {total} lanes; the largest gap per database: "
        f"{gaps(worst)} (allowed: {100 * BYTES_TOL:.0f}%)")
    total2, two_eq, worst2 = lanes["two loads"]
    say(f"bytes, the two runs on two loads (a load numbers Dgraph's nodes anew; said, not checked): equal on {two_eq} of "
        f"{total2} lanes; the largest gap per database: {gaps(worst2)}")
    gap = max(worst.values(), default=0.0)
    ok = n > 0 and eq == n and gap <= BYTES_TOL
    print(f"compare complex-kv-reads: S/G/N equal {eq}/{n}; bytes on the same load: {total} lanes within "
          f"{100 * BYTES_TOL:.0f}% (largest gap {100 * gap:.3f}%); on two loads: {total2} lanes, largest gap "
          f"{100 * max(worst2.values(), default=0.0):.3f}%, not checked: {'PASS' if ok else 'FAIL'}", flush=True)
    time_summary(fresh_tree, sf, say)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
