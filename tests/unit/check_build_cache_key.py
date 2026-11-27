"""The C++ build cache's key and locations (run/build_cpp.py, kvevaluator.paths.build_cache_dir), with no build: the
archived paths are cpp/kvevaluator plus CMake's repo paths block; a path's tree id moves the key exactly when git says
the path changed between two commits; the build settings, the compiler's environment and a dirty archived path are seen; the source manifest sees one byte and ignores
the build dirs; a location in the build cache passes the other-checkout refusal, and another one in the main clone does
not.

    python tests/unit/check_build_cache_key.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from kvevaluator import paths
from kvevaluator.datasets import schema_path
from kvevaluator.paths import ROOT

_spec = importlib.util.spec_from_file_location("build_cpp", ROOT / "run" / "build_cpp.py")
B = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(B)


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True).stdout.strip()


def main():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"  {'ok  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail and not ok else ""))

    named = B.repo_paths(B.cmakelists("HEAD"))
    check("the archived paths: cpp/kvevaluator and the repo paths block",
          named == ["cpp/kvevaluator", "inputs/schemas/ldbc", "inputs/specs", "inputs/moved_paths.json",
                    "inputs/contract"],
          str(named))
    check("each archived path exists at HEAD", all(git("cat-file", "-t", f"HEAD:{p}") for p in named))

    # over the last commits: a path's id differs exactly when `git diff` names a change under it
    # each commit against its FIRST parent (rev-list neighbours are not parent and child across a merge)
    pairs = wrong = 0
    for new in git("rev-list", "--max-count=8", "HEAD").split():
        old = git("rev-parse", "--verify", "--quiet", f"{new}^1")
        if not old:
            continue                                                      # a root commit has no parent
        # a path missing at either side (the block named it later, or a side branch predates it) is skipped
        here = [p for p in named if git("cat-file", "-t", f"{old}:{p}") and git("cat-file", "-t", f"{new}:{p}")]
        a, b = B.tree_ids(new, here), B.tree_ids(old, here)
        for p in here:
            md_aside = [":(exclude,glob)cpp/kvevaluator/**/*.md"] if p == B.CPP else []
            changed = bool(git("diff", "--name-only", old, new, "--", p, *md_aside))
            pairs += 1
            wrong += (a[p] != b[p]) != changed
    check(f"a path's tree id moves exactly with git's diff ({pairs} (commit pair, path) cases)", pairs > 0 and wrong == 0,
          f"{wrong} wrong")

    ids = B.tree_ids("HEAD", named)
    # a tree that differs from HEAD only in a README under cpp/kvevaluator keeps the key; one .cc byte moves it
    def tree_with(path, text):
        blob = subprocess.run(["git", "-C", str(ROOT), "hash-object", "-w", "--stdin"], input=text,
                              capture_output=True, text=True).stdout.strip()
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(d, "index")}
            for args in (["read-tree", "HEAD"], ["update-index", "--add", "--cacheinfo", f"100644,{blob},{path}"]):
                subprocess.run(["git", "-C", str(ROOT), *args], env=env, check=True)
            return subprocess.run(["git", "-C", str(ROOT), "write-tree"], env=env, capture_output=True,
                                  text=True, check=True).stdout.strip()
    md_tree = tree_with("cpp/kvevaluator/README.md", "a changed README\n")
    cc_tree = tree_with("cpp/kvevaluator/src/main.cc", git("show", "HEAD:cpp/kvevaluator/src/main.cc") + "\n// x\n")
    check("a README edit under cpp/kvevaluator keeps the key", B.tree_ids(md_tree, named) == ids)
    # the LDBC Python package is not archived: an edit there keeps the key; an edit to a schema JSON C++ reads moves it
    py_tree = tree_with("python/kvevaluator/datasets/ldbc/params.py",
                        git("show", "HEAD:python/kvevaluator/datasets/ldbc/params.py") + "\n# x\n")
    js = os.path.relpath(schema_path("ldbc", "label_only"), ROOT)
    js_tree = tree_with(js, git("show", f"HEAD:{js}") + "\n\n")   # git() strips: a lone newline would be no change
    check("an edit to the LDBC Python package keeps the key", B.tree_ids(py_tree, named) == ids)
    check("an edit to a schema JSON moves the key", B.tree_ids(js_tree, named)[os.path.dirname(js)] != ids[os.path.dirname(js)])
    check("a .cc edit moves the key", B.tree_ids(cc_tree, named)[B.CPP] != ids[B.CPP])
    tc = B.toolchain(B.cmakelists("HEAD"))
    k0 = B.entry_key(ids, [], tc)[1]
    check("the key is stable", k0 == B.entry_key(dict(ids), [], B.toolchain(B.cmakelists("HEAD")))[1])
    check("a build setting moves the key", k0 != B.entry_key(ids, ["-DEVALUATOR_ALLOCATOR=glibc"], tc)[1])
    check("a source tree moves the key", k0 != B.entry_key({**ids, "inputs/specs": "0" * 40}, [], tc)[1])
    old = os.environ.get("CXXFLAGS")
    os.environ["CXXFLAGS"] = "-O0"
    try:
        check("a compiler environment variable moves the key", k0 != B.entry_key(ids, [], B.toolchain(B.cmakelists("HEAD")))[1])
    finally:
        os.environ.pop("CXXFLAGS") if old is None else os.environ.__setitem__("CXXFLAGS", old)
    check("`-D X=Y` and `-DX=Y` are one setting", B.normalize(["-D", "A=1", "-DB=2"]) == ["-DA=1", "-DB=2"])
    check("the RocksDB release is read with or without a :TYPE",
          B.rocksdb_release(["-DKVEVAL_ROCKSDB:STRING=8.0.0"]) == "8.0.0" and B.rocksdb_release(["-DX=1"]) is None)
    check("a clean tree is not dirty", not B.worktree_dirty(named) or bool(git("status", "--porcelain", "--", *named)))

    with tempfile.TemporaryDirectory() as t:
        src = Path(t)
        (src / "cpp").mkdir()
        (src / "cpp" / "a.cc").write_text("int a;\n")
        m0 = B.source_manifest(src)
        (src / "build" / "cpp").mkdir(parents=True)
        (src / "build" / "cpp" / "a.o").write_text("object")
        check("the manifest ignores the build dirs", B.source_manifest(src) == m0)
        (src / "cpp" / "a.cc").write_text("int b;\n")
        check("the manifest sees one changed byte", B.source_manifest(src) != m0)

    cache = paths.build_cache_dir()
    check("the cache is <main clone>/build/cache", cache is not None and cache.parts[-2:] == ("build", "cache")
          and (cache.parent.parent / ".git").is_dir(), str(cache))
    main_clone = cache.parent.parent
    if main_clone != ROOT:   # a worktree: the main clone is another checkout
        env = {"KVEVAL_CPP_BIN": str(cache / "by-tree" / "x" / "kvevaluator")}
        try:
            paths._path("KVEVAL_CPP_BIN", None, env, {})
            check("a binary in the build cache passes the other-checkout refusal", True)
        except paths.PathsError as e:
            check("a binary in the build cache passes the other-checkout refusal", False, str(e))
        try:
            paths._path("KVEVAL_CPP_BIN", None, {"KVEVAL_CPP_BIN": str(main_clone / "build" / "cpp" / "kvevaluator")}, {})
            check("the main clone's own build dir is still refused", False, "accepted")
        except paths.PathsError:
            check("the main clone's own build dir is still refused", True)
        try:
            paths._path("KVEVAL_OUTPUT_DIR", None, {"KVEVAL_OUTPUT_DIR": str(cache / "by-tree" / "x")}, {})
            check("the build cache is refused as an output dir", False, "accepted")
        except paths.PathsError:
            check("the build cache is refused as an output dir", True)
    else:
        print("  (the main clone itself: the other-checkout cases need a worktree, not run)")

    print(f"check_build_cache_key: {sum(results)}/{len(results)} OK")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
