"""The property codec knob (cpp/kvevaluator/src/codec/props_codec.h), one definition for every Python reader.

schema = values only, the RowV2 row of the declared columns (the fair default); named = every property name stored
with its value (dgraph's posting facets, arangodb's edge AND vertex documents), the engines' habit, kept to reproduce
them. KVEVAL_PROPS_CODEC selects it everywhere:
the C++ store names (measure/stores.py) and the Python byte model of the formats that store names (arangodb). meta's spec
field value.edge_props is the same choice for its Condensed lists, stated in its spec file (this knob does not set it).
"""
import os

PROPS_CODEC_DEFAULT = "schema"


def props_codec():
    if "KVEVAL_EDGE_PROPS" in os.environ:   # another name for KVEVAL_PROPS_CODEC: never silently ignore a request
        raise SystemExit("KVEVAL_EDGE_PROPS is not read: set KVEVAL_PROPS_CODEC (it covers vertex docs too)")
    v = os.environ.get("KVEVAL_PROPS_CODEC", PROPS_CODEC_DEFAULT)
    if v not in ("named", "schema"):
        raise SystemExit(f"KVEVAL_PROPS_CODEC={v!r}: expected named or schema")
    return v
