#!/usr/bin/env python3
"""Derive complex-kv-reads' table: the KV pairs and bytes each live database reads on three multi-step reads, one where
each database reads the most (real engines, SF0.3): a person's friends with seven properties each (microbench
hop1p7_knows), LDBC IC1 and LDBC IC3 on LDBC's hosted parameter rows. Reads the figure's run in the tree --from names,
<tree>/engines/ldbc/sf<SF>/complex-kv-reads/ (kvengines.complex_kv_reads says what it holds: the live counts and the
times, all from one load), one parameter per query (its CELLS; the microbench person is the one with the median number
of friends, 63). A run that stores its IC1 variants in the earlier shape (dgraph-eq-filter-path/,
nebula-path-binding/) is refused, with the converter that fixes a copy (CKR.check_shape).

The bars are engine configurations, kvengines.complex_kv_reads.DBS (Dgraph through its first-name index, Dgraph
without it, NebulaGraph, ArangoDB, TuGraph, TuGraph detached), each measured on the queries its entry names (Dgraph
without its index on IC1 only: in the other panels Dgraph's bar stands in, CKR.shown_as); query-text variants
(QUERY_VARIANTS: NebulaGraph's IC1 binding the walk's edge list) are rows of their base query's panel. The tables
hold every configuration and variant the run holds; a run without one of MAY_LACK (the paper's run predates TuGraph)
is derived on the others, and says so. The number keys and the claims are over a selection: --show, default
FIGURE_DBS (the paper's figure: Dgraph without its index on IC1, TuGraph's detach_property off); the claims stop the
derive only on FIGURE_DBS. Each of the draw's other options (SHOW_OPTIONS) the run holds gets the same keys and claims
as log lines, said, never refused.

Writes complex-kv-reads.csv: one row per panel (1-3), configuration and query (the panel's query or a query-text
variant of it); title, the op counts G, S, N and the bytes get_bytes (GET) and scan_bytes (SEEK + NEXT).
Writes complex-kv-reads-times.csv: one row per panel, configuration measured on its query, and arm (live: the live
engine; engine / cheapest: the engine's plan and our cheapest plan on our evaluator, for the configurations whose
engine has a format on it: not TuGraph; the cheapest plan is the format's, timed on the engine's first configuration:
CKR.cheapest_of), each engine on one CPU with a 30 MiB block cache at SF0.3 (the figure's regime, CKR.REGIME:
warm-bounded), and the same per other regime the run was timed at (CKR.REGIME_OPTIONS: cold-bounded): runs, median / min / max ms, and on the live engines the time in the KV store (kv_ms = KV CPU +
disk wait; kv_pooled: its share pooled over the runs; kv_source: measured; archive when the run's machine records no
call stacks for that engine: kv_ms is then the paper's share of that cell's time times this run's median; not measured
when there is no paper's run to take it from). The
rule: kvengines.complex_kv_reads.
Writes complex-kv-reads-queries.csv: one row per panel and language, the query text as run (Dgraph DQL, NebulaGraph
nGQL, ArangoDB AQL, TuGraph's Cypher; engines/queries/) and a Cypher form equivalent to them (engines/queries/*/cypher/).
Writes complex-kv-reads-selections.csv: the draw's figures, one row each: figure (its name's suffix; "" the default),
dbs (its configurations, in order), regimes (the cache regimes it draws: one, or the figure's and another side by
side) and elsewhere (db=its stand-in, for a configuration measured on some panels only): the default is the keys'
selection, then each of SHOW_OPTIONS the run holds, then each of REGIME_OPTIONS the run was timed at.
Writes numbers complex_kv_reads.tex, over the selection:
complex-kv-reads.q<1-3>.<ops|bytes>.most-over-fewest (the most KV operations, G + S + N, and the most bytes, GET +
SEEK + NEXT, over the run's databases as a multiple of the fewest), complex-kv-reads.q3.kv-share.<min|max>.pct (the
smallest and largest share of a database's Q3 time inside its KV store) and
complex-kv-reads.q2.latency.slowest-over-fastest (live median times).

The figure's claims (one configuration reads the most on every parameter it was measured on; ArangoDB reads the most on
IC1, also with each of CLAIM_VARIANTS in place of its configuration's text;
on Q3 NebulaGraph issues the most operations and reads the fewest bytes) stop the derive at the paper's scale; at
another scale they are said, not refused.

    uv run python paper/derive/complex_kv_reads.py                       # the paper's run (paper-results/, once converted)
    uv run python paper/derive/complex_kv_reads.py --from output         # a `just results complex-kv-reads` run
"""
import argparse
import csv
import json

from kvengines import complex_kv_reads as CKR
from kvevaluator import sizes
from kvevaluator.paths import ROOT
import snapshots
from snapshots import COMPLEX_KV_READS, multiple, pct, write_numbers, write_table

ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
snapshots.add_tree_args(ap)
ap.add_argument("--show", default=None, help="the engine configurations the number keys and claims are over, a comma "
                                             f"list of {', '.join(CKR.DBS)} (default: {','.join(CKR.FIGURE_DBS)})")
ap.set_defaults(run=sizes.resolve(CKR.SCALE))
a = ap.parse_args() if __name__ == "__main__" else ap.parse_args([])
if a.tables is not None:
    snapshots.TABLES_DIR = snapshots.NUMBERS_DIR = a.tables
SF = snapshots.scale_in(a, "ldbc")
if SF is None:
    raise SystemExit("complex-kv-reads runs on LDBC: --dataset names no ldbc scale")
TREE = snapshots.tree_of(a.tree)
RUN = CKR.run_dir(TREE, SF)
AT_PAPER_SCALE = SF == sizes.scale(sizes.resolve(CKR.SCALE), "ldbc")
def run_dbs():
    """the figure's databases this run holds (CKR.DBS order): a database with no row in any of its counts files is not
    in the run, which only one of CKR.MAY_LACK may be"""
    present = set()
    for workload in sorted({w for w, _, _ in CKR.CELLS}):
        with open(CKR.counts_file(TREE, SF, workload)) as fh:
            present |= {r["db"] for r in csv.DictReader(fh)}
    lacking = [db for db in CKR.DBS if db not in present]
    if set(lacking) - set(CKR.MAY_LACK):
        raise SystemExit(f"complex-kv-reads: the run at {RUN} holds no rows of {sorted(set(lacking) - set(CKR.MAY_LACK))}")
    for db in lacking:
        print(f"no {db} rows in this run: the figure's databases here are {', '.join(d for d in CKR.DBS if d in present)}")
    return tuple(db for db in CKR.DBS if db in present)


CKR.check_shape(RUN)            # a run that stores its IC1 variants in the earlier shape is refused, with the fix
DBS = run_dbs()
EVALUATED = tuple(db for db in CKR.evaluator_dbs() if db in DBS)     # the databases with the evaluator's arms
if a.show:
    SHOW = tuple(a.show.split(","))
    if set(SHOW) - set(DBS):
        raise SystemExit(f"complex-kv-reads: --show names {sorted(set(SHOW) - set(DBS))}, which the run does not hold "
                         f"(it holds {', '.join(DBS)})")
else:
    SHOW = tuple(db for db in CKR.FIGURE_DBS if db in DBS)          # the paper's figure, as far as the run holds it


def claim(ok, text, refuse=True):
    """a claim of the figure: it stops the derive at the paper's scale on the paper's selection; at another scale, or on
    another selection (refuse False), it is said"""
    if ok:
        return
    if not refuse:
        print(f"NOTE (said, not refused): {text}")
        return
    if AT_PAPER_SCALE:
        raise SystemExit(f"complex-kv-reads: {text}")
    print(f"NOTE at --dataset {sizes.text(a.run)} (the claim is the paper's scale's, {CKR.SCALE}; said, not refused): {text}")


def read_counts(workload):
    """{(query, db, param): ({op: count}, {kind: bytes}, the rows it returns)} of a counts file's ok rows, for the
    configurations the run holds; a query is a query or a query-text variant"""
    out = {}
    with open(CKR.counts_file(TREE, SF, workload)) as fh:
        for r in csv.DictReader(fh):
            if r["status"] == "ok" and r["db"] in DBS:
                f = lambda k: float(r[k] or 0)
                out[(r["query"], r["db"], r["param_idx"])] = ({"G": f("G"), "S": f("S"), "N": f("N")},
                                                             {"G": f("get_bytes"), "N": f("seek_bytes") + f("next_bytes")},
                                                             r.get("rows", ""))
    return out


def per_param(q, sel):
    """{param: {db: ops}} for q's panel over the configurations sel (each through CKR.shown_as), on the parameters
    every one of them answered"""
    mine = {db: CKR.shown_as(db, q) for db in sel}
    got = {}
    for (qq, db, p), (o, _, _) in counts[q].items():
        if qq == q:
            got.setdefault(p, {})[db] = sum(o.values())
    return {p: {db: v[x] for db, x in mine.items()} for p, v in sorted(got.items(), key=lambda kv: int(kv[0]))
            if set(mine.values()) <= set(v)}


def reads_most(q, by_param, sel):
    """the claim of the figure: the configurations whose rows read the most in q's panel on some parameter, over `sel`
    (each through CKR.shown_as; one when the same one reads the most on every parameter)"""
    return {CKR.shown_as(max(sel, key=ops.get), q) for ops in by_param.values()}


# one row per panel, configuration and query (a query-text variant in its base query's panel): every one the run
# holds at the panel's drawn param
counts, titles, params_text, rows = {}, {}, {}, []
for panel, (workload, q, param) in enumerate(CKR.CELLS, 1):
    param = str(param)
    counts[q] = read_counts(workload)
    texts = [q] + [v for v in CKR.QUERY_VARIANTS if CKR.base_query(v) == q]
    want = [(qq, db) for qq in texts for db in DBS if CKR.measured_on(db, qq)]
    missing = [f"{db} {qq}" for qq, db in want if (qq, db, param) not in counts[q]]
    if missing:
        raise SystemExit(f"complex-kv-reads: the run at {RUN} has no row at {q}'s parameter {param} for {', '.join(missing)}")
    n_rows = {counts[q][(q, db, param)][2] for qq, db in want if qq == q}
    assert len(n_rows) == 1, f"{q}: the configurations return different row counts on parameter {param}"
    n_rows = n_rows.pop()
    every = per_param(q, [db for db in DBS if CKR.measured_on(db, q)])
    print(f"{q}: parameter {param} of {len(every)}" + (f" ({n_rows} rows)" if n_rows else "") + "; "
          + ", ".join(f"{db} {sum(counts[q][(qq, db, param)][0].values()):,.0f} ops" + ("" if qq == q else f" ({qq})")
                      for qq, db in want))
    asked = {}                  # the query's own parameters (LDBC captures keep them beside the raw counters)
    capture = CKR.counts_file(TREE, SF, workload).parent / "dgraph" / q / f"p{param}" / "kvstats.json"
    if capture.exists():
        with open(capture) as fh:
            asked = json.load(fh)["params"]
    titles[q] = CKR.TITLES[q].format(rows=n_rows, **asked)
    params_text[q] = CKR.PARAMS_TEXT[q].format(param=param, rows=n_rows, **asked)
    for qq, db in want:
        ops_, bytes_, _ = counts[q][(qq, db, param)]
        rows.append({"panel": panel, "db": db, "query": qq, "title": titles[q], **ops_,
                     "get_bytes": bytes_["G"], "scan_bytes": bytes_["N"]})
write_table("complex-kv-reads", ("panel", "db", "query", "title", "G", "S", "N", "get_bytes", "scan_bytes"), rows,
            key=("panel", "db", "query"))
row_of = {(r["panel"], r["db"], r["query"]): r for r in rows}


def count_keys(sel, refuse):
    """the claims on the counts and the counts' number keys, over the configurations `sel`"""
    for q in counts:
        by_param = per_param(q, sel)
        most = reads_most(q, by_param, sel)
        claim(len(most) == 1, f"{q}: the configuration that reads the most changes with the parameter "
                              f"({len(by_param)} parameters): {sorted(most)}", refuse)
        if q != "ic1":          # IC1's claim is on its drawn parameter, below, also with each query-text variant
            claim(most == {CKR.READS_MOST[q]}, f"{q}: {CKR.READS_MOST[q]} does not read the most ({sorted(most)})", refuse)
    panel, param = next((i, str(p)) for i, (_, q, p) in enumerate(CKR.CELLS, 1) if q == "ic1")
    for variant in (None,) + CKR.CLAIM_VARIANTS:
        totals = {}
        for db in sel:
            qq = variant if variant is not None and CKR.measured_on(db, variant) else "ic1"
            r = row_of[(panel, CKR.shown_as(db, qq), qq)]
            totals[db] = r["G"] + r["S"] + r["N"]
        claim(max(totals, key=totals.get) == CKR.READS_MOST["ic1"],
              f"ic1{'' if variant is None else f' with {variant}'}: {CKR.READS_MOST['ic1']} does not read the most "
              f"({totals})", refuse)
    keys = {}
    for panel, (_, q, _) in enumerate(CKR.CELLS, 1):
        mine = {db: row_of[(panel, CKR.shown_as(db, q), q)] for db in sel}
        for name, value in (("ops", lambda r: r["G"] + r["S"] + r["N"]), ("bytes", lambda r: r["get_bytes"] + r["scan_bytes"])):
            got = {db: float(value(r)) for db, r in mine.items()}
            keys[f"complex-kv-reads.q{panel}.{name}.most-over-fewest"] = multiple(max(got.values()) / min(got.values()))
            if panel == 3:      # the text: NebulaGraph issues the most KV operations and reads the fewest bytes on Q3
                want = max if name == "ops" else min
                claim(want(got, key=got.get) == "nebula",
                      f"q3 {name}: NebulaGraph is not the {'most' if name == 'ops' else 'fewest'} ({got})", refuse)
    return keys


# The times of the same three queries at the figure's cache regime (CKR.REGIME): the live engines, and on our evaluator
# the engine's plan and our cheapest plan; then the same at each other regime the run was timed at (CKR.REGIME_OPTIONS:
# the cells it holds, as measured)
def cells_of(tree, regime=CKR.REGIME):
    d = CKR.times_dir(tree, SF, regime=regime)
    return (CKR.cells(d / "live.csv", "live") if (d / "live.csv").exists() else {},
            CKR.cells(d / "evaluator.csv", "evaluator") if (d / "evaluator.csv").exists() else {})


live, evaluator = cells_of(TREE)
if not live:
    raise SystemExit(f"{CKR.times_dir(TREE, SF)}: no live.csv: the run was not timed at {CKR.REGIME} (just results complex-kv-reads)")
paper_live = None               # the paper's live cells, read only when this run has a cell without a measured KV time
times = []
for panel, ((_, q, _), (tq, src)) in enumerate(zip(CKR.CELLS, CKR.QUERIES), 1):
    assert tq == q, (tq, q)
    for db in (x for x in DBS if CKR.measured_on(x, q)):
        arms = (("live", live), ("engine", evaluator), ("cheapest", evaluator)) if db in EVALUATED else (("live", live),)
        for arm, cells in arms:
            # the cheapest plan is the format's: timed once per engine, on its first configuration
            s = CKR.summary(cells[(CKR.cheapest_of(db) if arm == "cheapest" else db, q, src, arm)])
            source = "" if arm != "live" else "measured" if s["kv_ms"] is not None else "not measured"
            if source == "not measured" and a.tree != "paper-results":
                # this run's machine records no call stacks for the engine: the paper's share of the cell's time in
                # the KV store (its SF0.3 run), times this run's median (the walls are this run's own)
                if paper_live is None:
                    CKR.check_shape(COMPLEX_KV_READS)
                    path = COMPLEX_KV_READS / "times" / "equivalent" / CKR.REGIME / "live.csv"
                    paper_live = CKR.cells(path, "live") if path.exists() else {}
                    if not paper_live:
                        print(f"paper-results/ holds no {CKR.RUN} run (just fetch-results): a KV time this run did not "
                              f"measure stays empty")
                if paper_live and (db, q, src, arm) not in paper_live:
                    print(f"q{panel} {db}: its KV time was not measured in this run, and the paper's run has no such cell: "
                          f"it stays empty")
                elif paper_live:
                    ref = CKR.summary(paper_live[(db, q, src, arm)])
                    share = ref["kv_ms"] / ref["median_ms"]
                    s.update(kv_ms=share * s["median_ms"], kv_pooled=ref["kv_pooled"])
                    source = "archive"
                    print(f"q{panel} {db}: its KV time was not measured in this run (no call stacks on its CPU): the "
                          f"paper's share at {CKR.SCALE}, {100 * share:.0f}% of the cell's time, is kept")
            times.append({"panel": panel, "query": q, "title": titles[q], "db": db, "arm": arm, "regime": CKR.REGIME,
                          "param_source": src, "kv_source": source,
                          **{k: ("" if v is None else round(v, 3) if isinstance(v, float) else v) for k, v in s.items()}})
by = {(t["panel"], t["db"]): t for t in times if t["arm"] == "live"}
# the other regimes the run was timed at: each cell it holds, as measured (no share is taken from another run)
other_regimes = {}                               # {figure suffix: its regime}, those with live times in this run
for suffix, regime in CKR.REGIME_OPTIONS.items():
    live_r, evaluator_r = cells_of(TREE, regime)
    for panel, ((_, q, _), (_, src)) in enumerate(zip(CKR.CELLS, CKR.QUERIES), 1):
        for db in (x for x in DBS if CKR.measured_on(x, q)):
            for arm, cells in (("live", live_r), ("engine", evaluator_r), ("cheapest", evaluator_r)):
                cell = cells.get((CKR.cheapest_of(db) if arm == "cheapest" else db, q, src, arm))
                if cell is None or (arm != "live" and db not in EVALUATED):
                    continue
                s = CKR.summary(cell)
                times.append({"panel": panel, "query": q, "title": titles[q], "db": db, "arm": arm, "regime": regime,
                              "param_source": src, "kv_source": "" if arm != "live" else "measured" if s["kv_ms"] is not None else "not measured",
                              **{k: ("" if v is None else round(v, 3) if isinstance(v, float) else v) for k, v in s.items()}})
    if live_r:
        other_regimes[suffix] = regime
        # how cold each cold start was: the most of a store in the OS page cache just before a run, per configuration
        seen = {}
        for (db, _, _, _), rows in sorted(live_r.items()):
            for r in rows:
                for x in r["settings"].split(";"):
                    if x.startswith("store_cached_mib="):
                        got, of = map(float, x.split("=")[1].split("/"))
                        seen[db] = max(seen.get(db, (0.0, of)), (got, of))
        if seen:
            print(f"{regime}: the most of a store in the OS page cache just before a timed run: "
                  + "; ".join(f"{db} {got:.1f} of {of:.0f} MiB" for db, (got, of) in seen.items()))
write_table("complex-kv-reads-times", ("panel", "query", "title", "db", "arm", "regime", "param_source", "runs", "median_ms",
                                       "min_ms", "max_ms", "kv_ms", "kv_cpu_ms", "io_wait_ms", "kv_pooled", "run_id", "kv_source"),
            times, key=("panel", "db", "arm", "regime"))


def selection_keys(sel, refuse):
    """the number keys over the configurations `sel` (and their claims), and where their KV shares come from"""
    keys = count_keys(sel, refuse)
    live_of = lambda panel: {db: by[(panel, CKR.shown_as(db, CKR.CELLS[panel - 1][1]))] for db in sel}
    shares = [float(t["kv_ms"]) / float(t["median_ms"]) for t in live_of(3).values() if t["kv_ms"] != ""]
    if shares:
        keys["complex-kv-reads.q3.kv-share.min.pct"], keys["complex-kv-reads.q3.kv-share.max.pct"] = pct(min(shares)), pct(max(shares))
    q2 = {db: float(t["median_ms"]) for db, t in live_of(2).items()}
    keys["complex-kv-reads.q2.latency.slowest-over-fastest"] = multiple(max(q2.values()) / min(q2.values()))
    return keys, sorted({t["kv_source"] for panel in range(1, len(CKR.CELLS) + 1) for t in live_of(panel).values()})


keys, kv_from = selection_keys(SHOW, refuse=SHOW == tuple(db for db in CKR.FIGURE_DBS if db in DBS))
shown = "" if SHOW == tuple(db for db in CKR.FIGURE_DBS if db in DBS) else f" over {', '.join(SHOW)} (--show)"
write_numbers("complex_kv_reads", keys, f"{CKR.RUN} (sf{SF}) of {a.tree if a.tree in ("paper-results", "output") else "a tree given by path"}: live counts{shown}; ops = G + S + N, "
              f"bytes = GET + SEEK + NEXT bytes; times: live medians, {CKR.REGIME}; KV share: {' and '.join(kv_from)}")
# the draw's figures: the default (the shown selection) and each option the run holds; the draw reads this table, so
# the option sets live in kvengines.complex_kv_reads only. elsewhere: for a configuration measured on some panels
# only, the configuration whose rows stand in its bar on the others (CKR.shown_as)
def selection(figure, sel, regimes=(CKR.REGIME,)):
    return {"figure": figure, "dbs": ",".join(sel), "regimes": ",".join(regimes),
            "elsewhere": ";".join(f"{db}={CKR.CONFIGURATIONS[db]['elsewhere']}" for db in sel
                                  if "elsewhere" in CKR.CONFIGURATIONS.get(db, {}))}


selections = [selection("", SHOW)] + [selection(suffix, sel) for suffix, sel in CKR.SHOW_OPTIONS.items()
                                      if set(sel) <= set(DBS)]
# a regime option: the shown configurations' latency at the figure's regime beside the option's
selections += [selection(suffix, SHOW, (CKR.REGIME, regime)) for suffix, regime in other_regimes.items()]
write_table("complex-kv-reads-selections", ("figure", "dbs", "regimes", "elsewhere"), selections, key=None)
# the draw's other options the run holds: the same keys and claims, said
for suffix, sel in CKR.SHOW_OPTIONS.items():
    if set(sel) <= set(DBS) and sel != SHOW:
        print(f"option {suffix} ({', '.join(sel)}):")
        other, _ = selection_keys(sel, refuse=False)
        for k, v in other.items():
            print(f"  {k} = {v}" + ("" if keys.get(k) == v else f"   (shown: {keys.get(k)})"))

# The query texts: each engine's text as run, and a Cypher form equivalent to them
QUERY_FILES = {"hop1p7_knows": ("microbench", "hop1p7_knows"), "ic1": ("ldbc", "interactive-complex-1"),
               "ic3": ("ldbc", "interactive-complex-3")}
LANGUAGES = (("dgraph-dql", "dgraph", "graphql", "Dgraph DQL, as run"),
             ("nebulagraph-ngql", "nebulagraph", "gql", "NebulaGraph nGQL, as run"),
             ("arangodb-aql", "arangodb", "aql", "ArangoDB AQL, as run"),
             ("tugraph-cypher", "tugraph", "cypher", "TuGraph Cypher, as run"),
             ("cypher", "cypher", "cypher", "Cypher form, equivalent to the engines' queries"))
LANGUAGE_DB = {"dgraph": "dgraph", "nebulagraph": "nebula", "arangodb": "arangodb", "tugraph": "tugraph"}   # folder -> engine
queries = []
for panel, (_, q, _) in enumerate(CKR.CELLS, 1):
    workload, stem = QUERY_FILES[q]
    for language, folder, ext, label in LANGUAGES:
        if folder in LANGUAGE_DB and LANGUAGE_DB[folder] not in {CKR.engine(db) for db in DBS}:
            continue                # an engine no configuration of this run runs
        source = f"engines/queries/{workload}/{folder}/{stem}.{ext}"
        queries.append({"panel": panel, "query": q, "language": language, "label": label, "source": source,
                        "params": params_text[q], "text": (ROOT / source).read_text()})
write_table("complex-kv-reads-queries", ("panel", "query", "language", "label", "source", "params", "text"), queries,
            key=("panel", "language"))
