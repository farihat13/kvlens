#!/usr/bin/env python3
import csv
from collections import defaultdict
import json
import argparse

def main():
    csv_path = "data/sample-ldbc-dataset/person_speaks_language_0_0.csv"  # adjust to your path
    out_sql = "update_languages.gql"
    batch_size = 200

    # take input arguments
    parser = argparse.ArgumentParser(description='Generate update emails GQL from CSV.')
    parser.add_argument('--csv', type=str, default=csv_path, help='Path to input CSV file')
    parser.add_argument('--out', type=str, default=out_sql, help='Path to output GQL file')
    parser.add_argument('--batch-size', type=int, default=batch_size, help='Number of updates per batch')
    args = parser.parse_args()
    csv_path = args.csv
    out_sql = args.out
    batch_size = args.batch_size

    groups = defaultdict(list)
    with open(csv_path, newline='') as f:
        r = csv.DictReader(f, delimiter='|')
        for row in r:
            vid = row.get('Person.id') or row.get('Person.id'.lower()) or row.get('Person.id'.strip()) or row.get('Person.id')
            # the CSV's columns are Person.id|language
            if not vid:
                # try fallback: first column
                items = list(row.values())
                if len(items) >= 2:
                    vid = items[0]
                    speaks = items[1]
                else:
                    continue
            else:
                speaks = row.get('language') or row.get('Language') or row.get('language'.lower())
            if speaks is None or speaks == "":
                continue
            vid = vid.strip()
            speaks = speaks.strip()
            groups[vid].append(speaks)

    # write batched UPDATE statements
    with open(out_sql, "w") as out:
        cnt = 0
        for vid, speaks in groups.items():
            # dedupe and escape
            uniq = list(dict.fromkeys(speaks))
            list_repr = "[" + ",".join(json.dumps(e) for e in uniq) + "]"
            out.write(f'UPDATE VERTEX "{vid}" SET Person.languages = {list_repr};\n')
            cnt += 1
            if cnt % batch_size == 0:
                out.write("YIELD 1;\n")  # optional to break large request
    print(f"Written {len(groups)} updates to {out_sql}")


if __name__ == "__main__":
    main()
