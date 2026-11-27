#!/usr/bin/env python3
"""Chunked at another chunk size as a mix option, named and found one way for measured and predicted captures (kvcost
search-mix --size, kvcost predict Ch4k-Ch4k). Store-free and data-free: synthetic dirs in a temp dir. Under a second.

Run:  python3 tests/unit/check_size_options.py

Cases:
  1. --size 4k: the option chunked_c4k, read from the layouts' own place, its cell props_chunked_c4k_adj_chunked_c4k;
  2. a size with a root (4k=<dir>, the retired form) is refused, and so is a size not <N>k;
  3. parse_sizes sorts by chunk size and refuses a size named twice;
  4. every size of a call is read from the one place it is given (no root per size);
  5. predicted_sizes: the sizes a predicted set holds a Chunked/Chunked cell of, by chunk size;
  6. the layout grammar (kvevaluator.layouts): a name at any chunk size names its cell (Ch2k-Ch2k; Ch64k is the grid's Chunked; a bare Ch is refused),
     its spec made from the name differs from another size's only in pack; a size on Split or 0k is refused; kvcost
     predict names a mix's spec file by its content (two files of one stem and other content: two names; one content
     in two files: one name), and a uniform spec file by its cell id;
  7. a mix with sized parts written as a spec (spec_of) reads back as the same mix (mixes.assignment with
     grid_settings(sizes)); without the sizes it is refused (the reader before the rule).
Exit 1 when a case goes the wrong way."""
import contextlib
import io
import os
import sys
import tempfile

import json

from kvcost.cli.predict import default_set, predicted_place, spec_files
from kvevaluator import layouts as L
from kvcost.cli.search import predicted_sizes
from kvcost.layout_cost import mixes
from kvcost.layout_cost.settings import parse_sizes, size_options, sized_cell

BAD = []


def check(name, ok, got=None):
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": got {got!r}"))
    if not ok:
        BAD.append(name)


def refused(fn):
    """True when fn raises SystemExit (its message swallowed)"""
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            fn()
    except SystemExit:
        return True
    return False


def counts_file(root, fam, cell):
    d = os.path.join(root, fam, "reported", "counts")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, cell + ".csv"), "w").write("query\n")


def main():
    with tempfile.TemporaryDirectory() as tmp:
        # 1
        got = size_options(["4k"], "/place")
        check("1 a size reads its ChK-ChK from the place", got == [("chunked_c4k", "/place", "props_chunked_c4k_adj_chunked_c4k")], got)
        # 2
        check("2 a size with a root is refused", refused(lambda: size_options(["4k=/a"], "/place")))
        check("2 a size not <N>k is refused", refused(lambda: size_options(["4kb"], "/place")))
        # 3
        got = parse_sizes(["16k", "1k"], "/place")
        check("3 sorted by chunk size", [x[0] for x in got] == ["chunked_c1k", "chunked_c16k"], got)
        check("3 a size named twice is refused", refused(lambda: parse_sizes(["4k", "4k"], "/place")))
        # 4
        place = os.path.join(tmp, "captures", "ldbc", "sf0.1")
        got = parse_sizes(["1k", "4k", "256k"], place)
        check("4 every size from the one place", {x[1] for x in got} == {place} and [x[2] for x in got]
              == [sized_cell(k) for k in ("1k", "4k", "256k")], got)
        # 5
        root = os.path.join(tmp, "predict")
        pset = str(predicted_place(root, "ldbc", "0.1", default_set()))
        for c in (sized_cell("16k"), sized_cell("1k"), "props_chunked_adj_chunked", "props_chunked_c4k_adj_chunked_c4k",
                  "props_chunked_c64k_adj_chunked_c256k"):
            counts_file(pset, "interactive", c)
        got = predicted_sizes(root, "ldbc", "0.1", default_set())
        check("5 the predicted set's sizes, by chunk size (a ChK-ChK cell at one size on both sides)", got == ["1k", "4k", "16k"], got)
        # 6
        c2 = L.cell_of("Ch2k-Ch2k")
        check("6 Ch2k-Ch2k names its cell", c2 == "props_chunked_c2k_adj_chunked_c2k", c2)
        check("6 Ch64k is the grid's Chunked, whose id carries no size", L.cell_of("Sp-Ch64k") == "props_split_adj_chunked")
        for bare, full in (("Sp-Ch", "Sp-Ch64k"), ("Ch-Ch4k", "Ch64k-Ch4k"), ("Ch-Ch", "Ch64k-Ch64k")):
            try:
                said = f"taken as {L.cell_of(bare)}"
            except SystemExit as e:
                said = str(e)
            check(f"6 a bare Ch is refused with the name to write ({bare})", said.endswith(f"is written {full}"), said)
        s2, s4 = L.spec_of(c2), L.spec_of(L.cell_of("Ch4k-Ch4k"))
        diff = sorted(k for k in s2 if s2[k] != s4[k])
        check("6 two sizes' specs differ only in pack", diff == ["adjacency", "vertex_props"]
              and s2["adjacency"]["owners"] == {"pack": 2048} and s4["adjacency"]["owners"] == {"pack": 4096}, diff)
        check("6 a size on Split is refused", refused(lambda: L.cell_of("Sp4k-Co")))
        check("6 a 0k chunk is refused", refused(lambda: L.cell_of("Ch0k-Ch0k")))
        mix_a, mix_b = {"vertex_props": "Split", "adjacency": "Condensed", "adjacency_by_etype": {"knows": "Chunked@4k"},
                        "index": "Split"}, {"vertex_props": "Split", "adjacency": "Condensed", "index": "Split"}
        paths = []
        for sub, spec in (("a", mix_a), ("b", mix_b), ("c", mix_a)):
            os.makedirs(os.path.join(tmp, sub))
            paths.append(os.path.join(tmp, sub, "best.json"))
            json.dump(spec, open(paths[-1], "w"))
        names = [next(iter(spec_files([x]))) for x in paths]
        check("6 two mixes of one stem: two content names; one content: one name",
              names[0] != names[1] and names[0] == names[2] and names[0].startswith("mix_"), names)
        check("6 a uniform spec file is named by its cell id", names[1] == "props_split_adj_condensed", names[1])
        # 7
        assign = {("props", "Person"): "chunked_c4k", ("props", "Message"): "condensed", ("adj", "knows"): "chunked_c1k",
                  ("adj", "likes"): "chunked"}
        spec, _ = mixes.spec_of(assign)
        units = sorted(assign, key=lambda u: (u[0], u[1:]))
        back = mixes.assignment(spec, units, mixes.grid_settings(["chunked_c1k", "chunked_c4k"]), "mix")
        check("7 a sized spec reads back as its mix", back == assign, back)
        check("7 without the sizes the spec is refused", refused(lambda: mixes.assignment(spec, units, mixes.grid_settings(), "mix")))
    print(f"check_size_options {'FAIL' if BAD else 'OK'}: {7 - len({b[0] for b in BAD})}/7 cases" + (f"; failed {BAD}" if BAD else ""))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
