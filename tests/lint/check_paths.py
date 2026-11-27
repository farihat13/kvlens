"""Paths come from one place: no machine paths, no sys.path edits, no root math or root override outside
python/kvevaluator/paths.py, and the locations (KVEVAL_*_DIR, KVEVAL_CPP_BIN) read only
there; setting one for a child process is not a read. Nothing joins onto the data dir itself (DATA_DIR): its two
part is read through datasets_dir() (or $DATASETS_DIR). A lint lists the repo's files with repo_files, never by walking the tree. A shell script finds the root only through run/env.sh, sourced
in exactly one form: source "$(dirname "${BASH_SOURCE[0]}")/<../ per level>run/env.sh".

    python tests/lint/check_paths.py      # exit 1 and one line per problem
"""
import re
import sys
from pathlib import Path

from kvevaluator.paths import ROOT
from repo_files import repo_files

PATHS_PY = "python/kvevaluator/paths.py"
CODE = (".py", ".sh", ".cmake", ".cc", ".cpp", ".h", ".hpp")
CODE_NAMES = ("justfile", "CMakeLists.txt")

LOC = (r"KVEVAL_(DATA_DIR|OUTPUT_DIR|BUILD_DIR|CPP_BIN|STORE_DIR"
       r"|IMDB_DIR)\b")
RULES = {
    "machine-path": re.compile(r"(?<![\w.])/(mnt|users|home|scratch|proj)/"),     # /tmp is everywhere
    "sys-path": re.compile(r"\bsys\.path\s*(\.\s*(insert|append|extend)|\[|\+?=)|\bPYTHONPATH\s*="),
    # going up from a file to the root: two parents, '..' twice, or nested dirname, on a line that names the file
    "root-math": re.compile(r"(?=.*(__file__|BASH_SOURCE|\$0\b))"
                            r".*(\.parents\s*\[\s*[1-9]|\.parent\s*\.\s*parent|dirname\s*\(\s*(os\.path\.)?dirname"
                            r"|(['\"]\.\.['\"]\s*,\s*){2}|(/\.\.){2})"),
    # the same climb split over lines (a variable holds the file's dir) or written as a repeated list
    "parent-hops": re.compile(r"\[\s*['\"]\.\.['\"]\s*\]\s*\*|['\"]\.\.['\"]\s*,\s*['\"]\.\.['\"]"),
    "root-override": re.compile(r"(getenv|environ\s*\.\s*get|environ\s*\[|\$ENV\{)\s*\(?\s*['\"]?\w*(ROOT|REPO)\b"
                                r"|\$\{\w*(ROOT|REPO)(:?-)"),
    "location-read": re.compile(rf"(environ\s*\.\s*get\s*\(|getenv\s*\()\s*['\"]{LOC}['\"]"      # Python reads
                                rf"|environ\s*\[\s*['\"]{LOC}['\"]\s*\](?!\s*=(?!=))"
                                rf"|['\"]{LOC}['\"]\s+(not\s+)?in\s+os\.environ"
                                rf"|\$\{{?{LOC}\b"),                                              # a shell read
    # the data dir read past its part (datasets/): DATA_DIR in Python or shell, not KVEVAL_DATA_DIR
    "data-dir": re.compile(r"(?<!\w)DATA_DIR\b"),
    # the one way a shell script reaches run/env.sh: relative to itself, the exact form
    "env-source": re.compile(r"(^|[;&|{(])\s*(source|\.)\s+[^#]*\brun/env\.sh\b"),
    # a path to a dataset's compiled schema JSON built by hand: it is datasets.schema_path(name, design) (Python) or
    # `python -m kvevaluator.datasets schema-path` (shell), which read the dataset's manifest
    "schema-path": re.compile(r"""(datasets|schemas)["']?\s*[,/]\s*["']?\w+["']?\s*[,/]\s*f?["']schema[\w{}.]*\.json"""
                              r"""|(datasets|schemas)/\w+/schema[\w{}]*\.json"""),
    # a lint that walks the whole repo tree (it reads the build cache, output/, .venv): it lists files with
    # repo_files (tests/lint/repo_files.py), git's tracked files, like every lint
    "root-walk": re.compile(r"""glob\(\s*os\.path\.join\(\s*(str\()?(EXP|ROOT|root)\)?\s*,\s*["']\*\*"""
                            r"""|os\.walk\(\s*(str\()?(EXP|ROOT|root)\)?\s*\)|\b(EXP|ROOT|root)\)?\s*\.rglob\("""),
}
ENV_SOURCE = re.compile(r'\s*source "\$\(dirname "\$\{BASH_SOURCE\[0\]\}"\)/(\.\./)+run/env\.sh"( \|\| exit 1)?\s*')
# (path, rule) -> why it is allowed
ALLOWED = {
    (PATHS_PY, "root-math"): "the one place the root is found",
    (PATHS_PY, "location-read"): "the one place the locations are read",
    (PATHS_PY, "data-dir"): "defines the data dir and its two parts",
    ("run/env.sh", "data-dir"): "exports the data dir beside its two parts",
    ("run/env.sh", "root-math"): "the one place a shell script finds the root",
    ("run/fetch_results.py", "root-math"): "stdlib only, runs before the Python environment exists",
    ("python/kvevaluator/datasets/ldbc/fetch.py", "root-math"): "stdlib only, also runs by its file before the Python environment exists",
    ("tests/lint/check_paths.py", "location-read"): "names the rule",
    ("tests/lint/check_import_all.py", "sys-path"): "its child process puts a script's dir first, as running the script does",
    ("tests/unit/check_records_key.py", "sys-path"): "runs a planted copy of python/ (no venv of its own) in a child process",
    ("tests/lint/check_paths.py", "data-dir"): "names the rule",
    ("tests/unit/check_manifest_schema_path.py", "schema-path"): "a store recorded the older path",
    ("tests/lint/check_locations.py", "location-read"): "tests paths.py's .env reading",
    ("tests/unit/check_fetch_results.py", "location-read"): "tests fetch_results's data dir (the environment, .env)",
    ("tests/unit/check_fetch_ldbc.py", "location-read"): "points LDBC's fetch at temp data dirs",
    ("tests/lint/repo_files.py", "root-walk"): "the one place a lint lists files (a plain walk without git)",
}
PLANTS = "tests/lint/check_lints_catch_plants.py"     # holds every rule's planted violation
# rule -> path prefixes it does not apply to, and why
NOT_UNDER = {"data-dir": (("engines/", "python/kvengines/"), "the engine tier's own DATA_DIR is an engine's dir, not the data dir")}
# rule -> the only path prefixes it applies to
ONLY_UNDER = {"root-walk": ("tests/lint/",)}


def is_code(rel):
    name = rel.rsplit("/", 1)[-1]
    return rel.endswith(CODE) or name in CODE_NAMES


def scan(root):
    """One 'path:line: rule: text' line per problem, and the number of code files scanned."""
    root = Path(root)
    problems, n = [], 0
    for rel in repo_files(root):
        if not is_code(rel) or rel == PLANTS:
            continue
        n += 1
        for i, line in enumerate((root / rel).read_text(errors="replace").splitlines(), 1):
            exact = rel.endswith(".sh") and ENV_SOURCE.fullmatch(line)
            for rule, rx in RULES.items():
                if exact and rule in ("root-math", "env-source"):
                    continue
                if rule in NOT_UNDER and rel.startswith(NOT_UNDER[rule][0]):
                    continue
                if rule in ONLY_UNDER and not rel.startswith(ONLY_UNDER[rule]):
                    continue
                if rx.search(line) and (rel, rule) not in ALLOWED:
                    problems.append(f"{rel}:{i}: {rule}: {line.strip()[:120]}")
    return problems, n


def main():
    problems, n = scan(ROOT)
    for p in problems:
        print(p)
    print(f"check_paths {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} code files")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
