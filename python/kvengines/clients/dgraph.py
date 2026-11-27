"""Dgraph database client (HTTP/JSON)."""

from __future__ import annotations

import time
from typing import Any

import requests

from kvengines.client import DatabaseClient, QueryResult


class DgraphClient(DatabaseClient):
    """Client for Dgraph via its HTTP /query endpoint.

    Args:
        host: Dgraph Alpha hostname.
        port: Dgraph Alpha HTTP port.
        timeout: Request timeout in seconds.
    """

    name = "dgraph"

    def __init__(
        self, host: str = "localhost", port: int = 8080, timeout: int = 60
    ) -> None:
        self.base_url = f"http://{host}:{port}"
        self.query_url = f"{self.base_url}/query"
        self.timeout = timeout
        if not self.health_check():
            raise ConnectionError(f"Cannot connect to Dgraph at {self.base_url}")

    def execute(self, query: str, params: dict | None = None) -> QueryResult:
        """Execute a DQL query.

        Parameters are sent as Dgraph variables. Keys are automatically
        ``$``-prefixed if not already.
        """
        variables = {}
        if params:
            for k, v in params.items():
                key = k if k.startswith("$") else f"${k}"
                variables[key] = str(v) if not isinstance(v, str) else v

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        t0 = time.perf_counter()
        resp = requests.post(
            self.query_url,
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=self.timeout,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()

        metadata: dict[str, Any] = {}
        if "extensions" in data:
            metadata["extensions"] = data["extensions"]

        return QueryResult(raw=data.get("data", data), latency_ms=latency_ms, metadata=metadata)

    def mutate(
        self, payload: str, fmt: str = "auto", commit_now: bool = True
    ) -> QueryResult:
        """Execute a Dgraph mutation via the ``/mutate`` endpoint.

        A mutation is a structurally different request from a query: it POSTs a
        JSON mutation / upsert block (``{"set": ...}`` / ``{"query": ..., "set":
        ...}``) or RDF N-Quads, not a DQL query with ``$``-variables. The write
        microbenchmarks use this; reads use :meth:`execute`.

        Args:
            payload: the mutation body (JSON object string or RDF N-Quads).
            fmt: ``"json"``, ``"rdf"``, or ``"auto"`` (JSON if the payload starts
                with ``{``, else RDF).
            commit_now: commit in the same request (``?commitNow=true``).
        """
        if fmt == "auto":
            fmt = "json" if payload.lstrip().startswith("{") else "rdf"
        content_type = {"json": "application/json", "rdf": "application/rdf"}[fmt]
        url = f"{self.base_url}/mutate" + ("?commitNow=true" if commit_now else "")

        t0 = time.perf_counter()
        resp = requests.post(
            url,
            headers={"Content-Type": content_type},
            data=payload.encode("utf-8"),
            timeout=self.timeout,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        resp.raise_for_status()
        data = resp.json()
        # Dgraph reports mutation failures as HTTP 200 with an "errors" array.
        if "errors" in data:
            raise RuntimeError(f"Dgraph mutation failed: {data['errors']}")

        metadata: dict[str, Any] = {}
        if "extensions" in data:
            metadata["extensions"] = data["extensions"]

        return QueryResult(raw=data.get("data", data), latency_ms=latency_ms, metadata=metadata)

    def admin(self, endpoint: str, method: str = "POST", **kwargs: Any) -> dict:
        """Call a Dgraph admin/debug endpoint.

        Examples:
            client.admin("/rollup/pause")
            client.admin("/trace/reset")
            client.admin("/debug/vars", method="GET")
        """
        url = f"{self.base_url}{endpoint}"
        resp = requests.request(method, url, timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        try:
            return resp.json()
        except requests.exceptions.JSONDecodeError:
            return {"text": resp.text}

    def health_check(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=5)
            return resp.ok
        except requests.exceptions.RequestException:
            return False

    def close(self) -> None:
        pass  # HTTP is stateless
