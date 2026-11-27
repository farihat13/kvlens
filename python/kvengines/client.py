"""Abstract database client interface and query result container."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    """Result from a database query execution."""

    raw: Any
    """Untouched response from the database (dict for Dgraph, ResultSet for Nebula)."""

    latency_ms: float
    """Wall-clock query latency in milliseconds."""

    metadata: dict = field(default_factory=dict)
    """Optional extras: query plan, trace info, extensions, etc."""


class DatabaseClient(ABC):
    """Abstract interface for graph database clients.

    Concrete implementations: DgraphClient, NebulaClient.
    Supports context manager protocol for resource cleanup.
    """

    name: str = ""
    """Database identifier (e.g. "dgraph", "nebula")."""

    @abstractmethod
    def execute(self, query: str, params: dict | None = None) -> QueryResult:
        """Execute a query with optional parameters.

        Args:
            query: Query text (DQL, nGQL, etc.)
            params: Query parameters as a dict. Key format is database-specific.

        Returns:
            QueryResult with raw response, latency, and metadata.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any held resources (connections, sessions)."""

    def health_check(self) -> bool:
        """Check if the database is reachable. Returns True if healthy."""
        return False

    def __enter__(self) -> DatabaseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
