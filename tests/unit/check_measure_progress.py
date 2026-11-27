#!/usr/bin/env python3
"""kvcost measure's screen output (run_measure.run_calls with a log dir): the stores run side by side, so a call's
per-query lines go to its own log only; the screen gets one progress line per kind of call (Progress: each layout's
queries done of its count, then done or FAILED, said once), each call's other lines, and a failed call's last lines.
-v prints every line. Fake calls (a Python one-liner each): no store, no binary.

Run:  python3 tests/unit/check_measure_progress.py
"""
import contextlib
import io
import os
import re
import sys
import tempfile

from kvevaluator.measure import run_measure as M

results = []


def case(name, ok, detail=""):
    results.append(ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))


def call(stage, cell, n, rc=0):
    code = (f"for i in range(1, {n} + 1): print('  {cell} q%d done %d/{n}, 0 s: 2 variants, 2 of 2 runs ok' % (i, i))\n"
            f"print('[grid_bodies] job {cell}: {n * 2} runs, 9 steps (0s)')\nraise SystemExit({rc})")
    return (stage, [sys.executable, "-c", code])


def per_query(text):
    """the per-query lines printed in `text` (a call's start line quotes the fake call's code: not one)"""
    return len(re.findall(r"q\d done \d/\d, 0 s", text))


def run(calls, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc, times = M.run_calls(calls, **kw)
    return rc, times, out.getvalue()


def main():
    p = M.Progress()
    took = [p.note("reads job Ch64k-Ch64k", "  props_chunked_adj_chunked job1b done 47/113, 8 s: 8 variants, 6 of 6 runs ok\n"),
            p.note("reads job Ch1k-Ch1k", "  props_chunked_c1k_adj_chunked_c1k job1a done 12/113, 1 s: the pick only\n"),
            p.note("reads job Co-Co", "[grid_bodies] job props_condensed_adj_condensed: 953 runs, 9 steps (3s)\n")]
    p.end("reads job Co-Co", 0)
    first = p.lines()
    p.note("reads job Ch64k-Ch64k", "  props_chunked_adj_chunked job1c done 48/113, 9 s: 8 variants, 6 of 6 runs ok\n")
    p.end("check job", 1)
    case("Progress: a per-query line is taken for its stage's state, any other line is not; one line per kind of call, "
         "the layouts' queries done of their count, a call that ended said done",
         took == [True, True, False] and first == ["reads job: Ch64k-Ch64k 47/113 · Ch1k-Ch1k 12/113 · Co-Co done"], (took, first))
    case("... a state is said once: the next line holds what moved since (a failed call: FAILED)",
         p.lines() == ["reads job: Ch64k-Ch64k 48/113", "check: job FAILED"] and p.lines() == [])

    tmp = tempfile.mkdtemp(prefix="measure-progress-")
    calls = [call("reads job Sp-Sp", "props_split_adj_split", 3), call("reads job Co-Co", "props_condensed_adj_condensed", 2)]
    rc, times, out = run(calls, log_dir=os.path.join(tmp, "quiet"), every=0.01)
    logs = {f: open(os.path.join(tmp, "quiet", f)).read() for f in sorted(os.listdir(os.path.join(tmp, "quiet")))}
    case("run_calls with a log dir: no per-query line on the screen, each call's other line there, the progress said, and "
         "every line in the call's own log",
         rc == 0 and [s for s, *_ in times] == ["reads job Sp-Sp", "reads job Co-Co"] and per_query(out) == 0
         and "[reads job Sp-Sp] [grid_bodies] job props_split_adj_split: 6 runs" in out
         and "[kvcost measure] reads job: " in out and "Sp-Sp done" in out and "Co-Co done" in out
         and sorted(logs) == ["reads-job-Co-Co.log", "reads-job-Sp-Sp.log"]
         and per_query(logs["reads-job-Sp-Sp.log"]) == 3 and "[kvcost measure] reads job Sp-Sp: -c" in logs["reads-job-Sp-Sp.log"],
         (rc, out, sorted(logs)))
    rc, _, out = run(calls, log_dir=os.path.join(tmp, "verbose"), verbose=True, every=0.01)
    case("-v: every line on the screen, as without a log dir (and in the logs)",
         rc == 0 and per_query(out) == 5 and "[kvcost measure] reads job Sp-Sp: -c" in out
         and len(os.listdir(os.path.join(tmp, "verbose"))) == 2 and per_query(run(calls)[2]) == 5, out)
    rc, times, out = run([call("reads job Sp-Sp", "props_split_adj_split", 2, rc=3), calls[1]],
                         log_dir=os.path.join(tmp, "failed"), every=0.01)
    case("a failed call: its exit code returned, no call after it started, its last lines printed (the per-query ones "
         "too) with its log's path, and FAILED in the progress",
         rc == 3 and len(times) == 1 and "reads job Sp-Sp FAILED (rc 3); its last lines" in out
         and "reads-job-Sp-Sp.log" in out and per_query(out) == 2 and "Sp-Sp FAILED" in out, (rc, out))

    n_ok = sum(results)
    print(f"check_measure_progress {'OK' if n_ok == len(results) else 'FAIL'}: {n_ok}/{len(results)} cases")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
