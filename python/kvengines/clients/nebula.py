"""NebulaGraph database client (nebula3-python / Thrift)."""

from __future__ import annotations

import time
from typing import Any

from kvengines.client import DatabaseClient, QueryResult

# Lazy imports — nebula3-python is an optional dependency.
# Importing at module level would break users who only use DgraphClient.
_nebula_imported = False
_ConnectionPool = None
_Config = None
_CommonTtypes = None


def _ensure_nebula() -> None:
    global _nebula_imported, _ConnectionPool, _Config, _CommonTtypes
    if _nebula_imported:
        return
    from nebula3.gclient.net import ConnectionPool
    from nebula3.Config import Config
    from nebula3.common import ttypes as CommonTtypes

    _ConnectionPool = ConnectionPool
    _Config = Config
    _CommonTtypes = CommonTtypes
    _nebula_imported = True


def to_value(pyval: Any) -> Any:
    """Convert a Python value to a NebulaGraph Thrift Value type."""
    _ensure_nebula()
    v = _CommonTtypes.Value()
    if isinstance(pyval, bool):
        v.set_bVal(pyval)
    elif isinstance(pyval, int):
        v.set_iVal(pyval)
    elif isinstance(pyval, float):
        v.set_fVal(pyval)
    elif isinstance(pyval, str):
        v.set_sVal(pyval.encode("utf-8"))
    elif isinstance(pyval, list):
        nlist = _CommonTtypes.NList()
        nlist.values = [to_value(x) for x in pyval]
        v.set_lVal(nlist)
    elif isinstance(pyval, dict):
        nmap = _CommonTtypes.NMap()
        nmap.kvs = {k.encode("utf-8"): to_value(vv) for k, vv in pyval.items()}
        v.set_mVal(nmap)
    else:
        raise ValueError(f"Unsupported param type: {type(pyval)}")
    return v


class NebulaClient(DatabaseClient):
    """Client for NebulaGraph via nebula3-python (Thrift binary protocol).

    Args:
        host: graphd hostname.
        port: graphd port.
        user: Username.
        password: Password.
        space: Graph space to USE on connect.
        timeout: Client timeout in milliseconds.
    """

    name = "nebula"

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9669,
        user: str = "root",
        password: str = "nebula",
        space: str = "ldbc",
        timeout: int = 600_000,
    ) -> None:
        _ensure_nebula()
        cfg = _Config()
        cfg.timeout = timeout

        self.pool = _ConnectionPool()
        if not self.pool.init([(host, port)], cfg):
            raise ConnectionError(f"Cannot connect to NebulaGraph at {host}:{port}")

        self.session = self.pool.get_session(user, password)
        if space:
            resp = self.session.execute(f"USE {space};")
            if not resp.is_succeeded():
                raise RuntimeError(f"Failed to switch to space '{space}': {resp.error_msg()}")

    def execute(self, query: str, params: dict | None = None) -> QueryResult:
        """Execute an nGQL query with optional parameters.

        Parameters are converted to NebulaGraph Thrift Value types automatically.
        """
        t0 = time.perf_counter()
        if params:
            thrift_params = {k: to_value(v) for k, v in params.items()}
            resp = self.session.execute_parameter(query, thrift_params)
        else:
            resp = self.session.execute(query)
        latency_ms = (time.perf_counter() - t0) * 1000

        if not resp.is_succeeded():
            raise RuntimeError(f"Query failed: {resp.error_msg()}")

        metadata: dict[str, Any] = {}
        if resp.latency() is not None:
            metadata["server_latency_us"] = resp.latency()
        if resp.plan_desc() is not None:
            metadata["plan_desc"] = resp.plan_desc()

        return QueryResult(raw=resp, latency_ms=latency_ms, metadata=metadata)

    def health_check(self) -> bool:
        try:
            resp = self.session.execute("YIELD 1;")
            return resp.is_succeeded()
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.session.release()
        except Exception:
            pass
        try:
            del self.pool
        except Exception:
            pass
