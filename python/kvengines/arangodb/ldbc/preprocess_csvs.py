#!/usr/bin/env python3
"""Preprocess LDBC CSV files for ArangoDB import.

For vertex CSVs: renames the id column to _key.
For edge CSVs: adds _from and _to columns in Collection/_key format.

Writes to a .tmp file first, then renames to final path — so the presence
of the output file means preprocessing completed successfully. Existing
output files are skipped.

Usage: python -m kvengines.arangodb.ldbc.preprocess_csvs <input_dir> [<output_dir>]
  If output_dir is omitted, defaults to <input_dir>_arangodb.
"""

import csv
import os
import shutil
import sys
from pathlib import Path

# Vertex files: filename -> collection name
VERTEX_FILES = {
    "dynamic/person_0_0.csv": "Person",
    "dynamic/post_0_0.csv": "Post",
    "dynamic/comment_0_0.csv": "Comment",
    "dynamic/forum_0_0.csv": "Forum",
    "static/organisation_0_0.csv": "Organisation",
    "static/place_0_0.csv": "Place",
    "static/tag_0_0.csv": "Tag",
    "static/tagclass_0_0.csv": "TagClass",
}

# Edge files: filename -> (from_collection, to_collection, edge_collection)
EDGE_FILES = {
    "dynamic/person_knows_person_0_0.csv":        ("Person", "Person", "knows"),
    "dynamic/person_likes_post_0_0.csv":          ("Person", "Post", "likes"),
    "dynamic/person_likes_comment_0_0.csv":       ("Person", "Comment", "likes"),
    "dynamic/person_hasInterest_tag_0_0.csv":     ("Person", "Tag", "hasInterest"),
    "dynamic/person_studyAt_organisation_0_0.csv": ("Person", "Organisation", "studyAt"),
    "dynamic/person_workAt_organisation_0_0.csv":  ("Person", "Organisation", "workAt"),
    "dynamic/person_isLocatedIn_place_0_0.csv":   ("Person", "Place", "isLocatedIn"),
    "dynamic/post_isLocatedIn_place_0_0.csv":     ("Post", "Place", "isLocatedIn"),
    "dynamic/comment_isLocatedIn_place_0_0.csv":  ("Comment", "Place", "isLocatedIn"),
    "dynamic/post_hasCreator_person_0_0.csv":     ("Post", "Person", "hasCreator"),
    "dynamic/comment_hasCreator_person_0_0.csv":  ("Comment", "Person", "hasCreator"),
    "dynamic/comment_replyOf_post_0_0.csv":       ("Comment", "Post", "replyOf"),
    "dynamic/comment_replyOf_comment_0_0.csv":    ("Comment", "Comment", "replyOf"),
    "dynamic/forum_containerOf_post_0_0.csv":     ("Forum", "Post", "containerOf"),
    "dynamic/forum_hasMember_person_0_0.csv":     ("Forum", "Person", "hasMember"),
    "dynamic/forum_hasModerator_person_0_0.csv":  ("Forum", "Person", "hasModerator"),
    "dynamic/forum_hasTag_tag_0_0.csv":           ("Forum", "Tag", "hasTag"),
    "dynamic/comment_hasTag_tag_0_0.csv":         ("Comment", "Tag", "hasTag"),
    "dynamic/post_hasTag_tag_0_0.csv":            ("Post", "Tag", "hasTag"),
    "static/organisation_isLocatedIn_place_0_0.csv": ("Organisation", "Place", "isLocatedIn"),
    "static/place_isPartOf_place_0_0.csv":        ("Place", "Place", "isPartOf"),
    "static/tag_hasType_tagclass_0_0.csv":        ("Tag", "TagClass", "hasType"),
    "static/tagclass_isSubclassOf_tagclass_0_0.csv": ("TagClass", "TagClass", "isSubclassOf"),
}

# Skip these (multi-value attributes handled separately)
SKIP_FILES = {
    "dynamic/person_email_emailaddress_0_0.csv",
    "dynamic/person_speaks_language_0_0.csv",
}


def preprocess_vertex(input_path, output_path):
    """Rename id column to _key. Writes to .tmp then renames."""
    tmp_path = str(output_path) + ".tmp"
    with open(input_path, "r") as fin, open(tmp_path, "w", newline="") as fout:
        reader = csv.reader(fin, delimiter="|")
        writer = csv.writer(fout, delimiter="|")
        header = next(reader)
        header[0] = "_key"
        writer.writerow(header)
        for row in reader:
            writer.writerow(row)
    os.rename(tmp_path, output_path)


def preprocess_edge(input_path, output_path, from_col, to_col):
    """Add _from and _to columns with Collection/_key format. Writes to .tmp then renames."""
    tmp_path = str(output_path) + ".tmp"
    with open(input_path, "r") as fin, open(tmp_path, "w", newline="") as fout:
        reader = csv.reader(fin, delimiter="|")
        writer = csv.writer(fout, delimiter="|")
        header = next(reader)
        new_header = ["_from", "_to"] + header[2:]
        writer.writerow(new_header)
        for row in reader:
            from_val = f"{from_col}/{row[0]}"
            to_val = f"{to_col}/{row[1]}"
            new_row = [from_val, to_val] + row[2:]
            writer.writerow(new_row)
    os.rename(tmp_path, output_path)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input_dir> [<output_dir>]")
        sys.exit(1)

    input_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(str(input_dir) + "_arangodb")

    if not input_dir.is_dir():
        print(f"ERROR: Input directory not found: {input_dir}")
        sys.exit(1)

    # Create output directory structure
    os.makedirs(output_dir / "dynamic", exist_ok=True)
    os.makedirs(output_dir / "static", exist_ok=True)

    # Process vertex files
    print("=== Preprocessing vertex files ===")
    for relpath, collection in VERTEX_FILES.items():
        src = input_dir / relpath
        dst = output_dir / relpath
        if dst.exists():
            print(f"  SKIP (already done): {collection}")
            continue
        if not src.exists():
            print(f"  SKIP (not found): {relpath}")
            continue
        preprocess_vertex(src, dst)
        n = sum(1 for _ in open(dst)) - 1
        print(f"  {collection}: {n} rows -> {dst}")

    # Process edge files
    print("=== Preprocessing edge files ===")
    for relpath, (from_col, to_col, edge_col) in EDGE_FILES.items():
        src = input_dir / relpath
        dst = output_dir / relpath
        if dst.exists():
            print(f"  SKIP (already done): {edge_col} ({from_col}->{to_col})")
            continue
        if not src.exists():
            print(f"  SKIP (not found): {relpath}")
            continue
        preprocess_edge(src, dst, from_col, to_col)
        n = sum(1 for _ in open(dst)) - 1
        print(f"  {edge_col} ({from_col}->{to_col}): {n} rows -> {dst}")

    # Copy multi-value files as-is (processed separately during load)
    print("=== Copying multi-value attribute files ===")
    for relpath in SKIP_FILES:
        src = input_dir / relpath
        dst = output_dir / relpath
        if dst.exists():
            print(f"  SKIP (already done): {relpath}")
            continue
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  Copied: {relpath}")

    print(f"\nPreprocessed data written to: {output_dir}")


if __name__ == "__main__":
    main()
