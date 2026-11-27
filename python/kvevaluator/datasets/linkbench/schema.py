"""The LinkBench physical design, authored as `linkbench.schema` (the shared DSL) and compiled to the
flat `Schema` every format consumes -- mirrors datasets/ldbc/schema.py. LinkBench has one design
(canonical); adding a `linkbench_<name>.schema` would add a `--schema <name>` choice with no code change.
"""
import os

from kvevaluator.schema.schemadef import compile_schema
from kvevaluator.schema.schema_txt import parse_schema

_DIR = os.path.dirname(os.path.abspath(__file__))


def _schema_path(name):
    return os.path.join(_DIR, "linkbench.schema" if name == "canonical" else f"linkbench_{name}.schema")


SCHEMAS = {"canonical": _schema_path("canonical")}
for _fn in sorted(os.listdir(_DIR)):
    if _fn.startswith("linkbench_") and _fn.endswith(".schema"):
        SCHEMAS[_fn[len("linkbench_"):-len(".schema")]] = os.path.join(_DIR, _fn)

_compiled = {}


def get_schema(name="canonical"):
    if name not in SCHEMAS:
        raise SystemExit(f"unknown schema {name!r}; known: {', '.join(sorted(SCHEMAS))}")
    if name not in _compiled:
        vertices, edges, indexes, groups, adj = parse_schema(SCHEMAS[name], resolve=_schema_path)
        _compiled[name] = compile_schema(vertices, edges, indexes, groups, adj)
    return _compiled[name]
