"""LDBC result parsers.

Each parser class converts a database's raw response into the shared
LDBC schema dataclasses defined in ``kvengines.ldbc.schemas``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class LDBCParser(ABC):
    """Abstract base for LDBC result parsers.

    Subclasses implement ``parse()`` which dispatches to per-query methods
    named ``_parse_ic1``, ``_parse_is3``, etc.
    """

    @abstractmethod
    def parse(self, query_type: str, query_num: int, raw: Any) -> list:
        """Parse a raw database response into LDBC result dataclasses.

        Args:
            query_type: "ic" or "is".
            query_num: Query number (1-14 for ic, 1-7 for is).
            raw: Raw response from the database client.

        Returns:
            List of result dataclass instances (or a single instance wrapped
            in a list for queries that return one row).
        """

    def _dispatch(self, query_type: str, query_num: int, raw: Any) -> list:
        """Look up and call the per-query parser method."""
        method_name = f"_parse_{query_type}{query_num}"
        method = getattr(self, method_name, None)
        if method is None:
            raise ValueError(
                f"{self.__class__.__name__} has no parser for {query_type}{query_num}"
            )
        result = method(raw)
        # Wrap single results in a list for uniform interface
        if not isinstance(result, list):
            result = [result]
        return result
