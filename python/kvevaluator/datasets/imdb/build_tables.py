#!/usr/bin/env python3
"""Build one data tag of "IMDb with repeated credits merged": the 21 JOB tables in a DuckDB file (what the reference
answers are computed on) and the vertex and edge files the loader reads. Needs the `duckdb` Python module.

    python3 -m kvevaluator.datasets.imdb.build_tables --tag full
    python3 -m kvevaluator.datasets.imdb.build_tables --tag sample1pct     (reads the full tag's tables)
full:       the dump (fetch.py), with the first row (smallest id) kept per mapping.MERGE_KEYS key.
sample1pct: 1% of the titles (by an arithmetic hash of the id) plus the titles of sample_forced_titles.csv (up to 20
            result rows per query, so no query that has an answer loses it), their parent series, every row that points
            at a kept title, the people, characters, companies and keywords those rows point at, and those people's
            person_info and aka_name rows. The type tables are kept whole.
Writes the tag's dir (paths.tag_dir): tables.duckdb, vertices/<Label>.csv, edges/<edge type>.csv (sorted by source,
target; an edge whose end does not exist is left out), manifest.json (every count).
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
import time

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.datasets.imdb import mapping
from kvevaluator.datasets.imdb import paths

FORCED = paths.forced_titles()
SAMPLE_MODULUS = 100            # 1% of the titles
TYPE_TABLES = ("kind_type", "company_type", "role_type", "info_type", "link_type", "comp_cast_type")
BY_MOVIE = ("cast_info", "movie_companies", "movie_keyword", "movie_info", "movie_info_idx", "complete_cast", "aka_title")


def count(con, table):
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def build_full(con, man):
    raw, qdir = paths.raw_dir(), paths.queries_dir()
    schema = open(os.path.join(qdir, "schema.sql")).read()
    tables = re.findall(r"CREATE TABLE (\w+)", schema)
    con.execute(schema)
    for t in tables:
        con.execute(f"COPY {t} FROM '{raw}/{t}.csv' (FORMAT csv, HEADER false, DELIMITER ',', QUOTE '\"', ESCAPE '\\', NULLSTR '')")
    man["dump_rows"] = {t: count(con, t) for t in tables}
    man["merged"] = {}
    for t, key in mapping.MERGE_KEYS.items():
        con.execute(f"CREATE TABLE merged_{t} AS SELECT * FROM {t} QUALIFY row_number() OVER (PARTITION BY {', '.join(key)} ORDER BY id) = 1")
        con.execute(f"DROP TABLE {t}")
        con.execute(f"ALTER TABLE merged_{t} RENAME TO {t}")
        man["merged"][t] = {"key": list(key), "rows_dropped": man["dump_rows"][t] - count(con, t)}
    return tables


def build_sample(con, man):
    src = paths.tables_db("full")
    if not os.path.exists(src):
        raise SystemExit(f"no {src}: build --tag full first")
    con.execute(f"ATTACH '{src}' AS src (READ_ONLY)")
    tables = [r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog = 'src' ORDER BY 1").fetchall()]
    forced = []
    if os.path.exists(FORCED):
        forced = sorted({int(r["title_id"]) for r in csv.DictReader(open(FORCED))})
    else:
        print(f"WARNING: no {os.path.basename(FORCED)}: the sample holds no forced titles, queries may be empty on it", file=sys.stderr)
    con.execute("CREATE TEMP TABLE forced (id INTEGER)")
    if forced:
        con.executemany("INSERT INTO forced VALUES (?)", [(i,) for i in forced])
    # the 1%: an arithmetic hash (Knuth's multiplier), so the choice does not depend on the DuckDB version
    con.execute(f"""CREATE TEMP TABLE keep0 AS SELECT id FROM src.title
                    WHERE (id::BIGINT * 2654435761) % 4294967296 % {SAMPLE_MODULUS} = 0 OR id IN (SELECT id FROM forced)""")
    con.execute("""CREATE TEMP TABLE keep AS SELECT id FROM keep0 UNION
                   SELECT episode_of_id FROM src.title WHERE id IN (SELECT id FROM keep0) AND episode_of_id IS NOT NULL""")
    con.execute("CREATE TABLE title AS SELECT * FROM src.title WHERE id IN (SELECT id FROM keep)")
    for t in BY_MOVIE:
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t} WHERE movie_id IN (SELECT id FROM keep)")
    con.execute("CREATE TABLE movie_link AS SELECT * FROM src.movie_link WHERE movie_id IN (SELECT id FROM keep) AND linked_movie_id IN (SELECT id FROM keep)")
    con.execute("CREATE TABLE name AS SELECT * FROM src.name WHERE id IN (SELECT person_id FROM cast_info)")
    con.execute("CREATE TABLE char_name AS SELECT * FROM src.char_name WHERE id IN (SELECT person_role_id FROM cast_info)")
    con.execute("CREATE TABLE company_name AS SELECT * FROM src.company_name WHERE id IN (SELECT company_id FROM movie_companies)")
    con.execute("CREATE TABLE keyword AS SELECT * FROM src.keyword WHERE id IN (SELECT keyword_id FROM movie_keyword)")
    for t in ("person_info", "aka_name"):
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t} WHERE person_id IN (SELECT id FROM name)")
    for t in TYPE_TABLES:
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
    man["sample"] = {"modulus": SAMPLE_MODULUS, "forced_titles": len(forced), "titles_kept": count(con, "title")}
    made = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_catalog = current_database() AND table_schema = 'main'").fetchall()}
    if set(tables) - made:
        raise SystemExit(f"sample: tables not built: {sorted(set(tables) - made)}")
    con.execute("DETACH src")
    return tables


TYPE_COLUMN = {"cast_info": "role_id", "movie_companies": "company_type_id"}


def edge_type_rows(con):
    return {t: {str(i): n for i, n in con.execute(f"SELECT {c}, count(*) FROM {t} GROUP BY 1 ORDER BY 1").fetchall()}
            for t, c in TYPE_COLUMN.items()}


def export(con, out, man):
    declared = {"cast_info": set(mapping.ROLE_EDGE), "movie_companies": set(mapping.COMPANY_EDGE)}
    for t, c in TYPE_COLUMN.items():
        stray = con.execute(f"SELECT count(*) FROM {t} WHERE {c} NOT IN ({', '.join(map(str, sorted(declared[t])))})").fetchone()[0]
        if stray:
            raise SystemExit(f"{t}: {stray} rows of an edge type mapping.py does not declare (rebuild --tag full first: "
                             f"it writes dataset/edge_type_rows_full.json)")
    vdir, edir = os.path.join(out, "vertices"), os.path.join(out, "edges")
    os.makedirs(vdir), os.makedirs(edir)
    man["vertices"], man["edges"], man["edges_left_out_no_end"] = {}, {}, {}
    for label, (table, props) in mapping.VERTICES.items():
        cols = ", ".join(f"{c} AS {p}" for p, c, _ in props)
        con.execute(f"COPY (SELECT id, {cols} FROM {table} ORDER BY id) TO '{vdir}/{label}.csv' (HEADER, DELIMITER ',')")
        man["vertices"][label] = count(con, table)
    table_of = {label: table for label, (table, _) in mapping.VERTICES.items()}
    for etype, (table, sl, sc, dl, dc, props, where) in mapping.EDGES.items():
        cond = f"x.{dc} IS NOT NULL" + (f" AND x.{where}" if where else "")
        cols = "".join(f", x.{c} AS {p}" for p, c, _ in props)
        sel = (f"FROM {table} x JOIN {table_of[sl]} s ON s.id = x.{sc} JOIN {table_of[dl]} d ON d.id = x.{dc} WHERE {cond}")
        n, pairs = con.execute(f"SELECT count(*), count(DISTINCT (x.{sc}, x.{dc})) {sel}").fetchone()
        if n != pairs:
            raise SystemExit(f"{etype}: {n} rows for {pairs} (source, target) pairs: parallel edges; the merge keys do not cover it")
        con.execute(f"COPY (SELECT x.{sc} AS src, x.{dc} AS dst{cols} {sel} ORDER BY 1, 2) TO '{edir}/{etype}.csv' (HEADER, DELIMITER ',')")
        man["edges"][etype] = n
        every = con.execute(f"SELECT count(*) FROM {table} x WHERE {cond}").fetchone()[0]
        if every != n:
            man["edges_left_out_no_end"][etype] = every - n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True, choices=paths.TAGS)
    ap.add_argument("--threads", type=int, default=16)
    a = ap.parse_args()
    t0 = time.time()
    out = paths.tag_dir(a.tag)
    tmp = out + ".building"
    if os.path.exists(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    con = duckdb.connect(os.path.join(tmp, "tables.duckdb"))
    con.execute(f"SET threads = {a.threads}")
    man = {"tag": a.tag, "mapping": mapping.MAPPING, "data": "IMDb with repeated credits merged (first row per key, smallest id)",
           "dump": json.load(open(os.path.join(paths.raw_dir(), "fetched.json")))["sha256"]}
    tables = build_full(con, man) if a.tag == "full" else build_sample(con, man)
    if a.tag == "full":      # which edge types exist: the mapping's input (tracked), written before the export reads it
        with open(os.path.join(HERE, "edge_type_rows_full.json"), "w") as f:
            json.dump(edge_type_rows(con), f, indent=1, sort_keys=True)
            f.write("\n")
        import importlib
        importlib.reload(mapping)
    man["tables"] = {t: count(con, t) for t in sorted(tables)}
    empty = [(t, c) for t in tables for (c,) in con.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{t}' AND data_type = 'VARCHAR'").fetchall()
             if con.execute(f"SELECT count(*) FROM {t} WHERE {c} = ''").fetchone()[0]]
    if empty:
        raise SystemExit(f"empty strings (they would read back as NULL from the CSV files): {empty}")
    export(con, tmp, man)
    con.close()
    wall = round(time.time() - t0, 1)
    with open(os.path.join(tmp, "manifest.json"), "w") as f:      # the content only: the same build gives the same file
        json.dump(man, f, indent=1, sort_keys=True)
    with open(os.path.join(tmp, "build.json"), "w") as f:
        json.dump({"build_wall_s": wall, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
    if os.path.exists(out):                       # the old build is moved aside, never removed here
        os.rename(out, f"{out}._aside-{int(t0)}")
    os.rename(tmp, out)
    print(f"{a.tag}: {sum(man['vertices'].values()):,} vertices, {sum(man['edges'].values()):,} edges in {out} ({wall} s)")
    if man["edges_left_out_no_end"]:
        print(f"  edges left out (an end does not exist): {man['edges_left_out_no_end']}")


if __name__ == "__main__":
    main()
