"""What the scale-check scripts share: one grid loaded as I01's data (LDBC reads and writes, each cell by its own
held-out picks, no pick record), and I01's cost per adjacency part with every list on one setting."""
import os
import sys
from kvevaluator.paths import ROOT

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost import compose
from kvevaluator.measure import grid_capture
from kvcost.layout_cost import picks
from kvcost.layout_cost import benchmarks

LIST_SETTINGS = compose.LAYOUTS       # the grid's three (python/kvcost/layout_cost/settings.py)
from kvevaluator.capture.step_record import (READ_BYTE_LANES as BYTE_LANES)  # (the 7 read lanes, one definition)


# no pick record (each grid by its own held-out rows), the default criterion, the grid's three settings, I01's
# families (no microbench)
RULE = picks.PlanRule(record=None)
COMPOSER = compose.Composer(criterion=None, families=benchmarks.families("ldbc", drop=("microbench",)),
                          plan_rule=RULE)


def load_w16(grid):
    """(data, I01, its units) of one grid capture, per type and direction"""
    data = compose.load(grid, True, composer=COMPOSER)
    w = next(w for w in benchmarks.select("ldbc", data.sf, None).values() if w["id"] == "i01")
    return data, w, compose.workload_units(w, data)


def lists_on(units, setting):
    """the layout with Condensed properties and every list on one setting"""
    return {u: ("condensed" if u[0] == "props" else setting) for u in units}


def part_name(u):
    return " ".join(str(x) for x in u[1:])
