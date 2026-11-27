#!/usr/bin/env python3
"""Apply an LDBC ArangoDB schema (database + collections + edge collections + indexes).

The schema is DATA (a JSON description); this script is the shared applier -- the arango analogue
of dgraph's ldbcTypes.schema + schema-set.sh, or nebula's schema.gql + importer. Pass --desc PATH
to apply a specific description (e.g. an experiment's frozen schema); the default is the canonical
engines/arangodb/data/ldbc/ldbc_schema.json.

Description format (JSON):
  { "vertex_collections": [name, ...],
    "edge_definitions":   [{"edge_collection": name, "from": [...], "to": [...]}, ...],
    "indexes":            [[collection, [field, ...]], ...] }   # persistent indexes
The from/to on each edge are only needed for an optional named graph (see below); the loader
imports into the edge collections directly, so they are informational for the plain load.
"""

import argparse
import json
from pathlib import Path

from kvevaluator.paths import ROOT

from arango import ArangoClient

HOST = "http://localhost:8529"
# The canonical description lives with the other schema DATA, in <arango>/data/ldbc/ (alongside
# dgraph's data/ldbcTypes.schema and nebula's data/ldbc/schema.gql).
DEFAULT_DESC = ROOT / "engines" / "arangodb" / "data" / "ldbc" / "ldbc_schema.json"


def main():
    ap = argparse.ArgumentParser(description="Apply an LDBC arango schema from a JSON description.")
    ap.add_argument("--desc", default=str(DEFAULT_DESC),
                    help="JSON schema description (default: data/ldbc/ldbc_schema.json)")
    args = ap.parse_args()

    with open(args.desc) as f:
        desc = json.load(f)
    vertex_collections = desc["vertex_collections"]
    edge_definitions = desc["edge_definitions"]
    indexes = desc["indexes"]

    client = ArangoClient(hosts=HOST)
    sys_db = client.db("_system", username="root", password="")

    if sys_db.has_database("ldbc"):
        sys_db.delete_database("ldbc")
        print("Dropped existing database: ldbc")
    sys_db.create_database("ldbc")
    print("Created database: ldbc")

    db = client.db("ldbc", username="root", password="")

    for name in vertex_collections:
        if not db.has_collection(name):
            db.create_collection(name)
            print(f"  Created vertex collection: {name}")

    for name in {e["edge_collection"] for e in edge_definitions}:
        if not db.has_collection(name):
            db.create_collection(name, edge=True)
            print(f"  Created edge collection: {name}")

    # Named graph: disabled on purpose. All LDBC queries traverse specific edge collections
    # (e.g. `FOR v IN 1..1 ANY person knows`), never `GRAPH 'ldbc'`. A named graph adds insert-time
    # validation overhead and makes traversals scan all 15 edge collections per hop. The from/to in
    # the description exist so a named graph COULD be built here if a query ever needs it.

    for collection_name, fields in indexes:
        db.collection(collection_name).add_index({"type": "persistent", "fields": fields})
        print(f"  Index: {collection_name}({', '.join(fields)})")

    print(f"Schema creation complete (from {args.desc}).")


if __name__ == "__main__":
    main()
