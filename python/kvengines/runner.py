"""General-purpose query runner with result-saving integration."""

from __future__ import annotations

from kvengines.client import DatabaseClient, QueryResult
from kvengines.results import ResultStore


class QueryRunner:
    """Runs queries through a client with optional result saving.

    Args:
        client: Database client to execute queries against.
        result_store: Optional result store for persisting results.
    """

    def __init__(
        self,
        client: DatabaseClient,
        result_store: ResultStore | None = None,
    ) -> None:
        self.client = client
        self.result_store = result_store

    def run(
        self,
        query: str,
        params: dict | None = None,
        *,
        save_as: tuple[str, str, str, int] | None = None,
        extra_metadata: dict | None = None,
    ) -> QueryResult:
        """Execute a single query.

        Args:
            query: Query text.
            params: Query parameters.
            save_as: If provided, (db, sf, query_id, param_idx) for saving results.
            extra_metadata: Additional metadata to save alongside results.

        Returns:
            QueryResult from the database client.
        """
        result = self.client.execute(query, params)

        if self.result_store and save_as:
            db, sf, query_id, param_idx = save_as
            self.result_store.save(
                db=db,
                sf=sf,
                query_id=query_id,
                param_idx=param_idx,
                raw=result.raw,
                latency_ms=result.latency_ms,
                metadata=extra_metadata,
            )

        return result
