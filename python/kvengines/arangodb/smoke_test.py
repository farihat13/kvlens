#!/usr/bin/env python3
"""Smoke test: create collections, insert data, run AQL queries."""

from arango import ArangoClient
import time

def main():
    HOST = "http://localhost:8529"

    client = ArangoClient(hosts=HOST)
    sys_db = client.db("_system", username="root", password="")

    # --- Create database ---
    if sys_db.has_database("testdb"):
        sys_db.delete_database("testdb")
        time.sleep(5)
        print("Deleted existing database: testdb")
    if not sys_db.has_database("testdb"):
        sys_db.create_database("testdb")
        print("Created database: testdb")

    db = client.db("testdb", username="root", password="")

    # --- Create vertex collections ---
    for name in ["Person", "City"]:
        if not db.has_collection(name):
            db.create_collection(name)
            print(f"Created collection: {name}")

    # --- Create edge collection ---
    if not db.has_collection("lives_in"):
        db.create_collection("lives_in", edge=True)
        print("Created edge collection: lives_in")

    # --- Insert vertices ---
    person = db.collection("Person")
    person.truncate() # Clear existing data
    person.insert_many([
        {"_key": "1", "name": "Alice", "age": 30},
        {"_key": "2", "name": "Bob", "age": 25},
        {"_key": "3", "name": "Charlie", "age": 35},
    ])
    print("Inserted 3 persons")

    city = db.collection("City")
    city.truncate() # Clear existing data
    city.insert_many([
        {"_key": "nyc", "name": "New York"},
        {"_key": "sf", "name": "San Francisco"},
    ])
    print("Inserted 2 cities")

    # --- Insert edges ---
    lives_in = db.collection("lives_in")
    lives_in.truncate() # Clear existing data
    lives_in.insert_many([
        {"_from": "Person/1", "_to": "City/nyc"},
        {"_from": "Person/2", "_to": "City/sf"},
        {"_from": "Person/3", "_to": "City/nyc"},
    ])
    print("Inserted 3 edges")

    # --- Create named graph ---
    if not db.has_graph("social"):
        db.create_graph("social", edge_definitions=[{
            "edge_collection": "lives_in",
            "from_vertex_collections": ["Person"],
            "to_vertex_collections": ["City"],
        }])
        print("Created graph: social")

    # --- Queries ---
    print("\n=== Query 1: All persons ===")
    cursor = db.aql.execute("FOR p IN Person SORT p.name RETURN p")
    for doc in cursor:
        print(f"  {doc['name']} (age {doc['age']})")

    print("\n=== Query 2: Persons in New York (graph traversal) ===")
    cursor = db.aql.execute("""
        FOR v, e IN 1..1 INBOUND 'City/nyc' lives_in
        RETURN v.name
    """)
    for doc in cursor:
        print(f"  {doc}")

    print("\n=== Query 3: Where does each person live? (join) ===")
    cursor = db.aql.execute("""
        FOR p IN Person
            FOR c IN 1..1 OUTBOUND p lives_in
            SORT p.name
            RETURN {person: p.name, city: c.name}
    """)
    for doc in cursor:
        print(f"  {doc['person']} -> {doc['city']}")

    print("\n=== Query 4: Count persons per city ===")
    cursor = db.aql.execute("""
        FOR p IN Person
            FOR c IN 1..1 OUTBOUND p lives_in
            COLLECT city = c.name WITH COUNT INTO cnt
            SORT cnt DESC
            RETURN {city, count: cnt}
    """)
    for doc in cursor:
        print(f"  {doc['city']}: {doc['count']}")

    # --- Cleanup ---
    # sys_db.delete_database("testdb")
    # print("\nDropped database: testdb")
    # print("Smoke test passed!")


if __name__ == "__main__":
    main()
