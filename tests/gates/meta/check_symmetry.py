#!/usr/bin/env python3
"""The symmetry test: does a layout change only the FRAMING, never what the data itself costs?

Per vertex label and per edge type, over the 9 grid cells + the own-placement cells + the per-direction mixes
(layout_choice/mixes_per_direction: an edge type's out lists and in lists in different layouts) at one scale:
  (a) the PAYLOAD (the property cells: int / date 8 B, a str its bytes, a multi its blob -- columns::payload_of) is the
      same in every cell, per stored copy (an edge's props are stored once per kept direction inline, once own);
  (d) that payload equals the same quantity counted from the neutral record stream the ingest reads -- a number that
      never passes through meta's codecs (the anchor; (a) follows from it);
  (e) where an edge type's props are stored inline in both directions, the out copy's payload equals the in copy's
      (each direction holds every edge once, whatever the two layouts are);
  (b) each store's census totals equal its manifest (every key counted once);
  (c) the framing per layout (keys, neighbour ids, headers, bitmaps, offsets) is REPORTED, not asserted, beside a
      coarse sanity estimate.
A positive control (one label's payload +1 B in one cell) must fail (a) and (d), named.

  python3 tests/gates/meta/check_symmetry.py [--sf 0.1] [--out DIR]
Writes <out>/census/<cell>.json (kvevaluator census, with payload_bytes / framing_bytes) and <out>/symmetry.json;
default out = output/summary/ldbc/sf<SF>/meta-symmetry/<date>. Exit 0 = (a), (b), (d) hold and the control was caught.
"""
import argparse
import copy
import glob
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path

_HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvevaluator.capture import binary
from kvevaluator.measure import records, stores
from kvevaluator.measure import grid_capture
from kvevaluator.output_tree import Tree

SCHEMA = schema_path("ldbc")
SPEC_DIRS = (os.path.join(EXP, "inputs", "specs", "grid"), os.path.join(EXP, "inputs", "specs", "grid_chunked_own"),
             # the per-direction mixes: an edge type's out lists and in lists in different layouts
             os.path.join(EXP, "inputs", "specs", "mixes_per_direction"))


def uvarint_len(n):
    k = 1
    while n >= 0x80:
        n >>= 7
        k += 1
    return k


def cell_payload(ptype, multi, v):
    """columns::payload_of on a record's value: int / date 8 B; str its UTF-8 bytes; multi its blob (uvarint count +
    per element uvarint length + bytes); absent / null 0"""
    if v is None:
        return 0
    if multi:
        items = [str(x).encode() for x in v]
        return uvarint_len(len(items)) + sum(uvarint_len(len(b)) + len(b) for b in items)
    if ptype == "str":
        return len(str(v).encode())
    return 8


def record_payload(sf):
    """(d): {('vertex', label): bytes, ('edge', etype): bytes, ...counts} from the record stream, declared props only
    (id is key-derived, never stored)"""
    s = json.load(open(SCHEMA))
    vprops = {v["label"]: {p["name"]: (p["type"], p.get("multi", False)) for p in v["props"] if p["name"] != "id"}
              for v in s["vertices"]}
    eprops = {e["etype"]: {p["name"]: (p["type"], p.get("multi", False)) for p in e.get("props", [])} for e in s["edges"]}
    pay, count = defaultdict(int), defaultdict(int)
    with open(records.records_file("ldbc", sf)) as f:
        for line in f:
            r = json.loads(line)
            if r[0] == "v":
                label, props = r[2], r[3] or {}
                count[("vertex", label)] += 1
                for name, (t, m) in vprops[label].items():
                    pay[("vertex", label)] += cell_payload(t, m, props.get(name))
            elif r[0] == "e":
                etype, props = r[1], (r[4] if len(r) > 4 and isinstance(r[4], dict) else {})
                count[("edge", etype)] += 1
                for name, (t, m) in eprops[etype].items():
                    pay[("edge", etype)] += cell_payload(t, m, props.get(name))
    return pay, count, {e["etype"]: bool(e.get("reverse")) for e in s["edges"]}


def census(sf, cell, spec, out):
    store = stores.ensure_store("ldbc", sf, "meta", chunk=None, quiet=True, spec=spec)
    path = os.path.join(out, f"{cell}.json")
    with open(path, "w") as f:
        subprocess.run([binary.binary_path(), "census", store], stdout=f, check=True)
    return json.load(open(path)), json.load(open(spec))


def per_type(cen, spec, reverse):
    """{('vertex'|'edge', type): {payload, framing, keys, copies}} from one cell's census groups"""
    out = defaultdict(lambda: {"payload": 0, "framing": 0, "keys": 0, "copies": 1, "by_dir": defaultdict(int)})
    for g in cen["groups"]:
        if g["part"] == "vertex_props":
            k = ("vertex", g["type"])
        elif g["part"] in ("adjacency", "edge_props"):
            k = ("edge", g["type"])
        else:
            continue                                   # the index is held fixed, not an axis
        out[k]["payload"] += g["payload_bytes"]
        if g["part"] == "adjacency":
            out[k]["by_dir"][g["direction"]] += g["payload_bytes"]
        out[k]["framing"] += g["framing_bytes"]
        out[k]["keys"] += g["keys"]
    adj = spec.get("adjacency", {})
    own = adj.get("edge_props") == "own"
    for (kind, t), v in out.items():
        if kind == "edge":
            both = spec.get("direction") == "both" or reverse.get(t, False)
            v["copies"] = 1 if own else (2 if both else 1)
    return out


def check(cells, rec, counts, reverse):
    fails, compared = [], 0
    types = sorted({k for c in cells.values() for k in c["types"]} | {k for k in rec if rec[k]})
    for k in types:
        per_copy = {}
        for name, c in cells.items():
            v = c["types"].get(k)
            if v is None:
                if rec.get(k, 0):
                    fails.append(f"{k}: {name} stores no {k[0]} part but the records hold {rec[k]} B of payload")
                continue
            per_copy[name] = v["payload"] / v["copies"]
        if not per_copy:
            continue
        compared += 1
        if len(set(per_copy.values())) > 1:
            fails.append(f"(a) {k}: payload per copy differs across cells: {sorted(set(per_copy.values()))[:4]}")
        want = rec.get(k, 0)
        off = {n: p for n, p in per_copy.items() if p != want}
        if off:
            fails.append(f"(d) {k}: payload per copy != the record stream's {want} B in {sorted(off)[:3]} "
                         f"({sorted(set(off.values()))[:3]})")
    for name, c in cells.items():   # (e): an inline etype stored in both directions holds the same payload in each
        for k, v in sorted(c["types"].items()):
            if k[0] == "edge" and v["copies"] == 2 and v["by_dir"].get("out", 0) != v["by_dir"].get("in", 0):
                fails.append(f"(e) {k}: {name} stores {v['by_dir'].get('out', 0)} B of payload in the out lists and "
                             f"{v['by_dir'].get('in', 0)} B in the in lists")
    for name, c in cells.items():
        chk = c["census"]["check"]
        if not (chk["keys_match"] and chk["logical_bytes_match"]):
            fails.append(f"(b) {name}: census totals differ from the manifest {chk}")
    return fails, compared, len(types)


def layout_of(spec, kind):
    if any(spec.get(k) for k in ("vertex_props_by_label", "adjacency_by_etype", "adjacency_by_etype_dir")):
        return "Mixed"   # several layouts in one store: not a row of the per-layout framing table
    c = spec["vertex_props"] if kind == "vertex" else spec["adjacency"]
    if isinstance(c.get("owners"), dict):
        return "Chunked" + ("-own" if c.get("edge_props") == "own" else "")
    return "Condensed" if c.get("group") == "owner" else "Split"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = a.out or str(Tree().summary("ldbc", a.sf, "meta-symmetry", time.strftime("%Y%m%d")))
    os.makedirs(os.path.join(out, "census"), exist_ok=True)
    t0 = time.time()
    rec, counts, reverse = record_payload(a.sf)
    cells = {}
    grid_capture.store_names_unique(SPEC_DIRS)          # one spec file per store name across every dir, before any store is opened
    for d in SPEC_DIRS:
        for spec in sorted(glob.glob(os.path.join(d, "*.json"))):
            name = os.path.basename(spec)[:-5]
            cen, sp = census(a.sf, name, spec, os.path.join(out, "census"))
            cells[name] = {"census": cen, "spec": sp, "types": per_type(cen, sp, reverse)}
    fails, compared, ntypes = check(cells, rec, counts, reverse)
    # the positive control: one cell's payload of one label + 1 B must fail (a) and (d), named
    ctl = copy.deepcopy(cells)
    victim = ("vertex", "Person")
    first = sorted(ctl)[0]
    ctl[first]["types"][victim]["payload"] += 1
    # ... and (e): one stored direction of one two-copy edge type + 1 B must fail it, named
    two = [(name, k) for name in sorted(ctl) for k, v in sorted(ctl[name]["types"].items())
           if k[0] == "edge" and v["copies"] == 2 and v["by_dir"].get("in", 0)]
    if two:
        ctl[two[-1][0]]["types"][two[-1][1]]["by_dir"]["in"] += 1
    cfails, _, _ = check(ctl, rec, counts, reverse)
    caught = (any("(a)" in f and str(victim) in f for f in cfails) and any("(d)" in f and first in f for f in cfails)
              and bool(two) and any("(e)" in f and two[-1][0] in f for f in cfails))
    # (c): the framing table, B per stored item, beside a coarse estimate
    table = []
    for k in sorted({k for c in cells.values() for k in c["types"]}):
        n = counts.get(k, 0)
        for name, c in sorted(cells.items()):
            v = c["types"][k]
            items = n * v["copies"]
            table.append({"type": f"{k[0]}:{k[1]}", "cell": name, "layout": layout_of(c["spec"], k[0]),
                          "items": items, "keys": v["keys"], "payload_bytes": v["payload"], "framing_bytes": v["framing"],
                          "framing_per_item": round(v["framing"] / items, 2) if items else None})
    json.dump({"sf": a.sf, "record_payload": {f"{k[0]}:{k[1]}": v for k, v in sorted(rec.items())},
               "table": table, "failures": fails, "control_caught": caught}, open(os.path.join(out, "symmetry.json"), "w"),
              indent=1)
    by = defaultdict(lambda: defaultdict(list))
    for r in table:
        by[r["type"]][r["layout"]].append(r["framing_per_item"])
    print(f"{'type':28s} " + " ".join(f"{l:>13s}" for l in ("Split", "Condensed", "Chunked", "Chunked-own")) +
          "   framing B per stored item (reported, not asserted)")
    for t in sorted(by):
        cols = []
        for l in ("Split", "Condensed", "Chunked", "Chunked-own"):
            vals = [x for x in by[t].get(l, []) if x is not None]
            cols.append(f"{min(vals):6.1f}-{max(vals):6.1f}" if vals else f"{'-':>13s}")
        print(f"{t:28s} " + " ".join(cols))
    for f in fails[:12]:
        print("  FAIL", f)
    ok = not fails and compared > 0 and caught
    print(f"[symmetry] sf{a.sf}: {len(cells)} cells, {compared} labels / edge types compared (of {ntypes}); payload per "
          f"copy equal across cells and to the record stream; census == manifest -> {'OK' if not fails else f'FAIL ({len(fails)})'}; "
          f"control {'caught' if caught else 'NOT CAUGHT'} ({time.time() - t0:.0f}s) -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
