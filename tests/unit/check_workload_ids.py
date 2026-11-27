#!/usr/bin/env python3
"""An id names one workload file: per dataset and scale, every tracked workload file's id and name is its own, so
kvevaluator.capture.workload_file.resolve (the one way a --workload id becomes a file) finds exactly that file; an
equal-share file's id is query_weighting.equal_share_id of the workload it weights. The control: two files that share an
id are refused by name. No data, no store, no binary.

    python3 tests/unit/check_workload_ids.py
"""
import collections
import os
import sys

from kvevaluator import datasets
from kvevaluator.capture import workload_file as WF
from kvcost.layout_cost import query_weighting as QW


def main():
    fails, pairs, files = [], 0, 0
    for ds in datasets.names():
        for sc in datasets.get(ds).scales:
            have = WF.files(ds, sc)
            pairs += 1
            files += len(have)
            for key in ("id", "name"):
                dup = [k for k, n in collections.Counter(w.get(key) for _p, w in have if w.get(key)).items() if n > 1]
                if dup:
                    fails.append(f"{ds} {sc}: {key} {dup} on more than one file")
            for p, w in have:
                got = WF.resolve(w["id"], ds, sc)
                if os.path.realpath(got) != os.path.realpath(p):
                    fails.append(f"{ds} {sc}: {w['id']} resolves to {got}, not {p}")
                if os.path.basename(os.path.dirname(p)) == "equal-share":
                    base = os.path.basename(p)[:-5]
                    if w["id"] != QW.equal_share_id(base):
                        fails.append(f"{p}: id {w['id']!r}, not {QW.equal_share_id(base)!r}")
    real = WF.files
    try:   # the control: a second file with an id already taken
        WF.files = lambda dataset=None, scale=None: [("a.json", {"id": "x", "items": [1]}),
                                                     ("b.json", {"id": "x", "items": [1]})]
        try:
            WF.resolve("x", "ldbc", "3")
            fails.append("the control: two files with id x resolved, not refused")
        except SystemExit as e:
            if "a.json" not in str(e) or "b.json" not in str(e):
                fails.append(f"the control's refusal names neither file: {e}")
    finally:
        WF.files = real
    for f in fails:
        print(f"FAIL {f}")
    print(f"check_workload_ids: {files} workload files at {pairs} (dataset, scale) pairs, 1 control; {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
