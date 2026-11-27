#!/usr/bin/env python3
"""
Parse and beautify QUERY_PLAN_DEBUG output from NebulaGraph storaged logs.

Extracts PLAN_DEBUG log lines from storaged log files and groups them
into readable blocks by storage request. Handles interleaved output from
concurrent threads by tracking thread IDs in glog prefixes.

Usage:
  # Parse all PLAN_DEBUG blocks from a log file
  python -m kvengines.nebula.parse_storage_plan_log runs/storaged/logs/storaged-stderr.log

  # Show only the last block
  python -m kvengines.nebula.parse_storage_plan_log runs/storaged/logs/storaged-stderr.log --last

  # Show the last N blocks
  python -m kvengines.nebula.parse_storage_plan_log runs/storaged/logs/storaged-stderr.log --last-n 3

  # Filter by processor type
  python -m kvengines.nebula.parse_storage_plan_log runs/storaged/logs/storaged-stderr.log --filter GetNeighbors

  # Read from stdin (for tail -f piping)
  tail -f runs/storaged/logs/storaged-stderr.log | python -m kvengines.nebula.parse_storage_plan_log --stdin

  # Output as JSON
  python -m kvengines.nebula.parse_storage_plan_log runs/storaged/logs/storaged-stderr.log --json
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


# glog line: I20260303 21:16:45.817585 1412348 LookupProcessor.cpp:76] PLAN_DEBUG: ...
GLOG_RE = re.compile(
    r"^[IWEF](\d{8} \d{2}:\d{2}:\d{2}\.\d+)\s+(\d+)\s+\S+\]\s+(.*)"
)
PLAN_DEBUG_RE = re.compile(r"^PLAN_DEBUG:\s?(.*)")

# Block boundaries
BLOCK_START_RE = re.compile(
    r"=== (GetNeighbors|GetProp|GetDstBySrc|ScanVertex|ScanEdge"
    r"|Storage Execution Plan) (Request |===)"
)
BLOCK_END_RE = re.compile(
    r"=== (GetNeighbors|GetProp|GetDstBySrc|ScanVertex|ScanEdge|End Plan) "
    r"(Completed|===)"
)
SECTION_RE = re.compile(r"^---\s+.+\s+---$")


@dataclass
class PlanBlock:
    """A group of related PLAN_DEBUG lines from one storage request."""
    timestamp: str = ""
    thread_id: str = ""
    processor: str = ""
    lines: list = field(default_factory=list)

    def is_empty(self):
        return len(self.lines) == 0


def parse_log_lines(raw_lines, filter_type=None):
    """Parse raw log lines into PlanBlocks, grouping by thread.

    Each block spans from a request/plan header to its corresponding
    completed/end marker. Interleaved threads are separated by tracking
    glog thread IDs.
    """
    open_blocks: dict[str, PlanBlock] = {}  # thread_id -> current block
    completed: list[PlanBlock] = []
    last_thread = None

    for raw in raw_lines:
        raw = raw.rstrip()
        if not raw:
            continue

        # Type 1: glog-prefixed line
        m = GLOG_RE.match(raw)
        if m:
            timestamp, thread_id, payload = m.group(1), m.group(2), m.group(3)
            last_thread = thread_id
            pm = PLAN_DEBUG_RE.match(payload)
            if pm:
                _add_content(pm.group(1), thread_id, timestamp,
                             open_blocks, completed)
            continue

        # Type 2: PLAN_DEBUG: continuation (from multi-line oss, no glog prefix)
        m = PLAN_DEBUG_RE.match(raw)
        if m and last_thread:
            _add_content(m.group(1), last_thread, "",
                         open_blocks, completed)
            continue

        # Type 3: Bare continuation (indented text from oss without PLAN_DEBUG
        # prefix, e.g. "  edgeProps=2 types  hasFilter=true")
        if last_thread and last_thread in open_blocks and raw and not raw[0:1].isalpha():
            block = open_blocks[last_thread]
            if block.lines:
                block.lines[-1] += " " + raw.strip()
            continue

    # Flush remaining open blocks (incomplete requests)
    for block in open_blocks.values():
        if not block.is_empty():
            completed.append(block)

    if filter_type:
        ft = filter_type.lower()
        completed = [b for b in completed if ft in b.processor.lower()]

    return completed


def _add_content(content, thread_id, timestamp, open_blocks, completed):
    """Route a single PLAN_DEBUG content string into the right block."""
    # Block start?
    m = BLOCK_START_RE.search(content)
    if m:
        proc = m.group(1)
        if proc == "Storage Execution Plan":
            proc = "Lookup"
        # Close any prior block on this thread
        if thread_id in open_blocks and not open_blocks[thread_id].is_empty():
            completed.append(open_blocks[thread_id])
        open_blocks[thread_id] = PlanBlock(
            timestamp=timestamp or "",
            thread_id=thread_id,
            processor=proc,
        )
        # Some headers have inline params after "===", e.g.
        # "=== GetProp Request === spaceId=1 partitions=55 entities=189"
        parts = content.split("===")
        if len(parts) >= 3:
            params = parts[-1].strip()
            if params:
                open_blocks[thread_id].lines.append(params)
        return

    # Block end?
    m = BLOCK_END_RE.search(content)
    if m:
        if thread_id in open_blocks:
            parts = content.split("===")
            if len(parts) >= 3:
                result = parts[-1].strip()
                if result:
                    open_blocks[thread_id].lines.append(result)
            completed.append(open_blocks.pop(thread_id))
        return

    # Regular content — append to open block for this thread
    if thread_id not in open_blocks:
        open_blocks[thread_id] = PlanBlock(
            timestamp=timestamp or "",
            thread_id=thread_id,
            processor="Unknown",
        )
    open_blocks[thread_id].lines.append(content)


# ── Formatting ──────────────────────────────────────────────────────────

def format_block(block: PlanBlock) -> str:
    """Format a PlanBlock for terminal display with ANSI colors."""
    parts = []
    parts.append(f"\033[36m{'─' * 60}\033[0m")
    parts.append(f"\033[1;33m[{block.timestamp}] {block.processor}\033[0m")
    for line in block.lines:
        if SECTION_RE.match(line):
            parts.append(f"  \033[36m{line}\033[0m")
        elif line.startswith("resultRows="):
            parts.append(f"  \033[32m{line}\033[0m")
        else:
            parts.append(f"  {line}")
    return "\n".join(parts)


def format_block_plain(block: PlanBlock) -> str:
    """Format a PlanBlock without ANSI colors."""
    parts = []
    parts.append("─" * 60)
    parts.append(f"[{block.timestamp}] {block.processor}")
    for line in block.lines:
        parts.append(f"  {line}")
    return "\n".join(parts)


def blocks_to_json(blocks: list[PlanBlock]) -> str:
    """Convert blocks to JSON format."""
    return json.dumps(
        [
            {
                "timestamp": b.timestamp,
                "thread_id": b.thread_id,
                "processor": b.processor,
                "lines": b.lines,
            }
            for b in blocks
        ],
        indent=2,
    )


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Parse QUERY_PLAN_DEBUG output from NebulaGraph storaged logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("logfile", nargs="?", help="Path to storaged log file")
    parser.add_argument("--stdin", action="store_true",
                        help="Read from stdin (for piping from tail -f)")
    parser.add_argument("--last", action="store_true",
                        help="Show only the last request block")
    parser.add_argument("--last-n", type=int, metavar="N",
                        help="Show the last N request blocks")
    parser.add_argument("--filter", metavar="TYPE",
                        help="Filter by processor type (e.g., GetNeighbors, Lookup)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color output")

    args = parser.parse_args()

    if args.stdin:
        lines = sys.stdin
    elif args.logfile:
        path = Path(args.logfile)
        if not path.exists():
            print(f"Error: {path} not found", file=sys.stderr)
            sys.exit(1)
        lines = path.read_text().splitlines()
    else:
        parser.print_help()
        sys.exit(1)

    # Pre-filter: keep PLAN_DEBUG lines and bare continuations that
    # immediately follow them (lines starting with whitespace/non-alpha).
    relevant = []
    prev_was_plan = False
    for line in lines:
        if "PLAN_DEBUG" in line:
            relevant.append(line)
            prev_was_plan = True
        elif prev_was_plan and line and not line[0:1].isalpha():
            relevant.append(line)
        else:
            prev_was_plan = False

    blocks = parse_log_lines(relevant, filter_type=args.filter)

    if not blocks:
        print("No PLAN_DEBUG output found.", file=sys.stderr)
        sys.exit(0)

    if args.last:
        blocks = blocks[-1:]
    elif args.last_n:
        blocks = blocks[-args.last_n:]

    if args.json:
        print(blocks_to_json(blocks))
    else:
        formatter = format_block_plain if args.no_color else format_block
        for block in blocks:
            print(formatter(block))
        print()
        print(f"Total blocks: {len(blocks)}")


if __name__ == "__main__":
    main()
