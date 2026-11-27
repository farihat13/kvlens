#!/usr/bin/env python3
"""
Check equivalence of two Dgraph queries by comparing their results.

Runs two query variants against the same Dgraph instance with identical
parameters and reports whether they produce the same results.

Usage examples:
  # Compare two query files with all parameters
  python -m kvengines.dgraph.query_check_equivalence \
    --query1 queries/interactive-complex-1.graphql \
    --query2 queries/interactive-complex-1.v2.graphql \
    --params data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt

  # Compare with specific parameter set
  python -m kvengines.dgraph.query_check_equivalence \
    --query1 queries/interactive-complex-1.graphql \
    --query2 queries/interactive-complex-1.v2.graphql \
    --params data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt \
    --param 5

  # Verbose mode (shows results when differences found)
  python -m kvengines.dgraph.query_check_equivalence \
    --query1 queries/interactive-complex-1.graphql \
    --query2 queries/interactive-complex-1.v2.graphql \
    --params data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt -v
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from kvengines.clients.dgraph import DgraphClient
from kvengines.ldbc.params import read_param_file, dgraph_param_preprocessor


def _sort_json(obj):
    """Recursively sort dicts by key and lists by content for stable comparison."""
    if isinstance(obj, dict):
        return {k: _sort_json(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, list):
        items = [_sort_json(x) for x in obj]
        try:
            items.sort(key=lambda x: json.dumps(x, sort_keys=True, default=str))
        except TypeError:
            pass
        return items
    return obj


def compare_raw(raw1, raw2):
    """Compare two Dgraph response dicts. Returns (match, message)."""
    norm1 = _sort_json(raw1)
    norm2 = _sort_json(raw2)
    if norm1 == norm2:
        return True, "Identical"

    # Find first difference
    s1 = json.dumps(norm1, indent=2, default=str).splitlines()
    s2 = json.dumps(norm2, indent=2, default=str).splitlines()
    for i, (l1, l2) in enumerate(zip(s1, s2)):
        if l1 != l2:
            return False, f"Differs at line {i+1}: {l1.strip()} vs {l2.strip()}"
    if len(s1) != len(s2):
        return False, f"Response size differs: {len(s1)} vs {len(s2)} lines"
    return False, "Unknown difference"


def main():
    parser = argparse.ArgumentParser(
        description="Check equivalence of two Dgraph queries",
    )
    parser.add_argument("--query1", required=True, help="First query file")
    parser.add_argument("--query2", required=True, help="Second query file")
    parser.add_argument("--params", required=True, help="Parameter file")
    parser.add_argument("--param", type=int, help="Use specific parameter set (1-based)")
    parser.add_argument("--nparam", type=int, help="Number of parameter sets")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show results when differences found")
    parser.add_argument("--show-results", action="store_true",
                        help="Always show full results")
    parser.add_argument("--continue", dest="continue_on_diff", action="store_true",
                        help="Continue after finding differences")
    parser.add_argument("--save-report", help="Save report to JSON file")

    args = parser.parse_args()

    query1_text = Path(args.query1).read_text()
    query2_text = Path(args.query2).read_text()
    q1_name = Path(args.query1).name
    q2_name = Path(args.query2).name
    print(f"Query 1: {q1_name}")
    print(f"Query 2: {q2_name}")

    all_params = read_param_file(args.params)
    if args.param:
        if args.param < 1 or args.param > len(all_params):
            print(f"Error: --param {args.param} out of range (1-{len(all_params)})")
            sys.exit(1)
        param_sets = [all_params[args.param - 1]]
    elif args.nparam:
        param_sets = all_params[:args.nparam]
    else:
        param_sets = all_params
    print(f"Parameters: {len(param_sets)} sets")

    print(f"\nConnecting to Dgraph at {args.host}:{args.port}...")
    client = DgraphClient(args.host, args.port)

    identical = different = errors = 0
    differences = []
    error_details = []
    total_dur1 = total_dur2 = 0.0

    for idx, raw_params in enumerate(param_sets, 1):
        print(f"\n{'='*70}")
        print(f"Parameter set {idx}/{len(param_sets)}")
        params = dgraph_param_preprocessor(raw_params, 0)
        print(f"Params: {params}")

        # Execute query 1
        try:
            qr1 = client.execute(query1_text, params)
            dur1 = qr1.latency_ms
            print(f"  {q1_name}: {dur1:.1f}ms")
            total_dur1 += dur1
        except Exception as e:
            print(f"  {q1_name}: FAILED: {e}")
            errors += 1
            error_details.append({"param_idx": idx, "query": q1_name, "error": str(e)})
            continue

        # Execute query 2
        try:
            qr2 = client.execute(query2_text, params)
            dur2 = qr2.latency_ms
            print(f"  {q2_name}: {dur2:.1f}ms")
            total_dur2 += dur2
        except Exception as e:
            print(f"  {q2_name}: FAILED: {e}")
            errors += 1
            error_details.append({"param_idx": idx, "query": q2_name, "error": str(e)})
            continue

        if args.show_results:
            print(f"\n  {q1_name} results:")
            print(json.dumps(qr1.raw, indent=2, default=str))
            print(f"\n  {q2_name} results:")
            print(json.dumps(qr2.raw, indent=2, default=str))

        match, msg = compare_raw(qr1.raw, qr2.raw)

        if match:
            print(f"  MATCH: {msg}")
            identical += 1
        else:
            print(f"  DIFFERENT: {msg}")
            different += 1
            differences.append({"param_idx": idx, "difference": msg})

            if args.verbose and not args.show_results:
                print(f"\n  {q1_name} results:")
                print(json.dumps(qr1.raw, indent=2, default=str))
                print(f"\n  {q2_name} results:")
                print(json.dumps(qr2.raw, indent=2, default=str))

            if not args.continue_on_diff:
                print("\nStopping on first difference (use --continue to check all)")
                break

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"  Identical: {identical}")
    print(f"  Different: {different}")
    print(f"  Errors:    {errors}")
    n = max(identical + different, 1)
    print(f"  Avg {q1_name}: {total_dur1 / n:.1f}ms")
    print(f"  Avg {q2_name}: {total_dur2 / n:.1f}ms")

    if args.save_report:
        report = {
            "query1": q1_name,
            "query2": q2_name,
            "params_file": args.params,
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "identical": identical,
                "different": different,
                "errors": errors,
            },
            "differences": differences,
            "errors": error_details,
        }
        Path(args.save_report).write_text(json.dumps(report, indent=2))
        print(f"\nReport saved to {args.save_report}")

    if different > 0 or errors > 0:
        print("\nQueries are NOT equivalent")
        sys.exit(1)
    else:
        print("\nQueries are equivalent!")


if __name__ == "__main__":
    main()
