#!/usr/bin/env python3
"""The stale-binary rule (capture/binary.py binary_cpp_verdict) covers every build input, not only cpp/kvevaluator/: a
binary whose commit differs from HEAD only in a shared data file it compiles in (inputs/contract/kv_ops.json) is
refused, and one that differs only in a .md file is not. Each case is a commit object made with git plumbing from HEAD's
tree with one blob replaced (no branch, no binary, no store); the old rule (cpp/kvevaluator/ only) is run beside it to
show the data-file case is one it passed. Seconds.

Run:  python3 tests/unit/check_binary_build_inputs.py
"""
import subprocess
import sys
import tempfile

from kvevaluator.capture import binary
from kvevaluator.paths import ROOT


def git(*args, env=None, inp=None):
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True, input=inp,
                          env=env).stdout.strip()


def commit_with(path, text):
    """a commit object: HEAD's tree with `path` holding `text` (a temporary index; HEAD and the branches stay)"""
    import os
    with tempfile.NamedTemporaryFile() as idx:
        env = {**os.environ, "GIT_INDEX_FILE": idx.name}
        git("read-tree", "HEAD", env=env)
        blob = git("hash-object", "-w", "--stdin", inp=text)
        git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
        tree = git("write-tree", env=env)
    return git("commit-tree", tree, "-p", "HEAD", "-m", "a build-input case (unreferenced)")


def old_rule_ok(sha):
    """the control rule: only cpp/kvevaluator/ is compared"""
    return not git("diff", "--name-only", sha, "HEAD", "--", "cpp/kvevaluator/", ":(exclude,glob)cpp/kvevaluator/**/*.md")


def main():
    data = "inputs/contract/kv_ops.json"
    head_text = git("show", f"HEAD:{data}")
    cases = [  # (name, path, text, the verdict must be ok)
        ("a data file the binary compiles in", data, head_text.replace('"default": "asc"', '"default": "desc"', 1), False),
        ("a .md file under cpp/kvevaluator", "cpp/kvevaluator/README.md", git("show", "HEAD:cpp/kvevaluator/README.md") + "\nx\n", True),
        ("a file outside the build inputs", "README.md", git("show", "HEAD:README.md") + "\nx\n", True),
    ]
    assert cases[0][2] != head_text, "the planted default edit did not apply"
    bad = 0
    for name, path, text, want in cases:
        sha = commit_with(path, text)
        ok, msg = binary.binary_cpp_verdict(sha, allow="")
        good = ok == want
        bad += not good
        print(f"  {'ok  ' if good else 'FAIL'} {name}: verdict {'ok' if ok else 'refused'} (want {'ok' if want else 'refused'})"
              f"; the cpp/kvevaluator-only rule: {'ok' if old_rule_ok(sha) else 'refused'}")
    caught_new = bad == 0 and old_rule_ok(commit_with(*cases[0][1:3]))   # the data-file case passed the old rule
    print(f"  {'ok  ' if caught_new else 'FAIL'} the data-file case is one the cpp/kvevaluator-only rule let through")
    print(f"binary build inputs {'PASS' if not bad and caught_new else 'FAIL'}: {len(cases)} cases")
    return 0 if not bad and caught_new else 1


if __name__ == "__main__":
    sys.exit(main())
