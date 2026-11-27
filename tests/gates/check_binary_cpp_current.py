#!/usr/bin/env python3
"""A gate's binary has HEAD's build inputs: its commit and HEAD have the same cpp/kvevaluator/ and the same paths CMake's
repo-paths block names (binary_cpp_verdict, the one rule). Prints the verdict; exit 3 when refused.
--allow-stale-cpp <name> (or $KVEVAL_ALLOW_STALE_CPP) runs a named control anyway, said in the output. --sha tests the
rule on another commit (no binary run).

    python3 tests/gates/check_binary_cpp_current.py [--binary PATH] [--allow-stale-cpp NAME] [--sha SHA]
"""
import argparse
import json
import os
import subprocess
import sys

from kvevaluator.capture.binary import binary_cpp_verdict, binary_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--binary", default=None, help="default: binary_path() (KVEVAL_CPP_BIN, else the build's)")
    ap.add_argument("--allow-stale-cpp", default=None, metavar="NAME", help="run as this named control anyway")
    ap.add_argument("--sha", default=None, help="check this commit instead of the binary's (a test of the rule)")
    a = ap.parse_args()
    if a.sha:
        sha, where = a.sha, "--sha"
    else:
        binary = a.binary or binary_path()
        sha = json.loads(subprocess.run([binary, "--version"], capture_output=True, text=True, check=True).stdout)["binary_git_sha"]
        where = binary
    ok, msg = binary_cpp_verdict(sha, a.allow_stale_cpp)
    print(f"[binary-cpp] {where}: {msg}")
    print("BINARY CPP STALE" if not ok else "BINARY CPP STALE, RUN AS A CONTROL" if "ALLOWED" in msg else "BINARY CPP CURRENT")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
