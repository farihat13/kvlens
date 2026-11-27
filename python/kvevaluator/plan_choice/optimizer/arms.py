"""A chunk ARM of a format -- `<format>-c<N>k`, the store slug the paper's arms carry (pertype_csr-c512k) -- as a
pinnable profile of its own: the format's plans and candidates, the store built at that chunk, and the format's
capabilities derived WITH the chunk (kvevaluator.formats.caps.caps_for(..., chunk=): `coarse_chunks` at >= CHUNK_COARSE).
Every plan stage (generate, emit, dump_planir, check_plan_files, the pick record) resolves a profile name through here,
so a plain format and an arm are interchangeable in their format lists."""
from kvevaluator.formats.pertype_chunk import CHUNKED_FORMATS
from kvevaluator.formats.pertype_chunk import PERTYPE_CHUNK_DEFAULT
from kvevaluator.capture.store_identity import chunk_of_slug


def split_arm(name):
    """(base format, chunk in bytes or None): 'pertype_csr-c512k' -> ('pertype_csr', 524288); a plain format name
    -> (name, None)."""
    chunk = chunk_of_slug(name)
    if chunk is None:
        return name, None
    return name[:name.rindex("-c")], chunk


def base_format(name):
    return split_arm(name)[0]


def is_arm(name):
    return split_arm(name)[1] is not None


def store_chunk_of(name):
    """The chunk the profile's STORE is built at: the arm's own, else the format's default store (the chunked
    per-type formats always carry one: pertype_csr -> pertype_csr-c64k; the rest have no chunk tag)."""
    base, chunk = split_arm(name)
    if chunk is not None:
        return chunk
    return PERTYPE_CHUNK_DEFAULT if base in CHUNKED_FORMATS else None


def caps_of(name, schema):
    """kvevaluator.formats.caps.caps_for for a profile name: the base format's caps derived with the store's chunk."""
    from kvevaluator.formats.caps import caps_for
    return caps_for(base_format(name), schema, chunk=store_chunk_of(name))


def edge_caps_of(name, schema):
    """kvevaluator.formats.caps.edge_caps_of for a profile name: etype -> that edge type's adjacency caps"""
    from kvevaluator.formats.caps import edge_caps_of as of
    return of(base_format(name), schema, chunk=store_chunk_of(name))


def resolver_of(name, schema):
    """kvevaluator.formats.caps.caps_resolver for a profile name (per-label caps, the store's chunk included)."""
    from kvevaluator.formats.caps import caps_resolver
    return caps_resolver(base_format(name), schema, chunk=store_chunk_of(name))
