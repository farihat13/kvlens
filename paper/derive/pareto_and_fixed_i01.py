#!/usr/bin/env python3
"""Derive pareto-and-fixed-i01's tables: I01 at SF3 (64 KB chunks), the complete front of the mixes per type, the nine
uniform layouts, the front per type and direction, and the density of all 3^21 mixes.

  build/data/pareto-and-fixed-i01.csv               kind front (1,588 mixes) / uniform (the nine) / direction (the
                                                    front per type and direction); line, name, ops (wops), bytes; for
                                                    front and uniform rows each part's setting and its wops and bytes
                                                    (blank where the file has no entry); for direction rows the
                                                    assignment, cap_bytes and cap_x_reference (B x Sp-Co's bytes)
  build/data/pareto-and-fixed-i01-density.csv       one row per non-empty bin of the 400 x 400 density: ix, iy, count
  build/data/pareto-and-fixed-i01-density-grid.csv  one row: nx, ny, the extent (log10 bytes and wops) and the parts

The fronts are MIX_SEARCH_SF3's rows; the nine and the density I01_FRONT_SF3's. Each part keeps its wops and bytes, not
its S, G, N. Checks: nine uniform layouts; the density holds 3^parts mixes; the per-direction front is not empty.

    uv run python paper/derive/pareto_and_fixed_i01.py
"""
import json
import re

import numpy as np

from kvcost.layout_search import results as R
from kvcost.layout_search.front import uniform_name
from kvcost.layout_search.questions import FRONT_WOPS_BYTES
from snapshots import PAPER, I01_FRONT_SF3, assignment_text, mix_search_row, multiple, staircase_reference, write_numbers, write_table

PROPS = ("Person", "Message", "Forum", "Organisation", "Place", "Tag")
LISTS = ("knows", "hasCreator", "likes", "replyOf", "containerOf", "hasMember", "hasModerator", "hasTag", "hasInterest",
         "hasType", "isLocatedIn", "isPartOf", "isSubclassOf", "studyAt", "workAt")
PARTS = [("props", p) for p in PROPS] + [("adj", lst) for lst in LISTS]


def name(u):
    return "index" if u is None else f"{u[0]}_{u[1]}"


def mix_row(kind, i, m):
    """a front or uniform row: its totals, assignment, and per part the setting and the wops and bytes"""
    r = {"kind": kind, "line": f"{i:05d}", "name": uniform_name(m["assign"]) if kind == "uniform" else "",
         "ops": m["ops"], "bytes": m["bytes"]}
    for u in [None] + PARTS:
        v = m["units"].get("None" if u is None else str(u))
        r[f"ops_{name(u)}"], r[f"bytes_{name(u)}"] = (v["ops"], v["bytes"]) if v else ("", "")
        if u is not None:
            r[f"setting_{name(u)}"] = m["assign"][f"{u[0]}|{u[1]}"]
    return r


def number_keys(rows):
    """pareto-and-fixed-i01.uniform.<layout>.<bytes|wops>.x: each uniform layout over Sp-Co, as a multiple;
    pareto-and-fixed-i01.front.replyof.bytes.gb: the bytes replyOf's own lists add at the front's step that makes them
    Chunked (the step's other parts move too; this is replyOf's part only)"""
    uniform = {r["name"]: r for r in rows if r["kind"] == "uniform"}
    ref = uniform["Sp-Co"]
    keys = {}
    for name, r in sorted(uniform.items()):
        short = re.sub(r"\d+k", "", name).replace("-", "").lower()      # the key keeps the settings' letters: spch
        keys[f"pareto-and-fixed-i01.uniform.{short}.bytes.x"] = multiple(float(r["bytes"]) / float(ref["bytes"]))
        keys[f"pareto-and-fixed-i01.uniform.{short}.wops.x"] = multiple(float(r["ops"]) / float(ref["ops"]))
    front = sorted((r for r in rows if r["kind"] == "front"), key=lambda r: float(r["bytes"]))
    steps = [(a, b) for a, b in zip(front, front[1:]) if (a["setting_adj_replyOf"], b["setting_adj_replyOf"])
             == ("condensed", "chunked")]
    if len(steps) != 1:
        raise SystemExit(f"I01's front: {len(steps)} steps make replyOf Chunked, want one")
    a, b = steps[0]
    keys["pareto-and-fixed-i01.front.replyof.bytes.gb"] = f"{(float(b['bytes_adj_replyOf']) - float(a['bytes_adj_replyOf'])) / 1e9:.0f}"
    return keys


def main():
    front = mix_search_row(PAPER, "3", "i01", "per-type", question="front", model=FRONT_WOPS_BYTES)
    if not front["walk_complete"]:
        raise SystemExit("I01's per-type front row: the walk did not finish")
    # a point's assignment as the row keeps it (parts as --mix spells them, settings in short) -> {"props|Person":
    # "condensed", ...}, the keys and setting ids of the nine's file and of this table
    mixes = [{"ops": p["wops"], "bytes": p["bytes"], "units": p["per_part"],
              "assign": {"|".join(u): v for u, v in R.assignment_of(p["assignment"]).items()}} for p in front["points"]]
    uni = json.load(open(I01_FRONT_SF3 / "uniform_layouts.json"))
    if len({uniform_name(u["assign"]) for u in uni}) != 9:
        raise SystemExit(f"{I01_FRONT_SF3}: {len(uni)} uniform layouts, not the nine")
    rows = [mix_row("front", i, m) for i, m in enumerate(mixes)] + [mix_row("uniform", i, u) for i, u in enumerate(uni)]
    stair = mix_search_row(PAPER, "3", "i01", "per-type-and-direction", question="staircase", model=FRONT_WOPS_BYTES)["staircase"]
    dirs = [(i, x) for i, x in enumerate(stair) if x["wops"] is not None]
    if not dirs:
        raise SystemExit("I01's per-type-and-direction staircase: no limit has a mix")
    # B's column: cap_x_reference = the limit over B = 1 (the staircase's reference_x1 limit, Sp-Co's bytes as the
    # equal-bytes front set it), at 4 decimals as that front wrote it
    xcol, ref = "cap_x_reference", staircase_reference(stair)
    rows += [{"kind": "direction", "line": f"{i:05d}", "ops": x["wops"], "bytes": x["bytes"],
              "assignment": assignment_text(x["assignment"]), "cap_bytes": x["cap_bytes"],
              xcol: round(x["cap_bytes"] / ref, 4)} for i, x in dirs]
    columns = (["kind", "line", "name", "ops", "bytes", "assignment", "cap_bytes", xcol, "ops_index", "bytes_index"]
               + [f"{c}_{name(u)}" for u in PARTS for c in ("setting", "ops", "bytes")])
    write_table("pareto-and-fixed-i01", columns, [{c: r.get(c, "") for c in columns} for r in rows], key=("kind", "line"))
    write_numbers("pareto_and_fixed_i01", number_keys(rows), f"{I01_FRONT_SF3.name} (sf3): uniform_layouts.json and I01's "
                  f"per-type front; a multiple is over Sp-Co's")
    z = np.load(I01_FRONT_SF3 / "all_mixes.npz")
    h, extent, order = z["hist"], z["extent"], [str(p) for p in z["order"]]
    if int(h.sum()) != 3 ** len(order):
        raise SystemExit(f"the density holds {int(h.sum()):,} mixes, not 3^{len(order)}")
    ix, iy = np.nonzero(h)
    write_table("pareto-and-fixed-i01-density", ("ix", "iy", "count"),
                [{"ix": f"{a:03d}", "iy": f"{b:03d}", "count": int(h[a, b])} for a, b in zip(ix, iy)], key=("ix", "iy"))
    xl, xh, yl, yh = (float(v) for v in extent)
    write_table("pareto-and-fixed-i01-density-grid", ("nx", "ny", "xl", "xh", "yl", "yh", "parts"),
                [{"nx": h.shape[0], "ny": h.shape[1], "xl": xl, "xh": xh, "yl": yl, "yh": yh, "parts": ";".join(order)}],
                key=("nx",))


if __name__ == "__main__":
    main()
