"""LDBC parameter file utilities and per-database param preprocessors.

The preprocessors convert raw CSV parameter dicts into the format each
database expects, handling:
- Date fields (ms timestamps → ISO8601 for Dgraph, kept as int for Nebula and TuGraph)
- ``durationDays`` → computed ``endDate``
- ID prefixing (Nebula VIDs like ``person_933``)
- Type coercion (Dgraph wants all strings; Nebula wants native types)
"""

from __future__ import annotations

import csv
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kvengines.utils import ms_to_iso8601


def read_param_file(path: Path | str) -> list[dict[str, str]]:
    """Read an LDBC parameter file (auto-detects ``|`` or ``,`` delimiter).

    Args:
        path: Path to parameter file (e.g. ``interactive_1_param.txt``).

    Returns:
        List of parameter dicts with string values.
    """
    path = Path(path)
    with open(path, newline="") as f:
        first_line = f.readline()
        delim = "|" if "|" in first_line else ","
        f.seek(0)
        reader = csv.DictReader(f, delimiter=delim)
        return list(reader)


# ── Date fields that need conversion ────────────────────────────────
_DATE_FIELDS = ("maxDate", "minDate", "startDate")


# ── Dgraph preprocessor ────────────────────────────────────────────


def dgraph_param_preprocessor(params: dict[str, str], qnum: int) -> dict[str, str]:
    """Prepare LDBC params for Dgraph.

    Transformations:
    1. Convert ``startDate``, ``maxDate``, ``minDate`` from ms → ISO8601.
    2. Compute ``endDate`` from ``startDate`` + ``durationDays`` (IC3, IC4).
    3. Remove ``durationDays``.
    4. Drop empty keys/values.
    5. All values become strings (Dgraph GraphQL JSON requirement).

    Note: ``$``-prefixing is handled by ``DgraphClient.execute()``, so
    this preprocessor does NOT add ``$`` prefixes.

    Pass to ``LDBCRunner(param_preprocessor=dgraph_param_preprocessor)``.
    """
    out: dict[str, Any] = {k: v for k, v in params.items() if k and v not in (None, "")}

    # Compute endDate before converting startDate, so we can do ms arithmetic
    if "startDate" in out and "durationDays" in out:
        start_ms = int(out["startDate"])
        duration_days = int(out["durationDays"])
        end_ms = start_ms + duration_days * 86400000
        out["endDate"] = ms_to_iso8601(end_ms)
        del out["durationDays"]

    # Convert remaining date fields from ms to ISO8601
    for key in _DATE_FIELDS:
        if key in out:
            try:
                out[key] = ms_to_iso8601(out[key])
            except (ValueError, TypeError) as e:
                warnings.warn(f"Failed to convert date field '{key}'={out[key]!r}: {e}")

    # Dgraph requires all values as strings
    return {k: str(v) for k, v in out.items()}


# ── NebulaGraph preprocessor ───────────────────────────────────────

# Map parameter names to entity-type prefixes for NebulaGraph VIDs.
PARAM_ID_PREFIXES: dict[str, str] = {
    "personId": "person",
    "person1Id": "person",
    "person2Id": "person",
    "messageId": "msg",
    "postId": "msg",
    "commentId": "msg",
    "forumId": "forum",
    "tagId": "tag",
    "placeId": "place",
    "cityId": "place",
    "countryId": "place",
}

def prefix_id_param(param_name: str, param_value: Any) -> str:
    """Add entity-type prefix to an ID parameter value.

    Example: prefix_id_param("personId", 933) -> "person_933"
    """
    if isinstance(param_value, str) and "_" in param_value:
        return param_value  # already prefixed
    prefix = PARAM_ID_PREFIXES.get(param_name)
    if prefix is None:
        return str(param_value)
    return f"{prefix}_{param_value}"

def nebula_param_preprocessor(params: dict[str, str], qnum: int) -> dict[str, Any]:
    """Prepare LDBC params for NebulaGraph.

    Transformations:
    1. Coerce numeric-looking values to ``int``.
    2. Prefix ID parameters with entity types (e.g. ``933`` → ``person_933``).
    3. Compute ``endDate`` from ``startDate`` + ``durationDays`` (as int ms).
    4. Remove ``durationDays``.

    Note: ``to_value()`` Thrift conversion is handled by
    ``NebulaClient.execute()``, so this preprocessor returns native
    Python types.

    Pass to ``LDBCRunner(param_preprocessor=nebula_param_preprocessor)``.
    """
    out = tugraph_param_preprocessor(params, qnum)       # steps 1, 3 and 4

    # Prefix ID parameters with entity types
    for key in list(out):
        if key in PARAM_ID_PREFIXES:
            out[key] = prefix_id_param(key, out[key])

    return out


# ── TuGraph preprocessor ──────────────────────────────────────────


def tugraph_param_preprocessor(params: dict[str, str], qnum: int) -> dict[str, Any]:
    """Prepare LDBC params for TuGraph (and the typed part of NebulaGraph's).

    Transformations:
    1. Coerce numeric-looking values to ``int`` (TuGraph's ``id`` is LDBC's id, an INT64; its dates are INT64 ms).
    2. Compute ``endDate`` from ``startDate`` + ``durationDays`` (as int ms).
    3. Remove ``durationDays``.

    The ``$`` on each key is added by ``TuGraphClient.execute()``.
    """
    out: dict[str, Any] = {}
    for k, v in params.items():
        if not k or v in (None, ""):
            continue
        # Try to coerce to int (most LDBC params are numeric)
        try:
            out[k] = int(v)
        except (ValueError, TypeError):
            out[k] = v

    # Compute endDate from startDate + durationDays
    if "startDate" in out and "durationDays" in out:
        out["endDate"] = int(out["startDate"]) + int(out["durationDays"]) * 86400000
        del out["durationDays"]
    return out


# ── ArangoDB preprocessor ─────────────────────────────────────────

# Params that should be coerced to int for AQL bind variables.
_ARANGO_NUMERIC_PARAMS = {
    "personId", "person1Id", "person2Id", "messageId",
    "maxDate", "startDate", "minDate", "durationDays",
    "month", "workFromYear",
}


def arangodb_param_preprocessor(params: dict[str, str], qnum: int) -> dict[str, Any]:
    """Prepare LDBC params for ArangoDB.

    Transformations:
    1. Coerce numeric-looking values to ``int``.
    2. No ID prefixing needed (AQL uses ``_key`` directly).
    3. No date conversion (AQL uses ms integers, same as param files).
    4. No ``endDate`` computation (AQL queries compute it internally from
       ``startDate`` + ``durationDays``).

    Pass to ``LDBCRunner(param_preprocessor=arangodb_param_preprocessor)``.
    """
    out: dict[str, Any] = {}
    for k, v in params.items():
        if not k or v in (None, ""):
            continue
        if k in _ARANGO_NUMERIC_PARAMS:
            try:
                out[k] = int(v)
            except (ValueError, TypeError):
                out[k] = v
        else:
            out[k] = v
    return out
