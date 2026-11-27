"""Shared parsing and conversion utilities."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def parse_int_safe(val: Any, default: int = 0) -> int:
    """Safely parse integer, handling prefixed IDs like 'person_933' -> 933."""
    if val is None or val == "":
        return default
    try:
        if isinstance(val, str) and "_" in val:
            val = val.split("_")[-1]
        return int(val)
    except (ValueError, TypeError):
        return default


def parse_float_safe(val: Any, default: float = 0.0) -> float:
    """Safely parse float from various types."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def normalize_list(val: Any) -> list[str]:
    """Normalize various list formats to list[str].

    Handles: list, bare string, JSON-encoded list string, None, empty string.
    """
    if val is None or val == "":
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        if val.startswith("["):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except (json.JSONDecodeError, ValueError):
                pass
        return [val]
    return [str(val)]


def ms_to_iso8601(ms: int | str) -> str:
    """Convert milliseconds-since-epoch to ISO8601 string.

    Used for Dgraph which expects ISO8601 date parameters.
    """
    ims = int(str(ms).strip())
    base = datetime.fromtimestamp(ims // 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # Keep sub-second precision when there is any: dropping it moved an exact message timestamp
    # (e.g. 1347528477814) to the whole second and changed which messages a `lt` bound admits.
    # Whole-second values keep the old form, so every day-aligned LDBC date is byte-identical.
    return f"{base}.{ims % 1000:03d}Z" if ims % 1000 else f"{base}Z"


def iso8601_to_ms(iso_str: str) -> int:
    """Convert ISO8601 string to milliseconds-since-epoch.

    Returns 0 on empty/invalid input.
    """
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


# Alias for clarity when working with Dgraph results.
parse_dgraph_datetime = iso8601_to_ms
