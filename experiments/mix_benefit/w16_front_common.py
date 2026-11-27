"""What the I01-front scripts share: the inputs' defaults, I01 loaded from one grid, its 21 parts in the pages' order
(a mix priced per part and the front walk: python/kvcost/layout_search/front.py)."""
import csv
import json
import os
from pathlib import Path
from kvevaluator.layouts import side_name
from kvevaluator.paths import ROOT
from kvevaluator.output_tree import Tree

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost import compose as C
from kvcost.layout_cost import benchmarks
from kvcost.layout_cost.settings import name_of
from kvcost.layout_search.front import ref_bytes_of

# Defaults: the output tree's SF3 captures place and I01's equal-bytes front (equal_bytes_front.py); the driver's --grid
# and --front name others.
GRID = str(Tree().place_dir("captures", "ldbc", "3"))
FRONT = str(Path(benchmarks.out_root("ldbc", "3", "wops_bytes4k")) / "equal-bytes-front" / "i01-rw-ldbc-mix" / "equal_bytes_front.csv")
OUT = str(Tree().summary("ldbc", "3", "mix-benefit", "i01-front"))

PROPS = ("Person", "Message", "Forum", "Organisation", "Place", "Tag")
LISTS = ("knows", "hasCreator", "likes", "replyOf", "containerOf", "hasMember", "hasModerator", "hasTag", "hasInterest",
         "hasType", "isLocatedIn", "isPartOf", "isSubclassOf", "studyAt", "workAt")
UNITS = [("props", p) for p in PROPS] + [("adj", lst) for lst in LISTS]     # the pages' order (load_w16 checks it is I01's set)
SETTINGS = C.LAYOUTS                  # the grid's three (python/kvcost/layout_cost/settings.py)
SHORT = {s: side_name(s) for s in ("split", "condensed", "chunked")}     # Sp, Co, Ch64k (the grid's Chunked)
LETTER = {"split": "S", "condensed": "C", "chunked": "H"}                  # the pages' one-letter code


def add_input_args(ap):
    ap.add_argument("--grid", default=GRID, help=f"the nine-cell grid capture (default {GRID})")
    ap.add_argument("--front", default=FRONT, help="the snapshot's I01 equal_bytes_front.csv: its per-type mixes are "
                                                   "checked against the complete front, and it gives B = 1 (Sp-Co's bytes; Co-Sp's in "
                                                   "a front in Co-Sp units)")
    ap.add_argument("--out", default=OUT, help=f"output dir (default {OUT})")


def abspath(p):
    return p if os.path.isabs(p) else os.path.join(EXP, p)


def load_w16(grid):
    """(data, w): I01 (LDBC reads at LDBC's frequencies, plus the updates) priced from the grid; no microbench"""
    grid = abspath(grid)
    os.chdir(EXP)                                  # the suite reads its params by paths relative to the experiment
    # the three settings only; I01 needs no microbench capture
    data = C.load(grid, False, "ldbc", composer=C.Composer(criterion=None, families=benchmarks.families("ldbc", drop=("microbench",)),
                                                         plan_rule=C.PlanRule()))
    _, w = next(iter(benchmarks.select("ldbc", "3", "i01").items()))
    assert sorted(UNITS) == sorted(C.workload_units(w, data)), "I01's parts are not the 21 expected"
    return data, w


def snapshot_front(path):
    """the snapshot's per-type front rows (fewest wops within each byte limit), by limit"""
    rows = [r for r in csv.DictReader(open(abspath(path)))
            if r["option_set"] == "per type" and r["option"] == "fewest_ops_within_cap"]
    if not rows:
        raise SystemExit(f"{path}: no per-type fewest_ops_within_cap rows")
    return sorted(rows, key=lambda r: float(r["cap_bytes"]))


def ref_bytes(path):
    """(B = 1 in bytes, its cell's short name) as the snapshot's front names it: Sp-Co's (Co-Sp's in a front before
    front.ref_bytes_of)"""
    b, unit = ref_bytes_of(list(csv.DictReader(open(abspath(path)))))
    return b, name_of(unit)


def read_json(out, name):
    p = os.path.join(abspath(out), name)
    if not os.path.exists(p):
        raise SystemExit(f"{p} is missing: run w16_complete_front.py first (or the driver run-mix-benefit-w16-front.sh)")
    return json.load(open(p))
