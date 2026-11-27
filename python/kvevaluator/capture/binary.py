"""The evaluator binary: its path, its provenance (commit, dirty flag, counting versions), and the stale-binary
verdict a run checks before it measures.
"""
import json
import os
import subprocess

from kvevaluator import paths
from kvevaluator.paths import ROOT, checkout_of, cpp_bin, recorded_path

_EXP = str(ROOT)


def binary_path():
    p = str(cpp_bin())
    if not os.path.exists(p):
        raise SystemExit(f"kvevaluator binary not found at {p} -- build it: just build (or set KVEVAL_CPP_BIN)")
    return p


def binary_provenance():
    """{path, binary_git_sha, binary_dirty} of the binary a run uses, from its --version: what every run record
    names, so a result can be traced to a clean commit (a gate result counts only on a clean, pushed sha); its path as
    paths.recorded_path writes it (build/cpp/kvevaluator in the repo: the commit names the binary, not its disk)"""
    p = binary_path()
    v = json.loads(subprocess.run([p, "--version"], capture_output=True, text=True, check=True).stdout)
    return {"path": recorded_path(p), "binary_git_sha": v.get("binary_git_sha"), "binary_dirty": v.get("binary_dirty"),
            "counting": v.get("counting")}   # the counting versions a capture is keyed on (measure/counting.py)


# A gate refuses a binary whose build inputs are not HEAD's: a binary built at an older commit can fail (or pass) a check
# for reasons that are not in the tree under test (10-18: a main-tree binary 100 C++ files behind). Its build inputs are
# cpp/kvevaluator/ and every path CMakeLists' repo-paths block names (run/build_cpp.py repo_paths: the shared data files
# of inputs/contract, which the binary compiles in, among them), .md files aside. The one rule, used by run/meta-gates.sh
# (through tests/gates/check_binary_cpp_current.py) and gate_preflight.py (just cpp-diff, just write-gate). A named
# control runs anyway: --allow-stale-cpp <name> or KVEVAL_ALLOW_STALE_CPP=<name>.
STALE_CPP_ENV = "KVEVAL_ALLOW_STALE_CPP"


def build_input_paths():
    """the repo paths the binary is built from, as HEAD names them: cpp/kvevaluator plus CMakeLists' repo-paths block
    (run/build_cpp.py repo_paths, the one reader of the block)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_cpp", ROOT / "run" / "build_cpp.py")
    build_cpp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_cpp)
    return build_cpp.repo_paths(build_cpp.cmakelists("HEAD"))


def _control_name(allow, env):
    """the named control a run is: `allow` when given, else the environment variable `env` (empty: none)"""
    return allow if allow is not None else os.environ.get(env, "")


def release_commit():
    """this tree's release commit (kvevaluator.paths.release_commit, read against this module's ROOT)"""
    return paths.release_commit(ROOT)


UNKNOWN_WARNING = "commit unknown: the records made here say unknown"
NO_CITE = ("a result cited outside this tree (a full gate's record, a merge, a snapshot) needs a binary built from a clean, "
           "known commit: build it in a git clone with no local changes")


# The one rule for a binary's dirty flag, read by every check that runs (the reviewer's path) and every step that cites
# a result (ours). binary_state names the state; may_run and may_cite are the two questions asked of it.
def binary_state(prov):
    """the state of a binary's provenance (binary_git_sha, binary_dirty): "clean" (a commit with no local changes),
    "release" (this release archive of its commit, RELEASE_COMMIT), "unknown" (a copy with no .git and no release
    stamp: no commit to name), or "dirty" (local changes, another tree's stamp, or no flag)"""
    flag = str(prov.get("binary_dirty")).lower()
    sha = str(prov.get("binary_git_sha"))
    if flag == "false":
        return "clean"
    release = release_commit()
    if flag == "release" and release is not None and sha == release:
        return "release"
    if flag == "unknown" and release is None and checkout_of(ROOT) is None:
        return "unknown"
    return "dirty"


def may_run(prov):
    """may a check or a run use this binary? Clean, this release, or an unknown copy (which runs with a warning)"""
    return binary_state(prov) != "dirty"


def may_cite(prov):
    """may a result of this binary be cited (a full gate's record, a merge, a snapshot)? Only a clean, known commit"""
    return binary_state(prov) == "clean"


def binary_cpp_verdict(binary_sha, allow=None):
    """(ok, message): ok when the binary's commit has HEAD's build inputs (`git diff <sha> HEAD -- <build_input_paths>`,
    .md files aside, is empty), or when `allow` (else $KVEVAL_ALLOW_STALE_CPP) names the control it is run as; a sha git
    does not know fails. In a tree with no .git there is no HEAD to diff: a release archive's binary must name the
    archive's own commit (RELEASE_COMMIT); in a copy with no release stamp, a binary built here ("unknown") runs."""
    allow = _control_name(allow, STALE_CPP_ENV)
    release = release_commit()
    if checkout_of(ROOT) is None:
        if release is not None and binary_sha == release:
            return True, (f"binary {binary_sha[:10]}: built from this release archive of {release[:10]} (its files are "
                          f"not checked against the commit)")
        if release is None and binary_sha == "unknown":
            return True, "binary unknown: built in a copy with no .git and no release stamp (no commit to compare with)"
        why = (f"binary {str(binary_sha)[:10]} is not this release archive's commit ({release[:10]})" if release
               else f"binary {str(binary_sha)[:10]} names a commit, but this copy has no .git to compare it with: "
                    f"rebuild it here (just build)")
        if allow:
            return True, f"STALE C++ ALLOWED as the named control {allow!r}: {why}; these results do NOT count as a gate"
        return False, f"REFUSED: {why}"
    paths = build_input_paths()
    r = subprocess.run(["git", "-C", _EXP, "diff", "--name-only", binary_sha, "HEAD", "--", *paths,
                        ":(exclude,glob)**/*.md"],          # docs are not what the binary runs
                       capture_output=True, text=True)
    if r.returncode != 0:
        why = f"cannot compare the binary's commit {binary_sha} with HEAD ({r.stderr.strip()[:120]})"
    else:
        files = r.stdout.split()
        if not files:
            return True, f"binary {binary_sha[:10]}: its build inputs ({', '.join(paths)}) are HEAD's"
        why = (f"binary {binary_sha[:10]}: its build inputs differ from HEAD's in {len(files)} files "
               f"(e.g. {', '.join(files[:3])})")
    if allow:
        return True, f"STALE C++ ALLOWED as the named control {allow!r}: {why}; these results do NOT count as a gate"
    return False, (f"REFUSED: {why}. Rebuild at HEAD (just build), or run it as a named control "
                   f"(--allow-stale-cpp <name> / {STALE_CPP_ENV}=<name>)")


# A check refuses a dirty binary (built from a tree with uncommitted changes, or with no dirty flag): its result cannot
# be traced to a commit. Used by check_cpp_vs_python.py, gate_preflight.py (just cpp-diff, just write-gate),
# check_golden_counts.py and check_tally_version.py. A named control runs anyway: --allow-dirty <name> or
# KVEVAL_ALLOW_DIRTY=<name> (run/meta-gates.sh --allow-dirty sets it for its inner gates).
DIRTY_ENV = "KVEVAL_ALLOW_DIRTY"


def binary_dirty_verdict(prov, allow=None):
    """(ok, message): ok when may_run (clean; this release archive's own commit; or, in a copy with no .git and no
    release stamp, "unknown", with the warning UNKNOWN_WARNING), or when `allow` (else $KVEVAL_ALLOW_DIRTY) names the
    control it is run as"""
    allow = _control_name(allow, DIRTY_ENV)
    sha = str(prov.get("binary_git_sha"))[:10]
    state = binary_state(prov)
    if state == "clean":
        return True, f"binary {sha}: built from a clean tree"
    if state == "release":
        return True, (f"binary {sha}: built from this release archive of that commit (RELEASE_COMMIT); its files are "
                      f"not checked against the commit")
    if state == "unknown":
        return True, f"WARNING: {UNKNOWN_WARNING} (a copy with no .git and no release stamp)"
    release = release_commit()
    why = f"binary {sha} is dirty (its dirty flag: {prov.get('binary_dirty')!r})"
    if str(prov.get("binary_dirty")).lower() == "release":
        why += f"; it is not this tree's release commit ({release[:10] if release else 'none: this tree is not a release archive'})"
    elif str(prov.get("binary_dirty")).lower() == "unknown":
        why += "; this tree is a git checkout or a release archive, whose builds name their commit: rebuild (just build)"
    if allow:
        return True, f"DIRTY ALLOWED as the named control {allow!r}: {why}; these results do NOT count as a gate"
    return False, (f"REFUSED: {why}. Build a clean commit, or run it as a named control "
                   f"(--allow-dirty <name> / {DIRTY_ENV}=<name>)")


def cite_verdict(prov):
    """(ok, message): may_cite, with the state named when refused (a full gate's record, a snapshot's revision)"""
    state = binary_state(prov)
    if state == "clean":
        return True, f"binary {str(prov.get('binary_git_sha'))[:10]}: a clean, known commit"
    return False, (f"REFUSED: binary {str(prov.get('binary_git_sha'))[:10]} is {state} (its dirty flag: "
                   f"{prov.get('binary_dirty')!r}). {NO_CITE}")


# The counting versions a capture is keyed on are the binary's own (`kvevaluator --version` "counting": the seam's in
# store/counting.h, each format's pair in formats/registry.cc), read and compared by measure/counting.py.


if __name__ == "__main__":   # `python -m kvevaluator.capture.binary may-cite <binary>`: run/gate-full.sh's binary check
    import sys
    if len(sys.argv) != 3 or sys.argv[1] != "may-cite":
        sys.exit("usage: python -m kvevaluator.capture.binary may-cite <binary>")
    v = json.loads(subprocess.run([sys.argv[2], "--version"], capture_output=True, text=True, check=True).stdout)
    ok, msg = cite_verdict(v)
    print(msg)
    sys.exit(0 if ok else 1)
