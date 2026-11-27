"""ArangoDB database client (python-arango / HTTP)."""

from __future__ import annotations

import time
from typing import Any

from kvengines.client import DatabaseClient, QueryResult

# Lazy import — python-arango is an optional dependency.
_arango_imported = False
_ArangoClient = None


def _ensure_arango() -> None:
    global _arango_imported, _ArangoClient
    if _arango_imported:
        return
    from arango import ArangoClient
    _ArangoClient = ArangoClient
    _arango_imported = True


class ArangoDBClient(DatabaseClient):
    """Client for ArangoDB via python-arango (HTTP/JSON).

    Args:
        host: Server hostname.
        port: Server port.
        db_name: Database name.
        username: Auth username.
        password: Auth password.
    """

    name = "arangodb"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8529,
        db_name: str = "ldbc",
        username: str = "root",
        password: str = "",
        request_timeout: float | None = None,
    ) -> None:
        _ensure_arango()
        # python-arango defaults request_timeout to 60s; heavy queries under cache
        # pressure (e.g. IC6/IC9 with a tiny block cache) can run for minutes, so
        # callers can raise it. None keeps the library default.
        client_kwargs = {} if request_timeout is None else {"request_timeout": request_timeout}
        self._client = _ArangoClient(hosts=f"http://{host}:{port}", **client_kwargs)
        self._db = self._client.db(db_name, username=username, password=password)

    def execute(self, query: str, params: dict | None = None) -> QueryResult:
        """Execute an AQL query with optional bind variables."""
        t0 = time.perf_counter()
        cursor = self._db.aql.execute(query, bind_vars=params or None)
        results = list(cursor)
        latency_ms = (time.perf_counter() - t0) * 1000

        metadata: dict[str, Any] = {}
        if hasattr(cursor, "statistics"):
            stats = cursor.statistics()
            if stats:
                metadata["cursor_statistics"] = stats

        return QueryResult(raw=results, latency_ms=latency_ms, metadata=metadata)

    def health_check(self) -> bool:
        try:
            self._db.version()
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
