#!/usr/bin/env python3
"""Compute date thresholds for target selectivities on LDBC CSV data.

Given an LDBC dataset directory, reads creationDate columns from the 4 entity
CSVs used by microbench queries (person, knows, comment, post) and finds the
date at each requested percentile.  This lets you calibrate date-range filters
like `creationDate >= min AND creationDate <= end` so they hit a known fraction
of rows regardless of scale factor.

Usage:
    python -m kvengines.nebula.microbench.selectivity <dataset_dir> [--pct 0.1 1 10 50] [--start 2011-01-01]
"""

import argparse
import bisect
import csv
import math
import os
import sys
from datetime import datetime, timezone

# (label, relative CSV path)
ENTITIES = [
    ("Person",  "dynamic/person_0_0.csv"),
    ("Knows",   "dynamic/person_knows_person_0_0.csv"),
    ("Comment", "dynamic/comment_0_0.csv"),
    ("Post",    "dynamic/post_0_0.csv"),
]

DATA_DIR = os.path.join(os.getenv("MY_NEBULA_ROOTDIR", os.path.dirname(os.path.abspath(__file__))), "data/ldbc/sf0.3/CsvBasic-LongDateFormatter")

def read_creation_dates(csv_path):
    """Return sorted list of creationDate values (int, ms) from a pipe-delimited CSV."""
    dates = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f, delimiter="|")
        for row in reader:
            dates.append(int(row["creationDate"]))
    dates.sort()
    return dates


def ms_to_date_str(ms):
    """Convert millisecond timestamp to YYYY-MM-DD string (UTC)."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def date_str_to_ms(date_str):
    """Convert YYYY-MM-DD string to millisecond timestamp (start of day, UTC)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def selectivity_end_date(sorted_dates, pct, start_ms=None):
    """Find end_date so that rows in [start_date, end_date] = pct% of total rows.

    Returns (end_date_ms, rows_in_range) or (None, None) if the target
    selectivity is unreachable (not enough rows after start_date).
    """
    total = len(sorted_dates)
    target_count = math.floor(pct / 100 * total)
    if target_count == 0:
        target_count = 1

    if start_ms is not None:
        start_idx = bisect.bisect_left(sorted_dates, start_ms)
    else:
        start_idx = 0

    available = total - start_idx
    if available < target_count:
        return None, None

    end_idx = start_idx + target_count - 1
    return sorted_dates[end_idx], target_count


def main():
    parser = argparse.ArgumentParser(
        description="Compute date thresholds for target selectivities on LDBC CSV data."
    )
    parser.add_argument("dataset_dir", help="Path to LDBC dataset directory", nargs="?", default=DATA_DIR)
    parser.add_argument(
        "--pct",
        type=float,
        nargs="+",
        default=[0.1, 1, 10],
        help="Target selectivity percentages (default: 0.1 1 10)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Fixed start date YYYY-MM-DD (default: min creationDate per entity)",
    )
    args = parser.parse_args()

    dataset_dir = args.dataset_dir
    start_ms = date_str_to_ms(args.start) if args.start else None

    pcts = args.pct

    # Column widths
    label_w = 10
    rows_w = 10
    min_w = 12
    max_w = 12
    # Each pct column: "end_date (count)" — estimate width
    pct_w = 22

    # Header
    print(f"Dataset: {os.path.abspath(dataset_dir)}")
    print()
    print("Range filter:  creationDate >= start_date AND creationDate <= end_date")
    if args.start:
        print(f"start_date = {args.start} (fixed)")
    else:
        print("start_date = min(creationDate) for each entity")
    print("Selectivity = rows_in_range / total_rows")
    print()
    header = f"{'Entity':<{label_w}} {'Rows':>{rows_w}} {'Min':>{min_w}} {'Max':>{max_w}}"
    for p in pcts:
        header += f"  {f'{p}% end_date':>{pct_w}}"
    print(header)

    for label, relpath in ENTITIES:
        csv_path = os.path.join(dataset_dir, relpath)
        if not os.path.isfile(csv_path):
            print(f"{label:<{label_w}} (file not found: {relpath})")
            continue

        dates = read_creation_dates(csv_path)
        n = len(dates)
        min_date = ms_to_date_str(dates[0])
        max_date = ms_to_date_str(dates[-1])
        row = f"{label:<{label_w}} {n:>{rows_w},} {min_date:>{min_w}} {max_date:>{max_w}}"

        for p in pcts:
            date_ms, count = selectivity_end_date(dates, p, start_ms)
            if date_ms is None:
                row += f"  {'—':>{pct_w}}"
            else:
                date_str = ms_to_date_str(date_ms)
                cell = f"{date_str} ({count:,})"
                row += f"  {cell:>{pct_w}}"

        print(row)


if __name__ == "__main__":
    main()
