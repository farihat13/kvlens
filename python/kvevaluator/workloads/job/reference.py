#!/usr/bin/env python3
"""The JOB reference: the 113 queries' answers from DuckDB on a data tag's tables (the same merged tables the stores
are loaded from). An answer is the query's MIN columns; the join's row count is kept beside it, because 5 queries
return no row and an empty answer checks nothing. Needs the `duckdb` Python module.

    python3 -m kvevaluator.workloads.job.reference --tag sample1pct
        -> <tag dir>/reference_answers.csv   (query, join_rows, answer as a JSON list; paths.tag_dir)
    python3 -m kvevaluator.workloads.job.reference --tag full --witnesses
        -> also the dataset's sample_forced_titles.csv (datasets/imdb/): the titles of up to 20 result rows per query (the sample keeps them)
Strings compare by byte order (DuckDB's default), as the plan IR does.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
JOB = HERE   # the workload's dir: the specs and entry counts it reads and writes
from kvevaluator.datasets.imdb import paths

WITNESS_ROWS = 20


def query_files():
    files = glob.glob(os.path.join(paths.queries_dir(), "[0-9]*.sql"))
    return sorted(files, key=lambda p: (int(re.match(r"\d+", os.path.basename(p)).group()), p))


def query_text(path):
    """the query, with the alias `at` renamed (a reserved word in DuckDB)"""
    text = open(path).read().strip().rstrip(";")
    return re.sub(r"\bat\.", "at_.", re.sub(r"\bAS at\b", "AS at_", text))


def title_aliases(text):
    frm = re.split(r"\bWHERE\b", re.split(r"\bFROM\b", text, 1, flags=re.I)[1], 1, flags=re.I)[0]
    return [t.split()[-1] for t in (x.strip() for x in frm.split(",")) if t.split()[0] == "title"]


def alias_counts(con):
    """[(query, alias, rows)]: per table alias that is not a type table, COUNT(DISTINCT alias.id) over the query's
    join: the rows of that alias that take part in a result row (what a plan's final rows of the alias must be)"""
    from kvevaluator.workloads.job import pattern
    out = []
    for path in query_files():
        q = "job" + os.path.basename(path)[:-4]
        text = query_text(path)
        spec = json.load(open(os.path.join(JOB, "specs", f"{q}.json")))
        aliases = [a for a, t in sorted(spec["aliases"].items()) if t not in pattern.TYPE_TABLES]
        sql_alias = lambda a: "at_" if a == "at" else a
        body = re.split(r"\bFROM\b", text, 1, flags=re.I)[1]
        row = con.execute("SELECT " + ", ".join(f"count(DISTINCT {sql_alias(a)}.id)" for a in aliases) + " FROM " + body).fetchone()
        out += [(q, a, n) for a, n in zip(aliases, row)]
    return out


def entry_counts(con):
    """[(query, alias, test, rows)]: per vertex alias with a test, the rows of its table that pass `all` its tests
    (the joined type tables' included), and that pass each test an index could serve alone (`p<i>`: its i-th
    conjunct; `t:<col>`: the type ids on a column)"""
    from kvevaluator.workloads.job import pattern, sqlspec
    out = []
    for name in sorted(os.listdir(os.path.join(JOB, "specs")), key=lambda n: (int(re.match(r"job(\d+)", n).group(1)), n)):
        p = pattern.Pattern(json.load(open(os.path.join(JOB, "specs", name))))
        for alias, e in sorted(p.elements.items()):
            if e.kind == "edge" or not e.selective():
                continue
            tests = {f"p{i}": sqlspec.to_sql(c, "x") for i, c in enumerate(e.preds)}
            tests.update({f"t:{col}": f"x.{col} IN ({', '.join(map(str, ids)) or 'NULL'})" for col, (ids, _, _) in sorted(e.type_in.items())})
            every = " AND ".join(tests.values())
            for test, cond in [("all", every)] + sorted(tests.items()):
                n = con.execute(f"SELECT count(*) FROM {e.table} x WHERE {cond}").fetchone()[0]
                out.append((p.query, alias, test, n))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, choices=paths.TAGS)
    ap.add_argument("--witnesses", action="store_true", help="also write the sample's forced titles (from --tag full)")
    a = ap.parse_args()
    if a.witnesses and a.tag != "full":
        raise SystemExit("--witnesses reads the full tag")
    t0 = time.time()
    con = duckdb.connect(paths.tables_db(a.tag), read_only=True)
    rows, forced = [], []
    for path in query_files():
        q = "job" + os.path.basename(path)[:-4]
        text = query_text(path)
        r = con.execute(re.sub(r"^\s*SELECT", "SELECT count(*) AS join_rows,", text, count=1, flags=re.I)).fetchone()
        rows.append({"query": q, "join_rows": r[0], "answer": json.dumps(list(r[1:]), ensure_ascii=False)})
        if a.witnesses:
            ids = ", ".join(f"{t}.id" for t in title_aliases(text))
            body = re.split(r"\bFROM\b", text, 1, flags=re.I)[1]
            for w in con.execute(f"SELECT DISTINCT {ids} FROM {body} ORDER BY ALL LIMIT {WITNESS_ROWS}").fetchall():
                forced += [(q, i) for i in w]
    out = os.path.join(paths.tag_dir(a.tag), "reference_answers.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["query", "join_rows", "answer"])
        w.writeheader(); w.writerows(rows)
    with open(os.path.join(paths.tag_dir(a.tag), "alias_counts.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["query", "alias", "rows"]); w.writerows(alias_counts(con))
    counts = entry_counts(con)
    with open(os.path.join(paths.tag_dir(a.tag), "entry_counts.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["query", "alias", "test", "rows"]); w.writerows(counts)
    if a.tag == "full":      # the generator's input: which aliases a query is entered at (the same plans at every tag)
        with open(os.path.join(JOB, "entry_counts_full.csv"), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["query", "alias", "test", "rows"]); w.writerows(counts)
    empty = [r["query"] for r in rows if r["join_rows"] == 0]
    print(f"{a.tag}: {len(rows)} answers in {out} ({time.time() - t0:.0f} s); empty: {len(empty)} {' '.join(empty)}")
    if a.witnesses:
        forced = sorted(set(forced), key=lambda x: (int(re.match(r"job(\d+)", x[0]).group(1)), x[0], x[1]))
        with open(paths.forced_titles(), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["query", "title_id"]); w.writerows(forced)
        print(f"  {len(forced)} forced (query, title) rows, {len({i for _, i in forced})} titles")


if __name__ == "__main__":
    main()
