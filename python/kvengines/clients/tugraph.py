"""TuGraph database client (its REST server, HTTP/JSON): log in once, then Cypher over POST /cypher."""

from __future__ import annotations

import time
from typing import Any

import requests

from kvengines.client import DatabaseClient, QueryResult


class TuGraphClient(DatabaseClient):
    """Client for TuGraph via its REST server (`POST /login`, then `POST /cypher` with the token).

    The answer is TuGraph's JSON as returned: {"elapsed", "header": [{"name", "type"}], "result": [[...]], "size"};
    an error comes back as {"error_message": ...} and is raised. Parameters go in the body's "parameters", their keys
    carrying the `$` as the query text names them (added here when missing), as Dgraph's client does.

    Args:
        host: Server hostname.
        port: REST port.
        graph: The graph (TuGraph's name for a database) the queries run on.
        user: Login user.
        password: Login password (TuGraph's built-in admin's default).
        timeout: Request timeout in seconds.
    """

    name = "tugraph"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7070,
        graph: str = "default",
        user: str = "admin",
        password: str = "73@TuGraph",
        timeout: int = 600,
    ) -> None:
        self.base_url = f"http://{host}:{port}"
        self.graph = graph
        self.timeout = timeout
        self._session = requests.Session()
        try:
            resp = self._session.post(f"{self.base_url}/login", json={"user": user, "password": password},
                                      timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise ConnectionError(f"Cannot connect to TuGraph at {self.base_url}: {exc}") from exc
        self._session.headers.update({"Content-Type": "application/json", "Accept": "application/json",
                                      "Authorization": f"Bearer {resp.json()['jwt']}"})

    def execute(self, query: str, params: dict | None = None) -> QueryResult:
        """Execute a Cypher query with optional parameters."""
        body: dict[str, Any] = {"graph": self.graph, "script": query}
        if params:
            body["parameters"] = {(k if k.startswith("$") else f"${k}"): v for k, v in params.items()}
        t0 = time.perf_counter()
        resp = self._session.post(f"{self.base_url}/cypher", json=body, timeout=self.timeout)
        latency_ms = (time.perf_counter() - t0) * 1000
        data = resp.json() if resp.content else {}
        if not resp.ok or "error_message" in data:
            raise RuntimeError(f"Query failed ({resp.status_code}): {data.get('error_message', resp.text[:500])}")
        return QueryResult(raw={"header": data.get("header", []), "result": data.get("result", [])},
                           latency_ms=latency_ms, metadata={"server_elapsed_s": data.get("elapsed")})

    def health_check(self) -> bool:
        try:
            self.execute("RETURN 1")
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
