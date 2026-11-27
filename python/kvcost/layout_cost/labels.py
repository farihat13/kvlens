"""How figures and READMEs name the pricer's things: the combined cost (SUM_LABEL), the plan pick rule (rule_label), a
workload (display_title) and a suite's axis value (axis_label). One place, so every study's figures say the same; the
values behind them are benchmarks.py's and the pick rule's (python/kvevaluator/plan_choice/optimizer/criteria.py)."""
from kvevaluator.datasets.linkbench.params import check_param_set as linkbench_params_source
from kvcost.layout_cost.benchmarks import criterion_name, kind


# How every figure and README names wops_bytes4k, in ONE place: 4 KB of bytes counts as one GET.
SUM_LABEL = "ops + bytes / 4 KB"


def sum_text(text):
    """a label that names wops_bytes4k the old way, as figures show it. Saved CSVs keep the old goal key
    ('fewest ops + bytes / 4,096'), so the key stays and only what is drawn changes."""
    return text.replace("ops + bytes / 4,096", SUM_LABEL).replace("bytes / 4,096", "bytes / 4 KB")


def rule_label(criterion):
    """how figures and READMEs name the plan pick rule"""
    from kvevaluator.plan_choice.optimizer.criteria import linear_score
    name = criterion_name(criterion)
    w = linear_score(name)
    if w and w["total_bytes"] == 0:
        return f"plan pick: ops ({name})"
    if w and w["total_bytes"] > 0 and w["G"] > 0:
        per_get = round(w["G"] / w["total_bytes"])
        return f"plan pick: {SUM_LABEL if per_get == 4096 else f'ops + bytes / {per_get:,}'} ({name})"
    return f"plan pick: {name}"


def display_title(w):
    """'W14 (rw): Hub writes vs person analytics' -- the id, kind and title, as every figure and README shows them"""
    return f"{w['id'].upper()} ({kind(w)}): {w['title']}"


def axis_label(dataset, at, param_set=None):
    """how figures and READMEs name the axis value: 'SF3' for LDBC, 'LinkBench n1m, params A' for LinkBench"""
    if dataset == "imdb":
        return f"IMDb {at}"
    return f"LinkBench {at}, params {linkbench_params_source(param_set)}" if dataset == "linkbench" else f"SF{at}"


# How figures draw the pricer's things, so every study draws them alike
# PUT / DELETE and the bytes written get their own colours: the canon gives PUT SEEK's steel blue and DELETE NEXT's light steel
# (they never share a bar there), but here a Chunked write's own SEEK and its PUT sit in one bar
PUT_COLOR, DEL_COLOR, WRITTEN_COLOR, RMW_READ_COLOR = "#e6ab02", "#7f7f7f", "#e6ab02", "#8c7fb0"
BLEND_OPS_COLOR, BLEND_BYTES_COLOR = "#1b9e77", "#a6761d"   # the two terms of ops + bytes / 4,096
BUDGET_COLOR = "#e7298a"
# the named limits: B x Sp-Co's bytes, the reference (settings.B_UNIT and settings.NAMED_BUDGETS)
B_STYLE = {1: ("#7a0177", "o"), 2: ("#e7298a", "s"), 3: ("#fb9a99", "^"), 5: ("#fdbf6f", "D")}
BUDGETS_NOTE = ("A named limit is B x the bytes of Sp-Co (the reference) for this workload (B = 1, 2, 3, 5); a limit "
                "past the fewest-ops mix's bytes buys nothing more.")
# B = 0.5 is named only in a file in Co-Sp units, where B is a multiple of Co-Sp's bytes
OLD_B_STYLE = {0.5: ("#49006a", "v")}


def b_style(b):
    """(colour, marker) of a named limit B; a B no style names (an older file's) is drawn grey"""
    return B_STYLE.get(b) or OLD_B_STYLE.get(b) or ("0.45", "x")


def budgets_note(unit_cell, budgets):
    """BUDGETS_NOTE for a file whose named limits are `budgets` x `unit_cell`'s bytes (Co-Sp's in a file in Co-Sp units)"""
    from kvcost.layout_cost.settings import B_UNIT, name_of
    if unit_cell == B_UNIT and tuple(budgets) == tuple(sorted(B_STYLE)):
        return BUDGETS_NOTE
    old = "" if unit_cell == B_UNIT else " (a file in Co-Sp units)"
    return (f"A named limit is B x the bytes of {name_of(unit_cell)}{old} for this workload "
            f"(B = {', '.join(f'{b:g}' for b in budgets)}); a limit past the fewest-ops mix's bytes buys nothing more.")


def gain_text(g):
    """a gain as "6.3x" (two decimals below 2: "1.25x")"""
    return f"{g:.1f}x" if g >= 1.995 else f"{g:.2f}x"
