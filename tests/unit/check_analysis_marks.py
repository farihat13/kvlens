#!/usr/bin/env python3
"""Analysis steps that met a moved input go on and mark it (kvevaluator.stale), with a control that stays clean:
  1. a built mix whose props are split into rows, not on the retired list: skipped, marked capture_shape_moved
     (kvcost.layout_cost.mixes.retired_grouped); one on the list: skipped, unmarked; a mix with no groups: kept
  2. a meta spec file named like a retired preset: read, marked shape_sibling (formats_meta.meta_spec); a grid spec: clean
Store-free, seconds.

    python3 tests/unit/check_analysis_marks.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile

from kvcost.layout_cost import mixes
from kvevaluator import stale
from kvevaluator.formats import meta as formats_meta
from kvevaluator.paths import ROOT


def kinds(sc):
    return [n["kind"] for n in sc]


def main():
    fails = 0

    def check(ok, what):
        nonlocal fails
        print(f"{'PASS' if ok else 'FAIL'} {what}")
        fails += not ok

    grouped = {"vertex_props": {"groups": [["firstName"]]}}
    with stale.scope() as a, contextlib.redirect_stderr(io.StringIO()):
        skipped = mixes.retired_grouped(grouped, "a_new_grouped_mix")
    with stale.scope() as b:
        listed = mixes.retired_grouped(grouped, mixes.RETIRED_GROUPED_MIXES[0])
        kept = mixes.retired_grouped({"vertex_props": {}}, "w16_b1")
    check(skipped and kinds(a) == ["capture_shape_moved"], f"1a an unlisted grouped mix: skipped, marked {kinds(a)}")
    check(listed and not kept and b == [], f"1b control: a listed one skipped unmarked, an ungrouped one kept {kinds(b)}")

    grid = ROOT / "inputs" / "specs" / "grid" / "props_split_adj_split.json"
    with tempfile.TemporaryDirectory() as t:
        retired = os.path.join(t, "dgraph.json")
        with open(retired, "w") as f:
            json.dump(json.load(open(grid)), f)
        with stale.scope() as c, contextlib.redirect_stderr(io.StringIO()):
            tag, _ = formats_meta.meta_spec(retired)
        with stale.scope() as d:
            formats_meta.meta_spec(str(grid))
    check(tag == "dgraph" and kinds(c) == ["shape_sibling"], f"2a a spec named like a retired preset: read, marked {kinds(c)}")
    check(d == [], "2b control: a grid spec, no mark")
    print(f"check_analysis_marks: 4 cases, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
