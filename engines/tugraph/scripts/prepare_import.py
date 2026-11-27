#!/usr/bin/env python3
"""Make what lgraph_import needs from LDBC's CsvBasic files and the tracked design (schema/ldbc_import.json):
<out>/prepared/person.csv, LDBC's person file with each person's emails and languages joined by ';' in the files'
own order (TuGraph has no list property), and <out>/import.json, the design with absolute paths and every label's
`detach_property` set. Prints the rows each file must load (its lines minus the header).

    prepare_import.py --csv <.../CsvBasic-LongDateFormatter> --schema schema/ldbc_import.json --out <dir> \
        --detach-property false
"""
import argparse
import collections
import json
import os


def joined(path):
    """person id -> its values in file order, joined by ';'"""
    vals = collections.defaultdict(list)
    with open(path, encoding="utf-8") as f:
        next(f)
        for line in f:
            pid, val = line.rstrip("\n").split("|", 1)
            vals[pid].append(val)
    return {pid: ";".join(v) for pid, v in vals.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True, help="LDBC's CsvBasic directory (holds static/ and dynamic/)")
    ap.add_argument("--schema", required=True, help="the tracked design, schema/ldbc_import.json")
    ap.add_argument("--out", required=True, help="where prepared/person.csv and import.json go")
    ap.add_argument("--detach-property", required=True, choices=("true", "false"),
                    help="false: a vertex's properties sit in its packed record (graph_data_pack.h); true: in a "
                         "table of their own, read by a GET (lgraph_import's default at v4.5.2)")
    a = ap.parse_args()
    dyn = os.path.join(a.csv, "dynamic")
    emails = joined(os.path.join(dyn, "person_email_emailaddress_0_0.csv"))
    langs = joined(os.path.join(dyn, "person_speaks_language_0_0.csv"))
    os.makedirs(os.path.join(a.out, "prepared"), exist_ok=True)
    person = os.path.join(a.out, "prepared", "person.csv")
    with open(os.path.join(dyn, "person_0_0.csv"), encoding="utf-8") as src, open(person, "w", encoding="utf-8") as dst:
        dst.write(next(src).rstrip("\n") + "|emails|languages\n")
        for line in src:
            pid = line.split("|", 1)[0]
            dst.write(f"{line.rstrip(chr(10))}|{emails.get(pid, '')}|{langs.get(pid, '')}\n")
    with open(a.schema, encoding="utf-8") as f:
        design = json.load(f)
    for label in design["schema"]:
        label["detach_property"] = a.detach_property == "true"
    for item in design["files"]:
        rel = item["path"]
        item["path"] = os.path.join(a.out, rel) if rel.startswith("prepared/") else os.path.join(a.csv, rel)
        with open(item["path"], "rb") as f:
            rows = sum(1 for _ in f) - item["header"]
        print(f"{item['label']}\t{rows}\t{rel}")
    with open(os.path.join(a.out, "import.json"), "w", encoding="utf-8") as f:
        json.dump({"schema": design["schema"], "files": design["files"]}, f, indent=1)


if __name__ == "__main__":
    main()
