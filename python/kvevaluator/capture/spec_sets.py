"""A spec set's layouts (the stems of its spec files) and the named-format spec marker."""
import glob
import os

from kvevaluator.layouts import GRID


def is_grid(spec_dir):
    return spec_dir is None or os.path.realpath(spec_dir) == os.path.realpath(GRID)


def spec_cells(spec_dir=None):
    """the cells of a spec set: the stems of its *.json files (sorted)"""
    return [os.path.splitext(os.path.basename(p))[0]
            for p in sorted(glob.glob(os.path.join(spec_dir or GRID, "*.json")))]


def named_cells(spec_dir, names, tool):
    """the cells of a spec set that a --cells comma list names (all when empty); an unknown name stops the tool"""
    cells = spec_cells(spec_dir)
    if not names:
        return cells
    wanted = names.split(",")
    unknown = [c for c in wanted if c not in cells]
    if unknown:
        raise SystemExit(f"[{tool}] --cells {','.join(unknown)}: no such spec in {spec_dir or GRID} "
                         f"(its cells: {', '.join(cells)})")
    return [c for c in cells if c in wanted]


def format_spec(fmt):
    return f"format:{fmt}"


def format_of(spec_path):
    """the named format a spec path stands for, None for a spec file"""
    return spec_path[len("format:"):] if isinstance(spec_path, str) and spec_path.startswith("format:") else None
