#!/usr/bin/env python3
"""Numbers for the evaluator-fidelity paragraph: given an engine's OWN plan, does the evaluator do the engine's
KV work? A case is one (query, engine, param) of the LDBC engine-plan validation (validate.py --drafts --live);
only finished plans count (status=suite), draft plans are left out. Keys are fidelity.sf03.<name>:
  cases / queries / pairs / params   the size of the check (params = params per pair)
  fit                                cases whose S, G and N are EACH within 10% of live (validate.py's fit)
  fit.tol5 / fit.tol20               the same rule at 5% / 20% (the 10% is not tuned)
  outside / outside.dgraph-split     cases outside the 10% fit, and how many of them are Dgraph's split residual exactly
  pairs.fit                          pairs that fit at every param
  exact                              cases equal on S, G and N
  exact-or-split                     exact, or off by exactly the predicted Dgraph split residual
  answers / answers.differ           cases whose answer matches the live engine's / differs
  bound / bound.cases / bound.fail   cases where our optimal plan's weighted ops <= the engine's, out of
                                     the cases where both plans answer the same question (same_answer)
  extra.median / .max / .cases       engine / optimal plan weighted ops, over those cases with a non-empty answer
  draft.pairs                        pairs of the 19 x 3 grid outside the suite (a draft plan; not checked)
  miss / miss.pairs                  cases that neither fit nor are the split residual, and their pairs
  {seen,holdout}.{cases,fit,exact-or-split,answers}   the same split by params: seen = the 6 params the plans
                                     were written with, holdout = 6 params measured after the freeze
  unseen.{...}                       the holdout cases whose counts no committed capture held (holdout_visibility.csv)
  repeat.cells / repeat.same         seen cases measured on both loads (09-22, and the capture's) with the same query and
                                     params, and how many repeat S, G and N exactly
and fidelity.sf01.<name>: cases, queries, pairs, fit, exact, and the seen / holdout / unseen splits;
fidelity.writes.{cases,exact,named,residual}: the write side (experiments/engine_fidelity/write_fidelity.py's compare,
engines/ldbc/sf0.3/write-fidelity/write_fidelity.csv): the compared write cells, equal, equal after the named
differences (engine bookkeeping no format is charged), and unexplained. The
unprefixed fidelity.* keys are main.tex's old paragraph (legacy_keys).
Weighted ops use the bound column's currency: python/kvevaluator/weights.py DEFAULT, G:S:N = 100:110:1.

The table holds every row the keys count:
  build/data/engine-fidelity-numbers-reads.csv    one row per read case of the four validation files (source: ldbc
      sf0.3, ldbc sf0.1, microbench sf0.3, legacy 08-11), every column of its file, plus `visibility` (seen / unseen,
      from holdout_visibility.csv) and, for sf0.3, the previous load's live counts (prev_live_S, _G, _N, _status)

    uv run python paper/derive/engine_fidelity.py   -> build/numbers/engine_fidelity.tex and the table

--from output --dataset ldbc=0.3,ldbc=0.1 reads the validation files of those scales from the output dir instead (a
re-run: `just results engine-fidelity`; a scale the run leaves out is read from paper-results/, and said). Three
inputs are read from paper-results/ always, because a re-run does not make them: holdout_visibility.csv (which
counts a plan author could read before the plans froze: a record of history), the previous load's live counts and
the 08-11 run.
"""
import argparse
import csv
import statistics
import sys
from collections import defaultdict

import snapshots
from snapshots import (ENGINE_FIDELITY, ENGINE_FIDELITY_LEGACY, ENGINE_FIDELITY_MB, ENGINE_FIDELITY_PREV,
                       ENGINE_FIDELITY_SF01, write_numbers, write_table)

from kvengines.live_workloads import FIDELITY, FIDELITY_SCALES, WRITE_FIDELITY
from kvevaluator import weights

ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
snapshots.add_tree_args(ap)
a = ap.parse_args() if __name__ == "__main__" else ap.parse_args([])
if a.tables is not None:
    snapshots.TABLES_DIR = snapshots.NUMBERS_DIR = a.tables
# the scales whose validation files are read from --from: the run's, when --from is not the paper's own tree
RERUN = [] if a.tree == "paper-results" else [sc for sc in a.run.get("ldbc", []) if sc in FIDELITY_SCALES]
MAIN, SMALL = FIDELITY_SCALES


def validation_dir(scale, archive):
    """a scale's engine-fidelity run: in --from's tree when the run re-made it, else the paper's (`archive`)"""
    return snapshots.tree_of(a.tree).engines("ldbc", scale, FIDELITY) if scale in RERUN else archive


FIT_REL = 0.10   # validate.py's FIT_REL; the recount below must reproduce its fit column
GRID_PAIRS = 19 * 3   # IC1-IC12 + IS1-IS7 x three engines (IC13/IC14 out of scope)
VALIDATION = {"ldbc sf0.3": validation_dir(MAIN, ENGINE_FIDELITY) / "interactive/engine_validation.csv",
              "ldbc sf0.1": validation_dir(SMALL, ENGINE_FIDELITY_SF01) / "interactive/engine_validation.csv",
              "microbench sf0.3": validation_dir(MAIN, ENGINE_FIDELITY_MB) / "microbench/engine_validation.csv",
              "legacy 08-11": ENGINE_FIDELITY_LEGACY / "engine_validation.csv"}
# the write side's compare (write_fidelity.py, filed under the sweep's scale): the run's when the run re-made the check
# at any scale (the chain makes the compare every time), else the paper's
WRITES = ((snapshots.tree_of(a.tree) if RERUN else snapshots.PAPER).engines("ldbc", MAIN, WRITE_FIDELITY)
          / "write_fidelity.csv")
_READ = {}                # path -> its rows, read once: the keys and the tables see the same rows


def rows_of(path):
    """the rows of a snapshot CSV, read once"""
    if path not in _READ:
        with open(path, newline="") as fh:
            _READ[path] = list(csv.DictReader(fh))
    return _READ[path]


def num(r, k):
    return float(r[k] or 0)


def within(r, tol):
    """validate.py's fit rule at any tolerance: S, G and N each within tol of live, no absolute slack."""
    return all(abs(num(r, f"sim_{c}") - num(r, f"live_{c}")) <= tol * num(r, f"live_{c}") for c in "SGN")


def exact(r):
    return all(r[f"sim_{c}"] == r[f"live_{c}"] for c in "SGN")


def wops(r, side):
    return weights.weighted(num(r, f"{side}_S"), num(r, f"{side}_G"), num(r, f"{side}_N"), weights.DEFAULT)


def load(source):
    """(rows, the finished plans' rows, they by (query, db), how many fit) of a validation file (VALIDATION's key)"""
    rows = rows_of(VALIDATION[source])
    suite = [r for r in rows if r["status"] == "suite"]
    pairs = defaultdict(list)
    for r in suite:
        pairs[(r["query"], r["db"])].append(r)
    fit = sum(r["fit"] == "YES" for r in suite)
    if sum(within(r, FIT_REL) for r in suite) != fit:
        sys.exit(f"{source}: the 10% recount disagrees with the fit column")
    return rows, suite, pairs, fit


def split_keys(prefix, snapshot, suite):
    """seen / holdout / unseen: cases, fit, exact-or-split, answers (the unseen label: holdout_visibility.csv)."""
    vis = {(r["query"], r["db"], r["param_idx"]): r["visibility"]
           for r in rows_of(snapshot / "interactive/holdout_visibility.csv")}
    if any(r["split"] not in ("seen", "holdout") for r in suite):
        sys.exit(f"{snapshot.name}: a case is neither seen nor holdout (holdout-edited: the code changed since the freeze)")
    groups = {"seen": [r for r in suite if r["split"] == "seen"],
              "holdout": [r for r in suite if r["split"] == "holdout"]}
    groups["unseen"] = [r for r in groups["holdout"] if vis[(r["query"], r["db"], r["param_idx"])] == "unseen"]
    out = {}
    for name, rs in groups.items():
        out[f"{prefix}.{name}.cases"] = str(len(rs))
        out[f"{prefix}.{name}.fit"] = str(sum(r["fit"] == "YES" for r in rs))
        out[f"{prefix}.{name}.exact-or-split"] = str(sum(exact(r) or r["residual_exact"] == "YES" for r in rs))
        out[f"{prefix}.{name}.answers"] = str(sum(r["live_match"] == "YES" for r in rs))
    return out


def repeat_keys(suite):
    """Do the live counts repeat on a fresh reload? The seen cases of both loads, same (query, engine, param)."""
    prev = {(r["query"], r["db"], r["param_idx"]): r
            for r in rows_of(ENGINE_FIDELITY_PREV / "interactive/live_kvstats.csv") if r["status"] == "ok"}
    # not comparable: IC3 / IC11 have new params, Dgraph IC4 / IC6 a new query text; IC5's params
    # are LDBC's hosted rows, and the capture in paper-results/ is on those
    changed = lambda r: r["query"] in ("ic3", "ic5", "ic11") or (r["query"], r["db"]) in (("ic4", "dgraph"), ("ic6", "dgraph"))
    both = [(r, prev[k]) for r in suite if r["split"] == "seen" and not changed(r)
            for k in [(r["query"], r["db"], r["param_idx"])] if k in prev]
    same = sum(all(r[f"live_{c}"] == p[c] for c in "SGN") for r, p in both)
    return {"fidelity.sf03.repeat.cells": str(len(both)), "fidelity.sf03.repeat.same": str(same)}


def sf03_keys():
    rows, suite, pairs, fit = load("ldbc sf0.3")
    params = {len(v) for v in pairs.values()}
    if len(params) != 1:
        sys.exit(f"params per pair differ: {sorted(params)}")
    answers = sum(r["live_match"] == "YES" for r in suite)
    differ = sum(r["live_match"] == "NO" for r in suite)
    if any(r.get("answer_source") == "stored-unchecked" for r in suite):
        sys.exit("a case's live answer is stored-unchecked (validate.py --stored-answers): not paper evidence")
    if answers + differ != len(suite):
        sys.exit("a case has no live answer check")
    # the bound compares two plans' work only where they answer the same question (validate.py's
    # `bound` docstring; deviations.py same_answer): an engine's different question is not counted
    same = [r for r in suite if r["same_answer"] == "YES"]
    bound = sum(r["bound"] == "YES" for r in same)
    if bound != sum(wops(r, "opt") <= wops(r, "live") for r in same):
        sys.exit("the bound recount disagrees with the bound column (weights changed?)")
    # non-empty = our optimal plan returns rows; rows_live is -1 where the engine's answer was not parsed
    extra = [wops(r, "live") / wops(r, "opt") for r in same if int(r["rows_opt"]) > 0]
    miss = [r for r in suite if r["fit"] != "YES" and r["residual_exact"] != "YES"]
    # the draft says every case outside the fit is Dgraph's split residual; stop if that ever stops being true
    if any(r["fit"] != "YES" and not (r["db"] == "dgraph" and r["residual_exact"] == "YES") for r in suite):
        sys.exit("a case outside the 10% fit is not Dgraph's split residual: revise the Fidelity text")
    return {"fidelity.sf03.cases": str(len(suite)),
            "fidelity.sf03.queries": str(len({q for q, _ in pairs})),
            "fidelity.sf03.pairs": str(len(pairs)),
            "fidelity.sf03.params": str(params.pop()),
            "fidelity.sf03.fit": str(fit),
            "fidelity.sf03.outside": str(len(suite) - fit),
            "fidelity.sf03.outside.dgraph-split": str(sum(r["fit"] != "YES" and r["db"] == "dgraph"
                                                      and r["residual_exact"] == "YES" for r in suite)),
            "fidelity.sf03.fit.tol5": str(sum(within(r, 0.05) for r in suite)),
            "fidelity.sf03.fit.tol20": str(sum(within(r, 0.20) for r in suite)),
            "fidelity.sf03.pairs.fit": str(sum(all(r["fit"] == "YES" for r in v) for v in pairs.values())),
            "fidelity.sf03.exact": str(sum(exact(r) for r in suite)),
            "fidelity.sf03.exact-or-split": str(sum(exact(r) or r["residual_exact"] == "YES" for r in suite)),
            **groups(suite, "fidelity.sf03"),
            "fidelity.sf03.answers": str(answers),
            "fidelity.sf03.answers.differ": str(differ),
            "fidelity.sf03.bound": str(bound),
            "fidelity.sf03.bound.cases": str(len(same)),
            "fidelity.sf03.bound.fail": str(len(same) - bound),
            "fidelity.sf03.extra.median": f"{statistics.median(extra):.1f}",
            "fidelity.sf03.extra.max": f"{max(extra):.0f}",
            "fidelity.sf03.extra.cases": str(len(extra)),
            "fidelity.sf03.draft.pairs": str(GRID_PAIRS - len(pairs)),
            "fidelity.sf03.miss": str(len(miss)),
            "fidelity.sf03.miss.pairs": str(len({(r["query"], r["db"]) for r in miss})),
            **split_keys("fidelity.sf03", ENGINE_FIDELITY, suite), **repeat_keys(suite)}


def groups(rows, prefix):
    """{prefix}.split / .rest: cases off by exactly the split residual, and the remaining non-exact cases."""
    split = sum(not exact(r) and r["residual_exact"] == "YES" for r in rows)
    rest = sum(not exact(r) and r["residual_exact"] != "YES" for r in rows)
    return {f"{prefix}.split": str(split), f"{prefix}.rest": str(rest)}


def sf01_keys():
    _, suite, pairs, fit = load("ldbc sf0.1")
    return {"fidelity.sf01.cases": str(len(suite)),
            "fidelity.sf01.queries": str(len({q for q, _ in pairs})),
            "fidelity.sf01.pairs": str(len(pairs)),
            "fidelity.sf01.fit": str(fit),
            "fidelity.sf01.exact": str(sum(exact(r) for r in suite)),
            "fidelity.sf01.answers": str(sum(r["live_match"] == "YES" for r in suite)),
            **groups(suite, "fidelity.sf01"),
            **split_keys("fidelity.sf01", ENGINE_FIDELITY_SF01, suite)}


def legacy_keys():
    """main.tex's old paragraph (the 20260811 run, total-ops measures). Drop with it once main.tex cites the keys above."""
    rows = rows_of(VALIDATION["legacy 08-11"])
    total = lambda r, side: num(r, f"{side}_S") + num(r, f"{side}_G") + num(r, f"{side}_N")
    by = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by[r["db"]][0] += total(r, "sim")
        by[r["db"]][1] += total(r, "live")
    neb = [abs(total(r, "sim") / total(r, "live") - 1) for r in rows if r["db"] == "nebula" and total(r, "live") > 0]
    out = {"fidelity.runs": str(len(rows)),
           "fidelity.dbs": str(len({r["db"] for r in rows})),
           "fidelity.queries": str(len({r["query"] for r in rows})),
           "fidelity.params": str(len({r["param_idx"] for r in rows})),
           "fidelity.exact": str(sum(exact(r) for r in rows)),
           "fidelity.within5": str(sum(1 for r in rows if total(r, "live") > 0
                                       and abs(total(r, "sim") / total(r, "live") - 1) <= 0.05)),
           "fidelity.nebula.maxdev": f"{100 * max(neb):.1f}"}
    dg = [r for r in rows if r["db"] == "dgraph" and r["query"] in ("is4", "is5", "is6")]
    miss = [num(r, "live_G") - num(r, "sim_G") for r in dg]
    out["fidelity.dgraph.is456.missget.min"], out["fidelity.dgraph.is456.missget.max"] = f"{min(miss):.0f}", f"{max(miss):.0f}"
    out["fidelity.dgraph.is456.liveops.max"] = f"{max(total(r, 'live') for r in dg):.0f}"
    ar = [r for r in rows if r["db"] == "arangodb" and r["query"] == "ic4"]
    for key, drop in (("fewerget", [1 - num(r, "sim_G") / num(r, "live_G") for r in ar]),
                      ("fewerops", [1 - total(r, "sim") / total(r, "live") for r in ar])):
        out[f"fidelity.arangodb.ic4.{key}.min"] = f"{100 * min(drop):.0f}"
        out[f"fidelity.arangodb.ic4.{key}.max"] = f"{100 * max(drop):.0f}"
    for db, (sim, live) in sorted(by.items()):
        out[f"fidelity.sum.{db}"] = f"{sim / live:.3f}"
    return out


def microbench_keys():
    """fidelity.mb.*: the microbench engine-plan check (16 shapes x 3 engines), same fit rule as the LDBC keys."""
    rows = rows_of(VALIDATION["microbench sf0.3"])
    fit = sum(r["fit"] == "YES" for r in rows)
    if sum(within(r, FIT_REL) for r in rows) != fit:
        sys.exit("microbench: the 10% recount disagrees with the fit column")
    outside = [r for r in rows if not exact(r)]
    if any(not (r["db"] == "dgraph" and r["residual_exact"] == "YES") for r in outside):
        sys.exit("a microbench case that is not exact is not Dgraph's split residual: revise the Fidelity text")
    return {"fidelity.mb.cases": str(len(rows)),
            "fidelity.mb.shapes": str(len({r["query"] for r in rows})),
            "fidelity.mb.fit": str(fit),
            "fidelity.mb.exact": str(sum(exact(r) for r in rows)),
            "fidelity.mb.split": str(len(outside)),
            "fidelity.mb.answers": str(sum(r["rows_match"] == "YES" for r in rows)),
            **groups(rows, "fidelity.mb")}


def reads_keys():
    """fidelity.reads.*: the LDBC and microbench read cases at SF0.3 together, in three groups: exact, off by exactly
    Dgraph's split residual, and the rest (each count within 10% of live). The draft's summary sentence names the
    rest as all NebulaGraph and bounds their deviation; stop if either stops being true."""
    ldbc = rows_of(VALIDATION["ldbc sf0.3"])
    mb = rows_of(VALIDATION["microbench sf0.3"])
    rows = ldbc + mb
    n_exact = sum(exact(r) for r in rows)
    split = [r for r in rows if not exact(r) and r["residual_exact"] == "YES"]
    rest = [r for r in rows if not exact(r) and r["residual_exact"] != "YES"]
    if any(r["db"] != "nebula" or not within(r, FIT_REL) for r in rest):
        sys.exit("a read case that is neither exact nor the split residual is not a small NebulaGraph residual: "
                 "revise the Fidelity summary")
    maxdev = max(abs(num(r, f"sim_{c}") - num(r, f"live_{c}")) / num(r, f"live_{c}")
                 for r in rest for c in "SGN" if num(r, f"live_{c}") > 0) if rest else 0.0
    pct = lambda k: f"{100 * k / len(rows):.0f}"
    return {"fidelity.reads.cases": f"{len(rows)}",
            "fidelity.reads.exact": str(n_exact), "fidelity.reads.exact.pct": pct(n_exact),
            "fidelity.reads.split": str(len(split)), "fidelity.reads.split.pct": pct(len(split)),
            "fidelity.reads.rest": str(len(rest)), "fidelity.reads.rest.pct": pct(len(rest)),
            "fidelity.reads.rest.maxdev": f"{100 * maxdev:.1f}"}


def read_table():
    """the reads table: each validation file's rows, tagged with its source, the holdout visibility and (sf0.3) the
    previous load's live counts"""
    vis = {"ldbc sf0.3": ENGINE_FIDELITY, "ldbc sf0.1": ENGINE_FIDELITY_SF01}
    prev = {(r["query"], r["db"], r["param_idx"]): r for r in rows_of(ENGINE_FIDELITY_PREV / "interactive/live_kvstats.csv")}
    columns, rows = ["source"], []
    for source, path in VALIDATION.items():
        columns += [c for c in rows_of(path)[0] if c not in columns]
        seen = ({(r["query"], r["db"], r["param_idx"]): r["visibility"]
                 for r in rows_of(vis[source] / "interactive/holdout_visibility.csv")} if source in vis else {})
        for r in rows_of(path):
            k = (r["query"], r["db"], r["param_idx"])
            p = prev.get(k, {}) if source == "ldbc sf0.3" else {}
            rows.append({**r, "source": source, "visibility": seen.get(k, ""),
                         **{f"prev_live_{c}": p.get(c, "") for c in ("S", "G", "N", "status")}})
    columns += ["visibility", "prev_live_S", "prev_live_G", "prev_live_N", "prev_live_status"]
    return columns, [{c: r.get(c, "") for c in columns} for r in rows]


def writes_keys():
    """fidelity.writes.*: the compared write cells, those equal, those equal after the named differences, and the rest
    (write_fidelity.py's verdicts); none when paper-results/ does not hold the compare yet, said"""
    if not WRITES.exists():
        print(f"  fidelity.writes.*: not written, {WRITES} is not there (`just results engine-fidelity` makes it)")
        return {}
    cells = [r for r in rows_of(WRITES) if r["residual"]]
    count = lambda v: str(sum(r["verdict"] == v for r in cells))
    return {"fidelity.writes.cases": str(len(cells)), "fidelity.writes.exact": count("equal"),
            "fidelity.writes.named": count("equal after the named differences"),
            "fidelity.writes.residual": count("unexplained residual")}


def main():
    if a.tree != "paper-results":
        print(f"--from {a.tree}: " + "; ".join(f"{k} from {'the run' if str(p).startswith(str(snapshots.tree_of(a.tree).root)) else 'paper-results'}"
                                             for k, p in VALIDATION.items())
              + "; holdout_visibility.csv and the previous load's counts from paper-results")
    out = {**sf03_keys(), **sf01_keys(), **microbench_keys(), **reads_keys(), **legacy_keys(), **writes_keys()}
    for k in ("fidelity.reads.cases", "fidelity.reads.exact", "fidelity.reads.split", "fidelity.reads.rest",
              "fidelity.reads.rest.maxdev", "fidelity.sf03.fit", "fidelity.sf03.answers", "fidelity.sf03.answers.differ",
              "fidelity.sf01.cases", "fidelity.sf01.fit", "fidelity.sf01.answers", "fidelity.mb.cases", "fidelity.mb.fit",
              *[k for k in out if k.startswith("fidelity.writes.")]):
        print(f"  {k} = {out[k]}")
    write_numbers("engine_fidelity", out,
                  f"{ENGINE_FIDELITY.name} (sf0.3) + {ENGINE_FIDELITY_SF01.name} (sf0.1), ldbc/engine_validation.csv; "
                  f"microbench: {ENGINE_FIDELITY_MB.name}; "
                  f"legacy keys: {ENGINE_FIDELITY_LEGACY.name}")
    columns, rows = read_table()
    write_table("engine-fidelity-numbers-reads", columns, rows, key=("source", "query", "db", "param_idx"))


if __name__ == "__main__":
    main()
