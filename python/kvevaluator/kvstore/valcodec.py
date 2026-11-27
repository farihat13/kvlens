"""Primitive value codec -- the reversible byte form of the "neutral" leaf shapes
(scalars, None, str, list, tuple) every format stores outside its packed layouts.

Each value carries a 1-byte type tag, so decode needs no schema and bool-vs-int /
tuple-vs-list round-trip exactly. Convention every format codec follows:
top-level None <-> b""; any non-None value encodes to >= 1
byte, so an empty str/list/dict can never read back as None. encode_leaf/
decode_leaf implement that top-level convention; encode_prim/decode_prim are the
tagged forms containers nest.
"""
import struct

# 1-byte type tags. bool is a tag pair (True/False in the tag itself, no payload).
_INT = b"i"      # + 8B big-endian signed (matches the neutral model's int width)
_FLOAT = b"f"    # + 8B big-endian double (defensive; LDBC stores none)
_STR = b"s"      # + uvarint byte-length + utf-8 bytes
_BYTES = b"b"    # + uvarint byte-length + the raw bytes (C++ prim.cc's 'b')
_NONE = b"n"
_TRUE = b"T"
_FALSE = b"F"
_LIST = b"l"     # + uvarint count + tagged elements
_TUPLE = b"t"    # + uvarint count + tagged elements
_DICT = b"d"     # + uvarint count + (str key + tagged value) pairs, insertion order


def uvarint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_uvarint(b, i):
    n = shift = 0
    while True:
        c = b[i]
        i += 1
        n |= (c & 0x7F) << shift
        if not c & 0x80:
            return n, i
        shift += 7


def encode_prim(v):
    if v is None:
        return _NONE
    if isinstance(v, bool):                     # bool BEFORE int (bool subclasses int)
        return _TRUE if v else _FALSE
    if isinstance(v, int):
        return _INT + struct.pack(">q", v)
    if isinstance(v, float):
        return _FLOAT + struct.pack(">d", v)
    if isinstance(v, str):
        u = v.encode("utf-8")
        return _STR + uvarint(len(u)) + u
    if isinstance(v, (bytes, bytearray)):
        return _BYTES + uvarint(len(v)) + bytes(v)
    if isinstance(v, (list, tuple)):
        head = _LIST if isinstance(v, list) else _TUPLE
        return head + uvarint(len(v)) + b"".join(encode_prim(x) for x in v)
    if isinstance(v, dict):
        out = _DICT + uvarint(len(v))
        for k, x in v.items():
            u = k.encode("utf-8")
            out += uvarint(len(u)) + u + encode_prim(x)
        return out
    raise TypeError(f"valcodec: unsupported type {type(v).__name__}: {v!r}")


def decode_prim(b):
    v, i = _decode(b, 0)
    if i != len(b):
        raise ValueError(f"valcodec: {len(b) - i} trailing byte(s) after decode")
    return v


def _decode(b, i):
    tag = b[i:i + 1]
    i += 1
    if tag == _NONE:
        return None, i
    if tag == _TRUE:
        return True, i
    if tag == _FALSE:
        return False, i
    if tag == _INT:
        return struct.unpack_from(">q", b, i)[0], i + 8
    if tag == _FLOAT:
        return struct.unpack_from(">d", b, i)[0], i + 8
    if tag == _STR:
        n, i = read_uvarint(b, i)
        return b[i:i + n].decode("utf-8"), i + n
    if tag == _BYTES:
        n, i = read_uvarint(b, i)
        return bytes(b[i:i + n]), i + n
    if tag in (_LIST, _TUPLE):
        n, i = read_uvarint(b, i)
        out = []
        for _ in range(n):
            x, i = _decode(b, i)
            out.append(x)
        return (out if tag == _LIST else tuple(out)), i
    if tag == _DICT:
        n, i = read_uvarint(b, i)
        out = {}
        for _ in range(n):
            kl, i = read_uvarint(b, i)
            k = b[i:i + kl].decode("utf-8")
            i += kl
            out[k], i = _decode(b, i)
        return out, i
    raise ValueError(f"valcodec: unknown tag {tag!r} at offset {i - 1}")


def encode_leaf(v):
    """Top-level form: None -> b'' (absent-value convention), else tagged bytes."""
    return b"" if v is None else encode_prim(v)


def decode_leaf(raw):
    return None if not raw else decode_prim(raw)


def decode_prim_stream(b, i):
    """Decode ONE tagged value starting at offset `i`; returns (value, next offset).
    For codecs that embed prim values inside their own framing (dgraph's facet array)."""
    return _decode(b, i)
