#!/usr/bin/env python3
"""Were a holdout cell's live counts readable before the freeze? Read-only report over engine_validation.csv.

The LDBC holdout params (ldbc_holdout.json) were not used to write the plans, but other studies had already
committed live counts at most of them before the first freeze. A holdout cell is `visible` when some committed capture below holds
the same (query, db, param) with identical S, G and N, so a plan author COULD have read it; else `unseen`
(never captured, or captured with different counts -- e.g. at the old IC3/IC11 params or an older engine
build). Fidelity is then reported for each group. Measures nothing; with --write it also keeps the labels
next to the table as holdout_visibility.csv (one row per holdout cell: query, db, param_idx, split, visibility,
seen_in = the committed files that held the same counts), so a reader needs no other study's files.

  python3 holdout_visibility.py --sf 0.3 --source-repo <checkout>       # reads the working LDBC output's engine_validation.csv
  python3 holdout_visibility.py --sf 0.3 --source-repo <checkout> --write
  python3 holdout_visibility.py --csv <snapshot>/interactive/engine_validation.csv   --sf 0.3 --source-repo <checkout>
The labels this writes ship in the snapshot (holdout_visibility.csv); re-deriving them needs the source history.
"""
from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from pathlib import Path
from kvevaluator.paths import ROOT

_HERE = ROOT / "experiments" / "engine_fidelity" / "holdout_visibility.py"
# The captures and their commit dates live in the history of the source repository (tag artifact-freeze-20261026),
# not in this one: --source-repo names a checkout of it. Its own layout is used there, as it was committed.
_REPO = None
_KVF = None
from kvengines import live_workloads as W


def _committed(path: Path) -> bool:
    """Committed BEFORE the first freeze: only a file an author could have read while writing the plans
    counts (this study's own later snapshots hold the holdout counts too, and do not)."""
    added = subprocess.run(["git", "log", "--diff-filter=A", "--format=%H", "--", str(path)], cwd=_REPO,
                           text=True, capture_output=True).stdout.split()
    first_freeze = W.ldbc_holdout()["freezes"][0]["commit"]
    return bool(added) and subprocess.run(["git", "merge-base", "--is-ancestor", added[-1], first_freeze],
                                          cwd=_REPO).returncode == 0


def captures(sf: str) -> dict:
    """{source: {(query, db, param): (S, G, N)}} from every committed live capture at this SF."""
    out = {}
    def add(name, rows):
        out[name] = {k: v for k, v in rows}
    # the kvformat engine sweeps and the engine-fidelity snapshots: sf,db,query,param_idx,...,S,G,N
    for p in sorted((_KVF / "output" / f"sf{sf}").glob("engine-*/**/*kvstats.csv")):
        if _committed(p):
            add(str(p.relative_to(_REPO)),
                (((r["query"], r["db"], int(r["param_idx"])), (int(r["S"]), int(r["G"]), int(r["N"])))
                 for r in csv.DictReader(open(p)) if r.get("S") not in (None, "")))
    # cost_eval's measured /kvstats: qtype,num,param_idx,cache_mode,S,G,N (one file per db)
    for p in sorted((_REPO / "experiments" / "20260228-kvstats-mb2" / "output" / "costeval").glob(
            f"sf{sf}-*/*/measured.csv")):
        if _committed(p):
            db = p.parent.name
            add(str(p.relative_to(_REPO)),
                (((f"{r['qtype']}{r['num']}", db, int(r["param_idx"])), (int(r["S"]), int(r["G"]), int(r["N"])))
                 for r in csv.DictReader(open(p)) if r.get("S") not in (None, "")))
    # the older live-engine experiments (IC only, query = number): query,param,get_count,seek_count,next_count
    for p in sorted((_REPO / "experiments").glob(f"2026022*/output/sf{sf}-*/*/*-kvstats.csv")):
        rows = list(csv.DictReader(open(p)))
        if _committed(p) and rows and "seek_count" in rows[0]:
            db = p.parent.name
            add(str(p.relative_to(_REPO)),
                (((f"ic{r['query']}", db, int(r["param"])),
                  (int(r["seek_count"]), int(r["get_count"]), int(r["next_count"]))) for r in rows))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.3"))
    ap.add_argument("--csv", type=Path, help="engine_validation.csv (default: the working LDBC output of --sf)")
    ap.add_argument("--write", action="store_true", help="also write holdout_visibility.csv next to the table")
    ap.add_argument("--source-repo", type=Path, required=True,
                    help="a checkout of the source repository (tag artifact-freeze-20261026), whose history holds the captures")
    a = ap.parse_args(argv)
    global _REPO, _KVF
    _REPO = a.source_repo.resolve()
    _KVF = _REPO / "experiments" / "20260625-kvformat-evaluator"
    if not (_REPO / ".git").exists() or not _KVF.is_dir():
        sys.exit(f"--source-repo {_REPO}: not a checkout of the source repository (no .git or no {_KVF.name}/)")
    src = a.csv or W.out_dir("interactive", a.sf) / "engine_validation.csv"
    rows = [r for r in csv.DictReader(open(src)) if r["status"] == "suite"]
    caps = captures(a.sf)
    print(f"committed captures at sf{a.sf}: " + (", ".join(f"{k} ({len(v)})" for k, v in caps.items()) or "none"))

    def seen_in(r):
        k, live = (r["query"], r["db"], int(r["param_idx"])), (int(r["live_S"]), int(r["live_G"]), int(r["live_N"]))
        return [name for name, cap in caps.items() if cap.get(k) == live]

    groups, labels = {}, []
    for r in rows:
        if r["split"] == "seen":
            continue
        found = seen_in(r)
        groups.setdefault((r["split"], "visible" if found else "unseen"), []).append(r)
        labels.append({"query": r["query"], "db": r["db"], "param_idx": r["param_idx"], "split": r["split"],
                       "visibility": "visible" if found else "unseen", "seen_in": ";".join(found)})
    if not groups:
        sys.exit(f"no holdout cells in {src} (split column) -- nothing to report")
    exact = lambda r: all(r[f"sim_{c}"] == r[f"live_{c}"] for c in "SGN") or r["residual_exact"] == "YES"
    for (split, vis), rs in sorted(groups.items()):
        live = [r for r in rs if r["live_match"]]
        print(f"  {split:14s} {vis:8s}: {len(rs):4d} cells, {len({(r['query'], r['db']) for r in rs})} pairs; "
              f"fit {sum(r['fit'] == 'YES' for r in rs)}, exact or split residual {sum(map(exact, rs))}; "
              f"answers vs live {sum(r['live_match'] == 'YES' for r in live)}/{len(live)}")
    unseen = sorted({(r["query"], r["db"]) for (sp, v), rs in groups.items() if v == "unseen" for r in rs})
    print(f"  unseen pairs: {unseen}")
    if a.write:
        out = src.parent / "holdout_visibility.csv"
        labels.sort(key=lambda x: (x["query"], x["db"], int(x["param_idx"])))
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(labels[0]))
            w.writeheader()
            w.writerows(labels)
        print(f"  {len(labels)} holdout cells -> {out}")


if __name__ == "__main__":
    main()
