"""The JOB workload's schema: job.schema (generated from mapping.py), compiled to the flat `Schema` every format reads."""
import os

from kvevaluator.schema.schemadef import compile_schema
from kvevaluator.schema.schema_txt import parse_schema

_DIR = os.path.dirname(os.path.abspath(__file__))


def _schema_path(name):
    return os.path.join(_DIR, "job.schema" if name == "canonical" else f"job_{name}.schema")


SCHEMAS = {"canonical": _schema_path("canonical")}
for _fn in sorted(os.listdir(_DIR)):
    if _fn.startswith("job_") and _fn.endswith(".schema"):
        SCHEMAS[_fn[len("job_"):-len(".schema")]] = os.path.join(_DIR, _fn)

_compiled = {}


def get_schema(name="canonical"):
    if name not in SCHEMAS:
        raise SystemExit(f"unknown schema {name!r}; known: {', '.join(sorted(SCHEMAS))}")
    if name not in _compiled:
        options = {}
        vertices, edges, indexes, groups, adj = parse_schema(SCHEMAS[name], resolve=_schema_path, options=options)
        _compiled[name] = compile_schema(vertices, edges, indexes, groups, adj,
                                         predicates_per_label=options.get("predicates") == "per_label")
    return _compiled[name]
