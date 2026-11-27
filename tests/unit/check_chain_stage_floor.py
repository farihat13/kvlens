#!/usr/bin/env python3
"""run/chain-stages.sh's `stage -c FLOOR REGEX`: a verdict line whose count is at or above the floor passes (a check
that gained a case), one below it fails, and so does a log with no verdict line; `-v` is unchanged. Each case runs one
stage in bash with set -uo pipefail, as the drivers do. Store-free.

    python3 tests/unit/check_chain_stage_floor.py
"""
import subprocess
import sys
import tempfile

from kvevaluator.paths import ROOT

CASES = [  # (label, the stage's flags, what its command prints, the rc the stage must give)
    ("1 the count at the floor: pass", ["-c", "9"], "9 cases, 0 failed", 0),
    ("2 a check that gained cases: pass", ["-c", "9"], "12 cases, 0 failed", 0),
    ("3 fewer cases than the floor: fail", ["-c", "9"], "8 cases, 0 failed", 97),
    ("4 zero cases: fail", ["-c", "9"], "0 cases, 0 failed", 97),
    ("5 no verdict line: fail", ["-c", "9"], "3 cases, 1 failed", 97),
    ("6 -v as before: pass", ["-v"], "9 cases, 0 failed", 0),
    ("7 -v with no verdict line: fail", ["-v"], "nothing", 97),
]
REGEX = "^[0-9]+ cases, 0 failed$"


def run(flags, out):
    with tempfile.TemporaryDirectory() as logs:
        script = ("set -uo pipefail; CHAIN_TAG=T; CHAIN_LOGS=\"$1\"; CHAIN_KEEP_GOING=1; source run/chain-stages.sh; "
                  "stage \"${@:3}\" \"s\" printf '%s\\n' \"$2\"; cut -f2 \"$CHAIN_LOGS/stages.tsv\"")
        r = subprocess.run(["bash", "-c", script, "bash", logs, out, *flags, REGEX], cwd=ROOT, capture_output=True,
                           text=True)
        return int(r.stdout.strip().splitlines()[-1]), r.stdout


def main():
    fails = 0
    for label, flags, out, want in CASES:
        got, text = run(flags, out)
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'} {label} (rc {got}){'' if ok else ': ' + text.strip()}")
        fails += not ok
    print(f"check_chain_stage_floor: {len(CASES)} cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
