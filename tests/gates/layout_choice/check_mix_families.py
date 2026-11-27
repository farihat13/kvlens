#!/usr/bin/env python3
"""A built mix's store, part by part (the composition proof's A2: a part holds the same items whatever the other parts'
settings are). Adjacency, family by family: every (edge type, direction) key family of a mix store must be the SAME
keys and values as that family in the uniform grid cell with the family's layout (a sha256 over every pair under the
prefix 'e'·etype·dir, `kvevaluator dump --prefix --digest`). This is the store-level half of the mix check
(check_mix_vs_built.py compares the measured costs): it shows a direction's setting reached that direction, and only it.

A spec's layout per (edge type, direction): adjacency_by_etype_dir, else adjacency_by_etype, else adjacency, each
matched against the grid's components (mixes.grid_settings). The reference store is meta-props_condensed_adj_<layout>
(an adjacency family does not depend on the props layout). The control (always run): the same comparison against
the OTHER direction's layout must differ for every edge type whose two directions differ.

Properties and indexes, the same way: each vertex label's property part (the pairs under 'v'·label id) must equal
that label's part in the uniform cell with the label's props layout (meta-props_<layout>_adj_split: a property part
does not depend on the adjacency layout), and the index (the pairs under 'i') must equal the uniform cell's: it is the
same in every layout. Controls: a label's part against a uniform cell of ANOTHER props layout must differ; the index
digest against the store's own property digest must differ (the prefix selects something). A label the mix stores in
grouped rows (vertex_props.groups) is counted and said, not compared: it has no reference cell.

    python3 tests/gates/layout_choice/check_mix_families.py --sf 0.1 [--spec-dir inputs/specs/mixes_per_direction]
Needs the stores (kvcost measure, on the grid's layouts and on the spec dirs). Exit 0 = every family, property part and index
equal and every control caught.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
from kvevaluator.paths import ROOT
from kvevaluator.layouts import SETTINGS, cell_id
from kvevaluator.datasets import schema_path

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = str(ROOT)
from kvcost.layout_cost.mixes import grid_settings, layout_name
from kvevaluator.capture import binary
from kvevaluator.measure import stores as S

SCHEMA = schema_path("ldbc")


def digest(store, etype_id, out, cache):
    """{keys, sha256} of one (etype, dir) family: the pairs under 'e' · etype id · dir (keys.h: 0 out, 1 in)"""
    return digest_of(store, f"65{etype_id:02x}{0 if out else 1:02x}", cache)


def digest_of(store, prefix, cache):
    """(keys, sha256) of the pairs under a key prefix given in hex ('v'·label id: 76 + the id; the index: 69)"""
    if (store, prefix) not in cache:
        r = subprocess.run([binary.binary_path(), "dump", store, "--prefix", prefix, "--digest"],
                           capture_output=True, text=True)
        line = [ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1:]
        if r.returncode or not line:
            raise SystemExit(f"dump {store} --prefix {prefix} --digest failed: {(r.stderr or r.stdout)[-300:]}")
        d = json.loads(line[0])
        cache[(store, prefix)] = (d["keys"], d["sha256"])
    return cache[(store, prefix)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", required=True)
    ap.add_argument("--spec-dir", default=os.path.join(EXP, "inputs", "specs", "mixes_per_direction"))
    ap.add_argument("--no-direction-control", dest="need_direction_control", action="store_false",
                    help="a spec set with no per-direction layout (mixes/): the adjacency control has nothing to compare")
    a = ap.parse_args()

    def store_of(name):
        """a store's dir through stores.store_dir: the round's hard-link copy (KVEVAL_STORE_COPIES) when it has one"""
        return S.store_dir("ldbc", a.sf, name)
    copies = os.environ.get("KVEVAL_STORE_COPIES")
    print(f"[mix families] stores read from {'the copies in ' + copies if copies else 'the originals'} (where a copy exists)")
    schema = json.load(open(SCHEMA))
    settings, cache = grid_settings(), {}
    fails, families, nonempty, ctl_want, ctl_caught, specs = [], 0, 0, 0, 0, 0
    p_parts = p_nonempty = p_grouped = p_ctl_want = p_ctl_caught = i_parts = i_keys = i_ctl_caught = 0
    for path in sorted(glob.glob(os.path.join(os.path.abspath(a.spec_dir), "*.json"))):
        name = os.path.basename(path)[:-5]
        store = store_of(f"meta-{name}")     # its store, meta-<hash> (named by its layout file)
        if not os.path.exists(os.path.join(store, "manifest.json")):
            print(f"{name}: no store at sf{a.sf} -- skipped")
            continue
        specs += 1
        spec = json.load(open(path))
        both = spec.get("direction") == "both"

        def layout(etype, d):
            c = (spec.get("adjacency_by_etype_dir", {}).get(etype, {}).get(d)
                 or spec.get("adjacency_by_etype", {}).get(etype) or spec["adjacency"])
            return layout_name(settings, "adj", c, f"{name} {etype} {d}")
        differ = 0
        for e in schema["edges"]:
            dirs = ["out"] + (["in"] if both or e["reverse"] else [])
            for d in dirs:
                want = layout(e["etype"], d)
                got = digest(store, e["id"], d == "out", cache)
                ref = digest(store_of("meta-" + cell_id("condensed", want)), e["id"], d == "out", cache)
                families += 1
                nonempty += got[0] > 0
                if got != ref:
                    fails.append(f"{name}: {e['etype']} {d} ({want}): {got[0]} keys {got[1][:12]} vs the uniform "
                                 f"{want} cell's {ref[0]} keys {ref[1][:12]}")
                other = layout(e["etype"], "in" if d == "out" else "out") if len(dirs) == 2 else want
                if other != want and got[0] > 0:   # the control: this family against the other direction's layout
                    ctl_want += 1
                    differ += 1
                    wrong = digest(store_of("meta-" + cell_id("condensed", other)), e["id"], d == "out", cache)
                    ctl_caught += got != wrong
        # the property parts and the index (keys.h: 'v' · vtype · ...; index/sorted_index.h: 'i' · ...)
        base = {k: v for k, v in spec["vertex_props"].items() if k != "groups"}
        grouped_labels = set(spec["vertex_props"].get("groups", {}))
        for v in schema["vertices"]:
            pfx = f"76{v['id']:02x}"
            got = digest_of(store, pfx, cache)
            if v["label"] in grouped_labels:   # grouped rows: no reference cell since the retirement
                p_grouped += 1
                continue
            want = layout_name(settings, "props", spec.get("vertex_props_by_label", {}).get(v["label"], base), f"{name} {v['label']}")
            ref = digest_of(store_of("meta-" + cell_id(want, "split")), pfx, cache)
            p_parts += 1
            p_nonempty += got[0] > 0
            if got != ref:
                fails.append(f"{name}: props of {v['label']} ({want}): {got[0]} keys {got[1][:12]} vs the uniform {want} "
                             f"cell's {ref[0]} keys {ref[1][:12]}")
            if got[0] > 0:                     # the control: the same part in a cell of another props layout
                other = next(x for x in SETTINGS if x != want)
                p_ctl_want += 1
                p_ctl_caught += got != digest_of(store_of("meta-" + cell_id(other, "split")), pfx, cache)
        got = digest_of(store, "69", cache)
        ref = digest_of(store_of("meta-props_split_adj_split"), "69", cache)
        i_parts += 1
        i_keys += got[0]
        if got != ref or got[0] == 0:
            fails.append(f"{name}: the index: {got[0]} keys {got[1][:12]} vs the uniform cell's {ref[0]} keys {ref[1][:12]}")
        i_ctl_caught += got != digest_of(store, "76", cache)
        print(f"{name}: {differ} families whose direction has its own layout; "
              f"{len(spec.get('vertex_props_by_label', {}))} labels with their own props layout, {len(grouped_labels)} in grouped rows")
    for f in fails[:12]:
        print("  FAIL", f)
    # a set with no per-direction layout (the per type mixes) has no adjacency control of its own: the property one stands
    ok = (not fails and families > 0 and ctl_caught == ctl_want and p_parts > 0 and p_ctl_want > 0
          and p_ctl_caught == p_ctl_want and i_parts > 0 and i_ctl_caught == i_parts and (ctl_want > 0 or not a.need_direction_control))
    print(f"[mix families] sf{a.sf}: {specs} mix stores, {families} (edge type, direction) families compared key by key "
          f"({nonempty} non-empty) -> {'OK: each equals the uniform cell of its layout' if not fails and families else f'FAIL ({len(fails)})'}; "
          f"control (the other direction's layout): {ctl_caught} of {ctl_want} families differ"
          + ("" if ctl_caught == ctl_want and (ctl_want or not a.need_direction_control) else " -> NOT CAUGHT"))
    print(f"[mix parts] sf{a.sf}: {p_parts} (store, label) property parts compared key by key ({p_nonempty} non-empty; "
          f"{p_grouped} in grouped rows: not compared, no reference cell) and {i_parts} indexes ({i_keys} keys) -> "
          f"{'OK' if not any('props of' in f or 'the index' in f for f in fails) and p_parts and i_parts else 'FAIL'}; controls: "
          f"{p_ctl_caught} of {p_ctl_want} property parts differ from another props layout's, {i_ctl_caught} of {i_parts} "
          f"index digests differ from the store's property digest"
          + ("" if p_ctl_want and p_ctl_caught == p_ctl_want and i_ctl_caught == i_parts else " -> NOT CAUGHT"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
