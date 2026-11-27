"""ArangoDB's and Dgraph's scripts/apply-patch.sh can run twice: in a temp copy with a tiny git source (one tracked file
the patch changes, one new file it creates) and an untracked build/ dir, a second run after the first (a failed build
leaves the patched tree) restores and reapplies cleanly, never prompts, and keeps build/. The control: the same scripts
with the untracked-file clean and the --forward --batch flags planted out fail the second run.

    python tests/unit/check_engine_patch_rerun.py
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kvevaluator.paths import ROOT

E = ROOT / "engines"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]


def patch_text(prefix):
    """a patch over <prefix>/: changes the tracked f.txt and creates new.cpp, as create-patch.sh's `git diff` writes it"""
    return (f"diff --git a/{prefix}/f.txt b/{prefix}/f.txt\n--- a/{prefix}/f.txt\n+++ b/{prefix}/f.txt\n"
            "@@ -1 +1 @@\n-original\n+patched\n"
            f"diff --git a/{prefix}/new.cpp b/{prefix}/new.cpp\nnew file mode 100644\n--- /dev/null\n"
            f"+++ b/{prefix}/new.cpp\n@@ -0,0 +1 @@\n+// created by the patch\n")


def make_source(src):
    """a git checkout with one tracked file, plus an untracked build/ dir with an object file (not gitignored: the worst
    case, a source tree whose own .gitignore does not cover build/)"""
    src.mkdir(parents=True)
    (src / "f.txt").write_text("original\n")
    subprocess.run([*GIT, "init", "-q"], cwd=src, check=True)
    subprocess.run([*GIT, "add", "f.txt"], cwd=src, check=True)
    subprocess.run([*GIT, "commit", "-qm", "base"], cwd=src, check=True)
    (src / "build").mkdir()
    (src / "build" / "obj.o").write_text("kept\n")


def run(script, cwd, *args):
    """the script with no terminal on stdin, so a prompt fails or times out instead of waiting"""
    try:
        p = subprocess.run(["bash", str(script), *args], cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return -1, "timed out (a prompt?)"


def tree_ok(src, keep_build):
    """the source is patched exactly once: f.txt patched, new.cpp as the patch wrote it, no .rej / .orig, build/ kept"""
    bad = []
    if (src / "f.txt").read_text() != "patched\n":
        bad.append(f"f.txt is {(src / 'f.txt').read_text()!r}")
    if not (src / "new.cpp").is_file() or (src / "new.cpp").read_text() != "// created by the patch\n":
        bad.append("new.cpp missing or wrong")
    left = sorted(p.name for p in src.rglob("*") if p.suffix in (".rej", ".orig"))
    if left:
        bad.append(f"left over: {left}")
    if keep_build and not (src / "build" / "obj.o").is_file():
        bad.append("build/obj.o was deleted")
    return bad


def case_arangodb(d, script_text):
    root = Path(d) / "engines" / "arangodb"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "apply-patch.sh").write_text(script_text)
    shutil.copy(E / "arangodb" / ".env", root / ".env")      # ARANGO_SRCDIR = <root>/arangodb-3.12.8
    src = root / "arangodb-3.12.8"
    make_source(src)
    pf = Path(d) / "arangodb.patch"
    pf.write_text(patch_text("arangodb/arangodb-3.12.8"))
    out = []
    for i in (1, 2):                                         # run 2 = the rerun after a failed build
        rc, o = run(root / "scripts" / "apply-patch.sh", root, "-f", str(pf))
        out.append((rc, o, tree_ok(src, keep_build=True)))
    return out


def case_dgraph(d, script_text):
    root = Path(d) / "engines" / "dgraph"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "apply-patch.sh").write_text(script_text)
    out = []
    for name in ("dgraph-25.0.0", "badger"):
        make_source(root / name)
        pf = Path(d) / f"{name}.patch"
        pf.write_text(patch_text(name))
        which = "dgraph" if name == "dgraph-25.0.0" else "badger"
        for i in (1, 2):
            rc, o = run(root / "scripts" / "apply-patch.sh", root, which, "-f", str(pf))
            out.append((rc, o, tree_ok(root / name, keep_build=False)))
    return out


def planted(text):
    """the script as it was before the fix: restore only (no clean), and a plain `patch` that may prompt"""
    t = re.sub(r"^.*git -C \S+ clean -fdq.*\n", "", text, flags=re.M)
    return t.replace("patch --forward --batch ", "patch ")


def all_ok(results):
    return all(rc == 0 and not bad for rc, _, bad in results)


def main():
    results = []

    def check(name, ok, detail=""):
        results.append(bool(ok))
        print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail.strip()[-600:]}"))

    def detail(res):
        return "\n".join(f"rc={rc} {bad} {o.strip()[-200:]}" for rc, o, bad in res)

    cases = {"arangodb": case_arangodb, "dgraph": case_dgraph}
    for db, case in cases.items():
        text = (E / db / "scripts" / "apply-patch.sh").read_text()
        with tempfile.TemporaryDirectory() as d:
            res = case(d, text)
        check(f"{db}: apply-patch.sh twice, both runs clean"
              + (", build/ kept" if db == "arangodb" else " (dgraph-25.0.0 and badger)"), all_ok(res), detail(res))
        p = planted(text)
        with tempfile.TemporaryDirectory() as d:
            res = case(d, p)
        check(f"control: {db}'s script without the clean and --forward --batch fails the second run",
              p != text and not all_ok(res), detail(res))
    bad = results.count(False)
    print(f"check_engine_patch_rerun {'OK' if not bad else 'FAIL'}: {len(results) - bad}/{len(results)} cases")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
