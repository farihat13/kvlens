"""The vid convention: a vertex id is a 64-bit uint `(tag << 60) | ldbc_id`, rendered as 16 lowercase hex chars, so
string order == numeric order and key_bytes prices it as 8 bytes. The 4-bit `tag` keeps ids unique across types
and is used ONLY at construction: a format never parses a vid for its type; the type lives in storage.
Kept out of the LDBC loader so a format never imports a workload module.
"""

# The vid GROUP a label's ids live in: Post & Comment share one Message id-space
# (their ids don't collide -- verified). Every other label is its own group.
_GROUP = {"Post": "Message", "Comment": "Message"}

# 4-bit tag per group (top of the u64). Person = 0 so a person's vid IS its raw id.
# Tags 0-6 are LDBC; 7 is the LinkBench workload's Node (its own id-space). A workload's labels
# get their own tag so vids never collide across workloads that might share a store namespace.
_TAG = {"Person": 0, "Message": 1, "Forum": 2, "Organisation": 3,
        "Place": 4, "Tag": 5, "TagClass": 6,
        "Node": 7}
# The JOB workload (IMDb) has 16 labels, more than the tags left, so it takes all 16 tag numbers again. A tag
# is read per SCHEMA (schema.json's vid_tags holds only its own labels), and an IMDb store holds IMDb labels only.
_TAG.update({label: tag for tag, label in enumerate(
    ("Title", "Name", "Character", "Company", "Keyword", "MovieInfo", "MovieRating", "PersonInfo", "AkaName",
     "AkaTitle", "CompleteCast", "MovieLink", "KindType", "InfoType", "LinkType", "CompCastType"))})

SHIFT = 60                       # id in the low 60 bits, tag in the top 4
ID_MASK = (1 << SHIFT) - 1
_WIDTH = 16                      # 16 hex chars = 64 bits
VID_WIDTH = _WIDTH               # public: the fixed encoded-vid length. A format whose keys
                                 # carry the vid as CONTENT (arango's pk/edge-index) may price
                                 # it structurally at this width in a symbolic byte coefficient.
_MAX_ID = 1 << (SHIFT - 1)       # loud guard: leave headroom (ids measured <= 46 bits)


def group_of(label):
    return _GROUP.get(label, label)


def vid(label, raw_id):
    """The order-preserving encoded vid for (label, raw_id). raw_id may be str or int."""
    i = int(raw_id)
    if i >= _MAX_ID:                 # measured ids are <= 46 bits; 59-bit ceiling is loud
        raise ValueError(f"LDBC id {i} exceeds the {SHIFT-1}-bit vid budget")
    return format((_TAG[group_of(label)] << SHIFT) | i, "016x")


def person_vid(p):
    """A bare LDBC person id (933) composes to the encoded Person vid; anything else (an encoded vid, a tiny-graph 'A')
    passes through.
    """
    p = str(p)
    return vid("Person", p) if p.isdigit() else p


def message_vids(m):
    """A message's vid as a one-element candidate list: Post and Comment share ONE Message id-space (an encoded vid
    passes through).
    """
    m = str(m)
    return [vid("Message", m)] if m.isdigit() else [m]


def uid_of(v):
    """The full 64-bit uid an encoded vid denotes (for UidPack / order), or None if `v`
    is not an encoded vid (a property value, demo's 'A', etc.)."""
    if isinstance(v, str) and len(v) == _WIDTH:
        try:
            return int(v, 16)
        except ValueError:
            return None
    return None


def key_id(v):
    """The plain LDBC id inside a vid (tag stripped), or None if `v` isn't a vid. Free to project for the formats whose
    vid IS the external id (nebula, arango): engines never GET an id. Dgraph's ids are opaque.
    """
    u = uid_of(v)
    return None if u is None else (u & ID_MASK)


def without_key_id(props):
    """A vertex's props minus `id` (the loader and the write params carry it): no format stores it -- the vid IS
    the id (key_id). The C++ twin is formats/common.h without_key_id / vertex_cols."""
    return {p: v for p, v in props.items() if p != "id"}
