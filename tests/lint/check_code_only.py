"""Code and input files only, under the size limit: every tracked file is a code or input kind (docs: README.md, and
AGENTS.md at the root), and only the listed big files pass the size limit.

    python tests/lint/check_code_only.py      # exit 1 and one line per problem
"""
import subprocess
import sys
from pathlib import Path

from repo_files import repo_files

from kvevaluator.paths import ROOT

SUFFIXES = {".py", ".sh", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".cmake", ".json", ".txt", ".gql", ".graphql", ".aql",
            ".cypher", ".schema", ".csv", ".tsv", ".toml", ".lock",
            ".go", ".mod", ".sum", ".bt", ".just", ".patch", ".conf", ".yaml", ".yml", ".template",   # engines/
            ".properties"}                   # LDBC's driver frequencies, inputs/frequencies/ldbc/
NAMES = {"README.md", "justfile", ".gitignore", ".gitattributes", ".env", ".env.example", "CRITERION", ".python-version",
         "RELEASE_COMMIT"}   # .env: an engine's knobs; .python-version: uv's interpreter pin; RELEASE_COMMIT: a release
                             # archive's commit (.gitattributes export-subst)
FIXTURE_ONLY = {".gz"}                       # binary fixtures, only under tests/fixtures/
FONTS_DIR = "paper/draw/fonts"              # the figures' vendored font, with its upstream license files
FONTS_ONLY = {".otf"}
FONTS_ONLY_NAMES = {"LICENSE", "COPYING", "LIBERTINE-COPYRIGHT"}
ROOT_DOCS = {"paper/numbers.tex",           # the numbers the paper prints, released by gs-paper's evidence/release.py
             "AGENTS.md",                   # the root's guide for coding agents (README.md links it); at the root only
             "docs/walkthrough.md"}         # the step-by-step walkthrough (README.md links it)
MAX_BYTES = 1 << 20
BIG_OK = {                                   # path -> why it may pass the size limit
    "cpp/kvevaluator/third_party/nlohmann/json.hpp": "the JSON library, one header",
    "uv.lock": "the locked Python environment",
}


def kind_problem(rel):
    name = rel.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name.lstrip(".") else ""
    if name in NAMES or suffix in SUFFIXES or ".conf." in name:     # nebula-storaged.conf.ldbc
        return None
    if suffix in FIXTURE_ONLY and rel.startswith("tests/fixtures/"):
        return None
    if (suffix in FONTS_ONLY or name in FONTS_ONLY_NAMES) and rel.rpartition("/")[0] == FONTS_DIR:
        return None
    if rel in ROOT_DOCS:
        return None
    if suffix == ".md":
        return "a doc (only README.md files ship; docs go with the studies)"
    return f"not a code or input kind ({suffix or 'no suffix'})"


def scan(root):
    """One 'path: rule: detail' line per problem."""
    root = Path(root)
    problems = []
    for rel in repo_files(root):
        path = root / rel
        if (p := kind_problem(rel)):
            problems.append(f"{rel}: kind: {p}")
        size = path.stat().st_size
        if size > MAX_BYTES and rel not in BIG_OK:
            problems.append(f"{rel}: size: {size:,} B is over {MAX_BYTES:,} B (data belongs in the archive)")
    return problems, len(repo_files(root))


def untracked(root):
    """the files git does not track under root (empty with no .git: then every file counts as shipped)"""
    if not (Path(root) / ".git").exists():
        return set()
    out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard"], check=True,
                         capture_output=True).stdout
    return {n for n in out.decode().split("\0") if n}


def main():
    problems, n = scan(ROOT)
    loose = untracked(ROOT)
    # an untracked file of another kind (a PDF or notes dropped into a checkout) is only a warning: it fails once it is
    # committed (the commit hook and the sf0.1 round scan tracked files only: KVEVAL_LINT_TRACKED_ONLY); a size
    # failure stays a failure, tracked or not
    warnings = [p for p in problems if p.split(":", 1)[0] in loose and p.split(":", 2)[1].strip() == "kind"]
    problems = [p for p in problems if p not in warnings]
    for p in problems:
        print(p)
    for w in warnings:
        print(f"warning (an untracked file of another kind; it would fail once committed): {w}")
    print(f"check_code_only {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} files"
          + (f", {len(warnings)} warnings on untracked files" if warnings else ""))
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
