"""Every test is reached: a tests/**/check_*.py is named on a live line (not a comment) of a runner
(tests/fast_tests.txt, the justfile, run/** but its .md files, experiments/**/run-*.sh) or listed in tests/by_hand.txt;
any other .py under tests/ is imported by another file (a helper) or named by a runner. A by_hand.txt line must name a
file that exists and that no runner names, and each of its trigger globs must match a tracked file. With --triggered
BASE: the by_hand.txt checks whose triggers match a file changed since BASE (committed, uncommitted or untracked), to
run before that change merges.

    python tests/lint/check_tests_reached.py                    # exit 1 and one line per problem
    python tests/lint/check_tests_reached.py --triggered main   # the by-hand checks this branch's change calls for
"""
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

from kvevaluator.paths import ROOT
from repo_files import repo_files

BY_HAND = "tests/by_hand.txt"
COMMENT = re.compile(r"(^|\s)#.*$")      # a shell, justfile or list comment, to the line's end


def is_runner(rel):
    return (rel in ("tests/fast_tests.txt", "justfile") or (rel.startswith("run/") and not rel.endswith(".md"))
            or (rel.startswith("experiments/") and Path(rel).name.startswith("run-") and rel.endswith(".sh")))


def names(text, path):
    """True when text names the path whole (not as the tail of a longer name)."""
    return re.search(rf"(?<!\w){re.escape(path)}(?!\w)", text) is not None


def live_text(path):
    """The file's lines with their comments cut off."""
    return "\n".join(COMMENT.sub("", line) for line in path.read_text(errors="replace").splitlines())


def read_by_hand(path):
    """[(line number, test path, [trigger globs], inputs and cost, note)] from by_hand.txt; '#' starts a comment line."""
    rows = []
    if not path.is_file():
        return rows
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        cells = [c.strip() for c in line.split("|")]
        cells += [""] * (4 - len(cells))
        rows.append((n, cells[0], cells[1].split(), cells[2], cells[3]))
    return rows


def imported(stem, sources):
    """True when some other file imports the module `stem` (by name, or by its file name for a load by path)."""
    rx = re.compile(rf"^\s*(import|from)\s+{stem}\b|[\"']{stem}\.py[\"']", re.M)
    return any(rx.search(text) for text in sources)


def scan(root):
    """(problems, tests covered, summary)"""
    root = Path(root)
    files = repo_files(root)
    tests = [f for f in files if f.startswith("tests/") and f.endswith(".py")]
    runner_text = "\n".join(live_text(root / f) for f in files if is_runner(f))
    sources = {f: (root / f).read_text(errors="replace") for f in files if f.endswith(".py")}
    by_hand = read_by_hand(root / BY_HAND)
    listed = {row[1] for row in by_hand}
    problems, named, hand, helpers = [], 0, 0, 0
    for t in tests:
        if names(runner_text, t):
            named += 1
        elif Path(t).name.startswith("check_"):
            if t in listed:
                hand += 1
            else:
                problems.append(f"{t}:1: orphan: no runner names it (fast_tests.txt, the justfile, run/, a study's "
                                f"run-*.sh) and {BY_HAND} does not list it: add it to one")
        elif imported(Path(t).stem, [s for f, s in sources.items() if f != t]):
            helpers += 1
        else:
            problems.append(f"{t}:1: helper: no file imports it and no runner names it")
    for n, test, globs, cost, note in by_hand:
        where = f"{BY_HAND}:{n}"
        if not (root / test).is_file():
            problems.append(f"{where}: by-hand-stale: {test} is gone: remove its line")
        elif names(runner_text, test):
            problems.append(f"{where}: by-hand-stale: {test} is run by a runner now: remove its line")
        if not globs:
            problems.append(f"{where}: trigger: {test} has no trigger glob (the change that must run it)")
        for g in globs:
            if not any(fnmatch.fnmatchcase(f, g) for f in files):
                problems.append(f"{where}: trigger: {g} matches no tracked file")
        if not cost:
            problems.append(f"{where}: cost: {test} gives no inputs and cost")
    summary = (f"{len(tests)} test files: {named} named by a runner, {hand} by hand ({len(by_hand)} lines in {BY_HAND}), "
               f"{helpers} helpers imported")
    return problems, len(tests), summary


def triggered(base):
    """The by_hand.txt lines whose triggers match a file changed since base."""
    def git(*args):
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout.split()
    changed = sorted(set(git("diff", "--name-only", base)) | set(git("ls-files", "--others", "--exclude-standard")))
    hits = 0
    for n, test, globs, cost, note in read_by_hand(ROOT / BY_HAND):
        why = [f for f in changed if any(fnmatch.fnmatchcase(f, g) for g in globs)]
        if why:
            hits += 1
            print(f"{test}  ({cost})\n    triggered by: {', '.join(why[:5])}{' ...' if len(why) > 5 else ''}")
    print(f"tests-triggered: {hits} by-hand checks for {len(changed)} files changed since {base}")


def main(argv):
    if argv[:1] == ["--triggered"]:
        triggered(argv[1] if len(argv) > 1 else "main")
        return 0
    problems, n, summary = scan(ROOT)
    for p in problems:
        print(p)
    print(f"check_tests_reached {'FAIL' if problems else 'OK'}: {len(problems)} problems; {summary}")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
