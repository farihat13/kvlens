#!/usr/bin/env python3
"""A cell interrupted mid-run resumes from its journal (grid_bodies.Journal, measure_runs). Live, at sf0.1, one cell,
ic8 and is7 on params 1-2:

  1. a whole run of the cell (the reference: its runs and its number of runner calls);
  2. the same cell in a child process whose runner stops it after half the calls (a kill): the journal holds the runs
     finished so far; one of them, a measured one, is then rewritten as a not-run row (an SF30 drop);
  3. the cell again: every journaled ok run is taken from the journal, not re-run (the runner is called for the rest
     and for the not-run row, now admitted); the capture's runs equal the reference's; no run appears twice in the
     journal (besides the not-run row's replacement); the capture says resumed 1, and the driver's "taken from the
     journal" count is the number of journaled ok runs.

    python3 tests/gates/meta/check_journal_resume.py [--cell props_split_adj_condensed]
"""
import argparse
import contextlib
import csv
import io
import json
import os
import re
import shutil
import sys
import tempfile

from kvevaluator.measure import grid_bodies as GV
from kvevaluator.paths import OUTPUT_DIR
from kvevaluator.capture import capture_layout
from kvevaluator.output_tree import Capture, capture_in

SF, QUERIES, PARAMS = "0.1", ["ic8", "is7"], [1, 2]
SAME = ("query", "param_idx", "body", "ok", "S", "G", "N", "result_hash", "measured_as")   # what must match


class CountingRunner(GV.ServeRunner):
    """the real runner, counting its calls; with `stop_after`, a kill after that many calls"""
    calls, stop_after = 0, None

    def call(self, req):
        if CountingRunner.stop_after is not None and CountingRunner.calls >= CountingRunner.stop_after:
            os._exit(3)                            # a kill: no cleanup, no capture written
        CountingRunner.calls += 1
        return super().call(req)


def runs_of(out, cell):
    return list(capture_layout.reader(open(Capture(out).path_of("counts", cell))))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", default="props_split_adj_condensed")
    cell = ap.parse_args().cell
    binary = GV.binary_path()
    inputs = GV.inputs_fingerprint("interactive", SF, len(PARAMS), PARAMS)
    GV.RUNNERS["count"] = CountingRunner
    tmp = tempfile.mkdtemp(dir=str(OUTPUT_DIR), prefix="check_journal_resume_")
    ref_out, out = (capture_in(os.path.join(tmp, t), "interactive").path for t in ("whole", "resumed"))
    ok = True

    def check(what, good, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{'PASS' if good else 'FAIL'} {what}{': ' + detail if detail else ''}")

    def measure(o, stop_after=None):
        CountingRunner.calls, CountingRunner.stop_after = 0, stop_after
        GV.measure_cell(cell, "interactive", SF, len(PARAMS), 4, 600, binary, o, inputs, GV.GRID, "count", (QUERIES, PARAMS))
        return CountingRunner.calls

    try:
        ref_calls = measure(ref_out)
        ref = runs_of(ref_out, cell)
        check("1 the whole run", ref_calls >= 4 and ref, f"{ref_calls} runner calls, {len(ref)} run rows")

        half = ref_calls // 2
        sys.stdout.flush()                         # the child must not print the parent's buffer again
        pid = os.fork()
        if pid == 0:                               # the child: killed after `half` calls
            measure(out, stop_after=half)
            os._exit(0)
        _pid, status = os.waitpid(pid, 0)
        jdir = GV.journal_dir(out)
        jruns = os.path.join(jdir, "runs", f"{cell}.csv")   # the journal's own layout (grid_bodies.Journal), not a capture's
        journaled = list(capture_layout.reader(open(jruns)))
        check("2 the killed run leaves its finished runs in the journal and no capture",
              os.WEXITSTATUS(status) == 3 and journaled and not os.path.exists(Capture(out).path_of("counts", cell)),
              f"exit {os.WEXITSTATUS(status)}, {len(journaled)} journaled runs")
        measured = [r for r in journaled if r["ok"] == "1" and not r["measured_as"]]
        planted = measured[-1]
        for r in journaled:                        # the last measured run becomes a not-run row (an SF30 drop)
            if r is planted:
                r.update({"ok": "0", "error": GV.NOT_RUN + " planted", "S": "", "G": "", "N": ""})
        with open(jruns, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(journaled[0]))
            w.writeheader(); w.writerows(journaled)
        kept_want = sum(r["ok"] == "1" for r in journaled)

        seen = {}
        real_finish = GV.Journal.finish

        def finish(self):                          # read the journal before it is removed
            seen["rows"] = list(capture_layout.reader(open(self.paths["runs"])))
            real_finish(self)
        GV.Journal.finish = finish
        log = io.StringIO()
        try:
            with contextlib.redirect_stdout(log):
                calls = measure(out)
        finally:
            GV.Journal.finish = real_finish
        sys.stdout.write(log.getvalue())
        got = runs_of(out, cell)
        # the runner's calls: the reference's, less the journaled measured runs, plus the planted one run again
        want_calls = ref_calls - len(measured) + 1
        check("3a the journaled runs are not run again; the not-run row now admitted runs", calls == want_calls,
              f"{calls} runner calls (want {want_calls} = {ref_calls} - {len(measured)} journaled measured + 1)")
        prow = next(r for r in got if (r["query"], r["param_idx"], r["body"]) ==
                    (planted["query"], planted["param_idx"], planted["body"]))
        check("3b the planted not-run row is measured", prow["ok"] == "1" and not prow["error"],
              f"{planted['query']} p{planted['param_idx']} {planted['body']}")
        proj = lambda rows: sorted(tuple(r.get(k, "") for k in SAME) for r in rows)
        check("3c the capture's runs equal the whole run's", proj(got) == proj(ref), f"{len(got)} vs {len(ref)} rows")
        keys = [(r["query"], r["param_idx"], r["body"]) for r in seen.get("rows", [])]
        pkey = (planted["query"], planted["param_idx"], planted["body"])
        dups = sorted({k for k in keys if keys.count(k) > 1 and k != pkey})
        check("3d no run is twice in the journal", seen.get("rows") and not dups, f"{len(keys)} rows, duplicates {dups[:3]}")
        rec = json.load(open(Capture(out).record(cell)))
        m = re.findall(r"\((\d+) runs so far taken from the journal", log.getvalue())
        kept = int(m[-1]) if m else 0
        check("3e resumed 1; the journal count is the journaled ok runs", rec.get("resumed") == 1 and kept == kept_want,
              f"resumed {rec.get('resumed')}, taken from the journal {kept} (want {kept_want})")
        print(f"check_journal_resume: {'OK' if ok else 'FAILED'} ({cell} sf{SF}, {len(QUERIES)} queries x "
              f"{len(PARAMS)} params, {len(ref)} run rows, {len(journaled)} journaled)")
    finally:
        GV.RUNNERS.pop("count", None)
        for o in (ref_out, out):
            shutil.rmtree(GV.journal_dir(o), ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
