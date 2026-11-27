#!/usr/bin/env python3
"""Convert a copy of paper-results/ to the file shapes and places today's code reads, in place.

    uv run --locked python run/convert-paper-results.py --tree <dir> [--aside <dir>] [--dry-run]

Run it on a copy (`cp -al paper-results <copy>` is enough), or on an output dir (output/, its aside output.aside/, which
git ignores): paper-results/ itself is refused. A file is never written
through: a file is renamed, or written new and os.replace'd over its name (steps 7 and 8), and steps 9-11 write new files, so a
hard-linked copy leaves paper-results/ as it was. A file the current shape no longer holds is moved to --aside (default:
<tree>.aside, beside the tree; it must be outside any git checkout, since step 10 writes its logs there), never
deleted. Idempotent: a step whose source is gone or whose target is there does nothing. Each step prints its name, the
commit whose shape it applies, and how many files it touched.

Every commit that changes a file's shape or place in the output tree adds its step here, in the same commit; an
analysis re-run from paper-results/ reads a converted copy. The script goes after paper-results/ is packed again in
today's shapes.

Steps (the commit that changed the shape):
  1. composed/ holds no copy of a layout measured itself (38b240be)
  2. each mix question's rows at its own dataset and scale, summary/<ds>/<sc>/mix-search/<rows>/; a larger scale's rows
     keep the name of the same rows at the main scale (a5e332bc, 8e010dc6)
  3. the combined panel's table, summary/ldbc/<sc>/mix-benefit/combined-other-benchmarks/ (a5e332bc)
  4. JOB's drift control, a regime dir <regime>-control beside Sp-Co's first timing (1fdf67e5)
  5. the predicted 49's tallies: byte-equal copies of the tally library's setting files, aside (1192051e)
  6. the tally library in its one layout, stats/<ds>/<sc>/tallies/ (2010b276)
  7. the workloads W16 and W17 renamed i01 and i02: each place named by the id (mix-search/w16, uniform-49/w16,
     predict-statistics/w17, w16-rw-ldbc-mix, mix-benefit/w16-front, ...) and each timed mix (lat_w17_* stores, counts,
     timings and specs), and the values that name them inside the CSV and JSON files (a cell or a string that is the
     id, a mix's name, the weights spec frequencies:ldbc/3/w16, the limits file of a row's key, the cross-benchmark
     costs' family "LDBC, all of W16"); the text a run printed (its command, a source path) is left as it was
  8. LinkBench's param sets named by words: all is log, A is quick, B is quick-next (each place <family>/<set> under
     captures/, composed/ and predicted/, the 49's dir l01-all, and a record's param_set) (1660bb77)
  9. the 49's files in cost-layouts' one place: summary/<ds>/<sc>/uniform-49/<subject>/ (three CSVs per chunk view)
     become layout-costs/<workload>/measured/ (query_costs.csv, query_parts.csv, layouts.csv, both views as rows),
     with no re-run; an old dir without both views' files goes aside
 10. where step 9 had no complete old files, and the 49 at B01's and J01's equal share (the runtime chain's): every pair
     composed (kvcost compose) and the 49 costed by kvcost cost-layouts (today's code) into the tree
 11. the scale_transfer study's predicted uniform.csv tables as query_costs.csv and layouts.csv (no re-run)
 12. the mix search's files named mixes-of-<settings>[-by-direction] (rows, .csv, mixes/, the local search's), every
     assignment as --mix spells it ({"knows lists": "Ch4k"}), and each -two-step-times row's two-step times carried
     into the matching row's times (two_step_a_s, two_step_b_s), the timed file aside (no re-run)
 13. no machine path in a file: every path a run recorded (a provenance field, a command line, a README's capture
     line, a key of composed_from_sha) as paths.recorded_path writes it: relative to the tree (a path into a copy of
     paper-results), to the repo, else <elsewhere>/ and its place; nothing else in a file changes (no re-run)
 14. the mix search's "tables" are the mix cost cache: in every result row (search-mix's rows files, the workload
     stories' run record) times.tables_s -> cost_cache_s, identity.table_keys -> cost_cache_keys, table_entries ->
     cost_cache_entries, a solver (and times.two_step_solver) "tables" -> "cost-cache" and "table-free" ->
     "no-cost-cache", each rows file written again as results.ResultFile writes it (rows sorted by row_key, which
     reads the solver); the combined panel's found_by "table-free" -> "no cost cache"; the saved cache's dir
     stats/<ds>/<sc>/table-cache -> mix-cost-cache. A command a run printed is left as it ran (no re-run)
 15. a capture holds rows only of the bodies its cell offers today (the full_read window rule and, on a grid cell,
     JOB's plain scan): in each read capture's counts, measured and composed, a row of a body that is no
     candidate today goes and an ok row of a body invalid on the cell today is the row a run writes for one
     (grid_bodies.rows_offered_today, the rule measure_runs applies); their steps, parts and row hashes go, and a
     removed body's exactness rows; a composed record's sha of a rewritten source is the new file's. Nothing else in a file changes (no
     re-run)
 16. a uniform layout's shown name with every Chunked side's chunk size, the grid's 64 KB as Ch64k
     (kvevaluator.layouts.name_of): in each summary CSV that holds a cell id beside a shown name (`layout` and `name`:
     layouts.csv, query_costs.csv, a predicted uniform.csv; `cell` and `layout`: uniform_totals.csv, a latency
     summary.csv) the name is written from the id, and in a front's uniform_layouts.json from the row's assignment.
     Nothing else in a file changes (no re-run)
 17. the complex-kv-reads figure's run in one place, engines/ldbc/sf0.3/complex-kv-reads/ (kvengines.complex_kv_reads):
     interactive/live_kvstats.csv is the engine-fidelity check's IC1 rows (fidelity/interactive, which keeps them) and the hosted IC3
     row's (figure2-times/hosted-ic3), microbench/live_kvstats.csv its hop1p7_knows rows under today's header
     (data_created empty: that load did not record it); the two cells' raw counter dumps, the two IC1 variants and the
     timed rows (live.csv and evaluator.csv in one times/equivalent/small-cache/) move there; hosted-ic3's rows file,
     now held twice, goes aside (no re-run)
 18. the complex-kv-reads run's two IC1 variants as rows, like every other cell (kvengines.complex_kv_reads:
     CONFIGURATIONS, QUERY_VARIANTS). Dgraph without its first-name index is the engine configuration dgraph-no-index:
     dgraph-eq-filter-path/<query>-p<N>.json's per_vertex run becomes a row of interactive/live_kvstats.csv
     (db dgraph-no-index) and the cell interactive/dgraph-no-index/<query>/p<N>/kvstats.json (its raw dump, pinned
     config and answer sizes); in the times, live.csv's Dgraph rows on path per_vertex and evaluator.csv's on path
     engine-per_vertex are db dgraph-no-index (the evaluator's path: engine), and a perf file of such a cell is named
     by that db. NebulaGraph's IC1 binding the edge list is the query-text variant ic1-edge-list:
     nebula-path-binding/ic1-p<N>.json's edge_list run becomes a row (db nebula, query ic1-edge-list) and the cell
     interactive/nebula/ic1-edge-list/p<N>/kvstats.json (its raw dump, text and answer). Each file's runs of the
     unchanged query must equal the live row, and are then held once; the two files go aside (no re-run)
 19. the complex-kv-reads run's cache regimes under the registry's names (inputs/contract/store_config.json;
     kvevaluator.measure.store_config.regimes): times/equivalent/small-cache/ is times/equivalent/warm-bounded/; in
     its live.csv and evaluator.csv the regime's name in operating_point and in settings' ENGINES_REGIME. No number
     changes (no re-run)
"""
import argparse
import filecmp
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from kvevaluator.paths import ROOT, paper_results_dir


class Converter:
    def __init__(self, tree, aside, dry_run):
        self.tree, self.aside, self.dry = tree, aside, dry_run
        self.touched = 0
        self.moved = set()      # a dry run's moved files: a file is counted once, as the real run counts it

    # --- the primitives: every change goes through one of these ---
    def move(self, src, dst):
        """rename src to dst (a file or a dir); refused when dst is there"""
        if dst.exists():
            sys.exit(f"refused: {dst} is there already (moving {src})")
        files = {p for p in src.rglob("*") if p.is_file()} if src.is_dir() else {src}
        n = len(files - self.moved)
        self.moved |= files if self.dry else set()
        if not self.dry:
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            self.prune(src.parent)
        self.touched += n
        return n

    def rewrite(self, path, text):
        """replace a file's content: written new beside it and os.replace'd, never written through a hard link; the
        text's line ends as given"""
        if not self.dry:
            tmp = path.with_name(path.name + ".converting")
            tmp.write_bytes(text.encode())
            os.replace(tmp, path)
        self.touched += 1

    def put_aside(self, src):
        """move src out of the tree into the aside dir, at its path relative to the tree"""
        return self.move(src, self.aside / src.relative_to(self.tree))

    def prune(self, d):
        """remove the empty dirs from d up to the tree's root"""
        while d != self.tree and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent

    def step(self, title):
        self.touched = 0
        print(f"== {title}")

    def done(self):
        print(f"   {self.touched} files touched{' (dry run)' if self.dry else ''}")


def composed_copies(c):
    """1. a composed/ file of a layout measured itself (captures/ at the same path holds its counts CSV and run record)
    must be a byte-equal copy of the captured file, or part of a composition (a run record of the layout in that composed
    place names what it was composed from: a layout measured after it was composed, whose composed files kvcost compose
    now leaves out): aside; any other difference is refused, moving nothing"""
    c.step("1 composed/ holds no copy of a layout measured itself (38b240be)")
    moves, bad = [], []
    for counts in sorted((c.tree / "composed").glob("**/counts")):
        cap = counts.parent
        meas = c.tree / "captures" / cap.relative_to(c.tree / "composed")
        for cell in sorted({p.stem for p in counts.glob("*.csv")}):
            if not ((meas / "counts" / f"{cell}.csv").is_file() and (meas / "counts" / f"{cell}.json").is_file()):
                continue
            # a composition: a run record of the layout in this composed place names what it was composed from (a
            # writes capture composed before its records were written has none: its read captures' record says it)
            composition = any("composed_from" in json.loads(r.read_text())
                              for r in cap.parent.parent.glob(f"*/*/counts/{cell}.json"))
            for p in sorted(cap.glob(f"*/{cell}.*")):
                q = meas / p.parent.name / p.name
                (moves if composition or (q.is_file() and filecmp.cmp(p, q, shallow=False)) else bad).append(p)
    if bad:
        sys.exit(f"refused: {len(bad)} composed files of a measured layout are not byte-equal copies: {bad[:5]}")
    for p in moves:
        c.put_aside(p)
    c.done()


# the rows' old place: one dir for every question, at LDBC SF3 (the paper's main LDBC scale)
OLD_ROWS = "summary/ldbc/sf3/mix-benefit/mix-search/mix-search"
# an old rows dir -> (dataset dir, scale dir, rows): a larger scale's rows and the other datasets' rows
ROWS_PLACE = {
    "j01": ("imdb-rows", "full", "j01"), "j01-once": ("imdb-rows", "full", "j01-once"),
    "j01-spco-share": ("imdb-rows", "full", "j01-spco-share"), "j01-predicted": ("imdb-rows", "full", "j01-predicted"),
    "l01": ("linkbench", "n1m", "l01"),
    "w16-sf30": ("ldbc", "sf30", "i01"), "b01-sf30": ("ldbc", "sf30", "b01-spco-share"),
    "b01-sf30-once": ("ldbc", "sf30", "b01-once"),
    # read by name at SF30 by paper/derive/stored_size.py
    "b01-sf30-spco-share": ("ldbc", "sf30", "b01-sf30-spco-share"),
    "b01-sf30-spco-share-bill": ("ldbc", "sf30", "b01-sf30-spco-share-bill"),
}


def mix_search_rows(c):
    """2. summary/ldbc/sf3/mix-benefit/mix-search/mix-search/<rows>/ -> summary/<ds>/<sc>/mix-search/<rows>/"""
    c.step("2 the mix questions' rows at their own dataset and scale (a5e332bc, 8e010dc6)")
    old = c.tree / OLD_ROWS
    for d in sorted(old.iterdir()) if old.is_dir() else ():
        ds, sc, rows = ROWS_PLACE.get(d.name, ("ldbc", "sf3", d.name))
        c.move(d, c.tree / "summary" / ds / sc / "mix-search" / rows)
    c.done()


def combined_other(c):
    """3. summary/ldbc/<sc>/mix-benefit/mix-search/combined-other-benchmarks -> .../mix-benefit/combined-other-benchmarks"""
    c.step("3 the combined panel's table (a5e332bc)")
    for d in sorted(c.tree.glob("summary/ldbc/*/mix-benefit/mix-search/combined-other-benchmarks")):
        c.move(d, d.parent.parent / d.name)
    c.done()


def job_control(c):
    """4. studies/job-latency-control/: its timing goes beside Sp-Co's first timing as <regime>-control; its picks.json,
    equal to the main run's Sp-Co picks (checked), aside with the rest of the study"""
    c.step("4 JOB's drift control beside Sp-Co's first timing (1fdf67e5)")
    study = c.tree / "studies" / "job-latency-control"
    if study.is_dir():
        a = json.load(open(study / "summary/imdb-rows/full/grid-latency/job/picks.json"))
        b = json.load(open(c.tree / "summary/imdb-rows/full/grid-latency/job/picks.json"))
        if (a["criterion"], a["counts_capture"]) != (b["criterion"], b["counts_capture"]) or any(
                picks != b["cells"].get(cell) for cell, picks in a["cells"].items()):
            sys.exit(f"refused: {study}'s picks.json differs from the main run's")
        for regime in sorted((study / "timings/imdb-rows/full/job").glob("*/*")):
            c.move(regime, c.tree / "timings/imdb-rows/full/job" / regime.parent.name / f"{regime.name}-control")
        c.put_aside(study)
    c.done()


def predicted_49_tallies(c):
    """5. summary/<ds>/<sc>/predict-statistics/uniform49/: nothing reads it since 1192051e (the statistics, tallies and
    specs are in stats/<ds>/<sc>/), aside whole. In paper-results/ it holds only tallies/, each byte-equal to the tally
    library's setting file of its name; a run of the retired driver in an output dir also holds its own specs, tallies
    and run files, so the files that are not such copies are counted and named, and go aside with the rest"""
    c.step("5 the predicted 49's tallies: copies of the tally library's, aside (1192051e)")
    for d in sorted(c.tree.glob("summary/*/*/predict-statistics/uniform49")):
        ds, sc = d.parts[-4], d.parts[-3]
        settings = [c.tree / "stats" / ds / sc / "tally-library" / "settings", c.tree / "stats" / ds / sc / "tallies"]
        own = [f for f in sorted(d.rglob("*")) if f.is_file() and not (f.parent.name == "tallies" and any(
               (s / f.name).is_file() and filecmp.cmp(f, s / f.name, shallow=False) for s in settings))]
        if own:
            print(f"   {d.relative_to(c.tree)}: {len(own)} files are this run's own, not copies of the tally library "
                  f"(e.g. {', '.join(str(f.relative_to(d)) for f in own[:3])}): aside with the rest, read by nothing")
        c.put_aside(d)
    c.done()


def tally_library(c):
    """6. stats/<ds>/<sc>/tally-library/settings/* -> stats/<ds>/<sc>/tallies/ (byte-equal where both hold one); every
    member checked against tally-library.files.sha256 first; the rest of the library (read by nothing) aside"""
    c.step("6 the tally library in its one layout, stats/<ds>/<sc>/tallies/ (2010b276)")
    import hashlib
    for lib in sorted(c.tree.glob("stats/*/*/tally-library")):
        sums = lib.parent / "tally-library.files.sha256"
        for line in open(sums):
            sha, name = line.split(None, 1)
            if hashlib.sha256(open(lib.parent / name.strip(), "rb").read()).hexdigest() != sha:
                sys.exit(f"refused: {name.strip()} differs from {sums}")
        tallies = lib.parent / "tallies"
        for f in sorted((lib / "settings").iterdir()):
            t = tallies / f.name
            if t.exists():
                if not filecmp.cmp(f, t, shallow=False):
                    sys.exit(f"refused: {t} is there and differs from {f}")
            else:
                c.move(f, t)
        c.put_aside(lib)
        c.put_aside(sums)
    c.done()


# the workload ids renamed: W16 -> i01, W17 -> i02
RENAMED = {"w16": "i01", "w17": "i02"}
# inside a CSV or JSON file: a value that is the id, a timed mix's name (lat_<id>_...), the weights spec, a row key's
# limits file. A run's own text (its command line, a source path) is left as it was
CONTENT = [(re.compile(r"lat_(w16|w17)_"), lambda m: f"lat_{RENAMED[m.group(1)]}_"),
           (re.compile(r'"(w16|w17)"'), lambda m: f'"{RENAMED[m.group(1)]}"'),
           (re.compile(r"(^|,)(w16|w17)(?=,|\r?$)", re.M), lambda m: m.group(1) + RENAMED[m.group(2)]),
           (re.compile(r"frequencies:ldbc/3/w16\b"), lambda m: "frequencies:ldbc/3/i01"),
           (re.compile(r"experiments/mix_benefit/limits/w16_"), lambda m: "experiments/mix_benefit/limits/i01_"),
           (re.compile(r'"LDBC, all of W16"'), lambda m: '"LDBC, all of I01"')]   # the cross-benchmark costs' family


def renamed(name):
    """a path component in the new ids: the id itself, a name <id>-... (rows, a question's dir, a layout-choice dir),
    a timed mix's lat_<id>_..."""
    for old, new in RENAMED.items():
        if name == old or name.startswith(old + "-"):
            name = new + name[len(old):]
        name = name.replace(f"lat_{old}_", f"lat_{new}_")
    return name


def aside_where_taken(c, pairs):
    """the old-named places whose new name the tree holds already (an output dir where today's runs wrote the new name
    beside an earlier run's old one): each aside, named; the new one is kept. -> the set of paths gone aside"""
    gone = set()
    for old, new in sorted(pairs):
        if any(p in gone for p in old.parents):
            continue
        print(f"   {old.relative_to(c.tree)}: {new.name} is there already (a newer run's); the old one aside")
        c.put_aside(old)
        gone.add(old)
    return gone


def workload_rename(c):
    """7. every place, file and value named by W16's or W17's id, renamed to i01's or i02's (deepest first); where the
    new name is taken already, the old-named place goes aside (aside_where_taken)"""
    c.step("7 the workloads W16 and W17 renamed i01 and i02")
    gone = aside_where_taken(c, [(Path(d) / n, Path(d) / renamed(n)) for d, dirs, files in os.walk(c.tree)
                                 for n in files + dirs if renamed(n) != n and (Path(d) / renamed(n)).exists()])
    rewritten = names = 0
    for d, dirs, files in os.walk(c.tree, topdown=False):
        if any(p in gone for p in (Path(d), *Path(d).parents)):
            continue
        files = [n for n in files if Path(d) / n not in gone]
        dirs = [n for n in dirs if Path(d) / n not in gone]
        for name in files:
            p = Path(d) / name
            if p.suffix in (".csv", ".json"):
                text = new = p.read_text()
                for pat, rep in CONTENT:
                    new = pat.sub(rep, new)
                if new != text:
                    c.rewrite(p, new)
                    rewritten += 1
        for name in files + dirs:
            if renamed(name) != name:
                c.move(Path(d) / name, Path(d) / renamed(name))
                names += 1
    print(f"   {rewritten} files rewritten, {names} files and dirs renamed")
    c.done()


LINKBENCH_SETS = {"all": "log", "A": "quick", "B": "quick-next"}     # LinkBench's param sets, old name: new


def linkbench_param_sets(c):
    """8. LinkBench's param sets by their new names: the <family>/<set> dirs, the 49's l01-<set> dir, a record's
    "param_set" field (written new, never through a hard link); where the new name is taken already, the old-named
    dir goes aside (aside_where_taken)"""
    c.step("8 LinkBench's param sets named by words (all: log, A: quick, B: quick-next) (1660bb77)")
    places = [d for kind in ("captures", "composed", "predicted") for d in (c.tree / kind / "linkbench").glob("*/*/*")
              if d.is_dir() and d.name in LINKBENCH_SETS]
    places += [d for d in (c.tree / "summary" / "linkbench").glob("*/uniform-49/*-*")
               if d.is_dir() and d.name.rsplit("-", 1)[1] in LINKBENCH_SETS]
    def target(d):
        if d.parent.name == "uniform-49":
            stem, old = d.name.rsplit("-", 1)
            return d.with_name(f"{stem}-{LINKBENCH_SETS[old]}")
        return d.with_name(LINKBENCH_SETS[d.name])
    gone = aside_where_taken(c, [(d, target(d)) for d in places if target(d).exists()])
    places = [d for d in places if d not in gone]
    rewritten = 0
    for d in places:
        for f in sorted(d.rglob("*.json")):
            text = f.read_text()
            new = re.sub(r'"param_set": "(all|A|B)"', lambda m: f'"param_set": "{LINKBENCH_SETS[m.group(1)]}"', text)
            if new != text:
                c.rewrite(f, new)
                rewritten += 1
    for d in places:
        c.move(d, target(d))
    print(f"   {rewritten} records rewritten, {len(places)} dirs renamed")
    c.done()


# the 49 the paper's chains cost (run/reproduce.py: cost_49): (dataset, scale, workload, its old uniform-49 dir)
PAPER_49 = (("ldbc", "3", "i01", "i01"), ("ldbc", "3", "b01", "b01"), ("imdb", "full", "j01", "j01"),
            ("linkbench", "n1m", "l01", "l01-log"), ("ldbc", "30", "i01", "i01"), ("ldbc", "30", "b01", "b01"))
# the 49 at B01's and J01's equal share, which the runtime chain costs (run/reproduce.py runtime_results): the run-time
# table's rows for composing and costing read cost-layouts' run record there; no old files (step 9), so step 10 costs them
EQUAL_SHARE_49 = (("ldbc", "3", "b01-equal-share", None), ("imdb", "full", "j01-equal-share", None))
OLD_VIEWS = {"half": "", "measured": "_measured"}     # an old file's name per chunk view: <stem>[_measured].csv


def _csv(path):
    import csv
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def layout_costs_reshaped(c):
    """9. each of the paper's 49 in the old shape (summary/<ds>/<sc>/uniform-49/<subject>/: three CSVs per chunk view,
    the half view's and the _measured one's) written into cost-layouts' one place, layout-costs/<workload>/measured/
    (kvcost.layout_cost.layout_costs), with no re-run: each row's values as written, the chunk view a column, a layout's
    measured_as `measured` or `composed`; an old dir that lacks a view's files goes aside, and step 10 costs it again"""
    c.step("9 the 49's files in cost-layouts' one place, layout-costs/<workload>/measured/ (no re-run)")
    from kvevaluator import layouts as L
    from kvevaluator.output_tree import Tree
    from kvcost.layout_cost import layout_costs as LC
    tree = Tree(c.tree)
    for ds, sc, wid, subject in PAPER_49:
        old = tree.summary(ds, sc, "uniform-49", subject)
        if not old.is_dir():
            continue
        need = [old / f"{stem}{suf}.csv" for suf in OLD_VIEWS.values()
                for stem in ("uniform_layouts_by_size", "query_costs", "query_parts")]
        out = LC.place(tree, ds, sc, wid)
        if not all(p.is_file() for p in need) or (out / "layouts.csv").exists():
            print(f"   {ds}={sc} {subject}: " + ("in the new place already" if (out / "layouts.csv").exists() else
                                                 "a view's files missing: aside, step 10 costs it"))
            c.put_aside(old)
            continue
        lay, qrows, prows = [], [], []
        for view, suf in OLD_VIEWS.items():
            for r in _csv(old / f"uniform_layouts_by_size{suf}.csv"):
                cell = r.pop("cell")
                r.pop("unit_set", None)
                lay.append({**r, "layout": cell, "chunk_view": view, "kind": "uniform", "name": L.name_of(cell),
                            "spec_sha": LC.spec_sha(L.spec_of(cell)),
                            "measured_as": "measured" if r["measured_as"] else "composed"})
            qrows += [{**r, "chunk_view": view, "kind": "uniform"} for r in _csv(old / f"query_costs{suf}.csv")]
            prows += [{**r, "chunk_view": view, "kind": "uniform"} for r in _csv(old / f"query_parts{suf}.csv")]
        items = list(dict.fromkeys((r["family"], r["query"]) for r in _csv(old / "query_costs.csv")))
        print(f"   {ds}={sc} {subject}: {len(lay)} layout rows, {len(qrows)} query rows, {len(prows)} part rows"
              + (": dry run" if c.dry else ""))
        if not c.dry:
            LC.upsert(str(out), items, lay, qrows, prows,
                      {"command": f"run/convert-paper-results.py (summary/{ds}/sf{sc}/uniform-49/{subject}/, not re-run)",
                       "layouts": sorted({r["layout"] for r in lay}), "source": "measured", "wall_s": None})
            c.touched += len(list(out.iterdir()))
        c.put_aside(old)
    c.done()


def costed_49(c):
    """10. each of the paper's 49 whose captures the tree holds and whose layout-costs place step 9 did not fill: the
    dataset's every pair composed (kvcost compose, into the tree's composed/; a replaced composed dir goes aside), then
    the 49 costed by kvcost cost-layouts (today's code: every model, both chunk views) into the tree"""
    c.step("10 the 49 composed and costed again where step 9 had no complete old files")
    from kvevaluator.output_tree import Tree
    from kvcost.layout_cost import layout_costs as LC
    from kvcost.predict_accuracy.uniform_49_tallies import NAMES
    tree = Tree(c.tree)
    composed = set()
    for ds, sc, wid, _subject in PAPER_49 + EQUAL_SHARE_49:
        if not tree.place_dir("captures", ds, sc).is_dir() or (LC.place(tree, ds, sc, wid) / "layouts.csv").exists():
            continue
        env = {**os.environ, "KVEVAL_OUTPUT_DIR": str(c.tree)}
        # cost-layouts reads the tree with --from, as a tree read (not the output dir it writes): the named formats'
        # runs a tree holds as not run (validity caps, SF30's limit) are no refusal there
        calls = ([] if (ds, sc) in composed else [["compose", "--dataset", f"{ds}={sc}"]]) + \
            [["cost-layouts", "--dataset", f"{ds}={sc}", "--workload", wid, *NAMES, "--from", str(c.tree)]]
        composed.add((ds, sc))
        for call in calls:
            print(f"   kvcost {' '.join(call[:4])}" + (": dry run" if c.dry else ""))
            if c.dry:
                continue
            log = c.aside / "logs" / f"{call[0]}-{ds}-{sc}-{wid}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with open(log, "w") as fh:
                r = subprocess.run([sys.executable, "-m", "kvcost.cli.cli", *call], cwd=ROOT, stdout=fh,
                                   stderr=subprocess.STDOUT, env=env)
            if r.returncode:
                sys.exit(f"refused: kvcost {call[0]} failed (rc {r.returncode}); its log: {log}")
            for aside in sorted(tree.place_dir("composed", ds, sc).parent.glob(
                    tree.place_dir("composed", ds, sc).name + ".aside-*")):
                c.put_aside(aside)          # the composed dir compose replaced
            c.touched += 1
    c.done()


def scale_transfer_predicted(c):
    """11. the scale_transfer study's predicted tables (studies/scale-transfer/runs/**/uniform.csv, cost-layouts
    --count-source's old one-file shape) as cost-layouts' query_costs.csv and layouts.csv in the same dir, the half
    view, with no re-run; the old file aside"""
    c.step("11 the scale_transfer study's predicted tables as query_costs.csv (no re-run)")
    from kvevaluator import layouts as L
    from kvcost.layout_cost import compose
    from kvcost.layout_cost import cost_models as CM
    from kvcost.layout_cost import layout_costs as LC
    for path in sorted((c.tree / "studies" / "scale-transfer").glob("**/uniform.csv")):
        rows = _csv(path)
        if (path.parent / "query_costs.csv").exists() or not rows or "source" not in rows[0]:
            continue
        q, sums = [], {}
        for r in rows:
            cell = r["layout"]
            kind = "uniform" if L.is_cell(cell) else "mix"
            p, a = L.cell_settings(cell) if kind == "uniform" else ("", "")
            q.append({**r, "body": r["plan"], "chunk_view": "half", "kind": kind, "props": p, "adjacency": a})
            t = sums.setdefault(cell, {"workload": "", "layout": cell, "chunk_view": "half", "kind": kind,
                                       "name": r["name"], "props": p, "adjacency": a, "spec_sha": "",
                                       "measured_as": "predicted", "lanes": {}})
            for k in compose.MEASURES + compose.CHANNELS:
                if r.get(k) not in ("", None):
                    t["lanes"][k] = t["lanes"].get(k, 0.0) + float(r["frequency"]) * float(r[k])
        lay = []
        for t in sums.values():
            lanes = t.pop("lanes")
            cur = CM.currencies({k: lanes.get(k, 0.0) for k in compose.MEASURES})
            lay.append({**t, **{k: round(v, 3) for k, v in lanes.items()}, "total_ops": round(cur["ops"], 3),
                        "total_bytes": round(cur["bytes"], 1)})
        items = list(dict.fromkeys((r["family"], r["query"]) for r in rows))
        print(f"   {path.relative_to(c.tree)}: {len(rows)} rows" + (": dry run" if c.dry else ""))
        if not c.dry:
            LC.upsert(str(path.parent), items, lay, q, [], {"command": "run/convert-paper-results.py (uniform.csv, not "
                                                                       "re-run)", "source": rows[0]["source"]})
            c.touched += 4
        c.put_aside(path)
    c.done()


def _old_assign(a):
    """an assignment in the rows' old form ({"props|Person": "split"}) -> the rows' form ({"Person props": "Sp"}); one in
    the rows' form already, as it is"""
    from kvcost.layout_search import results as R
    if not isinstance(a, dict) or not a or not all("|" in k for k in a):
        return a
    return R.row_assignment({tuple(k.split("|")): v for k, v in a.items()})


def _new_row(r):
    """one search row with every assignment it holds (its own, a staircase step's, a front point's, a named point's) in
    the rows' form"""
    r = dict(r)
    if "assignment" in r:
        r["assignment"] = _old_assign(r["assignment"])
    for k in ("staircase", "points"):
        if isinstance(r.get(k), list):
            r[k] = [dict(x, assignment=_old_assign(x.get("assignment"))) if isinstance(x, dict) and "assignment" in x
                    else x for x in r[k]]
    if isinstance(r.get("named"), dict):
        r["named"] = {n: (dict(p, assignment=_old_assign(p.get("assignment"))) if isinstance(p, dict) and "assignment" in p
                          else p) for n, p in r["named"].items()}
    return r


def mix_search_files(c):
    """12. the mix search's files named by the settings a part may take (results.stem): <option set>.json ->
    mixes-of-<settings>[-by-direction].json (and its .csv, written again from the rows; mixes/<option set>-<name>.json ->
    mixes/<stem>-<name>.json; <option set>-local-search.json -> <stem>-local-search.json); every assignment in the rows'
    form (parts as --mix spells them, settings in short: {"knows lists": "Ch4k"}); each <option set>-two-step-times.json's
    fewest row's two-step times carried into the matching row's times (two_step_a_s, two_step_b_s, and where they came
    from), then the timed file aside. No re-run. Where a new name is there already (an output dir where a newer run
    wrote it), the old file goes aside unconverted"""
    c.step("12 the mix search's files named mixes-of-<settings>, their assignments in short names (no re-run)")
    from kvcost.layout_search import export as X
    from kvcost.layout_search import results as R
    dirs = sorted({p.parent for p in c.tree.glob("**/mix-search/*/*.json")
                   if p.name.split(".")[0].split("-two-step-times")[0].split("-local-search")[0] in R.OPTION_SETS})
    for d in dirs:
        for oset in R.OPTION_SETS:
            rows_file, timed, local = d / f"{oset}.json", d / f"{oset}-two-step-times.json", d / f"{oset}-local-search.json"
            if rows_file.exists():
                rows = [_new_row(r) for r in json.loads(rows_file.read_text())["rows"]]
                if timed.exists():
                    for t in json.loads(timed.read_text())["rows"]:
                        two = ((t.get("start") or {}).get("two_step") or {})
                        if t.get("question") != "fewest" or "fixed_plan_s" not in two:
                            continue
                        for r in rows:
                            if (r.get("question"), r.get("model"), r.get("limit")) == ("fewest", t["model"], t.get("limit")):
                                r["times"] = {**r.get("times", {}), "two_step_a_s": two["fixed_plan_s"],
                                              "two_step_b_s": two["repick_s"], "two_step_solver": t.get("solver", "tables"),
                                              "two_step_times_from": f"{timed.relative_to(c.tree)} (a one-node search)"}
                by = {}
                for r in rows:
                    sizes = [x[0][len("chunked_c"):] for x in (r.get("sizes") or [])]
                    by.setdefault(R.stem(oset, sizes if oset == "per-type-any-size" else None), []).append(r)
                taken = [d / f"{stem}.json" for stem in sorted(by) if (d / f"{stem}.json").exists()]
                if taken:
                    print(f"   {rows_file.relative_to(c.tree)}: {', '.join(t.name for t in taken)} is there already (a "
                          f"newer run's); the old file aside, unconverted")
                    by = {}
                for stem, rs in sorted(by.items()):
                    out = d / f"{stem}.json"
                    print(f"   {rows_file.relative_to(c.tree)} -> {out.name}: {len(rs)} rows" + (": dry run" if c.dry else ""))
                    if not c.dry:
                        out.write_text(json.dumps({"rows": rs}, indent=1, sort_keys=True) + "\n")
                        one = [r for r in rs if r.get("assignment")]
                        if one:
                            cols, csv_rows = X.result_rows(one)
                            X.write_csv(str(d / f"{stem}.csv"), cols, csv_rows)
                        c.touched += 1
                for p in (rows_file, d / f"{oset}.csv", d / f"{oset}.json.lock", timed):
                    if p.exists():
                        c.put_aside(p)
            if local.exists() and (d / f"{R.stem(oset)}-local-search.json").exists():
                print(f"   {local.relative_to(c.tree)}: {R.stem(oset)}-local-search.json is there already; the old aside")
                c.put_aside(local)
            elif local.exists():
                out = d / f"{R.stem(oset)}-local-search.json"
                r = json.loads(local.read_text())
                r["assignment"] = _old_assign(r.get("assignment"))
                print(f"   {local.relative_to(c.tree)} -> {out.name}" + (": dry run" if c.dry else ""))
                if not c.dry:
                    out.write_text(json.dumps(r, indent=1) + "\n")
                    c.touched += 1
                c.put_aside(local)
        for p in sorted((d / "mixes").glob("*.json")) if (d / "mixes").is_dir() else []:
            oset = next((o for o in sorted(R.OPTION_SETS, key=len, reverse=True) if p.name.startswith(o + "-")), None)
            if oset is not None and p.with_name(R.stem(oset) + p.name[len(oset):]).exists():
                c.put_aside(p)              # a newer run's spec is there already
            elif oset is not None:
                c.move(p, p.with_name(R.stem(oset) + p.name[len(oset):]))
    c.done()


# a path a run recorded: relative to the repo root (../ up out of it), absolute on a machine's disk, or a placeholder
MACHINE_PATH = re.compile(r"(?:\.\./)+[^\s\"'`,;)\]}]+|/(?:mnt|users|home|tmp)/[^\s\"'`,;)\]}]+|<scratch>/[^\s\"'`,;)\]}]*")
INTO_TREE = re.compile(r"(?:^|/)paper-results(?:\.converted)?(?:/|$)")      # a path into a copy of the paper's tree


def machine_free(c, text):
    """text with each recorded machine path (MACHINE_PATH) as paths.recorded_path writes it, for the tree c.tree: a path
    into a copy of paper-results/ relative to the tree, a placeholder's place under <elsewhere>"""
    from kvevaluator.paths import ELSEWHERE, ROOT, recorded_path

    def one(m):
        tok = m.group(0)
        into = INTO_TREE.search(tok)
        if into:
            return tok[into.end():].rstrip("/") or "."
        if tok.startswith("<scratch>/"):
            return ELSEWHERE + tok[len("<scratch>"):]
        return recorded_path(os.path.normpath(os.path.join(ROOT, tok)) if tok.startswith("../") else tok, c.tree)
    return MACHINE_PATH.sub(one, text)


def machine_paths(c):
    """13. every file whose text names a machine path (a .json, .jsonl, .csv or .md file), written again with
    machine_free; the rest of the file as it was (no re-run)"""
    c.step("13 no machine path in a file: each recorded path relative to the tree or the repo (no re-run)")
    for p in sorted(c.tree.rglob("*")):
        if p.suffix not in (".json", ".jsonl", ".csv", ".md") or not p.is_file():
            continue
        text = p.read_text()
        new = machine_free(c, text) if MACHINE_PATH.search(text) else text
        if new != text:
            c.rewrite(p, new)
    c.done()


COST_CACHE_KEYS = {"tables_s": "cost_cache_s", "table_keys": "cost_cache_keys", "table_entries": "cost_cache_entries"}
COST_CACHE_SOLVERS = {"tables": "cost-cache", "table-free": "no-cost-cache"}
COST_CACHE_OLD = re.compile(r'"(?:tables_s|table_keys|table_entries)"|"(?:two_step_)?solver": "table')


def cost_cache_names(x):
    """x with each mix cost cache field renamed (COST_CACHE_KEYS, in place in its dict's order) and each solver value
    renamed (COST_CACHE_SOLVERS); the rest as it was"""
    if isinstance(x, list):
        return [cost_cache_names(v) for v in x]
    if not isinstance(x, dict):
        return x
    return {COST_CACHE_KEYS.get(k, k): (COST_CACHE_SOLVERS.get(v, v) if k in ("solver", "two_step_solver") else
                                        cost_cache_names(v)) for k, v in x.items()}


def cost_cache_rename(c):
    """14. the mix cost cache's names in the result rows, the combined panel's found_by and the saved cache's dir (no
    re-run)"""
    c.step("14 the mix search's tables are the mix cost cache: row fields, solver values, the cache dir (no re-run)")
    from kvcost.layout_search import results as R
    for p in sorted(c.tree.rglob("*")):
        if "removal-queue" in p.relative_to(c.tree).parts or not p.is_file():
            continue
        if p.suffix == ".json":
            text = p.read_text()
            if not COST_CACHE_OLD.search(text):
                continue
            d = cost_cache_names(json.loads(text))
            if isinstance(d, dict) and set(d) == {"rows"} and "mix-search" in p.parts:   # results.ResultFile's form
                rows = {R.row_key(r): r for r in d["rows"]}
                new = json.dumps({"rows": [rows[k] for k in sorted(rows)]}, indent=1, sort_keys=True) + "\n"
            else:                       # another record (a run record's timings): the names alone, its form kept
                new = re.sub(r'"(tables_s|table_keys|table_entries)":', lambda m: f'"{COST_CACHE_KEYS[m.group(1)]}":', text)
                new = re.sub(r'"((?:two_step_)?solver)": "(tables|table-free)"',
                             lambda m: f'"{m.group(1)}": "{COST_CACHE_SOLVERS[m.group(2)]}"', new)
                if json.loads(new) != d:
                    sys.exit(f"refused: {p}: renaming its text does not give its renamed fields")
            c.rewrite(p, new)
        elif p.suffix == ".csv" and p.name == "combined_options.csv":
            text = p.read_bytes().decode()          # its rows end in \r\n, as csv writes them: kept
            new = text.replace("kvcost search-mix, table-free)", "kvcost search-mix, no cost cache)")
            if new != text:
                c.rewrite(p, new)
    for d in sorted(c.tree.glob("stats/*/*/table-cache")):      # a cache: where the new name is there, the old aside
        if d.is_dir():
            (c.put_aside if d.with_name("mix-cost-cache").exists() else lambda s: c.move(s, s.with_name("mix-cost-cache")))(d)
    c.done()


def _rows_file(path):
    """(header, rows, line end) of a CSV, as written"""
    import csv
    with open(path, newline="") as f:
        first = f.readline()
        f.seek(0)
        r = csv.DictReader(f)
        return r.fieldnames, list(r), "\r\n" if first.endswith("\r\n") else "\n"


def _csv_text(header, rows, end):
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, lineterminator=end)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def bodies_offered(c):
    """15. a capture holds rows only of the bodies its cell offers today (no re-run)"""
    c.step("15 a capture holds rows only of the bodies its cell offers today (2026-11-20)")
    import hashlib
    from kvevaluator import layouts, workloads
    from kvevaluator.measure.grid_bodies import rows_offered_today
    from kvevaluator.output_tree import Capture
    new_sha, n_drop, n_inv = {}, 0, 0
    for counts in sorted(c.tree.glob("*/*/*/*/*/counts")):
        cap = Capture(str(counts.parent))
        if counts.parts[-6] not in ("captures", "composed") or "removal-queue" in counts.relative_to(c.tree).parts:
            continue
        wl = cap.workload()
        if wl not in workloads.names() or workloads.get(wl).kind != "read":
            continue
        for f in sorted(counts.glob("props_*.csv")):           # a layout's counts (not a figure's rows filed beside)
            header, rows, end = _rows_file(f)
            kept, fate = rows_offered_today(wl, layouts.resolve(f.stem).spec, rows)
            if not fate:
                continue
            n_drop += sum(v == "dropped" for v in fate.values())
            n_inv += sum(v == "invalid" for v in fate.values())
            text = _csv_text(header, kept, end)
            c.rewrite(f, text)
            new_sha[str(f.relative_to(c.tree))] = hashlib.sha256(text.encode()).hexdigest()
            for kind in ("steps", "parts", "row-hashes", "exact"):
                g = counts.parent / kind / f.name
                if not g.exists():
                    continue
                h, rs, e = _rows_file(g)
                gone = {k for k, v in fate.items() if v == "dropped" or kind != "exact"}   # compose keeps an invalid row's exactness
                left = [r for r in rs if (r["query"], str(r["param_idx"]), r["body"]) not in gone]
                if len(left) != len(rs):
                    text = _csv_text(h, left, e)
                    c.rewrite(g, text)
                    new_sha[str(g.relative_to(c.tree))] = hashlib.sha256(text.encode()).hexdigest()
    for rec in sorted(c.tree.glob("composed/**/counts/*.json")):
        text = rec.read_text()
        d = json.loads(text)
        shas = d.get("composed_from_sha") or {}
        if any(k in new_sha for k in shas):
            form = lambda x: json.dumps(x, indent=1, sort_keys=True) + text[len(text.rstrip("\n")):]
            if form(d) != text:                     # compose_cells' form, which the new text keeps
                sys.exit(f"refused: {rec} is not in compose_cells' JSON form")
            d["composed_from_sha"] = {k: new_sha.get(k, v) for k, v in shas.items()}
            c.rewrite(rec, form(d))
    print(f"   {n_drop} rows of bodies no longer candidates left out, {n_inv} rows of bodies invalid on their cell")
    c.done()


def shown_names(c):
    """16. a uniform layout's shown name carries every Chunked side's chunk size (no re-run)"""
    c.step("16 a uniform layout's shown name with each Chunked side's size, 64 KB as Ch64k (2026-11-20)")
    from kvevaluator import layouts
    from kvcost.layout_search.front import uniform_name

    def named(cell, old):
        try:
            return layouts.name_of(cell) if layouts.is_cell(cell) else old
        except ValueError:
            return old
    n_rows = 0
    for p in sorted(c.tree.glob("summary/**/*.csv")):
        if "removal-queue" in p.relative_to(c.tree).parts:
            continue
        with open(p, newline="") as f:
            head = f.readline().rstrip("\r\n").split(",")
        pair = ("layout", "name") if {"layout", "name"} <= set(head) else ("cell", "layout") if {"cell", "layout"} <= set(head) else None
        if pair is None:
            continue
        header, rows, end = _rows_file(p)
        text = p.read_bytes().decode()
        if _csv_text(header, rows, end) != text:
            sys.exit(f"refused: {p} is not in csv.DictWriter's form (a rewrite would change more than a name)")
        moved = [r for r in rows if r[pair[1]] != named(r[pair[0]], r[pair[1]])]
        if not moved:
            continue
        for r in moved:
            r[pair[1]] = named(r[pair[0]], r[pair[1]])
        n_rows += len(moved)
        c.rewrite(p, _csv_text(header, rows, end))
    for p in sorted(c.tree.glob("summary/**/uniform_layouts.json")):
        text = p.read_text()
        rows = json.loads(text)
        if json.dumps(rows, indent=1) != text:
            sys.exit(f"refused: {p} is not in its writer's JSON form")
        new = [{**r, "name": uniform_name(r["assign"])} for r in rows]
        if new != rows:
            n_rows += sum(a != b for a, b in zip(new, rows))
            c.rewrite(p, json.dumps(new, indent=1))
    print(f"   {n_rows} rows named again")
    c.done()


def complex_kv_reads_run(c):
    """17. the complex-kv-reads figure's run in one place (no re-run)"""
    c.step("17 the complex-kv-reads figure's run in one place, engines/ldbc/<scale>/complex-kv-reads/ (2026-11-21)")
    import csv
    import io
    from kvengines import complex_kv_reads as CKR
    for old in sorted(c.tree.glob("engines/ldbc/*/figure2-times")):
        place = old.parent
        run = place / CKR.RUN
        fid = place / "fidelity"
        if (run / "interactive" / "live_kvstats.csv").exists():
            sys.exit(f"refused: {run} holds counts already, and {old} is still there")

        def rows_of(path, queries):
            with open(path, newline="") as f:
                r = csv.DictReader(f)
                return r.fieldnames, [x for x in r if x["query"] in queries]

        def text(header, rows):
            out = io.StringIO()
            w = csv.DictWriter(out, fieldnames=header, restval="", lineterminator="\n")
            w.writeheader()
            w.writerows(sorted(rows, key=lambda x: (x["query"], x["db"], int(x["param_idx"]))))
            return out.getvalue()

        head, ic1 = rows_of(fid / "interactive" / "live_kvstats.csv", {"ic1"})
        head3, ic3 = rows_of(old / "hosted-ic3" / "live_kvstats.csv", {"ic3"})
        if head != head3 or not ic1 or not ic3:
            sys.exit(f"refused: {fid} and {old}/hosted-ic3 hold other columns, or no ic1 / ic3 rows")
        mb_head, hop = rows_of(fid / "microbench" / "live_kvstats.csv", {"hop1p7_knows"})
        if "data_created" not in mb_head:               # today's header (measure.py MB_COLUMNS)
            mb_head = mb_head[:mb_head.index("ineqpath") + 1] + ["data_created"] + mb_head[mb_head.index("ineqpath") + 1:]
        new = {run / "interactive" / "live_kvstats.csv": text(head, ic1 + ic3), run / "microbench" / "live_kvstats.csv": text(mb_head, hop)}
        moves = [(fid / "interactive" / "dgraph" / "ic1" / "p1" / "kvstats.json", run / "interactive" / "dgraph" / "ic1" / "p1" / "kvstats.json"),
                 (old / "hosted-ic3" / "dgraph" / "ic3" / "p1" / "kvstats.json", run / "interactive" / "dgraph" / "ic3" / "p1" / "kvstats.json"),
                 (place / "dgraph-eq-filter-path" / "ic1-p1.json", run / "dgraph-eq-filter-path" / "ic1-p1.json"),
                 (place / "nebula-path-binding" / "ic1-p1.json", run / "nebula-path-binding" / "ic1-p1.json"),
                 (old / "times" / "equivalent" / "small-cache" / "live.csv", run / "times" / "equivalent" / "small-cache" / "live.csv"),
                 (old / "times" / "equivalent" / "evaluator-small-cache" / "evaluator.csv", run / "times" / "equivalent" / "small-cache" / "evaluator.csv")]
        missing = [str(a) for a, _ in moves if not a.exists()]
        if missing:
            sys.exit(f"refused: not there: {missing}")
        for path, body in new.items():
            if not c.dry:
                path.parent.mkdir(parents=True, exist_ok=True)
            c.rewrite(path, body)
        for a, b in moves:
            c.move(a, b)
        c.put_aside(old / "hosted-ic3" / "live_kvstats.csv")
        left = [p for p in old.rglob("*") if p.is_file()] if old.exists() else []
        if left and not c.dry:
            sys.exit(f"refused: {old} still holds {left[:3]}")
        print(f"   {place.relative_to(c.tree)}: {len(ic1)} ic1 + {len(ic3)} ic3 rows, {len(hop)} hop1p7_knows rows, {len(moves)} files moved")
    c.done()


def complex_kv_reads_variants(c):
    """18. the complex-kv-reads run's two IC1 variants as rows: an engine configuration and a query-text variant"""
    c.step("18 the complex-kv-reads run's IC1 variants as rows: db dgraph-no-index, query ic1-edge-list (2026-11-23)")
    import csv
    import hashlib
    import io
    from kvengines import complex_kv_reads as CKR
    OPS = (("S", "seek_count"), ("G", "get_count"), ("N", "next_count"))
    LANES = ("get_bytes", "seek_bytes", "next_bytes")

    def table(path):
        """(header, rows, line end) of a CSV that reads and writes back to the same bytes; refused otherwise"""
        raw = path.read_bytes().decode()
        end = "\r\n" if "\r\n" in raw else "\n"
        reader = csv.DictReader(io.StringIO(raw, newline=""))
        rows = list(reader)
        if text(reader.fieldnames, rows, end) != raw:
            sys.exit(f"refused: {path} does not read and write back to the same bytes")
        return reader.fieldnames, rows, end

    def text(header, rows, end):
        out = io.StringIO(newline="")
        w = csv.DictWriter(out, fieldnames=header, lineterminator=end)
        w.writeheader()
        w.writerows(rows)
        return out.getvalue()

    for run in sorted(c.tree.glob(f"engines/ldbc/*/{CKR.RUN}")):
        counts = run / "interactive" / "live_kvstats.csv"
        variants = sorted(run.glob("dgraph-eq-filter-path/*-p*.json")) + sorted(run.glob("nebula-path-binding/*-p*.json"))
        added = 0
        if variants:
            head, rows, end = table(counts)
            for f in variants:
                d = json.loads(f.read_text())
                kind = f.parent.name
                base_db, field, same, other = (("dgraph", "eqfilterpath", "index", "per_vertex") if kind == "dgraph-eq-filter-path"
                                               else ("nebula", "form", "path", "edge_list"))
                db, query = ("dgraph-no-index", d["query"]) if base_db == "dgraph" else ("nebula", f"{d['query']}-edge-list")
                live = [r for r in rows if (r["query"], r["db"], r["param_idx"]) == (d["query"], base_db, str(d["param_idx"]))]
                if len(live) != 1:
                    sys.exit(f"refused: {counts} holds {len(live)} rows of {base_db} {d['query']} p{d['param_idx']}, want 1 ({f})")
                for r in d["runs"]:
                    if r[field] == same and any(int(float(live[0][k])) != r["normalized"][n] for k, n in OPS):
                        sys.exit(f"refused: {f}: its {same} run is not the live row of {base_db} {d['query']} p{d['param_idx']}")
                if any((r["query"], r["db"], r["param_idx"]) == (query, db, str(d["param_idx"])) for r in rows):
                    sys.exit(f"refused: {counts} holds {db} {query} p{d['param_idx']} already, and {f} is still there")
                new = next(r for r in d["runs"] if r[field] == other)
                n = new["normalized"]
                row = dict(live[0], db=db, query=query, secs="", **{k: str(n[name]) for k, name in OPS},
                           SG=str(n["seek_count"] + n["get_count"]), **{k: str(n[k]) for k in LANES})
                rows.append(row)
                base_cell = json.loads((run / "interactive" / base_db / d["query"] / f"p{d['param_idx']}" / "kvstats.json").read_text())
                cell = {"query_sha": base_cell["query_sha"] if base_db == "dgraph" else hashlib.sha256(new["query"].encode()).hexdigest()[:16],
                        "params": d["params"], "data_created": live[0]["data_created"], "measured_at": None, "raw": new["raw"]}
                cell.update({"config": new["config"], "answer_rows": new["answer_rows"]} if base_db == "dgraph"
                            else {"query": new["query"], "answer": new["answer"]})
                out = run / "interactive" / db / query / f"p{d['param_idx']}" / "kvstats.json"
                if not c.dry:
                    out.parent.mkdir(parents=True, exist_ok=True)
                c.rewrite(out, json.dumps(cell, indent=1) + "\n")
                added += 1
            # the file's own order (query, db, param) when it has one, the new rows in their places; else at its end
            key = lambda r: (r["query"], r["db"], int(r["param_idx"]))
            old_rows = rows[:len(rows) - added]
            if old_rows == sorted(old_rows, key=key):
                rows = sorted(rows, key=key)
            c.rewrite(counts, text(head, rows, end))
            for f in variants:
                c.put_aside(f)
        renamed = 0
        for times in sorted(run.glob("times/*/*")):
            for name, path_was, path_now in (("live.csv", "per_vertex", "per_vertex"), ("evaluator.csv", "engine-per_vertex", "engine")):
                f = times / name
                if not f.exists():
                    continue
                head, rows, end = table(f)
                mine = [r for r in rows if r["db"] == "dgraph" and r["path"] == path_was]
                for r in mine:
                    r["db"], r["path"] = "dgraph-no-index", path_now
                if mine:
                    renamed += len(mine)
                    c.rewrite(f, text(head, rows, end))
            for f in sorted(times.glob("perf/*-dgraph-*-per_vertex.*")):
                if "-dgraph-no-index-" not in f.name:
                    c.move(f, f.with_name(f.name.replace("-dgraph-", "-dgraph-no-index-", 1)))
        if variants or renamed:
            print(f"   {run.relative_to(c.tree)}: {added} variant runs as rows and cells, {renamed} timed rows named dgraph-no-index")
    c.done()


def complex_kv_reads_regimes(c):
    """19. the complex-kv-reads run's cache regimes under the registry's names"""
    c.step("19 the complex-kv-reads run's cache regime named as the registry names it: warm-bounded (2026-11-23)")
    import csv
    import io
    from kvengines import complex_kv_reads as CKR
    NOW = {"small-cache": "warm-bounded"}       # the one earlier name a run was timed under

    for run in sorted(c.tree.glob(f"engines/ldbc/*/{CKR.RUN}")):
        for was, now in NOW.items():
            old = run / "times" / "equivalent" / was
            if not old.is_dir():
                continue
            rows_named = 0
            for name in ("live.csv", "evaluator.csv"):
                f = old / name
                if not f.exists():
                    continue
                raw = f.read_bytes().decode()
                end = "\r\n" if "\r\n" in raw else "\n"
                reader = csv.DictReader(io.StringIO(raw, newline=""))
                rows = list(reader)

                def text(rows):
                    out = io.StringIO(newline="")
                    w = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator=end)
                    w.writeheader()
                    w.writerows(rows)
                    return out.getvalue()

                if text(rows) != raw:
                    sys.exit(f"refused: {f} does not read and write back to the same bytes")
                for r in rows:          # the regime's name as a whole word: live "equivalent/<regime>" (the paper's
                    point = r["operating_point"].split("/")     # run: "equivalent" alone, kept), evaluator "<regime>"
                    r["operating_point"] = "/".join(point[:-1] + [now if point[-1] == was else point[-1]])
                    r["settings"] = ";".join(f"ENGINES_REGIME={now}" if x == f"ENGINES_REGIME={was}" else x
                                             for x in r["settings"].split(";"))
                rows_named += len(rows)
                c.rewrite(f, text(rows))
            files = c.move(old, old.with_name(now))
            print(f"   {run.relative_to(c.tree)}: times/equivalent/{was}/ is {now}/ ({files} files, {rows_named} rows named)")
    c.done()


STEPS = (composed_copies, mix_search_rows, combined_other, job_control, predicted_49_tallies, tally_library,
         workload_rename, linkbench_param_sets, layout_costs_reshaped, costed_49, scale_transfer_predicted,
         mix_search_files, machine_paths, cost_cache_rename, bodies_offered, shown_names, complex_kv_reads_run,
         complex_kv_reads_variants, complex_kv_reads_regimes)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--tree", required=True, type=Path, help="the tree converted in place (a copy of paper-results/, "
                    "or an output dir)")
    ap.add_argument("--aside", type=Path, help="where files the current shape no longer holds go (default <tree>.aside)")
    ap.add_argument("--dry-run", action="store_true", help="print each step and its count; change nothing")
    ap.add_argument("--steps", default=None, help="only these steps, by number (e.g. 14): for an output dir whose files "
                    "are in the earlier steps' shapes already, so the other steps never walk its removal-queue/ (they "
                    "would convert the copies set aside there)")
    a = ap.parse_args()
    tree = a.tree.resolve()
    if tree == paper_results_dir().resolve():
        sys.exit(f"refused: {tree} is paper-results/ itself; convert a copy (cp -al paper-results <copy>)")
    if not (tree / "captures").is_dir():
        sys.exit(f"refused: {tree} is not an output tree (no captures/)")
    aside = (a.aside or tree.with_name(tree.name + ".aside")).resolve()
    checkout = next((p for p in (aside, *aside.parents) if (p / ".git").exists()), None)
    if checkout is not None and subprocess.run(["git", "-C", str(checkout), "check-ignore", "-q", f"{aside}/"]).returncode:
        # step 10's logs and every file set aside go under it: never a file git would see
        sys.exit(f"refused: the aside dir {aside} is inside the git checkout {checkout} and not ignored there; give "
                 f"--aside a dir outside it")
    c = Converter(tree, aside, a.dry_run)
    print(f"converting {tree}{' (dry run)' if a.dry_run else ''}; aside: {c.aside}")
    total = 0
    run = STEPS if a.steps is None else [STEPS[int(n) - 1] for n in a.steps.split(",")]
    for step in run:
        step(c)
        total += c.touched
    print(f"done: {total} files touched in {len(run)} steps")


if __name__ == "__main__":
    main()
