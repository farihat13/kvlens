#!/usr/bin/env python3
"""Write job.schema (the shared schema DSL) from mapping.py, so the mapping has one source.

    python3 -m kvevaluator.datasets.imdb.schema_text [--check]
--check: exit 1 when the committed job.schema differs from what the mapping gives.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
from kvevaluator.datasets.imdb import mapping


def text():
    out = ["# IMDb with the Join Order Benchmark, mapping `rows`. GENERATED from mapping.py by schema_text.py: edit the",
           "# mapping, not this file. Why each choice: README.md, \"The mapping\".", "",
           "# ---------------------------------------------------------------- vertices ----"]
    for label, (table, props) in mapping.VERTICES.items():
        out.append(f"vertex {label} prefix={table}")
        for p, _, t in props:
            out.append(f"    {p:16} {t}" + ("" if (label, p) in mapping.REQUIRED else " nullable"))
        out.append("")
    out.append("# ------------------------------------------------------------------- edges ----")
    for etype, (_, sl, _, dl, _, props, _) in mapping.EDGES.items():
        out.append(f"edge {etype} reverse")
        out.append(f"    {sl}->{dl}")
        for p, _, t in props:
            out.append(f"    {p:16} {t} nullable")
        out.append("")
    out += ["# One predicate per label on a store that names its data by predicate (Dgraph): `MovieInfo.info`, data keys",
            "# and index keys. The other formats key by label anyway.",
            "predicates per_label", ""]
    out.append("# ----------------------------------------------------------------- indexes ----")
    # type_scoped: several labels share a property name (info, note, name, kind_id); Dgraph's index is per predicate
    # unless told the label, and would hand back other labels' vertices. The other formats index per label anyway.
    # equality: D4's index is for = and IN; a number's default index is sorted (see schema_txt.py's note on Dgraph)
    types = {(label, p): t for label, (_, props) in mapping.VERTICES.items() for p, _, t in props}
    out += [f"index {label}.{p} type_scoped" + (" equality" if types[(label, p)] == "int" else "") for label, p in mapping.INDEXES]
    out.append("")
    out += [f"index {label}" for label in mapping.VERTICES]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = os.path.join(HERE, "job.schema")
    if "--check" in sys.argv:
        if not os.path.exists(path) or open(path).read() != text():
            sys.exit(f"{path} differs from mapping.py: run schema_text.py")
        print("job.schema matches mapping.py")
    else:
        open(path, "w").write(text())
        print(f"wrote {path}")
