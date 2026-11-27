#!/usr/bin/env python3
"""
Run LDBC Interactive queries against ArangoDB.

Usage:
  # Run single complex query
  python -m kvengines.arangodb.ldbc.run_ldbc --num 1 -v

  # Run all complex queries
  python -m kvengines.arangodb.ldbc.run_ldbc --all --nparam 35 -c

  # Run short queries
  python -m kvengines.arangodb.ldbc.run_ldbc --all -qt short -c

  # Run with before/after hooks (metrics collection)
  python -m kvengines.arangodb.ldbc.run_ldbc --all --nparam 35 --hooks

  # Save raw + parsed results to structured dirs
  python -m kvengines.arangodb.ldbc.run_ldbc --num 1 --save-results --outdir logs
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from kvengines.clients.arangodb import ArangoDBClient
from kvengines.ldbc.cli import DBConfig, LatencyResult, run_ldbc_cli
from kvengines.ldbc.params import arangodb_param_preprocessor
from kvengines.ldbc.parsers.arangodb import ArangoDBLDBCParser


DEFAULT_DB = "ldbc"
DEFAULT_PORT = 8529


# ── DB-specific callables ────────────────────────────────────────────

def add_extra_args(parser):
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="Database name (default: ldbc)")


def create_client(args):
    return ArangoDBClient(host=args.host, port=args.port, db_name=args.db)


def extract_latency(result):
    """Extract wall-clock latency (ArangoDB doesn't expose server-side breakdown)."""
    qr = result.query_result
    latency_ms = qr.latency_ms
    row_count = len(result.normalized) if result.normalized else len(qr.raw)

    return LatencyResult(
        latency_ms=latency_ms,
        row_count=row_count,
        detail="",
        csv_values=[latency_ms],
        metadata={
            "wall_ms": latency_ms,
        },
    )


def on_run_start(args):
    print(f"Connecting to ArangoDB at {args.host}:{args.port} (db={args.db})...")


def collect_metrics(output_dir: Path, prefix: str) -> bool:
    """Collect metrics from ArangoDB HTTP endpoints."""
    output_dir.mkdir(parents=True, exist_ok=True)
    commands = {
        "statistics": f"curl -s http://127.0.0.1:{DEFAULT_PORT}/_admin/statistics",
        "metrics": f"curl -s http://127.0.0.1:{DEFAULT_PORT}/_admin/metrics",
    }
    success = True
    for name, cmd in commands.items():
        output_file = output_dir / f"{prefix}{name}.log"
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if res.returncode == 0:
            with open(output_file, "w") as f:
                f.write(res.stdout)
        else:
            print(f"  WARNING: Failed to collect {name}")
            success = False
    return success


def write_info_txt(pdir, qname, pidx, lat, qr):
    pdir.mkdir(parents=True, exist_ok=True)
    with open(pdir / "info.txt", "w") as f:
        f.write(f"Query: {qname}\n")
        f.write(f"Param Index: {pidx}\n")
        f.write(f"Latency: {qr.latency_ms:.3f}ms\n")
        f.write(f"Rows: {lat.row_count}\n")


def format_raw(qr):
    """Print raw AQL results as formatted JSON."""
    for row in qr.raw:
        print(json.dumps(row, indent=2, default=str))


# ── Entry point ──────────────────────────────────────────────────────

config = DBConfig(
    db_name="arangodb",
    description="Run LDBC Interactive queries against ArangoDB.",
    default_host="localhost",
    default_port=DEFAULT_PORT,
    create_client=create_client,
    parser_class=ArangoDBLDBCParser,
    param_preprocessor=arangodb_param_preprocessor,
    extract_latency=extract_latency,
    csv_header=["query", "param", "latency_ms"],
    add_extra_args=add_extra_args,
    on_run_start=on_run_start,
    pre_hook=collect_metrics,
    post_hook=collect_metrics,
    write_info_txt=write_info_txt,
    format_raw=format_raw,
)

if __name__ == "__main__":
    run_ldbc_cli(config)
