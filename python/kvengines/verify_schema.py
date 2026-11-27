#!/usr/bin/env python3
"""Verify a live engine's physical design against the schema file that was supposed to build it.

    python -m kvengines.verify_schema --db dgraph|nebula|arangodb

engines/provenance.sh fingerprints the input (was this backup built from the schema file as it reads now?); this
checks the result. A load can run and still build the wrong store: `arangoimport` exits 0 on per-record failures, a
nebula index can fail to build after the data lands, a dgraph predicate can end up unindexed.

Compares only the physical design (the indexes and predicate flags that decide KV-op costs), not row counts (the
arangodb load script checks arangoimport's per-collection error count). Exits 1 on any difference, printing the diff.
Run against a started server.
"""

import argparse
import json
import re
import sys
import urllib.request

from kvevaluator.paths import ROOT

ENGINES = ROOT / "engines"


def fail(msg):
    print(f"   VERIFY FAILED: {msg}", file=sys.stderr)
    sys.exit(1)


# --- dgraph ------------------------------------------------------------------
# Compare every predicate's (type, index tokenizers, @reverse, @list) between the .schema file and
# what the running alpha reports for `schema {}`.
def verify_dgraph():
    path = f"{ENGINES}/dgraph/schema/ldbcTypes.schema"
    want = {}
    for m in re.finditer(r"^\s*(\w+)\s*:\s*([^.\n]+)\.", open(path).read(), re.M):
        name, body = m.group(1), m.group(2)
        idx = re.search(r"@index\(([^)]*)\)", body)
        want[name] = (
            re.sub(r"[\[\]]", "", body.split("@")[0]).strip().lower(),
            tuple(sorted(t.strip() for t in idx.group(1).split(","))) if idx else (),
            "@reverse" in body,
            body.strip().startswith("["),
        )

    req = urllib.request.Request(
        "http://localhost:8080/query", data=b"schema{}",
        headers={"Content-Type": "application/dql"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        live = json.load(r)["data"]["schema"]
    got = {
        p["predicate"]: (
            p.get("type", "").lower(),
            tuple(sorted(p.get("tokenizer", []))) if p.get("index") else (),
            bool(p.get("reverse")),
            bool(p.get("list")),
        )
        for p in live
        if not p["predicate"].startswith("dgraph.")  # internal predicates are not ours
    }
    return want, got, "predicates"


# --- nebula ------------------------------------------------------------------
# Compare the index inventory (name -> columns). Nebula's costs turn on which indexes exist, so a
# missing index is the failure that matters; tag/edge DDL without its index is still wrong.
def verify_nebula():
    path = f"{ENGINES}/nebulagraph/schema/schema.gql"
    want = {}
    for line in open(path):
        line = line.split("#")[0]  # '#' comments out an index in this file — respect it
        # Greedy `(.*)` to the LAST paren on the line: a column can carry a prefix length, as in
        # `Person(firstName(20))`, and a non-greedy/negated-class match would stop inside it and
        # capture "firstName(20". The prefix length is a storage detail, not a different index, so
        # it is stripped before comparing against what the server reports.
        m = re.search(r"CREATE\s+(?:TAG|EDGE)\s+INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?`?(\w+)`?\s*ON\s+`?\w+`?\s*\((.*)\)", line, re.I)
        if m:
            cols = tuple(re.sub(r"\(\d+\)?", "", c).strip().strip("`") for c in m.group(2).split(","))
            want[m.group(1)] = cols

    from nebula3.gclient.net import ConnectionPool
    from nebula3.Config import Config

    pool = ConnectionPool()
    if not pool.init([("127.0.0.1", 9669)], Config()):
        fail("cannot connect to nebula graphd on 127.0.0.1:9669")
    sess = pool.get_session("root", "nebula")
    got = {}
    try:
        sess.execute("USE ldbc")
        for stmt in ("SHOW TAG INDEXES", "SHOW EDGE INDEXES"):
            res = sess.execute(stmt)
            if not res.is_succeeded():
                fail(f"{stmt}: {res.error_msg()}")
            for i in range(res.row_size()):
                name = res.row_values(i)[0].as_string()
                cols = tuple(c.as_string() for c in res.row_values(i)[2].as_list())
                got[name] = cols
    finally:
        sess.release()
        pool.close()
    return want, got, "indexes"


# --- arangodb ----------------------------------------------------------------
# Compare the persistent-index set. Arango is schemaless, so its indexes ARE its physical design.
def verify_arango():
    path = f"{ENGINES}/arangodb/schema/ldbc_schema.json"
    with open(path) as f:
        idxs = json.load(f)["indexes"]      # [[collection, [field, ...]], ...]
    want = {f"{c}({','.join(fields)})": tuple(fields) for c, fields in idxs}

    got = {}
    for coll in sorted({k.split("(")[0] for k in want}):
        url = f"http://localhost:8529/_db/ldbc/_api/index?collection={coll}"
        with urllib.request.urlopen(url, timeout=15) as r:
            for idx in json.load(r)["indexes"]:
                if idx["type"] == "persistent":
                    fields = tuple(idx["fields"])
                    got[f"{coll}({','.join(fields)})"] = fields
    return want, got, "persistent indexes"


VERIFIERS = {"dgraph": verify_dgraph, "nebula": verify_nebula, "arangodb": verify_arango}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, choices=sorted(VERIFIERS))
    args = ap.parse_args()

    try:
        want, got, unit = VERIFIERS[args.db]()
    except SystemExit:
        raise
    except Exception as e:                       # a down/unreachable server must not look like a pass
        fail(f"could not read the live {args.db} schema: {e}")

    missing = {k: v for k, v in want.items() if got.get(k) != v}
    extra = {k: v for k, v in got.items() if k not in want}
    for k, v in sorted(missing.items()):
        print(f"   MISSING/WRONG on live server: {k} — schema says {v}, live has {got.get(k, 'nothing')}")
    for k, v in sorted(extra.items()):
        print(f"   EXTRA on live server: {k} = {v}")
    if missing or extra:
        fail(f"{args.db} live design != data/schema/{args.db}/ "
             f"({len(missing)} missing/wrong, {len(extra)} extra {unit})")

    print(f"   verified: {len(want)} {unit} match data/schema/{args.db}/")


if __name__ == "__main__":
    main()
