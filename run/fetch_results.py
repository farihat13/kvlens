#!/usr/bin/env python3
"""fetch_results.py: put the paper's results, paper-results/ (one output tree), at the repo's root, every file checked.

The pin is inputs/paper_results.json: the results repo's url, the commit, the file count and the sha256 of its file
list (files.sha256 at the tree's root: every other file's sha256, sorted by path; written by --pin).
The repo is cloned at that commit under a temporary name beside paper-results/, every file checked against the list
(none missing, none extra; .git aside), and only then moved into place. A paper-results/ already in place that checks
is kept; one that does not is refused, never overwritten. Stdlib and git only, so it runs before the Python
environment exists.

    python3 run/fetch_results.py                      # the pinned repo at the pinned commit
    python3 run/fetch_results.py --source DIR|URL     # another copy of that repo (a local clone, a mirror)
    python3 run/fetch_results.py --check              # check paper-results/ against the pin; writes nothing
    python3 run/fetch_results.py --pin                # `just pin-paper-results`: pin paper-results/, a clone of the repo

--pin writes paper-results/files.sha256 (the top-level .git aside). When that changes the list, it stops: commit and
push the list in paper-results/, then run it again. On a clean clone whose list is current and whose commit is on its
remote, it writes the pin: the remote's url (an ssh GitHub url as https), the commit, the file count and the list's
sha256. The repo is public: a file that names a machine path or our other repos is refused (the names in two parts, so
this file names neither); a recorded path is relative to the tree or the repo (python/kvevaluator/paths.py
recorded_path). Only plain files: a symlink is refused.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # run/ -> the repo root
PIN = os.path.join(REPO, "inputs", "paper_results.json")
DEST = os.path.join(REPO, "paper-results")
LIST = "files.sha256"
PRIVATE = re.compile(rb"/(mnt|users|home|tmp)/|gs-" rb"research|gs-" rb"harness")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def tree_files(root):
    """every file under root, relative, the top-level .git aside"""
    out = set()
    for d, dirs, names in os.walk(root):
        if d == root:
            dirs[:] = [x for x in dirs if x != ".git"]
        out |= {os.path.relpath(os.path.join(d, n), root) for n in names}
    return out


def problems(root, pin):
    """(files checked, [what is wrong]) with a paper-results tree: its list must be the pinned one, and every file it
    names present with its sha256; any other file is named too"""
    listed = os.path.join(root, LIST)
    if not os.path.isfile(listed):
        return 0, [f"{LIST}: missing"]
    if sha256_of(listed) != pin["files_sha256"]:
        return 0, [f"{LIST}: not the pinned list (inputs/paper_results.json files_sha256)"]
    want = {}
    with open(listed, encoding="utf-8") as f:
        for line in f:
            sha, rel = line.rstrip("\n").split("  ", 1)
            want[rel] = sha
    have = tree_files(root) - {LIST}
    bad = [f"{rel}: not in the list" for rel in sorted(have - set(want))]
    for rel, sha in sorted(want.items()):
        if rel not in have:
            bad.append(f"{rel}: missing")
        elif sha256_of(os.path.join(root, rel)) != sha:
            bad.append(f"{rel}: sha256 differs from the list")
    return len(want), bad


def report(what, n, bad):
    """print the verdict of problems(); 0 when it checks"""
    if bad:
        print(f"fetch_results: {what}: REFUSED, {len(bad)} problems in {n} files:\n  " + "\n  ".join(bad[:20])
              + (f"\n  ... {len(bad) - 20} more" if len(bad) > 20 else ""))
        return 1
    print(f"fetch_results: {what}: OK, {n} files, each equal to its sha256")
    return 0


def clone(source, commit, into):
    """the repo at `source`, checked out at `commit`, in `into`; SystemExit when either step fails"""
    for cmd in (["git", "clone", "--quiet", "--no-checkout", source, into],
                ["git", "-C", into, "checkout", "--quiet", "--detach", commit]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise SystemExit(f"fetch_results: `{' '.join(cmd)}` failed: {r.stderr.strip()}")


def git(*args):
    r = subprocess.run(["git", "-C", DEST, *args], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"fetch_results --pin: `git {' '.join(args)}` failed: {r.stderr.strip()}")
    return r.stdout.strip()


def write_pin():
    if not os.path.isdir(os.path.join(DEST, ".git")):
        raise SystemExit("fetch_results --pin: paper-results/ is not a clone of the results repo")
    files = sorted(tree_files(DEST) - {LIST})
    odd = [f for f in files if os.path.islink(os.path.join(DEST, f)) or not os.path.isfile(os.path.join(DEST, f))]
    if odd:
        raise SystemExit(f"fetch_results --pin: not a plain file (refused): {odd[:3]}")
    named = [f for f in files if PRIVATE.search(open(os.path.join(DEST, f), "rb").read())]
    if named:
        raise SystemExit(f"fetch_results --pin: {len(named)} files name a machine path or a private repo (refused): "
                         + ", ".join(named[:5]))
    text = "".join(f"{sha256_of(os.path.join(DEST, f))}  {f}\n" for f in files)
    listed = os.path.join(DEST, LIST)
    if not os.path.isfile(listed) or open(listed, encoding="utf-8").read() != text:
        with open(listed, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"fetch_results --pin: wrote {LIST} ({len(files)} files): commit and push it in paper-results/, then run "
              "this again")
        return 1
    if git("status", "--porcelain"):
        raise SystemExit("fetch_results --pin: paper-results/ has uncommitted changes: commit and push them first")
    commit = git("rev-parse", "HEAD")
    if not git("branch", "-r", "--contains", commit):
        raise SystemExit(f"fetch_results --pin: {commit[:12]} is not on the remote: push it first")
    url = re.sub(r"^git@github\.com:", "https://github.com/", git("remote", "get-url", "origin"))
    pin = {"repo": url, "commit": commit, "files": len(files), "files_sha256": sha256_of(listed)}
    with open(PIN, "w") as fh:
        json.dump(pin, fh, indent=2)
        fh.write("\n")
    print(f"fetch_results --pin: pinned {url} at {commit[:12]}, {len(files)} files, in inputs/paper_results.json")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", help="another copy of the results repo: a local clone or a url (default: the pin's)")
    ap.add_argument("--check", action="store_true", help="check paper-results/ against the pin; write nothing")
    ap.add_argument("--pin", action="store_true", help="pin paper-results/ (a clone of the results repo): its list, "
                    "then its commit")
    a = ap.parse_args()
    if a.pin:
        return write_pin()
    with open(PIN) as f:
        pin = json.load(f)
    if a.check or os.path.isdir(DEST):
        if not os.path.isdir(DEST):
            raise SystemExit("fetch_results: no paper-results/ here: run `just fetch-results` first")
        rc = report("paper-results/ in place", *problems(DEST, pin))
        if rc and not a.check:
            print("fetch_results: it is not replaced: move it aside, then fetch again")
        return rc
    source = a.source or pin["repo"]
    tmp = DEST + ".partial"
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)   # this tool's own unfinished clone
    print(f"fetch_results: {source} at {pin['commit'][:12]} ({pin['files']} files)", flush=True)
    clone(source, pin["commit"], tmp)
    if report("the clone", *problems(tmp, pin)):
        return 1
    os.rename(tmp, DEST)
    print(f"fetch_results: placed {DEST}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
