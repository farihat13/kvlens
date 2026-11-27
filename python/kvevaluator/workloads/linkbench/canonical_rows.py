"""The canonical form of a LinkBench read's answer: one definition that our responses and the in-memory log model both
reduce to, so two answers compare by one sha256.

A row becomes a tuple of plain values, in a fixed field order per row kind:
  node  (_vid, ntype, version, time, data)                 getnode
  link  (src, dst, visibility, version, time, data)        getlinklist_<t>, getlink_<t>
  count (count,)                                           countlink_<t>
An id is the decimal real id: a vid (our 16-hex-char string, or an int) & 0x00FFFFFFFFFFFFFF. data is the hex of its
bytes (a str is our latin1-decoded blob, loader.py; bytes are used as they are). Every other field is an int. A field
the row lacks (an absent key, or None: a node that only an update of a missing node wrote has no ntype) is the empty
string. A node row with no field besides its id is a missing node and is dropped (our getnode plan returns the vid
with every property null; LinkBench and the log model return no row). Rows keep the order the plan gives
(getlinklist: time desc, dst asc; getlink: dst asc); nothing here sorts.
"""

ID_MASK = 0x00FFFFFFFFFFFFFF
NODE_FIELDS = ("_vid", "ntype", "version", "time", "data")
LINK_FIELDS = ("src", "dst", "visibility", "version", "time", "data")
COUNT_FIELDS = ("count",)
_KINDS = (NODE_FIELDS, LINK_FIELDS, COUNT_FIELDS)
_ID_FIELDS = {"_vid", "src", "dst"}
_KEY_FIELDS = {NODE_FIELDS: {"_vid"}, LINK_FIELDS: {"src", "dst"}, COUNT_FIELDS: {"count"}}


def real_id(v):
    """the decimal real id of a vid: our hex vid string, or an int (a raw id passes unchanged)"""
    return (int(v, 16) if isinstance(v, str) else int(v)) & ID_MASK


def data_hex(d):
    """the hex of a data blob: our str holds one latin1 char per byte; bytes stay as they are"""
    return (d.encode("latin1") if isinstance(d, str) else bytes(d)).hex()


def _kind(row):
    """the row kind: its key fields all there, no field outside it, and some field besides the ids"""
    keys = set(row)
    for fields in _KINDS:
        if _KEY_FIELDS[fields] <= keys <= set(fields) and (keys - _ID_FIELDS or fields == COUNT_FIELDS):
            return fields
    raise ValueError(f"canonical_rows: a row with fields {sorted(row)} is no LinkBench answer row")


def canonical_row(row):
    """one answer row (a dict keyed by the plan's output fields) -> its canonical tuple"""
    def value(f):
        v = row.get(f)
        if v is None and f in _ID_FIELDS:
            raise ValueError(f"canonical_rows: a row without its id {f}: {row}")
        if v is None:
            return ""
        return real_id(v) if f in _ID_FIELDS else data_hex(v) if f == "data" else int(v)
    return tuple(value(f) for f in _kind(row))


def missing_node(row):
    """a node row that holds nothing but its id: the node does not exist"""
    return "_vid" in row and set(row) <= set(NODE_FIELDS) and all(row.get(f) is None for f in NODE_FIELDS[1:])


def canonical_rows(rows):
    """a read's answer rows, in the plan's order -> a list of canonical tuples (missing-node rows dropped)"""
    return [canonical_row(r) for r in rows if not missing_node(r)]


def canonical_text(canon):
    """the bytes the sha256 is taken over: one line per row, the values tab-separated, each line ending in a newline"""
    return "".join("\t".join(str(x) for x in t) + "\n" for t in canon).encode()

