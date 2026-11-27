"""The layout settings (Split, Condensed, Chunked) and the nine uniform layouts (cells) they make: their short names in
figures, and the reference layout every ratio is to."""
import json
import os
import re
import sys

from kvevaluator.layouts import CELLS, CODE, SETTINGS, cell_id, cell_parts, name_of  # noqa: F401  (the layout table: one definition)
from kvevaluator import layouts  # (GRID, for grid_size; the chunk sizes)

# The decided settings a unit can take: Split, Condensed, and Chunked at five chunk sizes. Their ids are the data's: the
# grid's own Chunked (64 KB) is "chunked" (the chunk-size front's line names it chunked_c64k), another size chunked_c<K>k.
GRID_CHUNK_KB = layouts.GRID_CHUNK_KB          # one home: kvevaluator.layouts (the measurement side reads them too)
CHUNK_SIZES_KB = layouts.CHUNK_SIZES_KB



def size_options(names, place):
    """--size 4k (repeatable) -> [(option id, place, cell)] in the order given: Chunked at another chunk size, priced
    from that size's Chunked/Chunked (props_chunked_c4k_adj_chunked_c4k) in the same place as every other layout,
    `place` (each layout read there by output_tree.layout_capture)"""
    out = []
    for name in names:
        if not re.fullmatch(r"\d+k", name):
            raise SystemExit(f"--size {name}: a chunk size, <N>k (4k, 16k); its Chunked/Chunked is read from the "
                             f"layouts' own place")
        out.append((f"chunked_c{name}", str(place), sized_cell(name)))
    return out


def sized_cell(name):
    """the Chunked/Chunked cell at chunk size `name` (4k) on both sides: props_chunked_c4k_adj_chunked_c4k
    (kvevaluator.layouts.resolve('Ch4k-Ch4k'))"""
    return layouts.cell_id(f"chunked_c{name}", f"chunked_c{name}")

# The reference every ratio is to: Sp-Co, Dgraph's layout.
REF = "props_split_adj_condensed"
# The second fixed baseline, where a mix is compared with a fixed layout: Co-Sp, NebulaGraph's and ArangoDB's layout.
SECOND = "props_condensed_adj_split"
# The byte limit B's unit: Sp-Co's bytes, the reference. The built mixes (inputs/specs/mixes_w16
# w16_b05 ... w16_b3 and the other B-named specs) were picked in Co-Sp's bytes, OLD_B_UNIT.
B_UNIT = REF
OLD_B_UNIT = "props_condensed_adj_split"   # B's unit in a file in Co-Sp units (cap_kind nebula_x<B>): Co-Sp
# The named byte limits B: cap_kind reference_x<B> in a front or sweep file.
NAMED_BUDGETS = (1, 2, 3, 5)

NAMES_NOTE = ("A cell is named properties-adjacency: Sp Split, Co Condensed, Ch<K>k Chunked at K KB (Sp-Co = Split properties, "
              "Condensed adjacency, the reference: Dgraph's layout; Co-Sp is NebulaGraph's and ArangoDB's).")


def settings(cell):
    """('condensed', 'split') of props_condensed_adj_split (a meta- prefix and a _c<N>k size on either part allowed)"""
    p, _pk, a, _ak = cell_parts(cell)
    return p, a


_FULL = "|".join(s.capitalize() for s in SETTINGS)


def short_names(text):
    """every 'Condensed/Split' in a label read from a data file -> 'Co-Sp', the cell's name (kvevaluator.layouts.name_of);
    a size the label gives its Chunked sides goes into the name ('Chunked/Chunked, 1 KB chunks' -> 'Ch1k-Ch1k'), and
    with none they are the grid's ('Condensed/Chunked' -> 'Co-Ch64k')"""
    def short(m):
        side = lambda s: f"{s}_c{m.group(3)}k" if s == "chunked" and m.group(3) else s
        return name_of(cell_id(side(m.group(1).lower()), side(m.group(2).lower())))
    return re.sub(rf"\b({_FULL})/({_FULL})\b(?:, (\d+) KB chunks)?", short, text)


# --- chunk sizes ------------------------------------------------------------------------------------------------
def size_name(pack):
    return f"{pack // 1024}k" if pack % 1024 == 0 else f"{pack}b"


def grid_size():
    """the grid's own chunk size, from its Chunked/Chunked spec"""
    with open(os.path.join(layouts.GRID, "props_chunked_adj_chunked.json")) as f:
        return size_name(json.load(f)["adjacency"]["owners"]["pack"])


def parse_sizes(names, place):
    """--size 4k ... -> size_options' [(option id, place, cell)], the smallest first (the order given changes neither a
    key nor a tie-break); a size named twice is refused"""
    sizes = sorted(size_options(names, place), key=lambda t: int(t[0][len("chunked_c"):-1]))
    dup = sorted({x[0] for x in sizes if [y[0] for y in sizes].count(x[0]) > 1})
    if dup:
        raise SystemExit(f"--size names {', '.join(dup)} more than once")
    return sizes
