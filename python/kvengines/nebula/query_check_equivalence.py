#!/usr/bin/env python3
"""
Check equivalence of two NebulaGraph queries by comparing their results.

Runs two query variants against the same NebulaGraph instance with identical
parameters and reports whether they produce the same results.

Usage examples:
  # Compare two query files with all parameters
  python -m kvengines.nebula.query_check_equivalence \
    --query1 queries/ic1.gql --query2 queries/ic1.v2.gql \
    --params data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt

  # Compare with specific parameter set
  python -m kvengines.nebula.query_check_equivalence \
    --query1 queries/ic1.gql --query2 queries/ic1.v2.gql \
    --params data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt \
    --param 5

  # Verbose mode (shows results only when differences found)
  python -m kvengines.nebula.query_check_equivalence \
    --query1 queries/ic1.gql --query2 queries/ic1.v2.gql \
    --params data/ldbc/substitution_parameters-sf0.3/interactive_1_param.txt -v
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from kvengines.clients.nebula import NebulaClient
from kvengines.ldbc.params import read_param_file, nebula_param_preprocessor


def raw_to_comparable(raw):
    """Convert Nebula ResultSet to sorted list of normalized dicts."""
    df = raw.as_data_frame()
    records = df.to_dict("records")
    normalized = []
    for record in records:
        norm = {}
        for k, v in record.items():
            if isinstance(v, (list, dict)):
                norm[k] = json.dumps(v, sort_keys=True)
            elif v is None:
                norm[k] = None
            else:
                norm[k] = v
        normalized.append(norm)
    normalized.sort(
        key=lambda r: tuple(str(v) if v is not None else "" for v in r.values())
    )
    return normalized


def compare_raw(result1, result2):
    """Compare two lists of normalized dicts. Returns (match, message)."""
    if len(result1) != len(result2):
        return False, f"Row count differs: {len(result1)} vs {len(result2)}"
    if not result1:
        return True, "Both results empty"
    if set(result1[0].keys()) != set(result2[0].keys()):
        return False, (
            f"Column mismatch: {set(result1[0].keys())} vs {set(result2[0].keys())}"
        )
    for i, (r1, r2) in enumerate(zip(result1, result2)):
        if r1 != r2:
            diffs = [f"{k}: {r1[k]} vs {r2[k]}" for k in r1 if r1[k] != r2[k]]
            return False, f"Row {i+1} differs: " + "; ".join(diffs)
    return True, f"Identical ({len(result1)} rows)"


def main():
    parser = argparse.ArgumentParser(
        description="Check equivalence of two NebulaGraph queries",
    )
    parser.add_argument("--query1", required=True, help="First query file")
    parser.add_argument("--query2", required=True, help="Second query file")
    parser.add_argument("--params", required=True, help="Parameter file")
    parser.add_argument("--param", type=int, help="Use specific parameter set (1-based)")
    parser.add_argument("--nparam", type=int, help="Number of parameter sets")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9669)
    parser.add_argument("--space", default="ldbc")
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

    print(f"\nConnecting to NebulaGraph at {args.host}:{args.port}...")
    client = NebulaClient(args.host, args.port, space=args.space)

    identical = different = errors = 0
    differences = []
    error_details = []
    total_dur1 = total_dur2 = 0.0

    try:
        for idx, raw_params in enumerate(param_sets, 1):
            print(f"\n{'='*70}")
            print(f"Parameter set {idx}/{len(param_sets)}")
            params = nebula_param_preprocessor(raw_params, 0)
            print(f"Params: {params}")

            # Execute query 1
            try:
                qr1 = client.execute(query1_text, params)
                dur1 = qr1.latency_ms
                rows1 = qr1.raw.row_size()
                print(f"  {q1_name}: {dur1:.1f}ms, {rows1} rows")
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
                rows2 = qr2.raw.row_size()
                print(f"  {q2_name}: {dur2:.1f}ms, {rows2} rows")
                total_dur2 += dur2
            except Exception as e:
                print(f"  {q2_name}: FAILED: {e}")
                errors += 1
                error_details.append({"param_idx": idx, "query": q2_name, "error": str(e)})
                continue

            if args.show_results:
                import pandas as pd
                pd.set_option("display.max_rows", None)
                pd.set_option("display.width", None)
                print(f"\n  {q1_name} results:")
                print(qr1.raw.as_data_frame().to_string(index=False))
                print(f"\n  {q2_name} results:")
                print(qr2.raw.as_data_frame().to_string(index=False))

            comp1 = raw_to_comparable(qr1.raw)
            comp2 = raw_to_comparable(qr2.raw)
            match, msg = compare_raw(comp1, comp2)

            if match:
                print(f"  MATCH: {msg}")
                identical += 1
            else:
                print(f"  DIFFERENT: {msg}")
                different += 1
                differences.append({"param_idx": idx, "difference": msg})

                if args.verbose and not args.show_results:
                    import pandas as pd
                    pd.set_option("display.max_rows", None)
                    pd.set_option("display.width", None)
                    print(f"\n  {q1_name} results:")
                    print(qr1.raw.as_data_frame().to_string(index=False))
                    print(f"\n  {q2_name} results:")
                    print(qr2.raw.as_data_frame().to_string(index=False))

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

    finally:
        client.close()


if __name__ == "__main__":
    main()
