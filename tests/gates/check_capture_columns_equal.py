#!/usr/bin/env python3
"""A writes capture measured again against the one it replaces: every column the old file has is equal, as text, on
every row; the new file only adds columns. The proof that a change which adds columns moved no number. Optionally
(--write-units) also: each new row's exact write units (put_units_1k, del_units_1k) lie inside what its KV-pair size
histograms allow, each request rounded up alone from its bucket's lowest and highest size, at least 1 unit, a DELETE
of unknown size 1 (store/size_hist.h's rule, seen through the histogram's bucket edges).

Reads two capture roots (a writes dir: <root>/{counts,steps,parts}/<cell>.csv) and compares every cell file the old
root has. Not compared: `run` (the run's id) and every clock column (a name ending in `_ns`). Rows are matched by their
keys (query, param_idx, body, step_idx, part, as the file kind has them); a key in one file only is a difference.

    python3 tests/gates/check_capture_columns_equal.py --old <old writes dir> --new <new writes dir> [--write-units]
        [--added put_units_1k,del_units_1k]

Prints, per file, the rows and columns compared and the columns it adds, and a PASS / FAIL line; exit 1 on any
difference, on a file that compares 0 rows or 0 columns, on a cell file in --new only, with --write-units on a steps or
parts file without the columns or on 0 rows checked, on a column the new file adds that neither --added nor
capture_columns.APPENDED_COLUMNS (a column appended to the files since, e.g. rt_levels) names, or on 0 files.
"""
import argparse
import csv
import glob
import math
import os
import sys

from kvevaluator.capture import step_record
from kvevaluator.capture.capture_columns import APPENDED_COLUMNS

csv.field_size_limit(1 << 30)
KINDS = ("counts", "steps", "parts")
KEYS = ("query", "param_idx", "body", "step_idx", "part")
UNIT = 1024


def skipped(col):
    """a column that differs between two runs of the same code: the run's id, a clock"""
    return col == "run" or col.endswith("_ns")


def read(path):
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        return list(rd.fieldnames or []), list(rd)


def keyed(path, header, rows):
    keys = [k for k in KEYS if k in header]
    out = {}
    for r in rows:
        k = tuple(r[c] for c in keys)
        if k in out:
            raise SystemExit(f"{path}: two rows with the key {dict(zip(keys, k))}")
        out[k] = r
    return out


def units_range(text):
    """(lowest, highest) 1 KB units a histogram cell allows, each entry rounded up alone and at least 1"""
    lo = hi = 0
    for k, n in step_record._pairs(text):
        n = int(n)
        if k == "u":
            lo += n
            hi += n
            continue
        i = int(k)                                      # bucket 0 = size 0, bucket i = [2^(i-1), 2^i)
        lo += n * max(1, math.ceil(2 ** (i - 1) / UNIT) if i else 1)
        hi += n * max(1, math.ceil((2 ** i - 1) / UNIT) if i else 1)
    return lo, hi


def compare(old_path, new_path, added, write_units, kind):
    """(rows compared, columns compared, columns added, rows whose write units were checked, problems) of one cell file"""
    ho, ro = read(old_path)
    hn, rn = read(new_path)
    problems = []
    gone = [c for c in ho if c not in hn]
    if gone:
        problems.append(f"columns the new file lacks: {gone}")
    new_cols = [c for c in hn if c not in ho]
    extra = [c for c in new_cols if c not in added and c not in APPENDED_COLUMNS]
    if extra:
        problems.append(f"columns the new file adds that neither --added nor APPENDED_COLUMNS names: {extra}")
    cols = [c for c in ho if c in hn and not skipped(c)]
    o, n = keyed(old_path, ho, ro), keyed(new_path, hn, rn)
    for k in sorted(set(o) ^ set(n)):
        problems.append(f"row {k} in {'the old' if k in o else 'the new'} file only")
    both = sorted(set(o) & set(n))
    for k in both:
        d = [c for c in cols if o[k][c] != n[k][c]]
        if d:
            problems.append(f"row {k}: {d[0]} old {o[k][d[0]]!r}, new {n[k][d[0]]!r} ({len(d)} columns differ)")
    checked = 0
    if write_units and kind != "counts":          # the units are steps' and parts' columns
        lacking = [c for c in step_record.WRITE_UNITS if c not in hn]
        if lacking:
            problems.append(f"--write-units: the new file has no {lacking} (measured by an older binary?)")
        for k in both if not lacking else ():
            r = n[k]
            checked += 1
            for col, hist in (("put_units_1k", "put_kvpair_hist"), ("del_units_1k", "del_kvpair_hist")):
                lo, hi = units_range(r[hist])
                if not r[col].isdigit():
                    problems.append(f"row {k}: {col} {r[col]!r} is not a count (not measured?)")
                elif not lo <= int(r[col]) <= hi:
                    problems.append(f"row {k}: {col} {r[col]} outside its histogram's [{lo}, {hi}]")
    if not both or not cols:
        problems.append(f"compared {len(both)} rows and {len(cols)} columns")
    return len(both), len(cols), new_cols, checked, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--old", required=True, help="the writes dir replaced (<dir>/{counts,steps,parts}/<cell>.csv)")
    ap.add_argument("--new", required=True, help="the writes dir measured again")
    ap.add_argument("--added", default=",".join(step_record.WRITE_UNITS), help="the columns the new files may add")
    ap.add_argument("--write-units", action="store_true", help="also check put_units_1k / del_units_1k against the histograms")
    a = ap.parse_args()
    added = {c for c in a.added.split(",") if c}
    files = rows = bad = units = 0
    for kind in KINDS:
        only_new = sorted({os.path.basename(x) for x in glob.glob(os.path.join(a.new, kind, "*.csv"))}
                          - {os.path.basename(x) for x in glob.glob(os.path.join(a.old, kind, "*.csv"))})
        for name in only_new:
            print(f"FAIL {kind}/{name}: in --new only")
            bad += 1
        for old in sorted(glob.glob(os.path.join(a.old, kind, "*.csv"))):
            new = os.path.join(a.new, kind, os.path.basename(old))
            files += 1
            if not os.path.exists(new):
                print(f"FAIL {kind}/{os.path.basename(old)}: no file in --new")
                bad += 1
                continue
            n, c, new_cols, checked, problems = compare(old, new, added, a.write_units, kind)
            rows += n
            units += checked
            print(f"{'ok  ' if not problems else 'FAIL'} {kind}/{os.path.basename(old)}: {n} rows x {c} columns, adds {new_cols}"
                  + "".join(f"\n       {p}" for p in problems[:5]) + (f"\n       ... {len(problems) - 5} more" if len(problems) > 5 else ""))
            bad += bool(problems)
    if a.write_units and not units:
        print("FAIL --write-units: no row's write units were checked")
        bad += 1
    ok = files and not bad
    print(f"check_capture_columns_equal {'PASS' if ok else 'FAIL'}: {files} files, {rows} rows compared, {bad} files differ"
          + (f"; write units inside the histograms' range on {units} step and part rows" if a.write_units else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
