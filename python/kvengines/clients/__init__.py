"""Concrete database client implementations."""

from kvengines.clients.arangodb import ArangoDBClient
from kvengines.clients.dgraph import DgraphClient
from kvengines.clients.nebula import NebulaClient
from kvengines.clients.tugraph import TuGraphClient


def connect(db, host=None, port=None, space=None, user=None, password=None, **kwargs):
    """Build the client for `db`: "dgraph" | "nebula" | "arangodb" | "tugraph".

    Forwards only the arguments the caller actually set, so each client's own
    canonical defaults apply (dgraph localhost:8080; nebula 127.0.0.1:9669,
    root/nebula, space "ldbc"; arangodb localhost:8529, root/"", db "ldbc"; tugraph 127.0.0.1:7070,
    admin/its default password, graph "default") and
    connect("dgraph") behaves exactly like DgraphClient().

    space: the Nebula space / ArangoDB database / TuGraph graph name (not a dgraph concept).
    Extra kwargs pass through to the client constructor (timeout,
    request_timeout, ...).
    """
    if db not in _CLIENTS:
        raise ValueError(f"unknown db {db!r}: expected {', '.join(map(repr, _CLIENTS))}")
    cls, names = _CLIENTS[db]
    given = {"host": host, "port": port, "space": space, "user": user, "password": password}
    for arg, value in given.items():
        if value is None:
            continue
        if arg not in names:
            raise TypeError(f"{cls.__name__} takes no {arg}")
        kwargs[names[arg]] = value
    return cls(**kwargs)


# each client and the names its constructor gives connect's arguments
_ADDRESS = {"host": "host", "port": "port"}
_CLIENTS = {
    "dgraph": (DgraphClient, _ADDRESS),
    "nebula": (NebulaClient, {**_ADDRESS, "space": "space", "user": "user", "password": "password"}),
    "arangodb": (ArangoDBClient, {**_ADDRESS, "space": "db_name", "user": "username", "password": "password"}),
    "tugraph": (TuGraphClient, {**_ADDRESS, "space": "graph", "user": "user", "password": "password"}),
}
