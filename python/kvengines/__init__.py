"""kvengines: clients, the KV-counter profiler and the LDBC runners for the real databases (Dgraph, NebulaGraph, ArangoDB,
TuGraph).

The names below load on first use (`from kvengines import connect`), so a module of this package that needs no
database client (kvengines.complex_kv_reads: a figure's cells and files; kvengines.live_workloads) imports without the
clients' packages, which only the engines extra installs (`just setup`)."""

__version__ = "0.1.0"

_HOME = {"DatabaseClient": "kvengines.client", "QueryResult": "kvengines.client",
         "ArangoDBClient": "kvengines.clients", "DgraphClient": "kvengines.clients", "NebulaClient": "kvengines.clients",
         "TuGraphClient": "kvengines.clients",
         "connect": "kvengines.clients",
         "KVProfiler": "kvengines.kv", "decode_arango_key": "kvengines.kv", "decode_badger_key": "kvengines.kv",
         "decode_nebula_key": "kvengines.kv"}
__all__ = sorted(_HOME)


def __getattr__(name):
    if name not in _HOME:
        raise AttributeError(f"module 'kvengines' has no attribute {name!r}")
    import importlib
    value = getattr(importlib.import_module(_HOME[name]), name)
    globals()[name] = value
    return value
