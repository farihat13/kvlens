"""Analysis code never chooses a cell's body by the runs CSV's planner_pick: the body a cell is scored on is the one the
pick record names (kvevaluator.measure.grid_capture.record_cell_picks).
planner_pick only marked the body the store's capabilities selected (the engine's own
first fit), which is not the pick (78e8fca: split IC3 was judged on `default`, not the picked body); captures since
have no such column (no plan file carries a selector), so the reads left are of older captures.

A read of planner_pick (r["planner_pick"], r.get("planner_pick"), r.planner_pick; a write is not a read) in the Python
under python/ and experiments/ fails, unless its (file, function) is in ALLOWED with the reason it is not a body choice.
An ALLOWED entry with no read left fails too (the list is stale). tests/ is not scanned: its rows are synthetic.

    python tests/lint/check_planner_pick_reads.py [--root <checkout>]
"""
import argparse
import ast
import sys
from pathlib import Path

from repo_files import repo_files
from kvevaluator.paths import ROOT

KEY = "planner_pick"
SCANNED = ("python/", "experiments/")

# (file, function): why this read is not a body choice
ALLOWED = {
    # the capture and its reuse: planner_pick is the column they write and check
    ("python/kvcost/layout_cost/compose_cells.py", "compose_reads"): "a composed row's own flag, which it just wrote",
    # the pick itself: the planner's row is its reference answer, never the pick
    ("python/kvevaluator/plan_choice/candidates.py", "canonical_rows"): "two captures of one body: the planner's row is kept",
    ("python/kvevaluator/plan_choice/candidates.py", "_valid_vectors"):
        "a capture with a planner_pick column: a candidate is valid when it returns the planner's answer (result_hash)",
    # the record's carried JOB entries: the body the selector ran in a capture with a planner_pick column, carried as it was
    ("python/kvevaluator/plan_choice/pick_record.py", "_carried_job"): "carries the selector's body of an old capture (carried_v1)",
    # a record pick of "(pin)" is the plan file's one body, which is the planner's row
    ("python/kvevaluator/measure/cell_latency.py", "is_plan_row"): "resolves a recorded (pin) pick to the planner's row",
    # a comparison with the planner, labelled as one
    ("experiments/meta/bi_cells_table.py", "main"): "the pick's cost beside the planner's, a labelled column",
    ("python/kvcost/layout_cost/grid_pick_record.py", "check_sections"): "--check: a carried BI / JOB entry is the body the selector ran "
                                                         "(the record's own claim), not a body choice",
    # LinkBench: no plan file has variants, so the planner's body is the only body
    ("experiments/linkbench/whole_log_counts_and_time.py", "counts"): "LinkBench: one body per request type",
    ("experiments/mix_benefit/check_cross_benchmark_numbers.py", "loader_costs"): "LinkBench: one body per request type",
    ("experiments/mix_benefit/per_query_disagreement.py", "linkbench"): "LinkBench: one body per request type",
}


def reads_of(tree):
    """[(line, function)] of every read of KEY; `function` = the innermost enclosing def, "<module>" outside one"""
    out = []

    def visit(node, fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = node.name
        hit = False
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            s = node.slice
            hit = isinstance(s, ast.Constant) and s.value == KEY
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
            hit = bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == KEY
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            hit = node.attr == KEY
        if hit:
            out.append((node.lineno, fn))
        for child in ast.iter_child_nodes(node):
            visit(child, fn)

    visit(tree, "<module>")
    return out


def scan(root, allowed=None):
    """One 'path:line: rule: detail' line per problem, and the number of Python files scanned."""
    allowed = ALLOWED if allowed is None else allowed
    root = Path(root)
    problems, n, used = [], 0, set()
    for rel in repo_files(root):
        if not rel.endswith(".py") or not rel.startswith(SCANNED):
            continue
        n += 1
        try:
            tree = ast.parse((root / rel).read_text(), rel)
        except SyntaxError as e:
            problems.append(f"{rel}:{e.lineno}: syntax: {e.msg}")
            continue
        for line, fn in reads_of(tree):
            if (rel, fn) in allowed:
                used.add((rel, fn))
                continue
            problems.append(f"{rel}:{line}: planner-pick: {fn}() reads {KEY}: a cell's body comes from the pick record "
                            f"(grid_capture.record_cell_picks), not the capabilities' pick; if this read is not a body "
                            f"choice, add ({rel!r}, {fn!r}) to ALLOWED with the reason")
    for rel, fn in sorted(set(allowed) - used):
        if not (root / rel).exists():          # another tree (a plant's, an older checkout) need not hold every file
            continue
        problems.append(f"{rel}:0: stale-allow: ALLOWED names ({rel}, {fn}()) but it reads no {KEY}: remove the entry")
    return problems, n


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(ROOT), help="the checkout to scan (default: this one)")
    a = ap.parse_args(argv)
    problems, n = scan(a.root)
    for p in problems:
        print(p)
    print(f"check_planner_pick_reads {'FAIL' if problems else 'OK'}: {len(problems)} problems in {n} Python files "
          f"({len(ALLOWED)} allowed readers)")
    return 1 if problems or n == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
