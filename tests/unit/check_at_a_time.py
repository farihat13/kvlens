#!/usr/bin/env python3
"""run_measure.at_a_time, the one helper that runs tasks in forked processes (search-mix, compose, predict): the
results come in the tasks' order; a worker's SystemExit is raised in the caller with its message or code, at one
process and at several (a pool alone drops it and waits forever); the stale notes a worker makes are kept in the
caller, in the tasks' order; a worker that is killed is an error, never a wait. No data, no store. A few seconds.

Run:  python3 tests/unit/check_at_a_time.py
"""
import os
import signal
import sys

from kvevaluator import stale
from kvevaluator.measure import run_measure as M


def square(x):
    return x * x


def refuse(x):
    if x == 2:
        raise SystemExit("task 2 refused: why")
    return x


def exit_code(x):
    if x == 1:
        raise SystemExit(3)
    return x


def note(x):
    stale.note("composed_stale", f"note of task {x}")
    return x


def killed(x):
    if x == 1:
        os.kill(os.getpid(), signal.SIGKILL)
    return x


def raised(fn, tasks, jobs):
    """what at_a_time raises: a SystemExit's code, else the error's type name; None when it returns"""
    try:
        M.at_a_time(fn, tasks, jobs)
    except SystemExit as e:
        return e.code
    except Exception as e:
        return type(e).__name__
    return None


def main():
    before = len(stale.notes())
    got = M.at_a_time(note, [0, 1, 2], 3)
    notes = [n["detail"] for n in stale.notes()[before:]]
    cases = [
        ("results in the tasks' order", M.at_a_time(square, [3, 1, 2, 5], 3) == [9, 1, 4, 25]),
        ("a refusal's message, one process", raised(refuse, [0, 1, 2, 3], 1) == "task 2 refused: why"),
        ("a refusal's message, three processes", raised(refuse, [0, 1, 2, 3], 3) == "task 2 refused: why"),
        ("an exit code, one process", raised(exit_code, [0, 1, 2, 3], 1) == 3),
        ("an exit code, three processes", raised(exit_code, [0, 1, 2, 3], 3) == 3),
        ("a worker's stale notes kept here, in the tasks' order",
         got == [0, 1, 2] and notes == [f"note of task {i}" for i in range(3)]),
        ("a killed worker is an error", raised(killed, [0, 1, 2], 3) == "BrokenProcessPool"),
    ]
    bad = [name for name, ok in cases if not ok]
    for name, ok in cases:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    print(f"check_at_a_time {'FAIL' if bad else 'OK'}: {len(cases) - len(bad)}/{len(cases)} cases")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
