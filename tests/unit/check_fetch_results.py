"""run/fetch_results.py (--pin, then the fetch) on a planted results repo in a temp dir (a bare repo as its remote,
a temp artifact repo with a copy of it; seconds):
  1. the pin writes the list and stops; once the list is committed and pushed, it pins the remote, the commit, the
     file count and the list's sha256; a file naming a machine path and a commit not on the remote are refused;
  2. a fetch clones the pinned commit into paper-results/, equal to the planted tree (.git aside); a second fetch keeps
     it; --check passes; --source takes another copy of the repo;
  3. refused, nothing placed or replaced: a pinned commit the repo does not hold; a tree in place with a changed file,
     a missing file or an extra file (--check names each, exit 1).
On the code before the repo (fetch_results unpacking a tarball), case 1 fails (no --pin).

    python tests/unit/check_fetch_results.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from kvevaluator.paths import ROOT

FILES = {"captures/ldbc/sf3/interactive/reported/counts/a.csv": "query,G\nic1,3\n",
         "summary/ldbc/sf3/grid-latency/bi/picks.json": "{}\n", "README.md": "planted\n"}
FAILED = []
GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.org"]


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        FAILED.append(name)


def git(cwd, *args):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def run(repo, tool, *args):
    """(rc, stdout + stderr) of a tool in the temp artifact repo"""
    r = subprocess.run([sys.executable, f"run/{tool}.py", *args], cwd=repo, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def files(root):
    """{relative path: bytes} under root, .git and the list aside"""
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts and p.name != "files.sha256"}


def main():
    with tempfile.TemporaryDirectory(prefix="check-fetch-results-") as tmp:
        d = Path(tmp)
        remote = d / "results.git"
        git(d, "init", "-q", "--bare", "-b", "main", str(remote))
        repo = d / "artifact"
        for rel in ("run/fetch_results.py",):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, repo / rel)
        (repo / "inputs").mkdir()
        tree = repo / "paper-results"
        git(d, "clone", "-q", str(remote), str(tree))
        for rel, text in FILES.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(text)

        (tree / "leak.txt").write_text("/" + "mnt/data/x\n")   # split: a machine path, planted
        rc, out = run(repo, "fetch_results", "--pin")
        check("1a a file naming a machine path is refused", rc != 0 and "leak.txt" in out, out)
        (tree / "leak.txt").unlink()
        rc, out = run(repo, "fetch_results", "--pin")
        check("1b the pin writes the list and stops", rc == 1 and (tree / "files.sha256").exists()
              and not (repo / "inputs" / "paper_results.json").exists(), out)
        git(tree, "add", "-A")
        git(tree, "commit", "-q", "-m", "results")
        rc, out = run(repo, "fetch_results", "--pin")
        check("1c a commit not on the remote is refused", rc != 0 and "not on the remote" in out, out)
        git(tree, "push", "-q", "origin", "main")
        rc, out = run(repo, "fetch_results", "--pin")
        pin = json.loads((repo / "inputs" / "paper_results.json").read_text()) if rc == 0 else {}
        check("1d pushed, it pins the remote, the commit and the list",
              pin.get("commit") == git(tree, "rev-parse", "HEAD") and pin.get("files") == len(FILES)
              and pin.get("repo") == str(remote), out)

        planted = d / "planted"
        tree.rename(planted)
        rc, out = run(repo, "fetch_results")
        check("2a a fetch clones the pinned commit, equal to the planted tree",
              rc == 0 and files(tree) == files(planted), out)
        rc, out = run(repo, "fetch_results")
        check("2b a second fetch keeps it", rc == 0 and "in place: OK" in out, out)
        rc, out = run(repo, "fetch_results", "--check")
        check("2c --check passes", rc == 0 and f"OK, {len(FILES)} files" in out, out)
        shutil.rmtree(tree)
        rc, out = run(repo, "fetch_results", "--source", str(planted))
        check("2d --source takes another copy of the repo", rc == 0 and files(tree) == files(planted), out)

        shutil.rmtree(tree)
        good = dict(pin)
        pin["commit"] = "0" * 40
        (repo / "inputs" / "paper_results.json").write_text(json.dumps(pin))
        rc, out = run(repo, "fetch_results")
        check("3a a commit the repo does not hold: refused, nothing placed", rc != 0 and not tree.exists(), out)
        (repo / "inputs" / "paper_results.json").write_text(json.dumps(good))
        run(repo, "fetch_results")
        target = tree / "README.md"
        for name, plant, words in (("a changed file", lambda: target.write_text("changed\n"), "README.md: sha256 differs"),
                                   ("a missing file", target.unlink, "README.md: missing"),
                                   ("an extra file", lambda: (tree / "x.txt").write_text("x"), "x.txt: not in the list")):
            plant()
            rc, out = run(repo, "fetch_results", "--check")
            rc2, out2 = run(repo, "fetch_results")
            check(f"3b {name}: --check names it, a fetch does not replace the tree",
                  rc == 1 and words in out and rc2 == 1 and "not replaced" in out2, out + out2)
            shutil.rmtree(tree)
            run(repo, "fetch_results")
    total = 4 + 4 + 1 + 3
    print(f"check_fetch_results {'FAIL' if FAILED else 'OK'}: {total - len(FAILED)}/{total} cases")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
