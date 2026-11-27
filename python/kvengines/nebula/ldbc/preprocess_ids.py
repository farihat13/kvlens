#!/usr/bin/env python3
"""
Preprocess LDBC CSV files to add entity type prefixes to IDs.
This prevents ID collisions between different entity types (Person, Post, Comment, etc.)
"""

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# Mapping of CSV files to their ID column configurations
# Format: {filename: {col_index: prefix}}
CSV_CONFIG = {
    # ========== VERTEX FILES ==========
    # Dynamic vertices
    "dynamic/person_0_0.csv": {0: "person"},
    "dynamic/post_0_0.csv": {0: "msg"},
    "dynamic/comment_0_0.csv": {0: "msg"},
    "dynamic/forum_0_0.csv": {0: "forum"},
    
    # Static vertices
    "static/organisation_0_0.csv": {0: "org"},
    "static/place_0_0.csv": {0: "place"},
    "static/tag_0_0.csv": {0: "tag"},
    "static/tagclass_0_0.csv": {0: "tagclass"},
    
    # ========== EDGE FILES ==========
    # Person edges
    "dynamic/person_knows_person_0_0.csv": {0: "person", 1: "person"},
    "dynamic/person_likes_post_0_0.csv": {0: "person", 1: "msg"},
    "dynamic/person_likes_comment_0_0.csv": {0: "person", 1: "msg"},
    "dynamic/person_hasInterest_tag_0_0.csv": {0: "person", 1: "tag"},
    "dynamic/person_studyAt_organisation_0_0.csv": {0: "person", 1: "org"},
    "dynamic/person_workAt_organisation_0_0.csv": {0: "person", 1: "org"},
    "dynamic/person_isLocatedIn_place_0_0.csv": {0: "person", 1: "place"},
    
    # Post edges
    "dynamic/post_hasCreator_person_0_0.csv": {0: "msg", 1: "person"},
    "dynamic/post_hasTag_tag_0_0.csv": {0: "msg", 1: "tag"},
    "dynamic/post_isLocatedIn_place_0_0.csv": {0: "msg", 1: "place"},
    
    # Comment edges
    "dynamic/comment_hasCreator_person_0_0.csv": {0: "msg", 1: "person"},
    "dynamic/comment_hasTag_tag_0_0.csv": {0: "msg", 1: "tag"},
    "dynamic/comment_isLocatedIn_place_0_0.csv": {0: "msg", 1: "place"},
    "dynamic/comment_replyOf_comment_0_0.csv": {0: "msg", 1: "msg"},
    "dynamic/comment_replyOf_post_0_0.csv": {0: "msg", 1: "msg"},
    
    # Forum edges
    "dynamic/forum_hasMember_person_0_0.csv": {0: "forum", 1: "person"},
    "dynamic/forum_hasModerator_person_0_0.csv": {0: "forum", 1: "person"},
    "dynamic/forum_containerOf_post_0_0.csv": {0: "forum", 1: "msg"},
    "dynamic/forum_hasTag_tag_0_0.csv": {0: "forum", 1: "tag"},
    
    # Static edges
    "static/organisation_isLocatedIn_place_0_0.csv": {0: "org", 1: "place"},
    "static/place_isPartOf_place_0_0.csv": {0: "place", 1: "place"},
    "static/tag_hasType_tagclass_0_0.csv": {0: "tag", 1: "tagclass"},
    "static/tagclass_isSubclassOf_tagclass_0_0.csv": {0: "tagclass", 1: "tagclass"},
    
    # Special: multi-value attributes (person_email, person_speaks)
    "dynamic/person_email_emailaddress_0_0.csv": {0: "person"},
    "dynamic/person_speaks_language_0_0.csv": {0: "person"},
}


def prefix_csv_file(
    input_path: Path,
    output_path: Path,
    id_columns: Dict[int, str],
    delimiter: str = "|"
) -> Tuple[int, int]:
    """
    Prefix IDs in a CSV file.
    
    Args:
        input_path: Source CSV file
        output_path: Destination CSV file
        id_columns: {col_index: prefix} mapping
        delimiter: CSV delimiter
        
    Returns:
        (rows_processed, rows_with_empty_ids)
    """
    rows_processed = 0
    rows_with_empty = 0
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(input_path, 'r', encoding='utf-8') as f_in, \
         open(output_path, 'w', encoding='utf-8', newline='') as f_out:
        
        reader = csv.reader(f_in, delimiter=delimiter)
        writer = csv.writer(f_out, delimiter=delimiter)
        
        for i, row in enumerate(reader):
            if i == 0:
                # Write header unchanged
                writer.writerow(row)
                continue
            
            # Prefix the specified columns
            for col_idx, prefix in id_columns.items():
                if col_idx < len(row):
                    original_id = row[col_idx].strip()
                    if original_id:
                        row[col_idx] = f"{prefix}_{original_id}"
                    else:
                        rows_with_empty += 1
            
            writer.writerow(row)
            rows_processed += 1
    
    return rows_processed, rows_with_empty


def process_update_streams(data_dir: Path, output_dir: Path) -> None:
    """Process update stream files (person and forum inserts)."""
    update_files = [
        "updateStream_0_0_person.csv",
        "updateStream_0_0_forum.csv"
    ]
    
    for filename in update_files:
        input_file = data_dir / "update_streams" / "social_network-sf0.3-numpart-1" / filename
        if not input_file.exists():
            continue
            
        output_file = output_dir / "update_streams" / "social_network-sf0.3-numpart-1" / filename
        
        # Determine entity type from filename
        if "person" in filename:
            prefix = "person"
        elif "forum" in filename:
            prefix = "forum"
        else:
            continue
        
        print(f"  Processing update stream: {filename}")
        rows, empty = prefix_csv_file(
            input_file,
            output_file,
            {1: prefix}  # Column 1 is the entity ID in update streams
        )
        print(f"    ✓ {rows} rows processed ({empty} with empty IDs)")


def main():
    parser = argparse.ArgumentParser(
        description="Add entity type prefixes to LDBC IDs to prevent collisions"
    )
    parser.add_argument(
        "data_dir",
        type=Path,
        help="LDBC data directory (e.g., ./data/ldbc/sf0.3/CsvBasic-LongDateFormatter)"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: <data_dir>_prefixed)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without writing files"
    )
    
    args = parser.parse_args()
    
    if not args.data_dir.exists():
        print(f"Error: Data directory not found: {args.data_dir}", file=sys.stderr)
        sys.exit(1)
    
    # Default output directory
    output_dir = args.output_dir or Path(str(args.data_dir) + "_prefixed")
    
    print(f"LDBC ID Preprocessing")
    print(f"=====================")
    print(f"Input:  {args.data_dir}")
    print(f"Output: {output_dir}")
    print(f"Mode:   {'DRY RUN' if args.dry_run else 'PROCESSING'}")
    print()
    
    # Process each CSV file
    total_files = 0
    total_rows = 0
    missing_files = []
    
    for csv_file, id_cols in CSV_CONFIG.items():
        input_path = args.data_dir / csv_file
        
        if not input_path.exists():
            missing_files.append(csv_file)
            continue
        
        output_path = output_dir / csv_file
        
        print(f"Processing: {csv_file}")
        print(f"  ID columns: {id_cols}")
        
        if args.dry_run:
            print(f"  Would write to: {output_path}")
            continue
        
        try:
            rows, empty = prefix_csv_file(input_path, output_path, id_cols)
            print(f"  ✓ {rows} rows processed ({empty} with empty IDs)")
            total_files += 1
            total_rows += rows
        except Exception as e:
            print(f"  ✗ Error: {e}", file=sys.stderr)
    
    # Process update streams
    if not args.dry_run:
        print("\nProcessing update streams...")
        try:
            process_update_streams(args.data_dir.parent.parent, output_dir.parent.parent)
        except Exception as e:
            print(f"  ✗ Error processing update streams: {e}", file=sys.stderr)
    
    # Copy non-CSV files (schema, importer config)
    if not args.dry_run:
        print("\nCopying configuration files...")
        for config_file in ["schema.gql", "importer.yaml", "params.ini"]:
            src = args.data_dir / config_file
            if src.exists():
                dst = output_dir / config_file
                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.copy2(src, dst)
                print(f"  ✓ Copied {config_file}")
    
    # Summary
    print(f"\n{'Summary (dry run)' if args.dry_run else 'Summary'}")
    print(f"=" * 40)
    print(f"Files processed: {total_files}/{len(CSV_CONFIG)}")
    print(f"Total rows:      {total_rows:,}")
    
    if missing_files:
        print(f"\nMissing files ({len(missing_files)}):")
        for f in missing_files[:10]:
            print(f"  - {f}")
        if len(missing_files) > 10:
            print(f"  ... and {len(missing_files) - 10} more")
    
    if not args.dry_run:
        print(f"\n✓ Preprocessing complete!")
        print(f"\nNext steps:")
        print(f"1. Update schema.gql: Change vid_type from INT64 to FIXED_STRING(30)")
        print(f"2. Update importer.yaml to use prefixed CSVs from: {output_dir}")
        print(f"3. Update queries to convert int parameters: id(person) == 'person_' + toString($personId)")


if __name__ == "__main__":
    main()