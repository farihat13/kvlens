"""Display utilities for LDBC query results."""

from __future__ import annotations

from dataclasses import fields as dc_fields


def print_parsed_results(results: list, max_col: int = 30):
    """Print LDBC result dataclasses as a formatted table."""
    print()
    if not results:
        print("  (no results)")
        return

    field_names = [f.name for f in dc_fields(results[0])]

    # Convert values to display strings
    rows = []
    for r in results:
        row = []
        for name in field_names:
            val = getattr(r, name)
            if isinstance(val, list):
                s = ", ".join(str(v) for v in val[:5])
                if len(val) > 5:
                    s += f" ...({len(val)})"
            else:
                s = str(val)
            if len(s) > max_col:
                s = s[:max_col - 3] + "..."
            row.append(s)
        rows.append(row)

    # Compute column widths
    widths = [min(max(len(h), *(len(r[i]) for r in rows)), max_col)
              for i, h in enumerate(field_names)]

    # Print
    header = "  ".join(h.ljust(w) for h, w in zip(field_names, widths))
    sep = "  ".join("-" * w for w in widths)
    print(f"  {header}")
    print(f"  {sep}")
    for row in rows:
        line = "  ".join(v.ljust(w) for v, w in zip(row, widths))
        print(f"  {line}")
    print(f"  ({len(results)} rows)")
    print()
