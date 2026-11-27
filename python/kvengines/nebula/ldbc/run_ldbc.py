#!/usr/bin/env python3
"""
Run LDBC Interactive queries against NebulaGraph.

Usage:
  # Run single complex query
  python -m kvengines.nebula.ldbc.run_ldbc --num 2 -v

  # Run all complex queries with profiling
  python -m kvengines.nebula.ldbc.run_ldbc --all --nparam 35 --profile -c

  # Run short queries
  python -m kvengines.nebula.ldbc.run_ldbc --all -qt short -c

  # Run with before/after hooks (metrics collection)
  python -m kvengines.nebula.ldbc.run_ldbc --all --nparam 35 --hooks

  # Run single query with EXPLAIN
  python -m kvengines.nebula.ldbc.run_ldbc --num 1 --nparam 1 --explain -v

  # Save raw + parsed results to structured dirs
  python -m kvengines.nebula.ldbc.run_ldbc --num 1 --save-results --outdir logs
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from kvengines.clients.nebula import NebulaClient
from kvengines.ldbc.cli import DBConfig, LatencyResult, run_ldbc_cli
from kvengines.ldbc.params import nebula_param_preprocessor
from kvengines.ldbc.parsers.nebula import NebulaLDBCParser


DEFAULT_SPACE = "ldbc"


# ── Query preprocessing ─────────────────────────────────────────────

def strip_comments(query_text: str) -> str:
    """Remove comment lines (-- and #) from query text."""
    return "\n".join(
        line for line in query_text.splitlines()
        if not line.strip().startswith(("--", "#"))
    )


def create_query_preprocessor(args):
    """Create a query preprocessor that optionally adds PROFILE or EXPLAIN."""
    if not args.profile and not args.explain:
        return None

    def preprocessor(query_text: str, qnum: int) -> str:
        text = strip_comments(query_text)
        if args.profile:
            return f"PROFILE {text}"
        elif args.explain:
            return f"EXPLAIN {text}"
        return text

    return preprocessor


# ── DB-specific callables ────────────────────────────────────────────

def add_extra_args(parser):
    parser.add_argument("--space", default=DEFAULT_SPACE,
                        help="Graph space (default: ldbc)")


def create_client(args):
    return NebulaClient(host=args.host, port=args.port, space=args.space)


def extract_latency(result):
    """Extract Nebula server_latency_us from metadata."""
    qr = result.query_result
    duration_s = qr.latency_ms / 1000.0
    server_latency_us = qr.metadata.get("server_latency_us", 0)
    latency_ms = server_latency_us / 1000.0
    row_count = len(result.normalized) if result.normalized else qr.raw.row_size()

    return LatencyResult(
        latency_ms=latency_ms,
        row_count=row_count,
        detail=f"(wall: {duration_s:.2f}s)",
        csv_values=[latency_ms],
        metadata={
            "server_latency_us": server_latency_us,
            "server_latency_ms": latency_ms,
        },
    )


def on_run_start(args):
    print(f"Connecting to NebulaGraph at {args.host}:{args.port}...")


def collect_metrics(output_dir: Path, prefix: str) -> bool:
    """Collect metrics from all Nebula service endpoints."""
    output_dir.mkdir(parents=True, exist_ok=True)
    commands = {
        "storage": "curl -s http://127.0.0.1:19779/stats",
        "graph": "curl -s http://127.0.0.1:19669/stats",
        "meta": "curl -s http://127.0.0.1:19559/stats",
        "rocksdb": "curl -s http://127.0.0.1:19779/rocksdb_stats",
    }
    success = True
    for service, cmd in commands.items():
        output_file = output_dir / f"{prefix}{service}.log"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            with open(output_file, "w") as f:
                f.write(res.stdout)
        else:
            print(f"  WARNING: Failed to collect {service} stats")
            success = False
    return success


def write_info_txt(pdir, qname, pidx, lat, qr):
    pdir.mkdir(parents=True, exist_ok=True)
    duration_s = qr.latency_ms / 1000.0
    server_latency_us = qr.metadata.get("server_latency_us", 0)
    with open(pdir / "info.txt", "w") as f:
        f.write(f"Query: {qname}\n")
        f.write(f"Param Index: {pidx}\n")
        f.write(f"Duration: {duration_s:.3f}s\n")
        f.write(f"Latency: {server_latency_us}us\n")
        f.write(f"Rows: {lat.row_count}\n")


def handle_profile(args, qr, pdir, lat, qname, pidx):
    """Save PROFILE plan or print EXPLAIN plan."""
    server_latency_us = qr.metadata.get("server_latency_us", 0)
    plan_desc = qr.metadata.get("plan_desc")

    if args.profile and plan_desc:
        pdir.mkdir(parents=True, exist_ok=True)
        plan_path = pdir / "plan.txt"
        with open(plan_path, "w") as f:
            f.write(f"Latency (us): {server_latency_us}\n\n")
            f.write(str(plan_desc))
        print(f"  Plan saved to: {plan_path}")
    elif args.explain and plan_desc:
        print("\n  Execution Plan:")
        print("  " + str(plan_desc))
    elif (args.profile or args.explain) and not plan_desc:
        print("  No plan description available")


def format_raw(qr):
    """Print raw Nebula response as a pandas DataFrame."""
    import pandas as pd
    df = qr.raw.as_data_frame()
    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    print("\n" + df.to_string())


def validate_result(qr):
    """Raise if BAD_* values appear in the result set."""
    if "BAD_" in str(qr.raw):
        raise RuntimeError("BAD_* found in results")


# ── Entry point ──────────────────────────────────────────────────────

config = DBConfig(
    db_name="nebula",
    description="Run LDBC Interactive queries against NebulaGraph.",
    default_host="127.0.0.1",
    default_port=9669,
    create_client=create_client,
    parser_class=NebulaLDBCParser,
    param_preprocessor=nebula_param_preprocessor,
    extract_latency=extract_latency,
    csv_header=["query", "param", "latency_ms"],
    add_extra_args=add_extra_args,
    create_query_preprocessor=create_query_preprocessor,
    on_run_start=on_run_start,
    pre_hook=collect_metrics,
    post_hook=collect_metrics,
    write_info_txt=write_info_txt,
    handle_profile=handle_profile,
    format_raw=format_raw,
    validate_result=validate_result,
)

if __name__ == "__main__":
    run_ldbc_cli(config)
