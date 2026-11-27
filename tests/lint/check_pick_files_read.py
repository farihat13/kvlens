"""The pick record is read through one module (kvevaluator.plan_choice.pick_record): readers get its rows from `load`, and
only the writers and their gates join in the decisions (`load_full`).

  pick-file:   a Python string literal that is a path to pick_stores.json, pick_decisions.json or
               pick_record_by_reference.json (the name, or a path ending in it; prose that mentions it is not one), or a
               shell line (not a comment) naming one, outside FILE_NAMED: a second reader of a file `load` hides, or of
               the writer's input
  load-full:   a call of load_full / load_full_at (the decisions) from a (file, function) not in WRITERS: a reader
               reaching for why a pick was made; a reader calls pick_record.load
  stale-allow: a WRITERS entry with no call left

The table (picks.csv) is named by many callers that pass its path to load or dump; reading it with csv, or the
decisions with json, is what this catches by the other two files' names and by load_full.

    python tests/lint/check_pick_files_read.py [--root <checkout>]
"""
import argparse
import ast
import re
import sys
from pathlib import Path

from repo_files import repo_files
from kvevaluator.paths import ROOT

SCANNED = ("python/", "experiments/", "tests/", "run/")
NAMES = ("pick_stores.json", "pick_decisions.json", "pick_record_by_reference.json")
FILE_NAMED = {
    "python/kvevaluator/plan_choice/pick_record.py": "the one module that reads and writes the three files",
    "python/kvcost/layout_cost/grid_pick_record.py": "the writer: --keep-by-reference names its input file",
    "tests/lint/check_pick_files_read.py": "this lint's own list",
}
LOAD_FULL = ("load_full", "load_full_at")
PATH_LITERAL = re.compile(r"^[\w./-]*(" + "|".join(re.escape(x) for x in NAMES) + r")$")
# (file, function): why it reads the decisions
WRITERS = {
    ("python/kvevaluator/plan_choice/pick_record.py", "main"): "the carry writer (--carry-selection, --carry-job)",
    ("python/kvevaluator/plan_choice/pick_record.py", "load_full_at"): "the git-ref form of load_full",
    ("python/kvcost/layout_cost/grid_pick_record.py", "write"): "the writer at the deciding scale: the previous record's decisions",
    ("python/kvcost/layout_cost/grid_pick_record.py", "check"): "grid_pins --check: the record against the captures",
    ("python/kvcost/layout_cost/grid_pick_record.py", "small_scale"): "the writer at a smaller scale",
    ("python/kvcost/layout_cost/grid_pick_record.py", "load_record_at"): "--before: the previous record",
    ("python/kvcost/layout_cost/grid_pick_record.py", "diff"): "--diff of two records, decisions included",
    ("tests/gates/check_count_equal_successors.py", "rewrite_record"): "re-shas every body sha, decisions included",
    ("tests/gates/meta/check_picks_by_reference.py", "load"): "the kept cells' fingerprints and by_reference",
    ("tests/gates/meta/check_grid_pins_check.py", "main"): "plants wrong decisions for grid_pins --check",
    ("tests/gates/meta/check_grid_body_key.py", "main"): "plants a moved pick through dump",
    ("tests/unit/check_pick_record.py", "check_scale_entries"): "the carried cells (fingerprints)",
    ("tests/unit/check_pick_record.py", "check_three_files"): "the round trip of the three files",
    ("tests/unit/check_pick_record.py", "check_bound_store"): "plants scale fingerprints through dump",
    ("tests/unit/check_pick_record.py", "check_dataset_tags"): "plants a scale fingerprint through dump",
}


def calls_of(tree):
    """[(line, function)] of every call of load_full / load_full_at; `function` = the innermost enclosing def"""
    out = []

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
            if name in LOAD_FULL:
                out.append((node.lineno, fn))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return out


def named_lines(rel, text):
    """[line] where a file of NAMES is named: a Python string literal that is its path, or a shell line that is not a
    comment"""
    if rel.endswith(".py"):
        tree = ast.parse(text, rel)
        return [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and PATH_LITERAL.match(n.value.strip())]
    return [i for i, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("#") and any(x in line for x in NAMES)]


def scan(root, writers=None):
    """One 'path:line: rule: detail' line per problem, and the number of files scanned."""
    writers = WRITERS if writers is None else writers
    root = Path(root)
    problems, n, used = [], 0, set()
    for rel in repo_files(root):
        if not rel.startswith(SCANNED) or not rel.endswith((".py", ".sh")):
            continue
        n += 1
        text = (root / rel).read_text()
        try:
            named = named_lines(rel, text)
            calls = calls_of(ast.parse(text, rel)) if rel.endswith(".py") else []
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}: syntax: {e.msg}")
            continue
        if rel not in FILE_NAMED:
            problems += [f"{rel}:{line}: pick-file: names a pick record file only pick_record reads: call "
                         f"pick_record.load (the rows), or, in a writer or its gate, pick_record.load_full" for line in named]
        for line, fn in calls:
            if (rel, fn) in writers:
                used.add((rel, fn))
                continue
            problems.append(f"{rel}:{line}: load-full: {fn}() reads the pick decisions: a reader calls pick_record.load; "
                            f"a writer or its gate goes into WRITERS ({rel!r}, {fn!r}) with the reason")
    for rel, fn in sorted(set(writers) - used):
        if not (root / rel).exists():          # another tree (a plant's) need not hold every file
            continue
        problems.append(f"{rel}:0: stale-allow: WRITERS names ({rel}, {fn}()) but it calls no load_full: remove the entry")
    return problems, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="the checkout to scan (default: this one)")
    a = ap.parse_args(argv)
    problems, n = scan(a.root)
    for p in problems:
        print(p)
    print(f"check_pick_files_read {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} files "
          f"({len(WRITERS)} allowed decision readers)")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
