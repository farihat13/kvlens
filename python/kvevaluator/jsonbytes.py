"""How a bytes value crosses JSON, the one definition on the Python side: {"$bytes": "<base64>"}, the same object the C++
engine code writes and reads (value.cc to_json / from_json). Use `dumps` / `loads`, or pass `default` / `object_hook`.
The drivers carry this JSON form as it is (params, requests, responses, logs), so any of them can save it with plain
json; Python bytes exist only where the Python oracle stores or binds a value (`to_bytes`) and where a check reads."""
import base64
import json

TAG = "$bytes"


def default(o):
    """json.dumps(..., default=default): a bytes value as its tagged base64 object"""
    if isinstance(o, (bytes, bytearray)):
        return {TAG: base64.b64encode(o).decode("ascii")}
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def object_hook(d):
    """json.loads(..., object_hook=object_hook): a tagged object back to bytes; a row never has the field"""
    if TAG in d:
        if len(d) != 1 or not isinstance(d[TAG], str):
            raise ValueError(f"a {{{TAG!r}: ...}} object must hold exactly one base64 string: {d!r}")
        return base64.b64decode(d[TAG], validate=True)
    return d


def to_bytes(v):
    """one value declared bytes -> Python bytes: the tagged object or bytes; anything else (a latin1 str from code
    before the type) is refused"""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, dict) and TAG in v:
        return object_hook(v)
    raise ValueError(f"a bytes value must be {{{TAG!r}: <base64>}} or bytes, got {type(v).__name__} "
                     f"(an input written before the bytes type?)")


def dumps(obj, **kw):
    return json.dumps(obj, default=default, **kw)


def loads(s):
    return json.loads(s, object_hook=object_hook)
