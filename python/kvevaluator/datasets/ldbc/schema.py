"""The LDBC SNB physical design, authored as readable TEXT files (ldbc*.schema in this directory) and compiled to
the flat `Schema` every format consumes.

- ldbc.schema         -> the `canonical` design (structure + property indexes).
- ldbc_<name>.schema  -> the `<name>` design; `--schema <name>` selects it. Each file is a COMPLETE schema;
  adding a design = a new file here, no code change.

The LDBC facts are pinned from the engines' own schemas (engines/<db>/schema/). The grammar: schema_txt;
the conventions: the .schema files' own comments.
"""
import os

from kvevaluator.schema.schemadef import compile_schema
from kvevaluator.schema.schema_txt import parse_schema

_DIR = os.path.dirname(os.path.abspath(__file__))


def _schema_path(name):
    return os.path.join(_DIR, "ldbc.schema" if name == "canonical" else f"ldbc_{name}.schema")


# name -> file, discovered from the directory: ldbc.schema is `canonical`, every ldbc_<name>.schema
# is `<name>`. So dropping a file in adds a --schema choice with zero code change.
SCHEMAS = {"canonical": _schema_path("canonical")}
for _fn in sorted(os.listdir(_DIR)):
    if _fn.startswith("ldbc_") and _fn.endswith(".schema"):
        SCHEMAS[_fn[len("ldbc_"):-len(".schema")]] = os.path.join(_DIR, _fn)
# Parked designs: not maintained; counts not checked. The label_only family still loads, but no gate
# or sweep over SCHEMAS runs it (ACTIVE_SCHEMAS).
PARKED_SCHEMAS = ("label_only",)
ACTIVE_SCHEMAS = [n for n in SCHEMAS if n not in PARKED_SCHEMAS]

_compiled = {}


def get_schema(name="canonical"):
    """The named LDBC physical design -- parsed from its .schema file, compiled, and cached. Raises
    on an unknown name (loud > silent canonical)."""
    if name not in SCHEMAS:
        raise SystemExit(f"unknown schema {name!r}; known: {', '.join(sorted(SCHEMAS))}")
    if name not in _compiled:
        path = SCHEMAS[name]
        vertices, edges, indexes, groups, adj = parse_schema(path, resolve=_schema_path)  # LDBC ldbc_<name>.schema resolver; errors carry file:line
        try:
            _compiled[name] = compile_schema(vertices, edges, indexes, groups, adj, variant=name)
        except ValueError as e:                                # index-validation errors have no
            raise SystemExit(f"{os.path.basename(path)}: {e}")  # line, so at least name the file
    return _compiled[name]


# The canonical STRUCTURE, exposed for the loader (it derives its prop lists + types from
# VERTICES/EDGES) and the schema-vs-loader drift guard. Parsed once at import.
VERTICES, EDGES, INDEXES, _GROUPS, _ADJ = parse_schema(SCHEMAS["canonical"], resolve=_schema_path)

# Back-compat aliases: every existing importer takes these by name. They ARE the compiled files.
LDBC_SCHEMA = get_schema("canonical")
LABEL_ONLY_SCHEMA = get_schema("label_only")
TYPE_INDEX_SCHEMA = get_schema("type_index")
