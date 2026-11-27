#!/usr/bin/env python3
"""Run AQL queries from string or file.

Usage:
  python -m kvengines.arangodb.query_run -s "FOR p IN Person LIMIT 5 RETURN p"
  python -m kvengines.arangodb.query_run -f queries/test.aql
  python -m kvengines.arangodb.query_run -s "FOR p IN Person FILTER p._key == @id RETURN p" -v id=933
"""

import argparse
import json
import time
import sys

from arango import ArangoClient


def parse_bind_vars(args):
    """Parse -v key=value pairs into a dict."""
    bind_vars = {}
    if not args:
        return bind_vars
    for item in args:
        if "=" not in item:
            print(f"WARNING: ignoring invalid bind var '{item}' (expected key=value)")
            continue
        key, val = item.split("=", 1)
        # Try to parse as int/float, fallback to string
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except ValueError:
                pass
        bind_vars[key] = val
    return bind_vars


def main():
    parser = argparse.ArgumentParser(description="Run AQL queries against ArangoDB")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--string", help="AQL query string")
    group.add_argument("-f", "--file", help="AQL query file")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8529)
    parser.add_argument("--db", default="ldbc", help="Database name")
    parser.add_argument("-v", "--var", action="append", help="Bind variable: key=value")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args()

    # Load query
    if args.string:
        query = args.string
    else:
        with open(args.file) as f:
            query = f.read()

    bind_vars = parse_bind_vars(args.var)

    # Connect
    client = ArangoClient(hosts=f"http://{args.host}:{args.port}")
    db = client.db(args.db, username="root", password="")

    # Execute
    t0 = time.time()
    cursor = db.aql.execute(query, bind_vars=bind_vars or None)
    results = list(cursor)
    elapsed = (time.time() - t0) * 1000

    # Print results
    indent = 2 if args.pretty else None
    for row in results:
        print(json.dumps(row, indent=indent, default=str))

    print(f"\n--- {len(results)} rows, {elapsed:.1f} ms ---", file=sys.stderr)


if __name__ == "__main__":
    main()
