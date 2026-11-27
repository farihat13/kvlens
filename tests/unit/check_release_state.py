"""The binary's state, one rule (capture/binary.py binary_state) read by two paths. The reviewer's path (the dirty and
stale-C++ verdicts) runs a clean binary, this release archive's own ("release", RELEASE_COMMIT), and, in a plain copy
with no .git and no release stamp, an "unknown" one with a warning. The cite path (cite_verdict: a full gate's record,
a snapshot) takes a clean, known commit only. Another commit's "release", a "release" or "unknown" flag in a git
checkout, and a dirty binary are refused by both. Fake trees in a temp dir; no git, no binary.

    python tests/unit/check_release_state.py
"""
import sys
import tempfile
from pathlib import Path

from kvevaluator.capture import binary

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
failures = []
n = 0


def check(name, got, want):
    global n
    n += 1
    print(f"{'ok  ' if got == want else 'FAIL'} {name}")
    if got != want:
        failures.append(name)


def tree(tmp, name, release_text=None, git=False):
    """a fake repo root: with a .git dir (a checkout) or without, and RELEASE_COMMIT holding release_text"""
    root = Path(tmp) / name
    root.mkdir()
    if git:
        (root / ".git").mkdir()
    if release_text is not None:
        (root / "RELEASE_COMMIT").write_text(release_text + "\n")
    return root


def dirty_ok(root, flag, sha=SHA):
    binary.ROOT = root
    return binary.binary_dirty_verdict({"binary_git_sha": sha, "binary_dirty": flag}, allow="")[0]


def stale_ok(root, sha):
    binary.ROOT = root
    return binary.binary_cpp_verdict(sha, allow="")[0]


with tempfile.TemporaryDirectory() as tmp:
    release = tree(tmp, "release", SHA)
    placeholder = tree(tmp, "placeholder", "$Format:%H$")
    plain = tree(tmp, "plain")
    checkout = tree(tmp, "checkout", SHA, git=True)
    check("release archive: the release commit is read", (setattr(binary, "ROOT", release), binary.release_commit())[1], SHA)
    check("a checkout has no release commit", (setattr(binary, "ROOT", checkout), binary.release_commit())[1], None)
    check("release archive, binary 'release' of its commit: passes", dirty_ok(release, "release"), True)
    check("release archive, binary 'release' of another commit: refused", dirty_ok(release, "release", OTHER), False)
    check("release archive, binary 'unknown': refused", dirty_ok(release, "unknown"), False)
    check("unfilled placeholder, binary 'release': refused", dirty_ok(placeholder, "release"), False)
    check("plain copy, binary 'unknown': runs", dirty_ok(plain, "unknown", "unknown"), True)
    check("unfilled placeholder (a tar of a clone), binary 'unknown': runs", dirty_ok(placeholder, "unknown", "unknown"), True)
    check("checkout, binary 'unknown': refused", dirty_ok(checkout, "unknown", "unknown"), False)
    check("checkout, binary 'release': refused", dirty_ok(checkout, "release"), False)
    check("checkout, binary clean: passes", dirty_ok(checkout, False), True)
    check("checkout, binary dirty: refused", dirty_ok(checkout, True), False)
    check("plain copy, binary dirty: refused", dirty_ok(plain, True), False)
    check("stale C++ rule, release archive of the binary's commit: passes", stale_ok(release, SHA), True)
    check("stale C++ rule, release archive of another commit: refused", stale_ok(release, OTHER), False)
    check("stale C++ rule, plain copy, binary built here ('unknown'): runs", stale_ok(plain, "unknown"), True)
    check("stale C++ rule, plain copy, a binary naming a commit: refused", stale_ok(plain, SHA), False)
    binary.ROOT = plain
    msg = binary.binary_dirty_verdict({"binary_git_sha": "unknown", "binary_dirty": "unknown"}, allow="")[1]
    check("the 'unknown' run prints the warning", binary.UNKNOWN_WARNING in msg, True)
    # the cite path: a clean, known commit only
    for root, flag, sha, want, name in [
            (checkout, False, SHA, True, "checkout, clean"),
            (checkout, True, SHA, False, "checkout, dirty"),
            (checkout, "release", SHA, False, "checkout, 'release'"),
            (release, "release", SHA, False, "release archive, its own binary"),
            (plain, "unknown", "unknown", False, "plain copy, 'unknown'")]:
        binary.ROOT = root
        prov = {"binary_git_sha": sha, "binary_dirty": flag}
        check(f"may_cite: {name}: {'cited' if want else 'refused'}", binary.may_cite(prov), want)
        check(f"cite_verdict agrees with may_cite: {name}", binary.cite_verdict(prov)[0], want)
    binary.ROOT = plain
    msg = binary.cite_verdict({"binary_git_sha": "unknown", "binary_dirty": "unknown"})[1]
    check("the cite refusal names the state and what to do", "unknown" in msg and "clean, known commit" in msg, True)
    binary.ROOT = release
    check("may_run: this release's binary", binary.may_run({"binary_git_sha": SHA, "binary_dirty": "release"}), True)
    check("may_run: another release", binary.may_run({"binary_git_sha": OTHER, "binary_dirty": "release"}), False)

    # what a run records as its code state, with no .git (paths.ROOT read at call time)
    from kvevaluator import paths
    from kvevaluator.measure.report import git_provenance
    from kvevaluator.plan_choice.candidates import plan_tree
    real = paths.ROOT
    try:
        paths.ROOT = plain
        check("a write capture's code state in a plain copy: unknown, never clean", git_provenance(), ("unknown", "unknown"))
        check("the plan files' commit in a plain copy: unknown", plan_tree(), {"git_sha": "unknown", "git_dirty": "unknown"})
        paths.ROOT = release
        check("a write capture's code state in a release archive: its commit, release", git_provenance(), (SHA[:7], "release"))
    finally:
        paths.ROOT = real

print(f"check_release_state {'FAIL' if failures or n == 0 else 'OK'}: {n} cases, {len(failures)} failed")
sys.exit(1 if failures or n == 0 else 0)
