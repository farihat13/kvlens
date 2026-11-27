"""The artifact names no other repo of ours: every file a reader gets must stand alone, so a pointer to the research
repo (its docs, its snapshots) is a dangling reference for them. The snapshots are the results archive's
(`just fetch-results`); a reason a comment needs is written where it is used.

    python tests/lint/check_no_research_repo.py
"""
import argparse
import sys
from pathlib import Path

from kvevaluator.paths import ROOT
from repo_files import is_text, repo_files

NAME = "gs-" + "research"   # in two parts, so this file does not name it


def scan(root):
    """One 'path:line: research-repo: text' line per mention, and the number of files scanned."""
    root = Path(root)
    problems, n = [], 0
    for rel in repo_files(root):
        path = root / rel
        if not is_text(path):
            continue
        n += 1
        for i, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if NAME in line:
                problems.append(f"{rel}:{i}: research-repo: {line.strip()[:120]}")
    return problems, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="the checkout to scan (default: this one)")
    a = ap.parse_args(argv)
    problems, n = scan(a.root)
    for p in problems:
        print(p)
    print(f"check_no_research_repo {'FAIL' if problems or n == 0 else 'OK'}: {len(problems)} mentions in {n} files")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
