#!/usr/bin/env python3
"""Update Person documents with emails and languages arrays.

LDBC stores these as separate CSVs (one row per value). This script groups
them by person and updates the Person documents with array fields.

Usage: python -m kvengines.arangodb.ldbc.update_emails_languages <data_dir>
  data_dir should contain dynamic/person_email_emailaddress_0_0.csv
  and dynamic/person_speaks_language_0_0.csv
"""

import csv
import sys
from collections import defaultdict
from pathlib import Path

from arango import ArangoClient

HOST = "http://localhost:8529"
DB_NAME = "ldbc"


def load_multi_value(csv_path):
    """Load a multi-value CSV into {key: [values]} dict."""
    groups = defaultdict(list)
    with open(csv_path, "r") as f:
        reader = csv.reader(f, delimiter="|")
        next(reader)  # skip header
        for row in reader:
            groups[row[0]].append(row[1])
    return groups


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <data_dir>")
        sys.exit(1)

    data_dir = Path(sys.argv[1])

    client = ArangoClient(hosts=HOST)
    db = client.db(DB_NAME, username="root", password="")
    person = db.collection("Person")

    # TODO: batch updates via AQL FOR/UPDATE for better performance at large scale factors
    # Update emails
    email_path = data_dir / "dynamic/person_email_emailaddress_0_0.csv"
    if email_path.exists():
        emails = load_multi_value(email_path)
        count = 0
        for person_id, email_list in emails.items():
            person.update({"_key": str(person_id), "emails": email_list})
            count += 1
        print(f"Updated emails for {count} persons")
    else:
        print(f"WARNING: {email_path} not found, skipping emails")

    # Update languages
    lang_path = data_dir / "dynamic/person_speaks_language_0_0.csv"
    if lang_path.exists():
        languages = load_multi_value(lang_path)
        count = 0
        for person_id, lang_list in languages.items():
            person.update({"_key": str(person_id), "languages": lang_list})
            count += 1
        print(f"Updated languages for {count} persons")
    else:
        print(f"WARNING: {lang_path} not found, skipping languages")


if __name__ == "__main__":
    main()
