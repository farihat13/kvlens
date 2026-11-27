"""Order-preserving codec: tuple-of-str <-> bytes — the serialized key-width model
(key_phybytes), and the byte order every disk store's keys followed (including prefix
order: a shorter tuple sorts before any tuple that extends it), so SEEK/NEXT semantics
match the abstract store's native tuple order.

Each component = its UTF-8 bytes with `0x00` escaped to `0x00 0xff`, terminated by
`0x00 0x00`. Since terminator `00 00` < escaped-null `00 ff`, and a shorter tuple's
encoding is a byte-prefix of a longer one's, lexicographic byte order == tuple order.
"""


def encode_key(key):
    out = bytearray()
    for part in key:
        if not isinstance(part, str):
            raise TypeError(f"key parts must be str, got {type(part).__name__}: {part!r}")
        out += part.encode("utf-8").replace(b"\x00", b"\x00\xff")
        out += b"\x00\x00"
    return bytes(out)


def key_nbytes(key):
    """Canonical encoded size of a key tuple, in bytes -- the key analogue of
    graph.value_bytes. Same key -> same size everywhere (it's the codec's output
    length), so key-byte cost is comparable across formats. Injected into CountingKV
    as `keysizeof`."""
    return len(encode_key(key))

