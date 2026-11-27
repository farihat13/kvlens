#!/usr/bin/env python3
"""
Interactive plan inspection for NebulaGraph queries.

Runs PROFILE on a query and produces:
  1. plan.txt — serialized PlanDescription
  2. plan_dag.png — Graphviz DAG, color-coded by time
  3. plan_breakdown.png — horizontal bar chart of time per node
  4. Terminal summary table

Can also re-analyze a previously saved plan.txt (--from-saved).

Usage:
  # Run PROFILE on a query file
  python -m kvengines.nebula.plan_query -f queries/interactive-complex-1.gql \
      --params "personId=933,firstName=Mahinda"

  # Inline query
  python -m kvengines.nebula.plan_query -s "MATCH (p:Person {id: 933})-[:KNOWS]->(f) RETURN f LIMIT 10"

  # LDBC param file
  python -m kvengines.nebula.plan_query -f queries/interactive-complex-6.gql \
      --param-file data/ldbc/substitution_parameters-sf0.3/interactive_6_param.txt \
      --param-index 1

  # Re-analyze a saved plan.txt (no DB connection needed)
  python -m kvengines.nebula.plan_query --from-saved path/to/plan.txt

  # Output to a specific directory
  python -m kvengines.nebula.plan_query -f queries/interactive-complex-1.gql --outdir output/
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from pathlib import Path

# Add lib/ to path for kvengines imports


def parse_params_string(params_str: str) -> dict:
    """Parse 'key1=val1,key2=val2' into a dict, coercing numeric values."""
    params = {}
    for pair in params_str.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        key, val = pair.split("=", 1)
        key, val = key.strip(), val.strip()
        # Coerce to int/float if possible
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except ValueError:
                pass
        params[key] = val
    return params


def load_param_from_file(param_file: str, param_index: int) -> dict:
    """Load a single param set from an LDBC substitution_parameters file."""
    path = Path(param_file)
    with open(path) as f:
        reader = csv.reader(f, delimiter="|")
        headers = next(reader)
        for i, row in enumerate(reader, start=1):
            if i == param_index:
                params = {}
                for h, v in zip(headers, row):
                    h = h.strip()
                    v = v.strip()
                    try:
                        v = int(v)
                    except ValueError:
                        try:
                            v = float(v)
                        except ValueError:
                            pass
                    params[h] = v
                return params
    raise ValueError(f"Param index {param_index} not found in {param_file}")


def run_profile(query_text: str, space: str, host: str, port: int,
                params: dict | None = None) -> tuple:
    """Run PROFILE on a query and return (plan_desc, server_latency_us, wall_ms).

    Returns the PlanDescription thrift object.
    """
    from kvengines.clients.nebula import NebulaClient

    client = NebulaClient(host=host, port=port, space=space)

    # Strip comments and add PROFILE prefix
    clean = "\n".join(
        line for line in query_text.splitlines()
        if not line.strip().startswith(("--", "#"))
    )
    profiled = f"PROFILE {clean}"

    qr = client.execute(profiled, params=params)
    plan_desc = qr.metadata.get("plan_desc")
    server_latency_us = qr.metadata.get("server_latency_us", 0)

    client.close()
    return plan_desc, server_latency_us, qr.latency_ms


def save_plan_txt(plan_desc, latency_us: int, outdir: Path) -> Path:
    """Save plan description in the standard plan.txt format."""
    outdir.mkdir(parents=True, exist_ok=True)
    plan_path = outdir / "plan.txt"
    with open(plan_path, "w") as f:
        f.write(f"Latency (us): {latency_us}\n\n")
        f.write(str(plan_desc))
    return plan_path


def analyze_plan(plan_desc, latency_us: int, outdir: Path, verbose: bool = True):
    """Generate visualizations and summary from a PlanDescription."""
    from kvengines.nebula import extract_plan_metrics as epm

    epm.verbose = verbose
    outdir.mkdir(parents=True, exist_ok=True)

    # 1. DAG visualization
    dag_path = outdir / "plan_dag"
    dag_file = epm.visualize_plan_graphviz(plan_desc, output_file=str(dag_path))
    if dag_file:
        print(f"  DAG: {dag_file}")

    # 2. Bar chart
    bar_path = outdir / "plan_breakdown"
    bar_file = epm.visualize_plan_barchart(plan_desc, output_file=str(bar_path),
                                            latency_us=latency_us)
    if bar_file:
        print(f"  Bar chart: {bar_file}")

    # 3. Terminal summary table
    df = epm.plan_to_dataframe(plan_desc)
    if not df.empty:
        print(f"\n  === Plan Summary (latency: {latency_us/1000:.1f}ms) ===")
        # Format the table nicely
        pd_fmt = df.to_string(index=False, float_format=lambda x: f"{x:.2f}")
        for line in pd_fmt.splitlines():
            print(f"  {line}")


def substitute_params(query_text: str, params: dict) -> str:
    """Replace $paramName placeholders with values in query text."""
    result = query_text
    for key, val in params.items():
        placeholder = f"${key}"
        if isinstance(val, str):
            replacement = f'"{val}"'
        else:
            replacement = str(val)
        result = result.replace(placeholder, replacement)
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Interactive plan inspection for NebulaGraph queries.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Query source (mutually exclusive)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-f", "--file", help="Query file path (.gql)")
    source.add_argument("-s", "--string", help="Inline query string")
    source.add_argument("--from-saved", metavar="PLAN_TXT",
                        help="Re-analyze a saved plan.txt (no DB needed)")

    # Parameters
    parser.add_argument("--params", help='Query params as "key1=val1,key2=val2"')
    parser.add_argument("--param-file", help="LDBC substitution_parameters file")
    parser.add_argument("--param-index", type=int, default=1,
                        help="Row index in param file (default: 1)")

    # Connection
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9669)
    parser.add_argument("--space", default="ldbc")

    # Output
    parser.add_argument("--outdir", default="./logs/plan-query",
                        help="Output directory for plan.txt + figures")
    parser.add_argument("--no-dag", action="store_true", help="Skip DAG visualization")
    parser.add_argument("--no-barchart", action="store_true", help="Skip bar chart")
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    outdir = Path(args.outdir)

    if args.from_saved:
        # Re-analyze mode: load from file
        from kvengines.nebula import extract_plan_metrics as epm

        plan_path = Path(args.from_saved)
        if not plan_path.exists():
            print(f"Error: {plan_path} not found", file=sys.stderr)
            sys.exit(1)

        print(f"Loading plan from: {plan_path}")
        plan_desc, latency = epm.parse_plan_file(str(plan_path))
        if plan_desc is None:
            print("Error: Failed to parse plan file", file=sys.stderr)
            sys.exit(1)

        # Use the plan file's parent as output dir if no --outdir given
        if args.outdir == "./logs/plan-query":
            outdir = plan_path.parent

        analyze_plan(plan_desc, latency, outdir, verbose=args.verbose)
        return

    # Live mode: run PROFILE
    # Load query
    if args.file:
        query_path = Path(args.file)
        if not query_path.exists():
            print(f"Error: {query_path} not found", file=sys.stderr)
            sys.exit(1)
        query_text = query_path.read_text()
        # Use query filename as output subdir
        if args.outdir == "./logs/plan-query":
            outdir = Path("./logs/plan-query") / query_path.stem
    else:
        query_text = args.string

    # Load params
    params = None
    if args.params:
        params = parse_params_string(args.params)
    elif args.param_file:
        params = load_param_from_file(args.param_file, args.param_index)
        # Apply Nebula LDBC preprocessing (VID prefixes, endDate computation)
        qnum_match = re.search(r'interactive_(\d+)_param', args.param_file)
        if qnum_match:
            from kvengines.ldbc.params import nebula_param_preprocessor
            qnum = int(qnum_match.group(1))
            params = nebula_param_preprocessor(params, qnum)

    # Substitute params into query (NebulaGraph requires inline params for PROFILE)
    if params:
        query_text = substitute_params(query_text, params)
        print(f"Params: {params}")

    print(f"Running PROFILE on query...")
    plan_desc, latency_us, wall_ms = run_profile(
        query_text, args.space, args.host, args.port
    )

    if not plan_desc:
        print("Error: No plan description returned (is PROFILE enabled?)", file=sys.stderr)
        sys.exit(1)

    print(f"  Server latency: {latency_us/1000:.1f}ms  Wall time: {wall_ms:.1f}ms")

    # Save plan.txt
    plan_path = save_plan_txt(plan_desc, latency_us, outdir)
    print(f"  Plan saved: {plan_path}")

    # Generate visualizations and summary
    analyze_plan(plan_desc, latency_us, outdir, verbose=args.verbose)


if __name__ == "__main__":
    main()
