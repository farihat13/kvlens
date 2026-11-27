#!/usr/bin/env python3
"""Is a tally assembled by lookup (kvevaluator.predict.tally_library.tally_for_spec: each part's rows from the setting
tally that stores the part the same way) the tally of that layout? Each layout is tallied directly (kvevaluator tally
over the record stream) and assembled from the library, whose seeds are the seven diagonals; the two must be equal in
every group, chunk family, locality row and total (wall time and provenance aside). Each diagonal is assembled too (its
rows all come from its own seed). A per-type mix (--mixes: per-label props, per-edge-type adjacency) also needs
settings outside the seven: the library builds them on demand. Opens no store.

    python3 experiments/predict_statistics/check_assembled_tallies.py --sf 0.1 --records <records.jsonl> \\
        --out <dir> [--cells all | a,b,...] [--mixes <spec.json>,...] [--jobs N] [--plant]
      -> <dir>/tallies/ (the library), <dir>/tally-direct/<layout>.json, <dir>/check_assembled_tallies.csv
         (one row per layout: cell, rows compared, rows that differ, the first difference)
--cells all: the 42 off-diagonals (sf0.1: seconds each); a list names some (SF3: about 4.5 min each); none: the seven
diagonals only. --mixes: default the I01 search's best mix at sf0.1 (inputs/specs/mixes_w16_20261030/w16_b2_spco.json);
'none' for no mix.
--plant (the control): one assembled tally gets one key more in one group before the comparison: must exit 1.
Exit 1 on any difference, or when 0 off-diagonal layouts were compared (unless --cells none).
"""
import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import subprocess

from kvcost.predict_accuracy import uniform_49_tallies as U
from kvevaluator.paths import cpp_bin
from kvevaluator.predict import assemble_tally as A
from kvevaluator.predict import tally_library as L

MIX = U.SPECS / "mixes_w16_20261030" / "w16_b2_spco.json"


def rows_of(t):
    return len(t["groups"]) + len(t["chunks"]) + len(t["locality"])


def direct_mix(spec_path, records, out_path):
    """the direct tally of a mix spec file; reused when made for the same spec, from the same records, at this binary's
    tally version (tally_library.reusable)"""
    with open(spec_path) as f:
        spec = json.load(f)
    now, version = L.binary_info()
    if os.path.exists(out_path):
        with open(out_path) as f:
            have = json.load(f)
        made = {"git_sha": have["provenance"]["binary_git_sha"], "dirty": have["provenance"]["binary_dirty"]}
        if (have["spec"] == spec and L.made_from_stream(have["records"], records)
                and L.reusable(made, have.get("tally_version"), now, version)):
            return spec, have
    tmp = f"{out_path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        subprocess.run([str(cpp_bin()), "tally", "--schema", str(U.SCHEMA), "--spec", str(spec_path), "--records",
                        records], stdout=f, check=True)
    os.replace(tmp, out_path)
    with open(out_path) as f:
        return spec, json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--records", required=True, help="the record stream the tallies read")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cells", default="all", help="'all' (the 42 off-diagonals), 'none' (the diagonals only, each "
                    "with itself) or a comma list of cells")
    ap.add_argument("--mixes", default=str(MIX), help="mix spec files, comma-separated, or 'none'")
    ap.add_argument("--jobs", type=int, default=4, help="tallies run at once")
    ap.add_argument("--plant", action="store_true", help="the control: one assembled tally off by one key (must exit 1)")
    a = ap.parse_args(argv)
    path, sha, dirty = U.binary()
    print(f"check_assembled_tallies: binary {path} {sha[:10]} dirty={dirty}; records {a.records}; sf{a.sf}")
    print(f"check_assembled_tallies: the grid's 9 spec files equal their diagonals' blocks ({U.check_grid_specs()} checked)")
    records = os.path.abspath(a.records)
    cells = ([c for c in U.CELLS if c not in U.DIAGONAL_CELLS] if a.cells == "all" else [] if a.cells == "none"
             else a.cells.split(","))
    for c in cells:
        U.settings_of(c)
    mixes = [] if a.mixes == "none" else a.mixes.split(",")
    direct_dir, spec_dir = (os.path.join(a.out, d) for d in ("tally-direct", "specs"))
    os.makedirs(direct_dir, exist_ok=True)
    lib = U.library(a.out, records)
    with ThreadPoolExecutor(a.jobs) as pool:       # the seven seeds, built (or reused) at once
        list(pool.map(lambda s: lib.entry(U.diagonal_spec(s)), U.SETTINGS))
    jobs = [(c, os.path.join(direct_dir, f"{c}.json")) for c in cells if c not in U.DIAGONAL_CELLS]
    with ThreadPoolExecutor(a.jobs) as pool:
        for (c, _), (p, s, reused) in zip(jobs, pool.map(lambda j: U.tally(j[0], records, j[1], spec_dir), jobs)):
            print(f"   tally {c}: {'reused' if reused else f'{s:.1f} s'}")
    want = {c: lib.entry(U.spec_of(c))[1] for c in U.DIAGONAL_CELLS}        # a diagonal's direct tally is its seed
    for c, _ in jobs:
        with open(os.path.join(direct_dir, f"{c}.json")) as f:
            want[c] = json.load(f)
    specs = {c: U.spec_of(c) for c in want}
    for m in mixes:
        name = os.path.splitext(os.path.basename(m))[0]
        specs[name], want[name] = direct_mix(m, records, os.path.join(direct_dir, f"{name}.json"))
        print(f"   tally {name} (mix): direct")
    rows, bad, planted = [], 0, False
    for c in want:
        got = L.tally_for_spec(specs[c], lib)
        if a.plant and not planted and c not in U.DIAGONAL_CELLS:
            got["groups"][0]["keys"] += 1
            planted = True
            print(f"[control] one key added to {c}'s first group")
        diff = A.differences(got, want[c])
        bad += bool(diff)
        rows.append({"cell": c, "rows_compared": rows_of(want[c]), "rows_differ": len(diff), "first_difference": diff[0] if diff else ""})
        print(f"   {c}: {rows_of(want[c])} rows, {len(diff)} differ{': ' + diff[0][:160] if diff else ''}")
    with open(os.path.join(a.out, "check_assembled_tallies.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, ["cell", "rows_compared", "rows_differ", "first_difference"])
        w.writeheader()
        w.writerows(rows)
    n_off = sum(r["cell"] not in U.DIAGONAL_CELLS for r in rows) - len(mixes)
    ok = not bad and (n_off > 0 or a.cells == "none") and len(rows) == len(U.DIAGONAL_CELLS) + n_off + len(mixes)
    print(f"check_assembled_tallies: {len(rows)} layouts compared ({n_off} off-diagonal, {len(U.DIAGONAL_CELLS)} "
          f"diagonals, {len(mixes)} mixes); settings built this run {len(lib.built)} "
          f"({sum(s for _, s in lib.built):.1f} s), {len(set(lib.reused))} reused; {sum(r['rows_compared'] for r in rows)} rows, "
          f"{bad} layouts differ: {'PASS' if ok else 'FAIL'}{' [control: must FAIL]' if a.plant else ''}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
