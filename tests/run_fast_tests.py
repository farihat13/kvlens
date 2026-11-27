"""The store-free tests (tests/fast_tests.txt): each runs with no data and no stores, only the built binary. One line per
test, a path and its arguments (pass / FAIL, seconds), then the count; exit 1 on any failure or an empty list.

    python tests/run_fast_tests.py               # `just tests-fast` (part of `just check`)
    python tests/run_fast_tests.py --no-binary   # `just tests-fast-no-binary` (CI): skips the tests tagged `binary:`
    python tests/run_fast_tests.py --check-tags  # runs every test with no binary: the ones that fail must be the tagged ones

A test tagged `git:` reads the repo's history (git show, git read-tree). With no .git (a release archive), or a history
without the commits the code reads plan files at (generate.V1_TREE, pick_record.SELECTOR_TREE), it is skipped, by name.
In a tree with no .git, --check-tags checks the `git:` tags too: there the failing set must be both tags'.
"""
import argparse
import os
import subprocess
import sys
import time

from kvevaluator.paths import ROOT

LIST = ROOT / "tests" / "fast_tests.txt"
TAG = "binary:"   # a test that runs the C++ binary
GIT_TAG = "git:"  # a test that reads the repo's history
MISSING = "kvevaluator binary not found"   # what kvevaluator.paths says when there is none


def tests():
    """(test, its tags) per line: the test is its path and arguments, the tags the set of `binary:` / `git:` before it"""
    out = []
    for line in LIST.read_text().splitlines():
        t, tags = line.split("#")[0].strip(), set()
        while t.startswith((TAG, GIT_TAG)):
            tag = TAG if t.startswith(TAG) else GIT_TAG
            tags.add(tag)
            t = t[len(tag):].strip()
        if t:
            out.append((t, tags))
    return out


def has_git():
    """True when the root has a .git (a release archive or a copy has none); a .git that git cannot read (no git on
    PATH, a "dubious ownership" refusal) fails here, rather than skip the git: tests as if there were no history"""
    if not (ROOT / ".git").exists():
        return False
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT, capture_output=True, text=True)
    except FileNotFoundError:
        raise SystemExit("run_fast_tests: this tree has a .git, but git is not on PATH: the git: tests need it")
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise SystemExit(f"run_fast_tests: this tree has a .git, but git cannot read it: {r.stderr.strip()[:200]}")
    from kvevaluator.plan_choice.generate import V1_TREE
    from kvevaluator.plan_choice.pick_record import SELECTOR_TREE
    return all(subprocess.run(["git", "cat-file", "-e", f"{c}^{{commit}}"], cwd=ROOT, capture_output=True).returncode == 0
               for c in (V1_TREE, SELECTOR_TREE))      # the commits the code reads plan files at


def run(t, env=None):
    """(passed, seconds, the output's last lines)"""
    s = time.time()
    path, *args = t.split()
    r = subprocess.run([sys.executable, str(ROOT / path), *args], cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, env=env)
    return r.returncode == 0, time.time() - s, (r.stdout + r.stderr).strip().splitlines()[-5:]


def check_tags(listed, git):
    """every test run with no binary: the set that fails must equal the set tagged `binary:` (and, with no .git, the
    set tagged `binary:` or `git:`)"""
    env = dict(os.environ, KVEVAL_CPP_BIN=str(ROOT / "build" / "no-binary-here" / "kvevaluator"))
    failed = set()
    for t, _ in listed:
        ok, secs, _ = run(t, env)
        print(f"{'pass' if ok else 'FAIL'} {secs:6.1f} s  {t}", flush=True)
        if not ok:
            failed.add(t)
    want = {TAG} if git else {TAG, GIT_TAG}
    tagged = {t for t, tags in listed if tags & want}
    names = " or ".join(f"`{w}`" for w in sorted(want))
    for t in sorted(failed - tagged):
        print(f"  untagged, fails with no binary{'' if git else ' and no .git'}: {t} (tag it {names})")
    for t in sorted(tagged - failed):
        print(f"  tagged, passes with no binary{'' if git else ' and no .git'}: {t} (drop its tag)")
    ok = bool(listed) and failed == tagged
    print(f"tests-fast --check-tags {'OK' if ok else 'FAIL'}: {len(listed)} tests, {len(failed)} fail with no binary"
          f"{'' if git else ' and no .git'}, {len(tagged)} tagged {names}"
          + ("" if not git else f" (the `{GIT_TAG}` tags are checked in a tree with no .git: git archive HEAD)"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-binary", action="store_true", help="skip the tests tagged `binary:` (CI builds no binary)")
    ap.add_argument("--check-tags", action="store_true", help="check the tags against a run with no binary")
    a = ap.parse_args()
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT, capture_output=True, text=True)
    if shallow.stdout.strip() == "true":   # some tests read plan files at older commits (git show)
        print("tests-fast FAIL: a shallow clone; the tests need the full history (git fetch --unshallow; in CI, "
              "actions/checkout's fetch-depth: 0)")
        return 1
    listed, git = tests(), has_git()
    if a.check_tags:
        return check_tags(listed, git)
    skip_binary = [t for t, tags in listed if TAG in tags and a.no_binary]
    skip_git = [t for t, tags in listed if GIT_TAG in tags and not git and t not in skip_binary]
    todo = [t for t, _ in listed if t not in skip_binary and t not in skip_git]
    bad, t0 = [], time.time()
    for t in todo:
        ok, secs, tail = run(t)
        print(f"{'pass' if ok else 'FAIL'} {secs:6.1f} s  {t}", flush=True)
        if not ok:
            bad.append(t)
            if a.no_binary and any(MISSING in line for line in tail):
                tail.append(f"it needs the C++ binary: tag it `{TAG}` in tests/fast_tests.txt")
            print("     " + "\n     ".join(tail))
    what = f"{len(todo) - len(bad)}/{len(todo)} passed"
    short = lambda ts: ", ".join(s.split()[0].rsplit("/", 1)[-1] for s in ts)
    if a.no_binary:
        what = (f"ran {len(todo)}, {len(todo) - len(bad)} passed; skipped {len(skip_binary)} (need the C++ binary): "
                f"{short(skip_binary)}")
    if skip_git:
        what += f"; skipped {len(skip_git)} (they read the repo's history, which this tree lacks): {short(skip_git)}"
    print(f"tests-fast {'FAIL' if bad or not todo else 'OK'}: {what} ({time.time() - t0:.0f} s)"
          + (f"; failed: {bad}" if bad else ""))
    return 1 if bad or not todo else 0


if __name__ == "__main__":
    sys.exit(main())
