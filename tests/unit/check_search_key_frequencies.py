#!/usr/bin/env python3
"""The mix search's result file keys a row by its workload's frequencies (results.frequencies, KEY), on toy rows, no
store:
  1. a row at other frequencies (another workload file searched into the same rows) is refused, and the file keeps the
     first row;
  2. the same row again replaces itself (one row);
  3. a file that already mixes two sets of frequencies is refused on read;
  4. a row written before the field (none) is read, and a new row beside it is added, with one warning per file;
  5. the hash: the benchmark's own i01 and its equal-share file (an id of its own) differ; item order does not change
     it;
  6. the solver is no key: the same question found with the other solver (search-mix with and without the mix cost
     cache) replaces its row.
Under a second.

Run:  python3 tests/unit/check_search_key_frequencies.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

from kvcost.cli.search import load_workload
from kvcost.layout_search import results as R
from kvcost.layout_cost import query_weighting as QW

FAILS, CASES = [], [0]


def check(what, ok, got=""):
    CASES[0] += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {what}" + (f": {got}" if got and not ok else ""))
    if not ok:
        FAILS.append(what)


def row(freq, **kw):
    """a result row with every KEY field; `freq` its frequencies (None: a row from before the field)"""
    r = {k: None for k in R.KEY}
    r.update(benchmark="ldbc", question="fewest", model="wops", weighting="sum", run_id="r1", answer=1)
    if freq is None:
        del r["frequencies"]
    else:
        r.update(frequencies=freq, workload_file=f"file-{freq}")
    r.update(kw)
    return r


def refused(fn):
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            fn()
    except SystemExit as e:
        return "REFUSED" in str(e)
    return False


def main():
    with tempfile.TemporaryDirectory() as t:
        print("1. a row at other frequencies")
        f = R.ResultFile(os.path.join(t, "a", "per-type.json"))
        f.upsert(row("h1"))
        check("refused", refused(lambda: f.upsert(row("h2", answer=2))))
        rows = f.read()
        check("the file keeps the first row", len(rows) == 1 and rows[0]["frequencies"] == "h1", rows)
        print("2. the same row again")
        f.upsert(row("h1", answer=3, run_id="r2"))
        rows = f.read()
        check("replaces itself", len(rows) == 1 and rows[0]["answer"] == 3, rows)
        print("3. a mixed file")
        m = os.path.join(t, "m.json")
        with open(m, "w") as fh:
            json.dump({"rows": [row("h1"), row("h2", model="bytes")]}, fh)
        check("refused on read", refused(lambda: R.ResultFile(m).read()))
        print("4. a row from before the field")
        old = os.path.join(t, "old.json")
        with open(old, "w") as fh:
            json.dump({"rows": [row(None), row(None, model="bytes")]}, fh)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            R.ResultFile(old).read()
            R.ResultFile(old).upsert(row("h1"))
            R.ResultFile(old).read()
        check("read, and a new row added beside it", len(R.ResultFile(old).read()) == 3)
        check("one warning for the file", err.getvalue().count("before the frequencies field") == 1, err.getvalue())
    print("5. the hash")
    own = load_workload("ldbc", 3, "i01")
    share = load_workload("ldbc", 3, str(QW.equal_share_path("i01")))
    check("the equal-share file: an id of its own, another hash", share["id"] == QW.equal_share_id(own["id"])
          and R.frequencies(own["items"]) != R.frequencies(share["items"]))
    check("item order does not change it", R.frequencies(own["items"]) == R.frequencies(own["items"][::-1]))
    print("6. the same question, the other solver")
    with tempfile.TemporaryDirectory() as t:
        f = R.ResultFile(os.path.join(t, "s", "per-type.json"))
        f.upsert(row("h1", solver="cost-cache"))
        f.upsert(row("h1", solver="no-cost-cache", run_id="r2", answer=4))
        rows = f.read()
        check("replaces its row", len(rows) == 1 and rows[0]["solver"] == "no-cost-cache" and rows[0]["answer"] == 4,
              [(r["solver"], r["answer"]) for r in rows])
    print(f"check_search_key_frequencies {'FAIL' if FAILS else 'OK'}: {CASES[0] - len(FAILS)}/{CASES[0]} cases")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
