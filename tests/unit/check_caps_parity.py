"""Capability PARITY: the planner's Python capability sets (kvevaluator.formats.caps.caps_for --
Format.capabilities() or a cpp-only format's PROFILE) must equal what the C++ evaluator's
format grants (`kvevaluator caps`), for every format x physical design. The vocabulary is one data file both
sides read (inputs/contract/capabilities.json); each format's capability RULES are code on both sides (they read the
schema and the layout spec: cpp/kvevaluator/src/contract/README.md), so this holds them equal. A drift means plans
are selected and emitted on stale facts: after the pertype posting-index flip the C++ store
gained type_index_single_posting while the Python side did not, so the emitter never planned
for the variants pertype actually took (the ic8 mispricing). Store-free.
Run: python3 tests/unit/check_caps_parity.py [--formats a,b] [--designs canonical,label_only,no_type_index]
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.capture.binary import binary_path
from kvevaluator.paths import ROOT
from kvevaluator.formats.caps import caps_for, caps_resolver
from kvevaluator.schema.schema_json import to_json
from kvevaluator.datasets.ldbc.schema import PARKED_SCHEMAS, get_schema

_EXP = str(ROOT)

FORMATS = ["dgraph", "nebula", "arangodb", "pertype_csr", "meta"]
DESIGNS = ["canonical", "no_type_index", "full_index", "key_sorted_date", "type_scoped_date", "value_sorted_date"]
assert not set(DESIGNS) & set(PARKED_SCHEMAS), "a parked design is in DESIGNS (datasets/ldbc/schema.py)"
# the STORE chunk arms (coarse_chunks, from the format's pack target given the store's chunk target: caps_for(chunk=...)
# vs the codec config's chunk_target): the formats with chunk-arm stores, below / at / above CHUNK_COARSE
CHUNK_ARM_FORMATS = ("dgraph", "pertype_csr")
CHUNK_ARMS = (65536, 524287, 524288, 1073741824)      # 524287: the boundary, one byte under CHUNK_COARSE
# (design, format) pairs whose layout C++ refuses to build, loudly: the expected C++ answer there. None today; any
# refusal outside this set fails
REFUSED = set()
# meta has no default layout (no presets): its default arm is this corner spec (nebula's, the old
# default preset); every committed spec file, the corners included, is swept in the spec-file arm below
META_CORNERS = os.path.join(_EXP, "inputs", "specs", "corners")
META_DEFAULT_ARM = os.path.join(META_CORNERS, "corner_nebula.json")


def meta_corner(name, **fields):
    """a corner spec (corners/corner_<name>.json) with whole top-level fields replaced or added"""
    with open(os.path.join(META_CORNERS, f"corner_{name}.json")) as f:
        return {**json.load(f), **fields}


def cpp_caps(binary, schema_path, fmt, config=None, by_label=False):
    cmd = [binary, "caps", "--schema", schema_path, "--format", fmt]
    if config:
        cmd += ["--codec-config", json.dumps(config)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        if by_label and ("not built by" in out.stderr or "Python-only" in out.stderr):
            return None                        # the design's layout is refused in C++ (REFUSED)
        raise SystemExit(f"kvevaluator caps {fmt}: exit {out.returncode}\n{out.stderr}")
    got = json.loads(out.stdout)
    if by_label:
        # per vertex label AND per edge type (the edge-type caps: reverse_adjacency, adjacency_lists): every name an
        # `on` may carry; the Python side resolves each through the one caps_resolver
        return set(got["capabilities"]), {name: set(c) for name, c in {**got["by_label"], **got["by_etype"]}.items()}
    return set(got["capabilities"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--formats", default=",".join(FORMATS))
    ap.add_argument("--designs", default=",".join(DESIGNS))
    a = ap.parse_args(argv)
    binary = binary_path()
    bad = 0
    nlabels = [0]                                # per-label comparisons made (0 = the check covered nothing)
    with tempfile.TemporaryDirectory() as tmp:
        for design in a.designs.split(","):
            schema = get_schema(design)
            path = os.path.join(tmp, f"schema_{design}.json")
            with open(path, "w") as f:
                json.dump(to_json(schema, name=design), f)
            for fmt in a.formats.split(","):
                # meta has no default layout: its default arm is a corner spec, the same file for both sides
                # (spec= for Python, the file's JSON for C++)
                arm = META_DEFAULT_ARM if fmt == "meta" else None
                py = set(caps_for(fmt, schema, spec=arm))
                got = cpp_caps(binary, path, fmt, json.load(open(META_DEFAULT_ARM)) if fmt == "meta" else None,
                               by_label=True)
                resolve = caps_resolver(fmt, schema, spec=arm)
                py_by = {lbl: set(resolve(lbl)) for lbl in got[1]} if got else {}
                if got is None or (design, fmt) in REFUSED:
                    ok = got is None and (design, fmt) in REFUSED
                    bad += not ok
                    print(f"  {'OK  ' if ok else 'FAIL'} {fmt:20s} @{design:14s} "
                          + ("C++ refuses the layout, as expected" if ok else
                             "C++ refusal does not match REFUSED"))
                    continue
                cc, cc_by = got
                ok = py == cc
                bad += not ok
                # the default-scan selector is held only where the scan is valid (the plan-file lint relies on it)
                loose = [w for w, c in [("store", cc)] + sorted(cc_by.items())
                         if "whole_label_scan_by_default" in c and "label_rows_one_run" not in c]
                if loose:
                    bad += 1
                    print(f"  FAIL {fmt:20s} @{design:14s} whole_label_scan_by_default without label_rows_one_run on {loose}")
                print(f"  {'OK  ' if ok else 'FAIL'} {fmt:20s} @{design:14s}"
                      + ("" if ok else f" python-only {sorted(py - cc)} cpp-only {sorted(cc - py)}"))
                # per label: what a variant's `on` checks (Python caps_resolver vs C++ Format::capabilities_for)
                diff = {lbl: (sorted(py_by[lbl] - c), sorted(c - py_by[lbl]))
                        for lbl, c in sorted(cc_by.items()) if py_by[lbl] != c}
                nlab = len(cc_by)
                nlabels[0] += nlab
                bad += bool(diff) or not nlab
                if diff or not nlab:
                    print(f"  FAIL {fmt:20s} @{design:14s} per label ({nlab} labels): "
                          + (f"{diff} (python-only, cpp-only)" if diff else "0 labels compared"))
                if fmt in CHUNK_ARM_FORMATS:
                    for chunk in CHUNK_ARMS:
                        py = set(caps_for(fmt, schema, chunk=chunk))
                        cc, cc_by = cpp_caps(binary, path, fmt, {"chunk_target": chunk}, by_label=True)
                        of_label = caps_resolver(fmt, schema, chunk=chunk)
                        lab = {lbl for lbl, c in cc_by.items() if set(of_label(lbl)) != c}
                        nlabels[0] += len(cc_by)
                        ok = py == cc and not lab and bool(cc_by)
                        bad += not ok
                        print(f"  {'OK  ' if ok else 'FAIL'} {f'{fmt}[c{chunk // 1024}k]':20s} @{design:14s}"
                              f" coarse_chunks={'coarse_chunks' in cc}"
                              + ("" if ok else f" python-only {sorted(py - cc)} cpp-only {sorted(cc - py)} labels {sorted(lab)}"))
                if fmt == "meta":
                    # the PACK-TARGET arms (coarse_chunks from meta's smallest vertex-props pack): props packed at 512K
                    # get it; one label left at 64K takes it away (the every-label min) -- Python via spec=,
                    # C++ via the same JSON
                    # + a grouped/item MIX (whole_row_read varies by label: Person item, the rest grouped)
                    pack512k = {"group": "owner", "owners": {"pack": 524288}, "split": "never"}
                    for tag, sp in (("pack512k", meta_corner("pertype_csr", vertex_props=pack512k)),
                                    ("nebula-person-item", meta_corner("nebula", vertex_props_by_label={"Person": {"group": "item"}})),
                                    ("pack512k-one64k", meta_corner("pertype_csr", vertex_props=pack512k, vertex_props_by_label={
                                        "Person": {"group": "owner", "owners": {"pack": 65536}}}))):
                        spath = os.path.join(tmp, f"meta_{tag}.json")
                        with open(spath, "w") as f:
                            json.dump(sp, f)
                        py = set(caps_for(fmt, schema, spec=spath))
                        cc, cc_by = cpp_caps(binary, path, fmt, sp, by_label=True)
                        of_label = caps_resolver(fmt, schema, spec=spath)
                        lab = {lbl for lbl, c in cc_by.items() if set(of_label(lbl)) != c}
                        nlabels[0] += len(cc_by)
                        ok = py == cc and not lab and bool(cc_by)
                        bad += not ok
                        print(f"  {'OK  ' if ok else 'FAIL'} {f'meta[{tag}]':36s} @{design:14s} coarse_chunks={'coarse_chunks' in cc}"
                              + ("" if ok else f" python-only {sorted(py - cc)} cpp-only {sorted(cc - py)}"))
                    # the spec-file arm: every committed spec (the engine-shaped corners, the design-space grid, the
                    # grouped-props grid, the per-type mixes) -- spec= selects the Python side, the file's
                    # JSON the C++ side.
                    # Compared store-wide AND per label: a mix's labels differ (Person chunked, the rest condensed), so its
                    # per-label caps (what a variant's `on` checks) are where meta's two capabilities_for can disagree
                    # (corner_*.json: the directory also holds expected_diffs.json, which is not a spec)
                    for sp in sorted(glob.glob(os.path.join(META_CORNERS, "corner_*.json"))
                                     + glob.glob(os.path.join(_EXP, "inputs", "specs", "grid", "*.json"))
                                     + glob.glob(os.path.join(_EXP, "inputs", "specs", "grid_grouped", "*.json"))
                                     + glob.glob(os.path.join(_EXP, "inputs", "specs", "mixes", "*.json"))
                                     + glob.glob(os.path.join(_EXP, "inputs", "specs", "mixes_per_direction", "*.json"))):
                        cc, cc_by = cpp_caps(binary, path, fmt, json.load(open(sp)), by_label=True)
                        py = set(caps_for(fmt, schema, spec=sp))
                        of_label = caps_resolver(fmt, schema, spec=sp)
                        lab = {lbl for lbl, c in cc_by.items() if set(of_label(lbl)) != c}
                        varies = len({frozenset(of_label(lbl)) for lbl in cc_by}) > 1
                        nlabels[0] += len(cc_by)
                        ok = py == cc and not lab and bool(cc_by)
                        bad += not ok
                        tag = os.path.splitext(os.path.basename(sp))[0]
                        print(f"  {'OK  ' if ok else 'FAIL'} {f'meta[{tag}]':36s} @{design:14s}"
                              + (" (per-label caps differ across labels)" if varies else "")
                              + ("" if ok else f" python-only {sorted(py - cc)} cpp-only {sorted(cc - py)} labels {sorted(lab)}"))
    print(f"  per-label comparisons: {nlabels[0]} (format x design x vertex label)")
    if not nlabels[0]:
        bad += 1
    if bad:
        print(f"CAPS PARITY FAIL: {bad} (format, design) cells differ between Python and C++")
        return 1
    print("CAPS PARITY OK (Python capability sets == kvevaluator for every format x design, and per label)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
