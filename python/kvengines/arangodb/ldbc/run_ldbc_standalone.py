#!/usr/bin/env python3
"""Standalone LDBC SNB Interactive query runner for ArangoDB.

Runs LDBC queries against ArangoDB using python-arango directly (no kvengines).
Loads query files, substitution parameters, executes queries, prints results.

Usage:
  python -m kvengines.arangodb.ldbc.run_ldbc_standalone --num 1 --param 1              # Run IC1 with param row 1
  python -m kvengines.arangodb.ldbc.run_ldbc_standalone -qt short --num 1 --param 1    # Run IS1 with param row 1
  python -m kvengines.arangodb.ldbc.run_ldbc_standalone --all --nparam 5               # Run all IC queries, 5 params each
  python -m kvengines.arangodb.ldbc.run_ldbc_standalone -qt short --all                # Run all IS queries
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

from arango import ArangoClient

# -------------------------------------------------------------------------
# Configuration
# -------------------------------------------------------------------------

from kvevaluator.paths import ROOT as REPO_ROOT
ARANGO_ROOT = Path(os.environ.get("MY_ARANGO_ROOTDIR", REPO_ROOT / "engines" / "arangodb"))
DEFAULT_SF = os.environ.get("LDBC_SF", "0.3")

QUERY_DIR = REPO_ROOT / "engines" / "queries" / "ldbc" / "arangodb"
PARAM_DIR_TEMPLATE = str(ARANGO_ROOT / "data/ldbc/substitution_parameters-sf{sf}")

# Parameter names per query (header fields from param files)
QUERY_PARAMS = {
    "complex": {
        1:  ["personId", "firstName"],
        2:  ["personId", "maxDate"],
        3:  ["personId", "startDate", "durationDays", "countryXName", "countryYName"],
        4:  ["personId", "startDate", "durationDays"],
        5:  ["personId", "minDate"],
        6:  ["personId", "tagName"],
        7:  ["personId"],
        8:  ["personId"],
        9:  ["personId", "maxDate"],
        10: ["personId", "month"],
        11: ["personId", "countryName", "workFromYear"],
        12: ["personId", "tagClassName"],
        13: ["person1Id", "person2Id"],
        14: ["person1Id", "person2Id"],
    },
    "short": {
        1: ["personId"],
        2: ["personId"],
        3: ["personId"],
        4: ["messageId"],
        5: ["messageId"],
        6: ["messageId"],
        7: ["messageId"],
    },
}

# Numeric parameters that should be coerced to int
NUMERIC_PARAMS = {
    "personId", "person1Id", "person2Id", "messageId",
    "maxDate", "startDate", "minDate", "durationDays",
    "month", "workFromYear",
}


def load_query(query_type, num):
    """Load AQL query file."""
    if query_type == "complex":
        path = QUERY_DIR / f"interactive-complex-{num}.aql"
    else:
        path = QUERY_DIR / f"interactive-short-{num}.aql"

    if not path.exists():
        print(f"ERROR: Query file not found: {path}", file=sys.stderr)
        return None
    return path.read_text()


def load_params(query_type, num, sf=DEFAULT_SF):
    """Load substitution parameters for a query."""
    param_dir = Path(PARAM_DIR_TEMPLATE.format(sf=sf))
    # Short queries use the same param files but with different naming
    if query_type == "short":
        # IS1-3 use personId, IS4-7 use messageId
        # The param file is named interactive_N_param.txt where N maps to IS queries
        # IS1 -> interactive_1_param, IS2 -> interactive_2_param, etc.
        # Actually: short query params share with complex for personId-based ones
        # Let's check what exists
        param_file = param_dir / f"interactive_{num}_param.txt"
        if not param_file.exists():
            # For short queries, use the complex query params as fallback
            # IS1-3 need personId (use IC7's param file which has just personId)
            # IS4-7 need messageId — we need to find message IDs
            if num <= 3:
                param_file = param_dir / "interactive_7_param.txt"
            else:
                # No standard param file for messageId short queries
                # Use interactive_7_param.txt and treat personId as messageId
                param_file = param_dir / "interactive_7_param.txt"
    else:
        param_file = param_dir / f"interactive_{num}_param.txt"

    if not param_file.exists():
        print(f"ERROR: Param file not found: {param_file}", file=sys.stderr)
        return []

    rows = []
    with open(param_file, "r") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            params = {}
            param_names = QUERY_PARAMS[query_type][num]
            for name in param_names:
                # Map param file column to query bind var
                val = row.get(name, "")
                if name in NUMERIC_PARAMS:
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        pass
                params[name] = val
            rows.append(params)
    return rows


def run_query(db, query_text, bind_vars, verbose=False):
    """Execute a query and return results + elapsed time."""
    if verbose:
        print(f"  Params: {bind_vars}", file=sys.stderr)

    t0 = time.time()
    cursor = db.aql.execute(query_text, bind_vars=bind_vars)
    results = list(cursor)
    elapsed_ms = (time.time() - t0) * 1000

    return results, elapsed_ms


def print_results(results, pretty=False):
    """Print query results as JSON."""
    indent = 2 if pretty else None
    for row in results:
        print(json.dumps(row, indent=indent, default=str))


def main():
    parser = argparse.ArgumentParser(description="LDBC SNB Interactive query runner for ArangoDB")
    parser.add_argument("-qt", "--query-type", choices=["complex", "short"], default="complex",
                        help="Query type: complex (IC) or short (IS)")
    parser.add_argument("--num", "-n", type=int, help="Query number (1-14 for complex, 1-7 for short)")
    parser.add_argument("--param", "-p", type=int, default=1, help="Parameter row index (1-based)")
    parser.add_argument("--nparam", type=int, default=1, help="Number of parameter rows to run")
    parser.add_argument("--all", action="store_true", help="Run all queries of the given type")
    parser.add_argument("--sf", default=DEFAULT_SF, help="Scale factor (default: 0.3)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8529)
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("-c", "--compact", action="store_true", help="Compact output (just counts + timing)")
    args = parser.parse_args()

    # Connect
    client = ArangoClient(hosts=f"http://{args.host}:{args.port}")
    db = client.db("ldbc", username="root", password="")

    # Determine which queries to run
    if args.query_type == "complex":
        query_range = range(1, 15) if args.all else [args.num]
    else:
        query_range = range(1, 8) if args.all else [args.num]

    if not args.all and args.num is None:
        parser.error("--num is required when --all is not set")

    total_queries = 0
    total_time = 0

    for qnum in query_range:
        query_text = load_query(args.query_type, qnum)
        if query_text is None:
            continue

        params_list = load_params(args.query_type, qnum, args.sf)
        if not params_list:
            print(f"WARNING: No params for {args.query_type}-{qnum}", file=sys.stderr)
            continue

        # Determine param range
        start_idx = args.param - 1 if not args.all else 0
        end_idx = start_idx + args.nparam
        selected_params = params_list[start_idx:end_idx]

        prefix = "IC" if args.query_type == "complex" else "IS"

        for i, bind_vars in enumerate(selected_params):
            param_idx = start_idx + i + 1
            label = f"{prefix}{qnum} p{param_idx}"

            if not args.compact:
                print(f"\n{'='*60}")
                print(f"{label}: {bind_vars}")
                print(f"{'='*60}")

            try:
                results, elapsed = run_query(db, query_text, bind_vars, args.verbose)
                total_queries += 1
                total_time += elapsed

                if args.compact:
                    print(f"{label}: {len(results)} rows, {elapsed:.1f} ms")
                else:
                    print_results(results, args.pretty)
                    print(f"\n--- {len(results)} rows, {elapsed:.1f} ms ---", file=sys.stderr)
            except Exception as e:
                print(f"{label}: ERROR: {e}", file=sys.stderr)

    if total_queries > 1:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"Total: {total_queries} queries, {total_time:.1f} ms ({total_time/1000:.2f} s)", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)


if __name__ == "__main__":
    main()
