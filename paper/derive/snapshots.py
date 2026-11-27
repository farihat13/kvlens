"""What every derive script shares: the places in paper-results/ it reads (the paper's runs, one output tree, checked by
`just fetch-results` against inputs/paper_results.json), and where it writes: its table into <output>/paper/build/data/<claim>.csv
(write_table), its number keys into <output>/paper/build/numbers/<stem>.tex (write_numbers). A derive script reads the
snapshots with the artifact's code; the claim's draw script (gs-paper, released into paper/draw/) reads only its table.
"""
import csv
import json
from pathlib import Path

from kvevaluator.paths import paper_results_dir
from kvevaluator.output_tree import Tree
from kvevaluator import sizes
from kvcost.layout_search import results as R

TABLES_DIR = Tree().paper("build", "data")      # the tables, one CSV per claim (or per claim and grain)
NUMBERS_DIR = Tree().paper("build", "numbers")  # one .tex fragment of number keys per claim

# --- the paper's runs: places in paper-results/ (every file a script reads is in its list, paper-results/files.sha256) ---
# Every snapshot below was costed before the last-chunk lane: its write costs are the measured chunk view
# (python/kvcost/layout_cost/README.md, "The chunk view"). A snapshot re-made under the half view says so here.
RESULTS = paper_results_dir()
PAPER = Tree(RESULTS)   # the paper's runs as a tree, for the scripts that do not take --from yet
# A snapshot's dated id is written once, as a root `NAME = RESULTS / "<id>"`; a name inside it is `NAME = ROOT / "<path>"`
# (a root, or a name above it). evidence/release.py (gs-paper) maps a bundle to the snapshots it reads by these two
# forms only: a bare alias (`NAME = OTHER`) is not read. Re-pointing a snapshot is then one line.
ENGINE_FIDELITY = RESULTS / "engines/ldbc/sf0.3/fidelity"      # engine plans vs live, SF0.3 (C++ evaluator)
ENGINE_FIDELITY_SF01 = RESULTS / "engines/ldbc/sf0.1/fidelity"
ENGINE_FIDELITY_MB = RESULTS / "engines/ldbc/sf0.3/fidelity"   # microbench: engine plans vs live, SF0.3
ENGINE_FIDELITY_PREV = RESULTS / "engines/ldbc/sf0.3/fidelity-repeat"  # the previous load: live counts repeat?
ENGINE_FIDELITY_LEGACY = RESULTS / "engines/ldbc/sf0.3/fidelity-legacy"   # main.tex's old paragraph only
COMPLEX_KV_READS = RESULTS / "engines/ldbc/sf0.3/complex-kv-reads"   # the complex-kv-reads figure's run: live counts, the two IC1 variants, the times (kvengines.complex_kv_reads)
GRID_SF3_CAPTURES = RESULTS / "captures/ldbc/sf3"   # its captures (layout 2): grid (the nine), grid_chunked_<k>, formats
LAYOUT_CHOICE_SF3_UNGROUPED = RESULTS / "summary/ldbc/sf3/layout-choice/reported/wops_bytes4k"   # W01-W15, I01, I02 at SF3 on that grid (the Sp-Co reference; parts-w11, parts-w02; I01's nine and its bill), a uniform layout priced like a mix
LAYOUT_CHOICE_SF3 = RESULTS / "summary/ldbc/sf3/layout-choice/reported/wops_bytes4k"    # the same study at SF3 (its budget rows: budget_picks.csv)
SUM_OPTIMUM_SF3 = RESULTS / "summary/ldbc/sf3/layout-choice/reported/sum-optimum"   # the fewest ops + bytes / 4 KB mix and the nine, per workload, on the same grid
MIX_BENEFIT_JOB_SF3 = RESULTS / "summary/ldbc/sf3/mix-benefit"   # the four benchmarks' per-query costs and cross-benchmark analysis (per-query-disagreement/, cross-benchmark-frequencies/), on the 11-03 grid
BI_SF3 = GRID_SF3_CAPTURES   # BI at SF3: the same place as Interactive (one capture per workload)
BI_COUNTS_SF3 = RESULTS / "captures/ldbc/sf3/bi/reported"   # its BI counts (layout 2): Sp-Sp, Co-Co, Ch64k-Ch64k; the six others are composed from them; the logical census reads it too
BI_STORES_SF3 = RESULTS / "stores/ldbc/sf3"   # its store manifests (records.counts: SF3's counts per vertex label and edge type)
I01_FRONT_SF3 = RESULTS / "summary/ldbc/sf3/mix-benefit/i01-front"   # I01's complete per-type front, the nine and the density of all 3^21 mixes
LOGICAL_CENSUS_SF3 = RESULTS / "summary/ldbc/sf3/logical-census"   # the elements each LDBC Interactive / BI query touches, by kind (bodies Dgraph and NebulaGraph can both run)
CHUNK_SIZE_UNIFORM_SF3 = RESULTS / "summary/ldbc/sf3/layout-costs/i01/measured"   # I01 on the 49 uniform layouts, Chunked at 1-256 KB (kvcost cost-layouts: layouts.csv, query_costs.csv, query_parts.csv)
IMDB_FULL = RESULTS / "captures/imdb-rows/full"   # IMDb (JOB), the whole dump: its captures
IMDB_STORES = RESULTS / "stores/imdb-rows/full"   # each store's manifest, meta-<cell>.manifest.json (Sp-Sp, Co-Co, ChK-ChK at 1-256 KB)
STEADY_LATENCY_SF3 = RESULTS / "timings/ldbc/sf3"   # the SF3 steady latency run: LDBC Interactive and BI (15 stores each, 10 measured passes), steps.csv and the raw steady.json per store (kvcost measure-latency Sp-Sp Co-Co Ch64k-Ch64k ... Ch4k-Ch16k; its picks in summary/ldbc/sf3/grid-latency/)
WRITE_LATENCY_SF3 = RESULTS / "timings/ldbc/sf3/writes"   # the SF3 steady write timing: IU1-8 on the 15 Interactive stores of STEADY_LATENCY_SF3, each after that run's steady read warm-up (run B), the same cache settings
STEADY_LATENCY_JOB = "job-latency"   # JOB's steady latency run of 2026-11-08/09 on IMDb full (15 stores, 10 measured passes, and Sp-Co again as a control): an id of experiments/job/steady_latency_table.py's UNITS
# the composition check of the mixed layouts built for the steady latency runs (experiments/mix_benefit/composed_vs_measured.py's
# output): the 11-03 SF3 run and the 11-06 JOB run hold it; the 11-08 runs built the same mixes and hold none
COMPOSED_CHECK_SF3 = RESULTS / "studies/latency-mixes-sf3/summary/ldbc/sf3/check-composed-vs-measured"
COMPOSED_CHECK_JOB = RESULTS / "studies/latency-mixes-job/summary/imdb-rows/full/check-composed-vs-measured"
LINKBENCH_N1M = RESULTS / "captures/linkbench/n1m"   # LinkBench, 1M nodes: its captures (param set all)
THREE_CELLS_SF3 = RESULTS / "summary/ldbc/sf3/mix-benefit/section7/three-cells"   # six uniform layouts priced from three, vs measured
BILL_SF3 = LAYOUT_CHOICE_SF3 / "bill"   # I01's DynamoDB bill per layout and the mix, 64 KB chunks
MIX_SEARCH_SF3 = RESULTS / "summary/ldbc/sf3/mix-search"   # every best mix and front the paper reads, solved exactly on the 11-03 grid (kvcost search-mix --question): <rows>/<option set>.json
COMBINED_OTHER_SF3 = RESULTS / "summary/ldbc/sf3/mix-benefit/combined-other-benchmarks"   # BI, JOB, LinkBench: the nine, the fewest-combined mix (read from the rows) and the ceiling
RANDOM_SPREAD_SF3 = RESULTS / "summary/ldbc/sf3/random-spread"   # (on the 11-03 grid) LDBC Interactive: random workloads per 5% tile of the complex : short : updates split, the cheapest of 25 and 49 uniform layouts
STATS_LDBC_SF3 = RESULTS / "stats/ldbc/sf3/stats.json"   # the LDBC SF3 statistics predict read
MEASURED_49_L01 = RESULTS / "summary/linkbench/n1m/layout-costs/l01/measured"   # L01 (LinkBench's whole log) on the 49, both chunk views (kvcost cost-layouts --workload l01 Sp-Sp Sp-Co ... Ch256k-Ch256k)
TALLY_LDBC_SF3 = RESULTS / "stats/ldbc/sf3/tallies"          # LDBC SF3
TALLY_IMDB_FULL = RESULTS / "stats/imdb-rows/full/tallies"        # IMDb, the whole dump
TALLY_LINKBENCH_N1M = RESULTS / "stats/linkbench/n1m/tallies"   # LinkBench, 1M nodes
STEADY_LATENCY_SF3_1103 = RESULTS / "studies/latency-mixes-sf3"   # the timed LDBC mixes' specs: specs/<set>/lat_<workload>_<measured|predicted|bill>_mix_any_size.json
JOB_LATENCY_1106 = RESULTS / "studies/latency-mixes-job"   # JOB's timed mixes' specs: specs/<set>/lat_j01_*_mix_any_size.json



def scale_of(path):
    """a run's scale (sf3, sf30, ...): the one timings/ldbc/sf<N>/ in its tree; refused when there is none or several"""
    scales = sorted(p.name for p in (Path(path) / "timings" / "ldbc").glob("sf*") if p.is_dir())
    if len(scales) != 1:
        raise SystemExit(f"{path}: {len(scales)} LDBC scales timed ({scales}), not one")
    return scales[0]

def add_tree_args(ap):
    """--from, --dataset and --tables: the output tree a derive reads, the scales its run measured (a.run, kvevaluator.sizes;
    scale_in(a, dataset) is one dataset's), where the tables go"""
    ap.add_argument("--from", dest="tree", default="paper-results", metavar="TREE",
                    help="the output tree to read: paper-results (the paper's runs; default), output (the output dir) "
                         "or a path")
    ap.add_argument("--dataset", dest="run", type=sizes.resolve, default=sizes.PAPER,
                    help="each dataset's scale, e.g. ldbc=0.1,imdb=sample1pct (default the paper's: "
                         f"{sizes.PAPER}; python/kvevaluator/sizes.py)")
    ap.add_argument("--tables", type=Path, default=None, help="where to write the tables (default: build/data/)")


def tables_arg(doc):
    """--tables for a derive that reads only paper-results/ (no --from, no --dataset): where its tables and number keys
    go (default build/data/ and build/numbers/, as with no argument); `just figure <item>` passes its figure dir's"""
    import argparse
    global TABLES_DIR, NUMBERS_DIR
    ap = argparse.ArgumentParser(description=doc.strip().splitlines()[0])
    ap.add_argument("--tables", type=Path, default=None, help="where to write the tables and the number keys "
                    "(default: build/data/ and build/numbers/)")
    a = ap.parse_args()
    if a.tables is not None:
        TABLES_DIR = NUMBERS_DIR = a.tables


def scale_in(a, dataset):
    """a dataset's scale in the run --dataset names, or None: the run leaves it out"""
    return sizes.scale(a.run, dataset)


def tree_of(name):
    """the Tree a --from names: paper-results, output (the output dir) or a path"""
    if name == "output":
        return Tree()                                       # the output dir
    return Tree(paper_results_dir() if name == "paper-results" else Path(name))


def store_manifests(root, cells):
    """{layout: its store's manifest.json} of `cells` below a tree's stores/<dataset>/<scale> place `root`: each the one
    meta store whose layout.json names the layout (a store dir is named by its spec's hash in the output dir and by its
    layout in the paper's archive: the layout.json inside says which, in both)"""
    out = {}
    for lay in sorted(Path(root).glob("meta-*/layout.json")):
        name = json.load(open(lay))["layout"]
        if name in cells:
            if name in out:
                raise SystemExit(f"{root}: two stores of {name}: {out[name].parent.name}, {lay.parent.name}")
            out[name] = lay.with_name("manifest.json")
    if set(out) != set(cells):
        raise SystemExit(f"{root}: no store of {sorted(set(cells) - set(out))}")
    return out


def mix_search_row(tree, scale, workload, option_set, unproven_label=None, dataset="ldbc", **where):
    """The one result row (kvcost search-mix or front) of the tree's summary/<dataset>/<scale>/mix-search/<workload>/
    <option set>.json (each question's rows at its own dataset and scale) whose fields equal `where` (question, model,
    weighting, limit, ...). A row that is not proven (a node or time limit stopped its search) is refused, unless the
    figure labels it so: unproven_label is that label's text, and it is printed. A front row is proven only if its walk
    and every staircase step finished."""
    import json
    path = tree.summary(dataset, scale, "mix-search", workload, R.stem(option_set) + ".json")
    with open(path) as fh:
        rows = [r for r in json.load(fh)["rows"] if all(r.get(k) == v for k, v in where.items())]
    if len(rows) != 1:
        raise SystemExit(f"{path}: {len(rows)} rows with {where}, not one")
    r = rows[0]
    if not r["proven"]:
        if not unproven_label:
            raise SystemExit(f"{path}: the row with {where} is not proven ({r['nodes']} nodes), and nothing labels it")
        print(f"{path}: the row with {where} is not proven; the figure labels it: {unproven_label}")
    return r


def staircase_reference(stair):
    """B = 1 of a staircase row: its limit of kind reference_x1 (Sp-Co's bytes as the limits were set). A row's
    reference_bytes is Sp-Co re-priced by today's code, which can differ in the last digits from the limits' own"""
    refs = [x["cap_bytes"] for x in stair if x["cap_kind"] == "reference_x1"]
    if len(refs) != 1:
        raise SystemExit(f"a staircase with {len(refs)} limits of kind reference_x1, not one")
    return refs[0]


def assignment_text(assign, rename=None):
    """a result row's assignment ({"Person props": "Sp", "knows in-lists": "Co", ...}) as the paper's CSVs write it
    (mixes.assignment_label, the setting ids: "knows in-lists=condensed; ... Person props=split"); rename: {setting id:
    the name written instead}"""
    from kvcost.layout_cost.mixes import assignment_label
    return assignment_label({u: (rename or {}).get(v, v) for u, v in R.assignment_of(assign).items()})


def write_table(name, columns, rows, key, out_dir=None):
    """Write build/data/<name>.csv (or <out_dir>/<name>.csv): a header, then `rows` (dicts over `columns`) sorted by the
    `key` columns, or in the order given when `key` is None (an order no column gives: the derive builds it from its own
    sorted rules). A float is written with repr (it reads back to the same float); a string as it is, so a snapshot's own
    text is kept."""
    out_dir = TABLES_DIR if out_dir is None else out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    text = lambda v: repr(v) if isinstance(v, float) else str(v)
    out = [[text(r[c]) for c in columns] for r in rows]
    if key is not None:
        out.sort(key=lambda row: [row[columns.index(k)] for k in key])
        if len({tuple(r[columns.index(k)] for k in key) for r in out}) != len(out):
            raise SystemExit(f"{name}: two rows share a key ({', '.join(key)})")
    with open(out_dir / f"{name}.csv", "w", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(columns)
        w.writerows(out)
    print(f"wrote {out_dir / (name + '.csv')} ({len(out)} rows)")


def size(x):
    """bytes in DECIMAL units (1 MB = 10^6 B), as the results doc and the text use; kvcost.plotting.formatting's bytes_to_human is binary"""
    x = float(x)
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if x >= scale:
            return f"{x / scale:.3g}\\,{unit}"
    return f"{x:.0f}\\,B"


def pct(share):
    """a share as a percent, two significant digits under 10%: 2.4, 17, 53"""
    return f"{100 * share:.1f}" if share < 0.1 else f"{100 * share:.0f}"


def multiple(x):
    """a ratio at two significant digits, trailing zero kept, as the text prints a multiple: 0.071, 0.98, 4.0, 10, 21"""
    import math
    return f"{x:.{max(0, 1 - math.floor(math.log10(x)))}f}"


def write_numbers(stem, values, provenance, out_dir=None):
    r"""Write build/numbers/<stem>.tex (or <out_dir>/<stem>.tex, beside a derive's --tables): every value as a keyed
    number the draft cites as \pnum{key} (so the
    text never hand-copies a value). `values` = {key: text}; a key is lowercase letters, digits, '.' and '-'
    (e.g. "csr64k.ic.ops.geomean"); `provenance` = one line on what produced them."""
    import re
    out = Path(out_dir) if out_dir else NUMBERS_DIR
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"% {stem}: {provenance}"]
    for key, text in values.items():
        if not re.fullmatch(r"[a-z0-9.\-]+", key):
            raise SystemExit(f"number key '{key}': lowercase letters, digits, '.', '-' only")
        lines.append(f"\\expandafter\\def\\csname pnum@{key}\\endcsname{{{text}}}")
    (out / f"{stem}.tex").write_text("\n".join(lines) + "\n")
    print(f"wrote {out / (stem + '.tex')} ({len(values)} numbers)")


# the head of numbers.tex: \pnum{key} prints the value, or a loud ??key?? if nothing defines it
NUMBERS_HEADER = r"""% GENERATED by kvlens/paper -- do not edit; cite a value as \pnum{key}.
\providecommand{\pnum}[1]{\ifcsname pnum@#1\endcsname\csname pnum@#1\endcsname\else\textbf{??#1??}\fi}
"""
