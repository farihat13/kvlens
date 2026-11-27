#!/usr/bin/env python3
"""The LDBC param-source gate (the rule in inputs/params/ldbc/param_sources.json): every (SF, IC query) reads exactly the file
inputs/params/ldbc/param_sources.json names, the hosted copies are LDBC's files byte for byte, and every source is hosted
(rule 2: always hosted; the census reports stop-early rows, it decides nothing).

Checks, each printing what it covered:
  1. resolve: for every tracked SF and IC1-12, params.param_file opens the table's choice (ours/ when "ours", the
     verbatim copy when "hosted"), and that file exists;
  2. copies: every hosted copy's sha256 equals hosted_params.meta.json's, and the download's where it is present;
  3. table: every ours/ file has a row (ours/ files stay as records), every source is hosted (rule 2);
  4. census: for an SF with sf<SF>/interactive/param_census.csv (its CENSUS_VERSION must be the tool's, its rows 1-10, every row
     ran), print each query's rows that stop early; a report only, never a failure;
  5. one function: no code in the evaluator builds an IC param path (interactive_<N>_param / substitution_parameters-sf)
     except the loader, the download's own code (its path, its fetch, their tests) and this gate, so every reader goes
     through param_file. The live engines' code (engines/, python/kvengines/) is out of scope: their runners read
     LDBC's download, which check 2 proves byte-identical to the hosted copies;
  6. --rerun SF: re-run the census at that SF (sf0.1: seconds) and compare each row's answer and work with the tracked file.
Exit 1 on any failure or on 0 cells checked.

    python3 tests/lint/check_param_sources.py [--rerun 0.1] [--sources FILE]
"""
import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from kvevaluator.paths import ROOT, ldbc_params_dir
from repo_files import repo_files

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.datasets.ldbc import params as P

ICS = [f"ic{n}" for n in range(1, 13)]
SEE = "see the rule in inputs/params/ldbc/param_sources.json"
# check 5: a code line that builds an IC param path; only these files may hold one
PARAM_PATH = re.compile(r"interactive_(\{|\d)[^\"']*_param\.txt|substitution_parameters-sf")
MAY_BUILD = {"python/kvevaluator/datasets/ldbc/params.py", "tests/lint/check_param_sources.py",
             # the download's own code: where it lives, how it is fetched, and their tests
             "python/kvevaluator/paths.py", "python/kvevaluator/datasets/ldbc/fetch.py", "tests/unit/check_fetch_ldbc.py",
             "tests/lint/check_locations.py", "tests/unit/check_engine_ldbc_links.py"}
OUT_OF_SCOPE = ("output", ".venv", "engines", os.path.join("python", "kvengines"))   # check 5: not the evaluator's code


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tracked_sfs():
    """the SFs with hosted copies: a dir sf<SF>/ holding hosted_params.meta.json (sf1/ holds only the short reads' params)"""
    return sorted(d[2:] for d in os.listdir(P._LDBC_OVERRIDE)
                  if d.startswith("sf") and os.path.isfile(os.path.join(P._interactive(d[2:]), "hosted_params.meta.json")))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--rerun", default=None, metavar="SF", help="re-run the census at this SF and compare with the tracked file")
    ap.add_argument("--sources", default=None, help="check another table (the gate's own negative test)")
    a = ap.parse_args()
    if a.sources:
        P.SOURCES_FILE = os.path.abspath(a.sources)
    table = P.param_sources()
    fails, n = [], {"resolve": 0, "copies": 0, "table": 0, "census": 0, "one function": 0, "rerun": 0}

    sfs = tracked_sfs()
    for d in sorted(os.listdir(P._LDBC_OVERRIDE)):
        if d.startswith("sf") and os.path.isdir(P._interactive(d[2:])) and d[2:] not in sfs:
            print(f"  {d}: no hosted_params.meta.json, so no hosted IC copies: not checked ({', '.join(sorted(os.listdir(P._interactive(d[2:]))))})")
    for sf in sfs:                                                     # 1. resolve
        for q in ICS:
            src = P.param_source(sf, q)
            name = f"interactive_{q[2:]}_param.txt"
            want = os.path.join(P._interactive(sf), *(["ours"] if src == "ours" else []), name)
            try:
                got = P.param_file(sf, q)
            except SystemExit as e:
                fails.append(f"resolve sf{sf} {q}: {e}")
                continue
            n["resolve"] += 1
            if os.path.abspath(got) != os.path.abspath(want) or not os.path.exists(got):
                fails.append(f"resolve sf{sf} {q}: the table says {src}, the loader opens {got} ({SEE})")

    for sf in sfs:                                                     # 2. copies
        mp = os.path.join(P._interactive(sf), "hosted_params.meta.json")
        recorded = json.load(open(mp))["sha256"] if os.path.exists(mp) else {}
        for q in ICS:
            name = f"interactive_{q[2:]}_param.txt"
            copy = os.path.join(P._interactive(sf), name)
            if not os.path.exists(copy):
                fails.append(f"copies sf{sf}: no hosted copy {name}")
                continue
            n["copies"] += 1
            s = sha(copy)
            if recorded.get(name) != s:
                fails.append(f"copies sf{sf} {name}: sha256 {s[:12]} is not hosted_params.meta.json's {str(recorded.get(name))[:12]}")
            dl = os.path.join(ldbc_params_dir(sf), name)
            if os.path.exists(dl) and sha(dl) != s:
                fails.append(f"copies sf{sf} {name}: differs from the download {dl}")

    pairs = table["pairs"]                                             # 3. table
    for sf in sfs:
        od = os.path.join(P._interactive(sf), "ours")
        ours = sorted(f for f in os.listdir(od) if f.endswith("_param.txt")) if os.path.isdir(od) else []
        for f in ours:
            n["table"] += 1
            q = "ic" + f.split("_")[1]
            if q not in pairs.get(f"sf{sf}", {}):
                fails.append(f"table: sf{sf}/interactive/ours/{f} has no row in param_sources.json (say why it is kept: {SEE})")
    for key, rows in pairs.items():
        for q, row in rows.items():
            n["table"] += 1
            if row.get("source") != "hosted":
                fails.append(f"table {key} {q}: source {row.get('source')!r}; rule 2 is always hosted ({SEE})")
    if table.get("default") != "hosted":
        fails.append(f"table: default {table.get('default')!r}; rule 2 is always hosted ({SEE})")

    from kvevaluator.measure import param_census as PC                                          # 4. census (a report: rule 2 decides nothing on it)
    for sf in sfs:
        cp = PC.census_path(sf)
        if not os.path.exists(cp):
            for q, row in sorted(pairs.get(f"sf{sf}", {}).items()):
                print(f"  census sf{sf} {q}: no census at this SF yet; the table's evidence: {row.get('evidence', '(none)')}")
            continue
        meta = json.load(open(cp[:-4] + ".meta.json"))
        if meta.get("census_version") != PC.CENSUS_VERSION:
            fails.append(f"census sf{sf}: CENSUS_VERSION {meta.get('census_version')} in the file, the tool is {PC.CENSUS_VERSION}: re-run python/kvevaluator/measure/param_census.py --sf {sf}")
            continue
        if meta.get("rows") != "1-10":
            fails.append(f"census sf{sf}: the tracked census covers rows {meta.get('rows')}, the census reports on rows 1-10")
            continue
        rows = [r for r in csv.DictReader(open(cp)) if 1 <= int(r["row"]) <= 10]
        for q in ICS:
            rq = [{"row": int(r["row"]), "ok": int(r["ok"]), "stops_early": int(r["stops_early"])} for r in rows if r["query"] == q]
            if len(rq) != 10 or not all(r["ok"] for r in rq):
                fails.append(f"census sf{sf} {q}: {sum(r['ok'] for r in rq)} of rows 1-10 ran, the census needs all 10: re-run python/kvevaluator/measure/param_census.py --sf {sf}")
                continue
            n["census"] += 1
            stops = sorted(r["row"] for r in rq if r["stops_early"])
            if stops:
                print(f"  census sf{sf} {q}: {len(stops)}/10 hosted rows stop early (rows {', '.join(map(str, stops))}); reported only")

    for rel in repo_files(EXP):                                          # 5. one function (the repo's files, as every lint)
        if not (rel.endswith((".py", ".sh")) or rel == "justfile"):
            continue
        path = os.path.join(EXP, rel)
        if rel in MAY_BUILD or any(rel == d or rel.startswith(d + os.sep) for d in OUT_OF_SCOPE):
            continue
        n["one function"] += 1
        for i, line in enumerate(open(path, errors="replace"), 1):
            if PARAM_PATH.search(line) and not line.lstrip().startswith("#"):
                fails.append(f"one function: {rel}:{i} builds an IC param path; read it through params.param_file ({SEE})")

    if a.rerun:                                                        # 6. re-run
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "census.csv")
            r = subprocess.run([sys.executable, os.path.join(EXP, "python", "kvevaluator", "measure", "param_census.py"), "--sf", a.rerun, "--out", out],
                               capture_output=True, text=True)
            if not os.path.exists(out):
                fails.append(f"rerun sf{a.rerun}: the census wrote nothing (rc {r.returncode}): {r.stderr[-300:]}")
            else:
                key = lambda rs: {(x["query"], x["row"]): (x["answer_rows"], x["result_hash"], x["S"], x["G"], x["N"]) for x in rs}
                new = key(csv.DictReader(open(out)))
                old = key(csv.DictReader(open(PC.census_path(a.rerun))))
                n["rerun"] = len(new)
                diff = sorted(k for k in set(new) | set(old) if new.get(k) != old.get(k))
                if diff:
                    fails.append(f"rerun sf{a.rerun}: {len(diff)} rows differ from the tracked census, e.g. {diff[:3]}")

    print("checked: " + ", ".join(f"{k} {v}" for k, v in n.items()))
    for f in fails:
        print(f"FAIL {f}")
    zero = [k for k, v in n.items() if v == 0 and not (k == "rerun" and not a.rerun)]
    if zero:
        print(f"FAIL 0 cells checked: {zero}")
    ok = not fails and not zero
    print(f"param sources ({', '.join('sf' + s for s in sfs)}): " + ("PASS" if ok else f"FAIL ({len(fails)})"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
