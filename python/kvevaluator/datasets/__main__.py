"""The datasets' facts for a shell script.

    python -m kvevaluator.datasets schema-path ldbc [design] [--relative]   # the compiled schema JSON's path
    python -m kvevaluator.datasets params ldbc 3 interactive                # a query set's tracked params dir
    python -m kvevaluator.datasets frequencies ldbc 3                       # the benchmark's frequencies dir
    python -m kvevaluator.datasets write-schema ldbc [design] [--out F]      # compile the schema and write that JSON
"""
import argparse
import json
import os

from kvevaluator.datasets import data_in, frequencies_in, params_in, schema, schema_file, schema_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("schema-path", help="the compiled schema JSON's absolute path")
    sp.add_argument("name")
    sp.add_argument("design", nargs="?", default="canonical")
    sp.add_argument("--relative", action="store_true", help="relative to the repo root (what a binary resolves)")
    dp = sub.add_parser("data", help="a dataset's own files at a scale (<data dir>/datasets/<dataset>/<scale>)")
    dp.add_argument("name")
    dp.add_argument("scale")
    pp = sub.add_parser("params", help="a query set's tracked params dir at a scale (inputs/params/...)")
    pp.add_argument("name")
    pp.add_argument("scale")
    pp.add_argument("query_set")
    fp = sub.add_parser("frequencies", help="the benchmark's own frequencies dir at a scale (inputs/frequencies/...)")
    fp.add_argument("name")
    fp.add_argument("scale")
    ws = sub.add_parser("write-schema", help="compile a dataset's schema and write its JSON (what the C++ binary reads)")
    ws.add_argument("name")
    ws.add_argument("design", nargs="?", default="canonical")
    ws.add_argument("--out", default=None, help="default: the dataset's schema path (schema-path)")
    a = ap.parse_args()
    if a.cmd == "data":
        print(data_in(a.name, a.scale))
        return
    if a.cmd == "params":
        print(params_in(a.name, a.scale, a.query_set))
        return
    if a.cmd == "frequencies":
        print(frequencies_in(a.name, a.scale))
        return
    if a.cmd == "write-schema":
        from kvevaluator.schema.schema_json import to_json
        out = a.out or schema_file(a.name, a.design)
        doc = to_json(schema(a.name, a.design), a.design)
        with open(out, "w") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
        print(f"wrote {os.path.relpath(out)}  ({len(doc['vertices'])} vertices, {len(doc['edges'])} edges, "
              f"{len(doc['indexes'])} index ids, hash {doc['hash'][:12]})")
        return
    path = schema_path(a.name, a.design)
    if a.relative:
        from kvevaluator.paths import ROOT
        path = os.path.relpath(path, ROOT)
    print(path)


if __name__ == "__main__":
    main()
