#!/usr/bin/env python3
# released from gs-paper at ea785fb (evidence/complex-kv-reads/fig_complex_kv_reads.py); edit it there, then run evidence/release_draw.py
"""The KV operations and bytes each live database reads on three multi-step reads (real engines, SF0.3): a person's
friends with seven properties each (microbench hop1p7_knows), LDBC IC1 (Dgraph without its first-name index: it reads
the first name of every candidate) and LDBC IC3, with each engine's latency. Into figures/background/:
complex_kv_reads_stacked_times (Figure 2: GETs and scans as KV operations and as bytes, then the latency inside and
outside the KV store), one complex_kv_reads_stacked_times<suffix> per option, and complex_kv_reads_times-with-cheapest
(an appendix option: the counts, the live engines' time, and our evaluator's time running each engine's plan and our
cheapest plan; the configurations with an evaluator arm in the table, which TuGraph has not: no format of ours), and,
when the run was also timed at another cache regime, complex_kv_reads_latency-cold (each live engine's latency at the
figure's regime, warm, beside a cold start: every run on a server started anew, nothing of its store in memory). Each
as .pdf and .png. A bar is an engine configuration (a database with its settings: TuGraph with detach_property off or
on). Which configurations each figure shows is complex-kv-reads-selections.csv's (the derive writes it from
kvengines.complex_kv_reads: FIGURE_DBS, SHOW_OPTIONS): the default and the options -tugraph-detached and
-without-tugraph when the run holds them; a table dir without it: every configuration of the table, in
paper_style.DB_LABEL's order. --show a,b,c draws the default figure on those configurations instead.

Reads only its tables (the artifact's paper/derive/complex_kv_reads.py makes them from the snapshots):
complex-kv-reads.csv, per panel, configuration and query (a query-text variant has its own query name; the figures
draw the panel's own query), the op counts and bytes; complex-kv-reads-times.csv, per query, configuration and arm
(live, engine, cheapest), the median, min and max time and, live, the time in the KV store; the selections' elsewhere
column: for a configuration measured on some panels only (dgraph-no-index: IC1), the configuration whose rows stand in
its bar on the others. The selections' regimes column: one regime, a figure of the default kind at it; two, the latency at
both side by side (the times table holds a row per regime).

    python fig_complex_kv_reads.py --data <dir of complex-kv-reads.csv> --out <dir> [--show dgraph,nebula,arangodb,tugraph-detached]
"""
import argparse
import sys

import matplotlib
matplotlib.use("Agg")

from common import args, has_table, read_table, use_paper_font
from paper_style import DB_LABEL, db_reads_column_times, db_reads_latency_regimes, db_reads_times

def regime_label(regime):
    """a cache regime in a legend: the regime its name starts with (the registry's names, the artifact's
    inputs/contract/store_config.json: warm-bounded is a warm regime, cold-bounded a cold one)"""
    return regime.split("-")[0].capitalize()


def save(fig, out_dir, name):
    out = out_dir / "figures/background" / f"{name}.pdf"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight")
    fig.savefig(out.with_suffix(".png"), dpi=150, bbox_inches="tight")
    print(f"-> {out}")


def main():
    pick = argparse.ArgumentParser(add_help=False)
    pick.add_argument("--show", default=None, help="the default figure's configurations, a comma list")
    shown, sys.argv[1:] = pick.parse_known_args()
    table, out_dir = args(__doc__)
    use_paper_font()
    rows = read_table(table, "complex-kv-reads")
    timed = read_table(table, "complex-kv-reads-times")
    by_regime = {(r["regime"], r["query"], r["db"], r["arm"]): {k: (float(r[k]) if r[k] else None) for k in
                                                                ("median_ms", "min_ms", "max_ms", "kv_ms", "io_wait_ms")}
                 | {"kv_pooled": r["kv_pooled"] == "1"} for r in timed}
    # each panel's own query (the times table holds no query-text variant), in panel order
    queries = list(dict.fromkeys(r["query"] for r in sorted(timed, key=lambda r: int(r["panel"]))))
    mine = [r for r in rows if r["query"] in queries]              # each panel's own query, not its text variants
    unknown = {r["db"] for r in mine} - DB_LABEL.keys()
    if unknown:
        raise SystemExit(f"complex-kv-reads.csv: databases paper_style.DB_LABEL does not name: {sorted(unknown)}")
    dbs = tuple(db for db in DB_LABEL if any(r["db"] == db for r in mine))     # the table's, in the paper's order
    if has_table(table, "complex-kv-reads-selections"):
        chosen = read_table(table, "complex-kv-reads-selections")
        # a selection of one regime is a figure of the default kind; of two, the latency at both side by side
        regimes_of = {r["figure"]: tuple(r["regimes"].split(",")) for r in chosen}
        odd = {f: r for f, r in regimes_of.items() if len(r) not in (1, 2)}
        if odd:
            raise SystemExit(f"complex-kv-reads-selections.csv: a figure draws one regime, or two side by side: {odd}")
        figures = {r["figure"]: tuple(r["dbs"].split(",")) for r in chosen if len(regimes_of[r["figure"]]) == 1}
        beside = {r["figure"]: tuple(r["dbs"].split(",")) for r in chosen if len(regimes_of[r["figure"]]) == 2}
        elsewhere = dict(x.split("=") for r in chosen for x in r["elsewhere"].split(";") if x)
    else:
        figures, elsewhere = {"": tuple(db for db in dbs if all(any(r["db"] == db and r["query"] == q for r in mine)
                                                                 for q in queries))}, {}
        held = sorted({r["regime"] for r in timed})
        if len(held) != 1:
            raise SystemExit(f"complex-kv-reads-times.csv holds {len(held)} regimes ({', '.join(held)}) and the table dir no "
                             f"complex-kv-reads-selections.csv, which says the figure's")
        regimes_of, beside = {"": (held[0],)}, {}
    regime = regimes_of[""][0]                                    # the figure's own regime
    times = {k[1:]: v for k, v in by_regime.items() if k[0] == regime}
    if shown.show:
        figures[""] = tuple(shown.show.split(","))
    missing = {db for sel in figures.values() for db in sel} - set(dbs)
    if missing:
        raise SystemExit(f"complex-kv-reads.csv: no rows of {sorted(missing)} (it holds {', '.join(dbs)})")
    titles = {r["query"]: r["title"] for r in mine}
    counted = {(r["db"], r["query"]): ({o: float(r[o]) for o in ("G", "S", "N")},
                                       {"G": float(r["get_bytes"]), "N": float(r["scan_bytes"])}) for r in mine}
    # a bar's rows: its configuration's where it is measured on the panel's query, else its stand-in's (elsewhere)
    stand = lambda db, q: db if (db, q) in counted else elsewhere.get(db, db)
    data = {db: {q: counted[(stand(db, q), q)] for q in queries if (stand(db, q), q) in counted} for db in dbs}
    times = times | {(q, db, arm): times[(q, stand(db, q), arm)] for db in dbs for q in queries
                     for arm in ("live", "engine", "cheapest") if (q, db, arm) not in times and (q, stand(db, q), arm) in times}
    # the evaluator's arms (engine, cheapest): only a configuration whose engine has a format on our evaluator has them
    evaluated = tuple(db for db in figures[""] if all((q, db, "engine") in times for q in queries))
    want = ({(q, db, "live") for q in queries for sel in figures.values() for db in sel}
            | {(q, db, arm) for q in queries for db in evaluated for arm in ("engine", "cheapest")})
    if want - times.keys():
        raise SystemExit(f"complex-kv-reads-times.csv: no row for {sorted(want - times.keys())}")
    for suffix, sel in figures.items():
        save(db_reads_column_times(data, times, titles, sel), out_dir, f"complex_kv_reads_stacked_times{suffix}")
    save(db_reads_times(data, times, titles, evaluated, with_cheapest=True), out_dir,
         "complex_kv_reads_times-with-cheapest")
    for suffix, sel in beside.items():
        both = regimes_of[suffix]
        at = {(r, q, db, "live"): by_regime[(r, q, stand(db, q), "live")] for r in both for q in queries for db in sel
              if (r, q, stand(db, q), "live") in by_regime}
        lacking = sorted((r, q, db) for r in both for q in queries for db in sel if (r, q, db, "live") not in at)
        if lacking:
            raise SystemExit(f"complex-kv-reads-times.csv: figure {suffix}: no live row for {lacking}")
        save(db_reads_latency_regimes(at, titles, sel, both, {r: regime_label(r) for r in both}), out_dir,
             f"complex_kv_reads_latency{suffix}")


if __name__ == "__main__":
    main()
