"""Shared CLI for running LDBC Interactive queries against any database.

Provides the `run_ldbc_cli(config)` entry point and the `DBConfig` /
`LatencyResult` dataclasses that each database wrapper fills in.
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from kvengines.ldbc.runner import LDBCQueryResult, LDBCRunner
from kvengines.results import ResultStore


# ── Constants ────────────────────────────────────────────────────────

IC_RANGE = range(1, 15)   # IC1..IC14
IS_RANGE = range(1, 8)    # IS1..IS7

DEFAULT_SF = "0.3"
DEFAULT_PARAMS_DIR_TEMPLATE = "data/ldbc/substitution_parameters-sf{sf}"
DEFAULT_QUERIES_DIR = "queries"
DEFAULT_OUTDIR = "logs"


# ── Signal handling ──────────────────────────────────────────────────

def _signal_handler(sig, frame):
    print("\n\nInterrupt received, exiting...")
    sys.exit(130)

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ── Dataclasses ──────────────────────────────────────────────────────

@dataclass
class LatencyResult:
    """Bridges different per-DB latency formats into a common container."""

    latency_ms: float
    """Primary latency for tracking and averages."""

    row_count: int | str
    """Result row count (int, or '?' if unknown)."""

    detail: str
    """Parenthesised detail string, e.g. '(parsing: 1.2, ...)' or '(wall: 0.12s)'."""

    csv_values: list
    """Values AFTER [qname, pidx] — the DB determines the columns."""

    metadata: dict
    """Saved into metadata.json under 'server_latency'."""


@dataclass
class DBConfig:
    """Database-specific configuration for the shared LDBC CLI."""

    # Required
    db_name: str
    """Short name used in paths and messages, e.g. 'dgraph' or 'nebula'."""

    description: str
    """argparse description line."""

    default_host: str
    default_port: int

    create_client: Callable
    """(args) -> context-manager DatabaseClient."""

    parser_class: type
    """LDBCParser subclass to instantiate."""

    param_preprocessor: Callable
    """(params_dict, qnum) -> params_dict."""

    extract_latency: Callable
    """(result: LDBCQueryResult) -> LatencyResult."""

    csv_header: list[str]
    """Full CSV header row including 'query' and 'param'."""

    # Optional hooks (all default to None)
    add_extra_args: Callable | None = None
    """(parser) -> None; add DB-specific CLI flags."""

    create_query_preprocessor: Callable | None = None
    """(args) -> callable | None."""

    on_run_start: Callable | None = None
    """(args) -> None; called once before the main loop."""

    pre_hook: Callable | None = None
    """(output_dir, prefix) -> bool; called before each query execution (e.g. reset counters)."""

    post_hook: Callable | None = None
    """(output_dir, prefix) -> bool; called after each query execution (e.g. collect metrics)."""

    write_info_txt: Callable | None = None
    """(pdir, qname, pidx, lat, qr) -> None."""

    handle_profile: Callable | None = None
    """(args, qr, pdir, lat, qname, pidx) -> None."""

    format_raw: Callable | None = None
    """(qr) -> None; prints raw response to stdout."""

    validate_result: Callable | None = None
    """(qr) -> None; raise on bad data."""


# ── CSV output ───────────────────────────────────────────────────────

def write_latency_csv(latencies: list, csv_path: Path, header: list[str]):
    """Write latency CSV with the given header row."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in latencies:
            writer.writerow(row)
    print(f"Latencies written to {csv_path}")


# ── Arg parsing ──────────────────────────────────────────────────────

def parse_ldbc_args(config: DBConfig) -> argparse.Namespace:
    """Build and parse the shared LDBC CLI, with optional DB-specific args."""
    p = argparse.ArgumentParser(
        description=config.description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Query selection
    p.add_argument("--num", type=int, help="Run single query number N")
    p.add_argument("--all", action="store_true", help="Run all queries")
    p.add_argument("-qt", "--query-type", choices=["complex", "short"],
                   default="complex", help="Query type (default: complex)")

    # Param selection
    p.add_argument("-x", "--nparam", type=int, default=1,
                   help="Number of param rows per query (default: 1, 0 = all)")
    p.add_argument("-p", "--param", type=int,
                   help="Run only specific param index (1-based)")

    # Directories
    p.add_argument("--params-dir", help="Substitution parameters directory")
    p.add_argument("--queries-dir", default=DEFAULT_QUERIES_DIR,
                   help="Queries directory")
    p.add_argument("--sf", default=DEFAULT_SF,
                   help="Scale factor (default: 0.3)")
    p.add_argument("--outdir", default=DEFAULT_OUTDIR,
                   help="Output directory (default: logs)")

    # Connection
    p.add_argument("--host", default=config.default_host,
                   help=f"{config.db_name} host")
    p.add_argument("--port", type=int, default=config.default_port,
                   help=f"{config.db_name} port")

    # Execution
    p.add_argument("-c", "--continue-on-error", action="store_true",
                   help="Continue to next query on error")
    p.add_argument("--dur", type=int, default=0,
                   help="Sleep N seconds between param executions (default: 0)")
    p.add_argument("--profile", action="store_true",
                   help="Enable profiling (DB-specific behaviour)")
    p.add_argument("--explain", action="store_true",
                   help="Show query/execution plan (DB-specific behaviour)")

    # Output
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose output")
    p.add_argument("--show-raw", action="store_true",
                   help="Print full response data")

    # Extended features
    p.add_argument("--hooks", action="store_true",
                   help="Enable before/after hooks (metrics collection)")
    p.add_argument("--save-results", action="store_true",
                   help="Save raw + parsed results to structured directories")
    p.add_argument("--show-results", action="store_true",
                   help="Print parsed results to console")
    # DB-specific args
    if config.add_extra_args:
        config.add_extra_args(p)

    args = p.parse_args()

    if not args.all and args.num is None:
        p.error("Specify --num N or --all")

    if not args.params_dir:
        args.params_dir = DEFAULT_PARAMS_DIR_TEMPLATE.format(sf=args.sf)

    if args.verbose:
        args.show_raw = True

    return args


# ── Main entry point ─────────────────────────────────────────────────

def run_ldbc_cli(config: DBConfig):
    """Shared main function for LDBC CLI runners."""
    args = parse_ldbc_args(config)

    # Determine query range
    if args.query_type == "short":
        qtype = "is"
        max_num = 7
        all_range = IS_RANGE
    else:
        qtype = "ic"
        max_num = 14
        all_range = IC_RANGE

    if args.all:
        query_numbers = list(all_range)
    else:
        if args.num < 1 or args.num > max_num:
            print(f"ERROR: --num must be between 1 and {max_num}")
            sys.exit(1)
        query_numbers = [args.num]

    # Output base: <outdir>/<db>/sf<SF>/
    sf_dir = Path(args.outdir) / config.db_name / f"sf{args.sf}"

    # Query preprocessor (Nebula uses PROFILE/EXPLAIN; Dgraph returns None)
    query_preprocessor = None
    if config.create_query_preprocessor:
        query_preprocessor = config.create_query_preprocessor(args)

    result_store = ResultStore(Path(args.outdir)) if args.save_results else None

    # One-time start message
    if config.on_run_start:
        config.on_run_start(args)

    with config.create_client(args) as client:
        runner = LDBCRunner(
            client=client,
            queries_dir=args.queries_dir,
            params_dir=args.params_dir,
            param_preprocessor=config.param_preprocessor,
            parser=config.parser_class(),
            query_preprocessor=query_preprocessor,
            result_store=result_store,
            sf=str(args.sf),
        )

        error_list = []
        latencies = []
        start_time = datetime.now()

        for qnum in query_numbers:
            qname = f"{qtype}{qnum}"
            print(f"\n=== {qname} ===")

            # Determine param indices to run
            try:
                if args.param:
                    param_indices = [args.param]
                elif args.nparam == 0:
                    all_params = runner.load_params(qtype, qnum, nparam=0)
                    param_indices = list(range(1, len(all_params) + 1))
                else:
                    param_indices = list(range(1, args.nparam + 1))
            except FileNotFoundError as e:
                print(f"ERROR: {e}")
                if args.continue_on_error:
                    continue
                sys.exit(3)

            query_latencies = []

            for pidx in param_indices:
                print(f"\n--- {qname}: Param {pidx} ---")

                try:
                    pdir = sf_dir / qname / f"p{pidx}"

                    # Pre-hook
                    if args.hooks and config.pre_hook:
                        config.pre_hook(pdir / "hooks_before", "")

                    # Execute + parse + save via LDBCRunner
                    result = runner.run_query(qtype, qnum, param_idx=pidx)
                    qr = result.query_result

                    # Extract latency
                    lat = config.extract_latency(result)
                    query_latencies.append(lat.latency_ms)
                    latencies.append([qname, pidx] + lat.csv_values)

                    print(f"  Latency: {lat.latency_ms:.2f} ms | Rows: {lat.row_count}"
                          f" | Parse: {result.parse_ms:.2f} ms  {lat.detail}")

                    # Post-hook
                    if args.hooks and config.post_hook:
                        config.post_hook(pdir / "hooks_after", "")
                    if config.write_info_txt:
                        config.write_info_txt(pdir, qname, pidx, lat, qr)

                    # Save server latency into metadata.json
                    if result_store:
                        meta_path = result_store._result_dir(
                            client.name, str(args.sf), qname, pidx,
                        ) / "metadata.json"
                        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
                        meta["server_latency"] = lat.metadata
                        meta["parse_ms"] = result.parse_ms
                        meta_path.parent.mkdir(parents=True, exist_ok=True)
                        meta_path.write_text(json.dumps(meta, indent=2))

                    # Handle profile/explain
                    if config.handle_profile and (args.profile or args.explain):
                        config.handle_profile(args, qr, pdir, lat, qname, pidx)

                    # Print parsed results or raw
                    if result.normalized and args.show_results:
                        from kvengines.ldbc.display import print_parsed_results
                        print_parsed_results(result.normalized)
                    elif args.show_raw:
                        if config.format_raw:
                            config.format_raw(qr)
                        else:
                            print("  Raw:", json.dumps(qr.raw, indent=2))

                    # Validate
                    if config.validate_result:
                        config.validate_result(qr)

                    print("  [OK]")

                    if args.dur > 0:
                        time.sleep(args.dur)

                except Exception as e:
                    print(f"  [ERROR] {e}")
                    if args.continue_on_error:
                        error_list.append((qname, pidx, str(e)))
                        continue
                    sys.exit(6)

            # Per-query summary
            if query_latencies:
                avg = sum(query_latencies) / len(query_latencies)
                print(f"\n  {qname}: {len(query_latencies)} runs, avg {avg:.2f} ms")

        # ── Summary ──────────────────────────────────────────────
        if latencies:
            csv_path = sf_dir / "latencies.csv"
            write_latency_csv(latencies, csv_path, config.csv_header)

        end_time = datetime.now()
        duration_sec = (end_time - start_time).total_seconds()

        if error_list:
            print("\n=== ERRORS SUMMARY ===")
            for qname, pidx, err_msg in error_list:
                print(f"  {qname} param {pidx}: {err_msg}")
            print(f"Done! Time took: {duration_sec:.2f} seconds")
            sys.exit(10)

        print("\n==============================")
        print("ALL QUERIES COMPLETED SUCCESSFULLY")
        print("==============================")
        print(f"Done! Time took: {duration_sec:.2f} seconds")
