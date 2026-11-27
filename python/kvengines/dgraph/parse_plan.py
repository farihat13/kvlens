#!/usr/bin/env python3
"""Extract and format Dgraph query plans from alpha.log.

With the dgraph-profile patch applied and alpha run with --v 1, ProcessQuery prints each query's SubGraph tree
(DebugPrint); this module parses those lines into a readable form.

Usage:
  python3 -m kvengines.dgraph.parse_plan                       # the last plan (alpha.log under $DGRAPH_RUNSDIR)
  python3 -m kvengines.dgraph.parse_plan --last 3              # the last N plans (--all: every plan)
  python3 -m kvengines.dgraph.parse_plan path/to/alpha.log     # a given log
  python3 -m kvengines.dgraph.parse_plan --save path/to/plan.txt
  python3 -m kvengines.dgraph.parse_plan --include-internal    # also the dgraph.* queries
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path


# ── Log location ────────────────────────────────────────────────────

def find_log_path() -> Path | None:
    """Locate alpha.log from $DGRAPH_RUNSDIR."""
    runs_dir = os.environ.get("DGRAPH_RUNSDIR")
    if not runs_dir:
        return None
    p = Path(runs_dir) / "alpha.log"
    return p if p.exists() else None


# ── Plan block extraction ───────────────────────────────────────────

# Matches the glog header that starts a plan block:
#   I0227 21:04:50.227290 3518898 query.go:2894] T/DG: Request Subgraphs: len 13
_HEADER_RE = re.compile(
    r"^(I\d{4} [\d:.]+)\s+\d+\s+query\.go:\d+\]\s*T/DG:\s*Request Subgraphs:\s*len\s+(\d+)"
)

# Matches subgraph index lines:
#   I0227 21:04:50.227323 3518898 query.go:2896] T/DG: \t sub graph 0: id
_SUBGRAPH_RE = re.compile(
    r"^I\d{4} [\d:.]+\s+\d+\s+query\.go:\d+\]\s*T/DG:\s*\t\s*sub graph (\d+):\s*(.*)"
)

# Matches raw tree lines (tab-indented, starting with [ or |):
#   \t["id" Alias:"var" Func:&{eq [{...}]} SrcSz:0 ...]
#   \t|->["firstName" ...]
_TREE_LINE_RE = re.compile(r"^\t(\|.*?\[.*\]|(\[.*\]))$")


def extract_plan_blocks(text: str) -> list[dict]:
    """Find all 'Request Subgraphs' blocks in log text.

    Returns a list of dicts:
      {timestamp, num_subgraphs,
       subgraphs: [{index, attr, tree_lines: [str]}],
       raw_text}
    """
    lines = text.splitlines()
    blocks = []
    i = 0

    while i < len(lines):
        m = _HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue

        timestamp = m.group(1)
        num_subgraphs = int(m.group(2))
        raw_start = i
        i += 1

        subgraphs = []
        # Collect subgraph sections until we hit the next non-tree, non-subgraph line
        while i < len(lines):
            sm = _SUBGRAPH_RE.match(lines[i])
            if sm:
                sg_index = int(sm.group(1))
                sg_attr = sm.group(2).strip()
                i += 1
                tree_lines = []
                # Collect tree lines (tab-indented)
                while i < len(lines):
                    line = lines[i]
                    if line.startswith("\t") and (
                        _TREE_LINE_RE.match(line) or line.strip() == ""
                    ):
                        if line.strip():
                            tree_lines.append(line)
                        i += 1
                    else:
                        break
                subgraphs.append({
                    "index": sg_index,
                    "attr": sg_attr,
                    "tree_lines": tree_lines,
                })
            else:
                break

        raw_text = "\n".join(lines[raw_start:i])
        blocks.append({
            "timestamp": timestamp,
            "num_subgraphs": num_subgraphs,
            "subgraphs": subgraphs,
            "raw_text": raw_text,
        })

    return blocks


# ── Node line parsing ───────────────────────────────────────────────

# Parse a Go %v Func field: &{eq [{person false false}] false false false}
_FUNC_RE = re.compile(
    r"&\{(\w+)\s+\[\{(.*?)\}\]"
)


def parse_func(func_str: str) -> str:
    """Parse Go %v Func format into readable form.

    &{eq [{person false false}] false false false}  →  eq(person)
    &{uid [] false false false}                     →  uid()
    &{has [] false false false}                     →  has()
    <nil>                                           →  ""
    """
    if not func_str or func_str == "<nil>":
        return ""

    # Match func with [{...}] args
    m = _FUNC_RE.search(func_str)
    if m:
        func_name = m.group(1)
        args_raw = m.group(2).strip()
        if args_raw:
            first_arg = args_raw.split()[0]
            return f"{func_name}({first_arg})"
        return f"{func_name}()"

    # Match func with empty [] args (uid, has, etc.)
    m_empty = re.search(r"&\{(\w+)\s+\[\]", func_str)
    if m_empty:
        return f"{m_empty.group(1)}()"

    return func_str


# Parse a full DebugPrint node line
_NODE_RE = re.compile(
    r'^(?P<prefix>[|\-f>]*?)\["(?P<attr>[^"]*)"'
    r'\s+Alias:"(?P<alias>[^"]*)"'
    r"\s+Func:(?P<func>(?:<nil>|&\{[^}]*\}[^S]*))"
    r"\s*SrcSz:(?P<srcsz>\d+)"
    r'\s+Op:"(?P<op>[^"]*)"'
    r"\s+DestSz:(?P<destsz>\d+)"
    r"\s+IsCount:\s*(?P<iscount>\w+)"
    r"\s+ValueSz:(?P<valsz>\d+)\]"
)


def format_node(line: str) -> str:
    """Parse a DebugPrint line into human-readable form.

    Input:  \\t|->["firstName" Alias:"friendFirstName" Func:<nil> SrcSz:0 ...]
    Output: |-> firstName (alias: friendFirstName)

    Input:  \\t|-f->["dgraph.type" Alias:"" Func:&{eq [{person ...}]} SrcSz:0 ...]
    Output: |-filter-> dgraph.type [eq(person)]
    """
    stripped = line.lstrip("\t")
    m = _NODE_RE.match(stripped)
    if not m:
        # Fallback: return stripped line
        return stripped

    prefix = m.group("prefix")
    attr = m.group("attr")
    alias = m.group("alias")
    func_str = m.group("func").strip()
    srcsz = int(m.group("srcsz"))
    op = m.group("op")
    destsz = int(m.group("destsz"))
    iscount = m.group("iscount").lower() == "true"
    valsz = int(m.group("valsz"))

    # Build readable prefix: |-f-> becomes |-filter->
    readable_prefix = prefix.replace("|-f->", "|-filter->")

    # Build parts
    parts = []
    if attr:
        parts.append(attr)
    if alias:
        parts.append(f"(alias: {alias})")

    func = parse_func(func_str)
    if func:
        parts.append(f"[{func}]")

    if op:
        parts.append(f"op:{op}")
    if srcsz > 0:
        parts.append(f"src:{srcsz}")
    if destsz > 0:
        parts.append(f"dst:{destsz}")
    if iscount:
        parts.append("COUNT")
    if valsz > 0:
        parts.append(f"val:{valsz}")

    return f"{readable_prefix} {' '.join(parts)}".strip()


# ── Plan block formatting ───────────────────────────────────────────

def format_plan_block(block: dict) -> str:
    """Format an entire plan block as a readable tree."""
    out = []
    out.append(f"Request Subgraphs: {block['num_subgraphs']}")
    out.append(f"Timestamp: {block['timestamp']}")
    out.append("")

    for sg in block["subgraphs"]:
        out.append(f"--- Subgraph {sg['index']}: {sg['attr'] or '(anonymous)'} ---")
        for line in sg["tree_lines"]:
            out.append("  " + format_node(line))
        out.append("")

    return "\n".join(out)


# ── Internal query detection ────────────────────────────────────────

def is_internal_query(block: dict) -> bool:
    """Detect internal ACL/schema queries.

    Internal queries reference only dgraph.* predicates (e.g., dgraph.type,
    dgraph.graphql.schema, dgraph.xid).  User queries that use type() have
    dgraph.type as the root attr but always include non-dgraph.* children
    (the fields being selected), so we check ALL predicates in the tree.
    """
    if not block["subgraphs"]:
        return False
    for sg in block["subgraphs"]:
        attr = sg["attr"]
        if attr and not attr.startswith("dgraph."):
            return False
        # Check every tree line — if any predicate is not dgraph.*, it's a
        # user query (e.g. type(person) selecting fqid, lastName, etc.)
        for line in sg["tree_lines"]:
            m = re.search(r'\["([^"]*)"', line)
            if m:
                pred = m.group(1)
                if pred and not pred.startswith("dgraph."):
                    return False
    return True


# ── CLI ─────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Extract and format Dgraph query plans from alpha.log.",
    )
    p.add_argument("logfile", nargs="?", help="Path to alpha.log (default: auto-detect)")
    p.add_argument("--last", type=int, default=1,
                   help="Show last N plans (default: 1)")
    p.add_argument("--all", action="store_true",
                   help="Show all plans")
    p.add_argument("--save", metavar="PATH",
                   help="Save formatted output to file")
    p.add_argument("--include-internal", action="store_true",
                   help="Include internal (dgraph.*) queries")
    args = p.parse_args()

    # Find log file
    if args.logfile:
        log_path = Path(args.logfile)
    else:
        log_path = find_log_path()

    if not log_path or not log_path.exists():
        print("ERROR: alpha.log not found. Specify path or set $DGRAPH_RUNSDIR.",
              file=sys.stderr)
        sys.exit(1)

    text = log_path.read_text()
    blocks = extract_plan_blocks(text)

    if not args.include_internal:
        blocks = [b for b in blocks if not is_internal_query(b)]

    if not blocks:
        print("No plan blocks found.")
        return

    # Select which blocks to show
    if args.all:
        selected = blocks
    else:
        selected = blocks[-args.last:]

    # Format
    output_parts = []
    for i, block in enumerate(selected):
        if i > 0:
            output_parts.append("\n" + "=" * 60 + "\n")
        output_parts.append(format_plan_block(block))

    output = "\n".join(output_parts)

    if args.save:
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(output)
        print(f"Plan saved to: {save_path}")
    else:
        print(output)


if __name__ == "__main__":
    main()
