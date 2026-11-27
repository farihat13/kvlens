"""The 49 uniform layouts of meta, as predict mode names and builds them (their specs and tallies): props and adjacency each Split, Condensed or
Chunked at 1, 4, 16, 64 or 256 KB. A layout is named by its cell id (kvevaluator.layouts.cell_id, the one rule: 64 KB
Chunked is `chunked`, and a ChK-ChK id of two sizes names both), so a predicted layout joins its measured row by name.

Each setting's spec blocks are taken from its diagonal cell's spec in inputs/specs (the grid's three and the four other
sizes' Chunked/Chunked cells); a layout's spec is P's vertex_props block with A's adjacency block.

    python -m kvcost.predict_accuracy.uniform_49_tallies cells all|a,b     # the layouts, one per line
    python -m kvcost.predict_accuracy.uniform_49_tallies specs <out>       # the 49 specs alone: <out>/specs/<cell>.json
        # (no schema in them; each equals the spec kvevaluator.layouts makes from the layout's name)
    python -m kvcost.predict_accuracy.uniform_49_tallies assemble <out> <records> [<dataset>]
        # the 49: <out>/specs/<cell>.json, and <out>/tally/meta-<sha12>.json (the store name kvcost predict finds a
        # tally by) from the run's own tally library <out>/tallies/ (kvevaluator.predict.tally_library); <dataset>
        # (default ldbc; imdb for J01): the record stream's dataset, whose schema the tallies encode with
    python -m kvcost.predict_accuracy.uniform_49_tallies tier-set <tier>   # kvcost predict --tier <tier>'s set (tier_set)
"""
import copy
import json

from kvevaluator.paths import ROOT
from kvevaluator import layouts
from kvevaluator.layouts import DIAGONALS, cell_id, cell_settings
from kvevaluator.datasets import schema_path

SPECS = ROOT / "inputs" / "specs"
# the diagonal cell whose spec defines each setting
DIAGONAL_SPEC = {
    "split": SPECS / "grid" / "props_split_adj_split.json",
    "condensed": SPECS / "grid" / "props_condensed_adj_condensed.json",
    "chunked": SPECS / "grid" / "props_chunked_adj_chunked.json",
    "chunked_c1k": SPECS / "grid_chunked_1k" / "props_chunked_c1k_adj_chunked_c1k.json",
    "chunked_c4k": SPECS / "grid_chunked_4k" / "props_chunked_c4k_adj_chunked_c4k.json",
    "chunked_c16k": SPECS / "grid_chunked_16k" / "props_chunked_c16k_adj_chunked_c16k.json",
    "chunked_c256k": SPECS / "grid_chunked_256k" / "props_chunked_c256k_adj_chunked_c256k.json",
}
SHORT = {"split": "Sp", "condensed": "Co", "chunked_c1k": "Ch1k", "chunked_c4k": "Ch4k", "chunked_c16k": "Ch16k",
         "chunked": "Ch64k", "chunked_c256k": "Ch256k"}


def cell(props, adj):
    return cell_id(props, adj)


def settings_of(name):
    """(props setting, adjacency setting) of a cell name, as DIAGONALS spells them (kvevaluator.layouts.cell_settings,
    never by hand: a ChK-ChK id of two sizes writes 64 KB as chunked_c64k)"""
    try:
        p, a = cell_settings(name)
    except ValueError as e:
        raise SystemExit(f"uniform49: {name} is not one of the 49 layouts ({e})")
    if p not in DIAGONALS or a not in DIAGONALS:
        raise SystemExit(f"uniform49: {name} is not one of the 49 layouts")
    return p, a


def short(name):
    p, a = settings_of(name)
    return f"{SHORT[p]}-{SHORT[a]}"


SETTINGS = DIAGONALS     # the 49's seven settings (readers name them U.SETTINGS)
CELLS = [cell(p, a) for p in DIAGONALS for a in DIAGONALS]
NAMES = [short(c) for c in CELLS]   # the 49 by name, Sp-Sp ... Ch256k-Ch256k: what a command line writes out


def is_the_49(names):
    """True when the layouts named (short names or cell ids) are the 49, each once; False for any other list (a spec
    file among them: the layouts are then the files)"""
    if any(n.endswith(".json") for n in names):
        return False
    cells = [layouts.cell_of(n) for n in names]
    return len(cells) == len(set(cells)) and set(cells) == set(CELLS)
DIAGONAL_CELLS = [cell(s, s) for s in DIAGONALS]


def diagonal_spec(setting):
    with open(DIAGONAL_SPEC[setting]) as f:
        return json.load(f)


def spec_of(name):
    """the meta spec of a cell: P's diagonal spec with A's adjacency block"""
    p, a = settings_of(name)
    out = copy.deepcopy(diagonal_spec(p))
    out["adjacency"] = copy.deepcopy(diagonal_spec(a)["adjacency"])
    return out


DATASET = "ldbc"   # the study's own dataset; J01 (IMDb) is named where it is read
SCHEMA = schema_path(DATASET)


def binary():
    """(path, commit, dirty) of the kvevaluator binary, read from the binary itself"""
    import subprocess
    from kvevaluator.paths import cpp_bin
    v = json.loads(subprocess.run([str(cpp_bin()), "--version"], capture_output=True, text=True, check=True).stdout)
    return str(cpp_bin()), v["binary_git_sha"], v["binary_dirty"]


def tally(name, records, out_path, spec_dir):
    """the direct tally of one cell (kvevaluator tally over the record stream), at out_path; reused when the file there
    was made for the same spec, from the same records, at this binary's tally version (tally_library.reusable).
    Returns (path, seconds, reused)"""
    import os
    import subprocess
    import time
    from kvevaluator.paths import cpp_bin
    spec = spec_of(name)
    spec_path = os.path.join(spec_dir, f"{name}.json")
    os.makedirs(spec_dir, exist_ok=True)
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=1, sort_keys=True)
    from kvevaluator.predict import tally_library as L
    now, version = L.binary_info()
    if os.path.exists(out_path):
        try:
            with open(out_path) as f:
                have = json.load(f)
            made = {"git_sha": have["provenance"]["binary_git_sha"], "dirty": have["provenance"]["binary_dirty"]}
            if (have["spec"] == spec and L.made_from_stream(have["records"], records)
                    and L.reusable(made, have.get("tally_version"), now, version) and "assembled_from" not in have["provenance"]):
                return out_path, 0.0, True
        except (ValueError, KeyError):
            pass
    t0 = time.time()
    tmp = f"{out_path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        subprocess.run([str(cpp_bin()), "tally", "--schema", str(SCHEMA), "--spec", spec_path, "--records", records],
                       stdout=f, check=True)
    os.replace(tmp, out_path)
    return out_path, time.time() - t0, False


def check_grid_specs():
    """the grid's nine spec files are what spec_of gives (the two spec sources agree); returns the number checked"""
    n = 0
    for p in layouts.SETTINGS:
        for a in layouts.SETTINGS:
            with open(SPECS / "grid" / f"{cell(p, a)}.json") as f:
                if json.load(f) != spec_of(cell(p, a)):
                    raise SystemExit(f"uniform49: inputs/specs/grid/{cell(p, a)}.json differs from its diagonals' blocks")
            n += 1
    return n


def write_spec(out, c):
    """<out>/specs/<cell>.json, the spec the 49's predict requests read; returns its path"""
    import os
    path = os.path.join(out, "specs", f"{c}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(spec_of(c), f, indent=1, sort_keys=True)
    return path


# The sample of the owners the paper's predictions count their tallies on (kvevaluator tally --owner-sample,
# --owner-floor, --seed): 1% of the owners, at least 100 per vertex type. The figure registry (run/reproduce.py) and the
# paper's derives (paper/derive/predict_49.py, run_time.py) read it here
PAPER_SAMPLE = {"percent": 1, "floor": 100, "seed": 1}


def sample_name(percent, seed, floor=0):
    """a sample's name, in its tallies' dir and its predicted set: p<P>-s<S>, or p<P>-floor<F>-s<S> with a floor"""
    return f"p{percent:g}-floor{floor}-s{seed}" if floor else f"p{percent:g}-s{seed}"


def sample_text(sample):
    """a sample in words, for a figure's note and an item's description"""
    floor = f" + floor {sample['floor']}" if sample.get("floor") else ""
    return f"{sample['percent']:g}%{floor} sampled tallies, seed {sample['seed']}"


def tier_set(tier):
    """kvcost predict --tier <tier>'s own predicted set, from the 49's own tallies (every owner): predicted-tier<tier>, so
    a set names its tier and none is named bare `predicted`; its anchor level's set adds -anchor (kvcost.cli.predict
    TIER_SETS)"""
    return f"predicted-tier{tier}"


def sampled_set(sample):
    """the predicted set of tier-1 predictions made from a sample's tallies: the tier's own set with the sample's name,
    so two samples never share a set"""
    return f"{tier_set(1)}-{sample_name(**sample)}"


def sampled_dir(tree, dataset, scale, sample):
    """where a sample's tallies are in an output tree (kvevaluator.output_tree.Tree): the predict_statistics study's
    place for sampled tallies, <summary>/<dataset>/<scale>/predict-statistics/sampled/<sample>/ (tally-diagonal/: the
    seven diagonals, each with its wall_s; specs/, tally/: the 49 assembled)"""
    return tree.summary(dataset, scale, "predict-statistics", "sampled") / sample_name(**sample)


def tally_file(out, c):
    """the tally of cell c in a run's tree: <out>/tally/<the store name of <out>/specs/<c>.json>.json"""
    import os
    from kvevaluator.measure import stores
    return os.path.join(out, "tally", f"{stores.meta_store_name(os.path.join(out, 'specs', f'{c}.json'))}.json")


def library(out, records, dataset=DATASET, lib_dir=None, fresh=False):
    """a tally library seeded with the seven diagonals: lib_dir (the dataset's own, tally_library.library_dir, for its
    own record stream), else the run's own <out>/tallies (a sampled stream's); fresh: its entries built again"""
    import os
    from kvevaluator.predict import tally_library as L
    if lib_dir is not None:
        return L.Library(lib_dir, records, seeds=[diagonal_spec(s) for s in DIAGONALS], schema=schema_path(dataset),
                         fresh=fresh)
    return L.Library(os.path.join(out, L.LIBRARY), records, seeds=[diagonal_spec(s) for s in DIAGONALS], root=out,
                     schema=schema_path(dataset), fresh=fresh)


def assemble_all(out, records, dataset=DATASET, lib_dir=None, fresh=False, jobs=1):
    """the 49 tallies by lookup in the library (kvevaluator.predict.tally_library.tally_for_spec): <out>/specs and
    <out>/tally; lib_dir: as library's; jobs: the diagonals' entries built at once first (one stream pass each)"""
    import os
    from concurrent.futures import ThreadPoolExecutor
    from kvevaluator.predict import tally_library as L
    lib = library(out, records, dataset, lib_dir, fresh=fresh)
    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            list(ex.map(lib.entry, [diagonal_spec(s) for s in DIAGONALS]))
    for c in CELLS:
        L.write_named(L.tally_for_spec(spec_of(c), lib), write_spec(out, c), os.path.join(out, "tally"),
                      None if dataset == DATASET else dataset)
    print(f"uniform49: {len(CELLS)} tallies assembled ({len(lib.built)} settings built, {len(set(lib.reused))} reused) -> "
          f"{os.path.join(out, 'tally')}")


def assemble_pairs(out):
    """the 49 tallies, each from its two diagonals' in <out>/tally-diagonal/ (python -m kvevaluator.predict.assemble_tally):
    for tallies the library does not hold (the sampled ones, kvevaluator tally --owner-sample)"""
    import os
    from kvevaluator.predict import assemble_tally as A
    from kvevaluator.predict import tally_library as L
    for c in CELLS:
        p, a = settings_of(c)
        pp, ap = (os.path.join(out, "tally-diagonal", f"{cell(s, s)}.json") for s in (p, a))
        with open(pp) as f:
            pt = json.load(f)
        with open(ap) as f:
            at = json.load(f)
        L.write_named(A.assemble(pt, at, pp, ap), write_spec(out, c), os.path.join(out, "tally"))
    print(f"uniform49: {len(CELLS)} tallies assembled -> {os.path.join(out, 'tally')}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "cells":
        print("\n".join(CELLS if sys.argv[2] == "all" else sys.argv[2].split(",")))
    elif len(sys.argv) == 3 and sys.argv[1] == "specs":
        print(f"uniform49: {len([write_spec(sys.argv[2], c) for c in CELLS])} specs -> {sys.argv[2]}/specs")
    elif len(sys.argv) in (4, 5) and sys.argv[1] == "assemble":
        assemble_all(*sys.argv[2:])
    elif len(sys.argv) == 3 and sys.argv[1] == "tier-set":
        print(tier_set(int(sys.argv[2])))
    else:
        sys.exit("usage: python -m kvcost.predict_accuracy.uniform_49_tallies cells all|a,b | specs <out> | assemble <out> "
                 "<records> [<dataset>] | tier-set <tier>")
