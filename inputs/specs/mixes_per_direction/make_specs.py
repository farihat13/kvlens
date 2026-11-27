#!/usr/bin/env python3
"""Write the per-direction mix specs of this directory (adjacency per edge type AND direction), never typed by hand.

Two kinds: (1) a pick of the layout search, read from a `budget_picks.csv` row (I01 at B = 2, the "per type and
direction" pick without grouped options); (2) single-edge-type specs, one direction pair each. Every component is
copied from the grid's own cells (inputs/specs/grid/*.json), so check_mix_vs_built can map a spec back to its assignment.

Rules for a pick: an edge type whose two listed directions agree, or that lists one direction only, goes to
`adjacency_by_etype` (an unlisted direction takes its etype's listed layout: each such fill is printed); an edge
type whose directions differ goes to `adjacency_by_etype_dir`; an edge type the pick does not list keeps the
default; the labels of one vid group (Post + Comment = Message) get the same props layout, as meta requires.

    python3 inputs/specs/mixes_per_direction/make_specs.py [--picks <budget_picks.csv>] [--check]

--check: write nothing, exit 1 if a spec on disk differs from what this script would write.
To fold into experiments/layout_choice/pick_specs.py (its owner's, stage 2).
"""
import argparse
import csv
import json
import os
import sys
from kvevaluator.paths import ROOT
from kvevaluator.layouts import SETTINGS, cell_id

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.schema.vids import group_of
from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA
from kvevaluator.output_tree import Tree

GRID = os.path.join(EXP, "inputs", "specs", "grid")
PICKS = str(Tree().summary("ldbc", "3", "meta-v4-20261009", "layout-choice", "wops_bytes4k", "budget_picks.csv"))
LAYOUTS = SETTINGS
# (spec stem, edge type, out layout, in layout): one edge type per spec, every other part Condensed
SINGLE = [
    ("knows_out_chunked_in_condensed", "knows", "chunked", "condensed"),
    ("hastag_out_chunked_in_split", "hasTag", "chunked", "split"),
    ("knows_out_split_in_condensed", "knows", "split", "condensed"),   # edge-exists: out Split answers alone
    ("knows_out_condensed_in_split", "knows", "condensed", "split"),   # edge-exists: the in-key GET
]


def grid_cell(props, adj):
    return json.load(open(os.path.join(GRID, cell_id(props, adj) + ".json")))


def components():
    """{"props"|"adj": {layout: the grid's component}}"""
    return {"props": {p: grid_cell(p, "condensed")["vertex_props"] for p in LAYOUTS},
            "adj": {a: grid_cell("condensed", a)["adjacency"] for a in LAYOUTS}}


def base_spec():
    return grid_cell("condensed", "condensed")   # direction, index and both defaults as the grid has them


def single(etype, out, into, comp):
    spec = base_spec()
    spec["adjacency_by_etype_dir"] = {etype: {"out": comp["adj"][out], "in": comp["adj"][into]}}
    return spec


def from_pick(assignment, comp, name, notes):
    """the spec of a pick's assignment string ('knows in-lists=condensed; Message props=chunked; ...')"""
    props, lists = {}, {}
    for item in assignment.split("; "):
        part, layout = item.rsplit("=", 1)
        if layout not in LAYOUTS:
            raise SystemExit(f"{name}: {item}: not one of the grid's layouts (a grouped option needs pick_specs.py)")
        if part.endswith(" props"):
            props[part[:-len(" props")]] = layout
        elif part.endswith(" out-lists") or part.endswith(" in-lists"):
            etype, d = part.rsplit(" ", 1)
            lists.setdefault(etype, {})[d.split("-")[0]] = layout
        else:
            raise SystemExit(f"{name}: {item}: not a per-direction part (want '<etype> out-lists' / '<etype> in-lists')")
    spec = base_spec()
    by_label = {}
    for label in LDBC_SCHEMA.vertex_props:
        layout = props.get(group_of(label), props.get(label))
        if layout and layout != "condensed":
            by_label[label] = comp["props"][layout]
    by_etype, by_dir = {}, {}
    for etype, d in sorted(lists.items()):
        if len(d) == 2 and d["out"] != d["in"]:
            by_dir[etype] = {"out": comp["adj"][d["out"]], "in": comp["adj"][d["in"]]}
            continue
        layout = d.get("out", d.get("in"))
        if len(d) == 1 and etype in LDBC_SCHEMA.reverse_etypes:   # a stored direction the workload never reads
            missing = "in" if "out" in d else "out"
            notes.append(f"{name}: {etype} {missing}-lists not in the pick -> {layout} (its listed direction's layout)")
        if layout != "condensed":
            by_etype[etype] = comp["adj"][layout]
    if by_label:
        spec["vertex_props_by_label"] = by_label
    if by_etype:
        spec["adjacency_by_etype"] = by_etype
    if by_dir:
        spec["adjacency_by_etype_dir"] = by_dir
    return spec


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--picks", default=PICKS, help="the search's budget_picks.csv (default: the sf3 snapshot's)")
    ap.add_argument("--check", action="store_true", help="compare with the specs on disk instead of writing them")
    a = ap.parse_args()
    comp = components()
    notes, specs = [], {}
    rows = [r for r in csv.DictReader(open(a.picks))
            if r["id"].lower().startswith("i01") and r.get("grouped_options", "no") == "no" and float(r["budget_B"]) == 2.0
            and r["pick_kind"] == "per type and direction"]
    if len(rows) != 1:
        raise SystemExit(f"{a.picks}: {len(rows)} rows for I01, B = 2, per type and direction, no grouped options (want 1)")
    # named by what it is: the per-direction pick of an sf3 snapshot at B = 2, a fixture of the per-direction
    # checks -- NOT the registered I01 arm w16_b2 (inputs/specs/mixes_w16), whose store name it must not share
    specs["w16_b2_per_direction_20261010"] = from_pick(rows[0]["pick_assignment"], comp, "w16_b2_per_direction_20261010", notes)
    for name, etype, out, into in SINGLE:
        specs[name] = single(etype, out, into, comp)
    differ = 0
    for name, spec in sorted(specs.items()):
        path = os.path.join(HERE, f"{name}.json")
        text = json.dumps(spec, indent=2) + "\n"
        n_dir = len(spec.get("adjacency_by_etype_dir", {}))
        if a.check:
            same = os.path.exists(path) and open(path).read() == text
            differ += not same
            print(f"{name}: {'same' if same else 'DIFFERS from the generator'} ({n_dir} edge types per direction)")
        else:
            with open(path, "w") as f:
                f.write(text)
            print(f"wrote {name}.json ({n_dir} edge types per direction, {len(spec.get('adjacency_by_etype', {}))} per type, "
                  f"{len(spec.get('vertex_props_by_label', {}))} labels)")
    for n in notes:
        print("fill:", n)
    print(f"{len(specs)} specs, {len(notes)} fills" + (f", {differ} differ" if a.check else ""))
    sys.exit(1 if differ else 0)


if __name__ == "__main__":
    main()
