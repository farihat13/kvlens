#!/usr/bin/env python3
"""
Run LDBC Interactive queries against Dgraph.

Usage:
  # Run single IC query
  python -m kvengines.dgraph.ldbc.run_ldbc --num 1 --nparam 5 -v

  # Run all IC queries
  python -m kvengines.dgraph.ldbc.run_ldbc --all --nparam 35

  # Run with trace hooks (reset + counts/metrics collection)
  python -m kvengines.dgraph.ldbc.run_ldbc --all --nparam 35 --hooks

  # Run and save raw + parsed results to structured dirs
  python -m kvengines.dgraph.ldbc.run_ldbc --num 1 --save-results --outdir logs

  # Run single query with plan capture (requires profile patch + v=1)
  python -m kvengines.dgraph.ldbc.run_ldbc --num 1 --nparam 1 --explain -v
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kvengines.clients.dgraph import DgraphClient
from kvengines.ldbc.cli import DBConfig, LatencyResult, run_ldbc_cli
from kvengines.ldbc.params import dgraph_param_preprocessor
from kvengines.ldbc.parsers.dgraph import DgraphLDBCParser


# ── DB-specific callables ────────────────────────────────────────────

def create_client(args):
    return DgraphClient(host=args.host, port=args.port)


def extract_latency(result):
    """Extract Dgraph server_latency breakdown, falling back to wall-clock."""
    qr = result.query_result
    extensions = qr.metadata.get("extensions", {})
    sl = extensions.get("server_latency")

    if sl:
        lat = {
            "total_ms": sl.get("total_ns", 0) / 1_000_000,
            "parsing_ms": sl.get("parsing_ns", 0) / 1_000_000,
            "processing_ms": sl.get("processing_ns", 0) / 1_000_000,
            "encoding_ms": sl.get("encoding_ns", 0) / 1_000_000,
            "storage_ms": sl.get("storage_ns", 0) / 1_000_000,
        }
        detail = (f"(parsing: {lat['parsing_ms']:.2f},"
                  f" processing: {lat['processing_ms']:.2f},"
                  f" encoding: {lat['encoding_ms']:.2f},"
                  f" storage: {lat['storage_ms']:.2f})")
        return LatencyResult(
            latency_ms=lat["total_ms"],
            row_count=len(result.normalized) if result.normalized else "?",
            detail=detail,
            csv_values=[lat["total_ms"], lat["parsing_ms"],
                        lat["processing_ms"], lat["encoding_ms"],
                        lat["storage_ms"]],
            metadata=lat,
        )

    # Wall-clock fallback
    return LatencyResult(
        latency_ms=qr.latency_ms,
        row_count=len(result.normalized) if result.normalized else "?",
        detail=f"(wall: {qr.latency_ms / 1000:.2f}s)",
        csv_values=[qr.latency_ms, 0, 0, 0, 0],
        metadata={"total_ms": qr.latency_ms, "source": "wall_clock"},
    )


# ── Plan capture ────────────────────────────────────────────────────

_log_offset = 0  # alpha.log byte offset before current query


def _get_alpha_log_path() -> Path | None:
    runs_dir = os.environ.get("DGRAPH_RUNSDIR")
    if not runs_dir:
        return None
    p = Path(runs_dir) / "alpha.log"
    return p if p.exists() else None


def on_run_start(args):
    global _log_offset
    if args.explain:
        log_path = _get_alpha_log_path()
        if log_path:
            _log_offset = log_path.stat().st_size
            print(f"Plan capture enabled (reading from {log_path})")
        else:
            print("WARNING: alpha.log not found — plan capture will not work."
                  " Set $DGRAPH_RUNSDIR or start Dgraph with v=1.")


def collect_metrics(output_dir: Path, prefix: str) -> bool:
    """Collect metrics from Dgraph HTTP endpoints."""
    output_dir.mkdir(parents=True, exist_ok=True)
    commands = {
        "metrics": "curl -s http://localhost:8080/metrics",
        "vars": "curl -s http://localhost:8080/debug/vars",
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


def handle_profile(args, qr, pdir, lat, qname, pidx):
    """Extract plan from alpha.log and save to pdir/plan.txt."""
    global _log_offset
    from kvengines.dgraph.parse_plan import extract_plan_blocks, format_plan_block, is_internal_query

    log_path = _get_alpha_log_path()
    if not log_path or not log_path.exists():
        print("  Plan: alpha.log not found")
        return

    # Read new content since last offset
    with open(log_path) as f:
        f.seek(_log_offset)
        new_content = f.read()
    _log_offset = log_path.stat().st_size
    if len(new_content) == 0:
        print("WARNING: No new log content found for plan capture. Ensure Dgraph is running with v=1 and writing to alpha.log.")
        return

    blocks = extract_plan_blocks(new_content)
    user_blocks = [b for b in blocks if not is_internal_query(b)]

    if not user_blocks:
        print("  Plan: no plan found in log")
        return

    # Take the last user-query plan block
    block = user_blocks[-1]
    formatted = format_plan_block(block)

    pdir.mkdir(parents=True, exist_ok=True)
    plan_path = pdir / "plan.txt"
    with open(plan_path, "w") as f:
        f.write(f"Query: {qname}\n")
        f.write(f"Param: {pidx}\n")
        f.write(f"Latency: {lat.latency_ms:.2f}ms\n\n")
        f.write(formatted)
        f.write("\n\n--- Raw ---\n\n")
        f.write(block["raw_text"])
    print(f"  Plan saved: {plan_path}")


def write_info_txt(pdir, qname, pidx, lat, qr):
    pdir.mkdir(parents=True, exist_ok=True)
    with open(pdir / "info.txt", "w") as f:
        f.write(f"Query: {qname}\n")
        f.write(f"Param Index: {pidx}\n")
        f.write(f"Latency: {lat.latency_ms:.2f}ms\n")


# ── Entry point ──────────────────────────────────────────────────────

config = DBConfig(
    db_name="dgraph",
    description="Run LDBC Interactive queries against Dgraph.",
    default_host="localhost",
    default_port=8080,
    create_client=create_client,
    parser_class=DgraphLDBCParser,
    param_preprocessor=dgraph_param_preprocessor,
    extract_latency=extract_latency,
    csv_header=["query", "param", "latency_ms", "parsing_ms",
                "processing_ms", "encoding_ms", "storage_ms"],
    on_run_start=on_run_start,
    pre_hook=collect_metrics,
    post_hook=collect_metrics,
    write_info_txt=write_info_txt,
    handle_profile=handle_profile,
)

if __name__ == "__main__":
    run_ldbc_cli(config)
