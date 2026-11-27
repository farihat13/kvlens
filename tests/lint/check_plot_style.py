"""Every plotter (a tracked plot_*.py) imports the shared figure style, kvcost.plotting.style, so the figures share one
look (palettes, fonts, number formats). A plotter that does not fails, unless it is in ALLOWED with the reason; an
ALLOWED entry that imports the style now, or names no file, fails too (the list is stale).

    python tests/lint/check_plot_style.py [--root <checkout>]
"""
import argparse
import ast
import os
import sys
from pathlib import Path

from kvevaluator.paths import ROOT
from repo_files import repo_files

STYLE = "kvcost.plotting.style"

# plotter: why it is not on the shared style
ALLOWED = {
    "experiments/layout_choice/plot_w16_reads_and_updates.py": "not ported to the house style yet",
    "experiments/linkbench/plot_whole_log_time_vs_models.py": "not ported to the house style yet",
}


def imports_style(tree):
    """True when the module imports kvcost.plotting.style (import ..., from it, or `from kvcost.plotting import style`)"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name == STYLE for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module == STYLE:
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "kvcost.plotting" and any(a.name == "style" for a in node.names):
            return True
    return False


def scan(root, allowed=None):
    """One 'path:line: rule: detail' line per problem, and the number of plotters scanned."""
    allowed = ALLOWED if allowed is None else allowed
    root = Path(root)
    problems, n, seen = [], 0, set()
    for rel in repo_files(root):
        name = os.path.basename(rel)
        if not (name.startswith("plot_") and name.endswith(".py")):
            continue
        n += 1
        seen.add(rel)
        try:
            tree = ast.parse((root / rel).read_text(), rel)
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}: syntax: {e.msg}")
            continue
        on_style = imports_style(tree)
        if not on_style and rel not in allowed:
            problems.append(f"{rel}:1: plot-style: a plotter that does not import {STYLE}: use its palettes and "
                            f"apply_house_style, or add {rel!r} to ALLOWED with the reason")
        elif on_style and rel in allowed:
            problems.append(f"{rel}:1: stale-allow: ALLOWED names {rel} but it imports {STYLE}: remove the entry")
    for rel in sorted(set(allowed) - seen):
        if (root / rel).exists() or root == ROOT:   # another tree (a plant's) need not hold every file
            problems.append(f"{rel}:0: stale-allow: ALLOWED names {rel}, which is not a tracked plotter: remove the entry")
    return problems, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="the checkout to scan (default: this one)")
    a = ap.parse_args(argv)
    problems, n = scan(a.root)
    for p in problems:
        print(p)
    print(f"check_plot_style {'FAIL' if problems or n == 0 else 'OK'}: {len(problems)} problems in {n} plotters "
          f"({len(ALLOWED)} allowed off the style)")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
