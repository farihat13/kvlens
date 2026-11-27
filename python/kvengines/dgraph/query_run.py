#!/usr/bin/env python3
"""
Run a Dgraph DQL (GraphQL+-) query.

Usage:
  python -m kvengines.dgraph.query_run -s '{ me(func: uid(0x1)) { name } }'
  python -m kvengines.dgraph.query_run -f query.graphql --var personId=933 --var firstName=John
  python -m kvengines.dgraph.query_run -f queries.graphql --pretty --continue-on-error
"""

import argparse
import json
import os
import re
from urllib.parse import urlparse

from kvengines.clients.dgraph import DgraphClient

DEFAULT_DGRAPH_URL = os.getenv("DGRAPH_URL", "http://localhost:8080/query")


def parse_args():
    p = argparse.ArgumentParser(description="Run a Dgraph DQL query.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--statement", help="Run a single DQL query string")
    group.add_argument("-f", "--file", help="File containing one or more DQL queries")
    p.add_argument(
        "--var", "-v", action="append", default=[],
        help="Variable binding key=value (repeatable). Example: -v personId=933",
    )
    p.add_argument("--url", default=DEFAULT_DGRAPH_URL,
                    help="Dgraph /query endpoint (default: %(default)s)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print response")
    p.add_argument("--verbose", action="store_true", help="Verbose output")
    p.add_argument("--continue-on-error", action="store_true",
                    help="Continue executing subsequent queries when one fails")
    p.add_argument("--exist-ok", action="store_true",
                    help="Do not treat 'already exists' errors as failures")
    return p.parse_args()


def parse_vars(var_list):
    """Convert --var key=value pairs into a params dict.

    No ``$`` prefix needed — DgraphClient handles that.
    """
    out = {}
    for pair in var_list:
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k = k.strip().lstrip("$")
        v = v.strip()
        if k:
            out[k] = v
    return out


def load_queries_from_file(path):
    """Load queries from file. Splits by ';', strips '#' comments."""
    text = open(path, "r", encoding="utf-8").read()
    lines = text.splitlines()
    non_comment_lines = [re.sub(r"#.*$", "", ln) for ln in lines]
    filtered_text = "\n".join(non_comment_lines)
    parts = re.split(r";\s*(?=\n|$)", filtered_text)
    return [p.strip() for p in parts if p and not p.isspace()]


def run_one_query(client, query, params=None, pretty=False):
    """Execute a single DQL query and print results."""
    qr = client.execute(query, params or None)
    if pretty:
        print(json.dumps(qr.raw, indent=2))
    return qr


def main():
    args = parse_args()

    if args.file:
        if not os.path.exists(args.file):
            raise SystemExit(f"File not found: {args.file}")
        queries = load_queries_from_file(args.file)
        if not queries:
            raise SystemExit("No queries found in file.")
    else:
        queries = [args.statement]

    params = parse_vars(args.var)
    if args.verbose:
        print("Debug args:", args)

    parsed = urlparse(args.url)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8080

    with DgraphClient(host=host, port=port) as client:
        for i, query in enumerate(queries, start=1):
            if args.verbose:
                print("=" * 60, f"\nQuery #{i}:\n", query)
            else:
                print(f"[QUERY #{i}] {query[:60]}{'...' if len(query) > 60 else ''}", end=" ")

            try:
                run_one_query(client, query, params=params, pretty=args.pretty)
                print("[OK]")
            except Exception as err:
                if args.exist_ok and "already exists" in str(err).lower():
                    print("[OK] (already exists)")
                elif args.continue_on_error:
                    print(f"[ERROR] {err}")
                else:
                    print(f"[ERROR] {err}")
                    raise


if __name__ == "__main__":
    main()
