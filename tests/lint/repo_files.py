"""The files a lint scans under a root: git's tracked plus untracked-not-ignored files (a plain walk without git).
KVEVAL_LINT_TRACKED_ONLY=1 scans the tracked files alone: the sf0.1 round checks a commit, not a tree's scratch files."""
import os
import subprocess
from pathlib import Path


def repo_files(root):
    """Sorted paths relative to root, as strings with '/' separators."""
    root = Path(root)
    if (root / ".git").exists():
        which = ["--cached"] if os.environ.get("KVEVAL_LINT_TRACKED_ONLY") == "1" else ["--cached", "--others",
                                                                                        "--exclude-standard"]
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", *which], check=True, capture_output=True).stdout
        names = {n for n in out.decode().split("\0") if n and (root / n).is_file()}   # a staged delete is gone
    else:
        names = set()
        for d, dirs, files in os.walk(root):
            # with no git to say what is ignored: skip what the tools write (.gitignore's build/, data/, output/)
            if d == str(root):
                dirs[:] = [x for x in dirs if x not in ("build", "data", "output")]
            dirs[:] = [x for x in dirs if x not in (".git", ".venv", "__pycache__")]
            names |= {os.path.relpath(os.path.join(d, f), root).replace(os.sep, "/") for f in files}
    return sorted(names)


def is_text(path):
    """False for a file with a NUL byte in its first 8 KB."""
    with open(path, "rb") as fh:
        return b"\0" not in fh.read(8192)
