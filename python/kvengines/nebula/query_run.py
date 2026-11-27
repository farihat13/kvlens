#!/usr/bin/env python3
"""
Run a NebulaGraph nGQL query.

Usage:
  python -m kvengines.nebula.query_run -s "SHOW SPACES;"
  python -m kvengines.nebula.query_run -f queries.txt
  python -m kvengines.nebula.query_run -s "CREATE SPACE ..." --user root --password nebula --address 127.0.0.1 --port 9669
"""

import argparse
import json
import os
import re
import pandas as pd

from kvengines.clients.nebula import NebulaClient

nebula_address = os.environ.get("NEBULA_HOST", "127.0.0.1")
nebula_port = int(os.environ.get("NEBULA_PORT", "9669"))
nebula_user = os.environ.get("NEBULA_USER", "root")
nebula_password = os.environ.get("NEBULA_PASSWORD", "nebula")


def parse_args():
    p = argparse.ArgumentParser(description="Run nGQL against NebulaGraph.")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("-s", "--statement", help="Run a single nGQL statement")
    group.add_argument("-f", "--file", help="File containing one or more nGQL statements")
    p.add_argument(
        "--address", default=nebula_address,
        help="Host/IP of graphd (default: 127.0.0.1 or $NEBULA_HOST)",
    )
    p.add_argument(
        "--port", type=int, default=nebula_port,
        help="Port of graphd (default: 9669 or $NEBULA_PORT)",
    )
    p.add_argument(
        "--user", default=nebula_user,
        help="Username (default: root or $NEBULA_USER)",
    )
    p.add_argument(
        "--password", default=nebula_password,
        help="Password (default: nebula or $NEBULA_PASSWORD)",
    )
    p.add_argument("--timeout", type=int, default=60000, help="Client timeout in ms (default: 60000)")
    p.add_argument("--space", help="Graph space to USE before executing queries")
    p.add_argument("--json", action="store_true",
                    help="Execute in JSON format (for EXPLAIN/PROFILE)")
    p.add_argument("--pretty", action="store_true", help="Pretty-print response")
    p.add_argument("--verbose", action="store_true", help="Verbose output")
    p.add_argument("--continue-on-error", action="store_true",
                    help="Continue executing subsequent queries when one fails")
    p.add_argument("--exist-ok", action="store_true",
                    help="Do not treat 'already exists' errors as failures")
    return p.parse_args()


def load_queries_from_file(path):
    """Load queries from file. Splits by ';', strips '#' comments."""
    text = open(path, "r", encoding="utf-8").read()
    lines = text.splitlines()
    non_comment_lines = [re.sub(r"#.*$", "", ln) for ln in lines]
    filtered_text = "\n".join(non_comment_lines)
    parts = re.split(r";\s*(?=\n|$)", filtered_text)
    return [p.strip() for p in parts if p and not p.isspace()]


def run_one_query(client, query, use_json=False, pretty=False):
    """Execute a single nGQL query and print results."""
    if use_json:
        resp = client.session.execute_json(query)
        raw = resp.decode("utf-8") if isinstance(resp, (bytes, bytearray)) else str(resp)
        resp_json = json.loads(raw)

        errors = resp_json.get("errors", None)
        if errors is not None:
            if not (isinstance(errors, list) and all(e.get("code", 0) == 0 for e in errors)):
                err = errors[0].get("message", "<unknown error>") if isinstance(errors, list) and errors else "<unknown error>"
                raise Exception(f"Query failed: {err}")

        if pretty:
            print(json.dumps(resp_json, indent=2))
        return resp_json

    else:
        resp = client.session.execute(query)
        if not resp.is_succeeded():
            raise Exception(f"Query failed: {resp.error_msg()}")

        if pretty:
            rows = resp.rows()
            print(len(rows), "rows returned")
            if len(rows) > 0:
                df = resp.as_data_frame()
                pd.set_option("display.max_rows", None)
                pd.set_option("display.max_columns", None)
                pd.set_option("display.width", None)
                pd.set_option("display.colheader_justify", "center")
                pd.set_option("display.precision", 3)
                print(df)
        return resp


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

    if args.verbose:
        print("Debug args:", args)

    with NebulaClient(
        host=args.address,
        port=args.port,
        user=args.user,
        password=args.password,
        space=args.space or "",
        timeout=args.timeout,
    ) as client:
        for i, query in enumerate(queries, start=1):
            if args.verbose:
                print("=" * 60, f"\nQuery #{i}:\n", query)
            else:
                print(f"[QUERY #{i}] {query[:60]}{'...' if len(query) > 60 else ''}", end=" ")

            use_json = args.json or query.lower().startswith(("explain", "profile"))
            try:
                run_one_query(client, query, use_json=use_json, pretty=args.pretty)
                print("[OK]")
            except Exception as err:
                if args.exist_ok and "existed" in str(err).lower():
                    print("[OK] (already exists)")
                elif args.continue_on_error:
                    print(f"[ERROR] {err}")
                else:
                    print(f"[ERROR] {err}")
                    raise


if __name__ == "__main__":
    main()
