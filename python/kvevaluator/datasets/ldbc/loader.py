"""LDBC SNB loader: the full SNB schema (all dynamic + static CSVs).

`stream(sf)` yields records lazily from the CsvBasic-LongDateFormatter CSVs (never holds the whole graph), fed
into a format's add_vertex/add_edge; `load(sf)` builds an in-memory Graph from it (small data). The prop lists and
edge endpoints are the SCHEMA's; the loader holds only the CSV file mapping and the streaming mechanics.

Conventions:
- vids are `vids.vid()`'s 64-bit `(tag<<60)|ldbc_id`; Post and Comment share the one Message tag (their ids are
  disjoint). A format recovers the concrete type from storage, never from the vid.
- every vertex gets an 'id' prop (int): LDBC results project it.
- EVERY property is loaded with its real value: the row stores (nebula, arango) charge whole-record bytes, so the
  byte count is wrong unless records carry their full weight. Empty CSV fields are OMITTED, as in the real loads.
- dates are epoch-ms ints, Person.birthday included.
- KNOWS is emitted ONCE per CSV row (directed, as the dataset stores it); symmetry is each format's job, reached
  via read_neighbors(dir="both").
- person.email / person.language are multi-valued string props, merged in from their own CSVs.
- edge records carry (src_label, dst_label): arango needs them for _from/_to.
"""
import csv
import os
import sys
import time

from kvevaluator.paths import BUILD_DIR, CPP_BIN, ldbc_dir
from kvevaluator.schema.graph import Graph
from kvevaluator.schema.vids import vid
from kvevaluator.schema.schemadef import STR, INT, DATE
from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA, VERTICES, EDGES

# Message vids are UNIFIED: Post and Comment share one vid space, as in all three real engines. The concrete label
# rides in every record, so a format recovers Post-vs-Comment from storage, never the vid.
# Derived from the schema's type_groups so it can't drift: {"Post":"Message","Comment":...}.
_VID_LABEL = {member: group
              for group, members in LDBC_SCHEMA.type_groups.items()
              for member in members}


def _vid_label(label):
    """The label whose TAG the vid carries: the message GROUP for Post/Comment, else the label itself."""
    return _VID_LABEL.get(label, label)

# Vertex CSVs: (subdir, filename, label). The prop list + types come from the SCHEMA
# (schema.VERTICES, via _VERTEX_PROPS below) -- only the file mapping is loader knowledge.
# The 'id' column is added to props automatically (see stream); it is not a schema fact.
VERTEX_FILES = [
    ("dynamic", "person_0_0.csv", "Person"),
    ("dynamic", "post_0_0.csv", "Post"),
    ("dynamic", "comment_0_0.csv", "Comment"),
    ("dynamic", "forum_0_0.csv", "Forum"),
    ("static", "organisation_0_0.csv", "Organisation"),
    ("static", "place_0_0.csv", "Place"),
    ("static", "tag_0_0.csv", "Tag"),
    ("static", "tagclass_0_0.csv", "TagClass"),
]

# Edge CSVs: (subdir, filename, etype, src label, dst label). Edge props + types come from the
# SCHEMA (schema.EDGES, via _EDGE_PROPS below). The endpoints stay here -- they map a FILE to its
# (etype, src, dst); arango needs them for _from/_to. Cols are positional: 0 = src id, 1 = dst id,
# 2.. = the edge props (headers repeat names like "Person.id", so DictReader can't be used here).
EDGE_FILES = [
    ("dynamic", "person_knows_person_0_0.csv", "knows", "Person", "Person"),
    ("dynamic", "person_likes_post_0_0.csv", "likes", "Person", "Post"),
    ("dynamic", "person_likes_comment_0_0.csv", "likes", "Person", "Comment"),
    ("dynamic", "person_hasInterest_tag_0_0.csv", "hasInterest", "Person", "Tag"),
    ("dynamic", "person_isLocatedIn_place_0_0.csv", "isLocatedIn", "Person", "Place"),
    ("dynamic", "person_studyAt_organisation_0_0.csv", "studyAt", "Person", "Organisation"),
    ("dynamic", "person_workAt_organisation_0_0.csv", "workAt", "Person", "Organisation"),
    ("dynamic", "post_hasCreator_person_0_0.csv", "hasCreator", "Post", "Person"),
    ("dynamic", "comment_hasCreator_person_0_0.csv", "hasCreator", "Comment", "Person"),
    ("dynamic", "post_hasTag_tag_0_0.csv", "hasTag", "Post", "Tag"),
    ("dynamic", "comment_hasTag_tag_0_0.csv", "hasTag", "Comment", "Tag"),
    ("dynamic", "post_isLocatedIn_place_0_0.csv", "isLocatedIn", "Post", "Place"),
    ("dynamic", "comment_isLocatedIn_place_0_0.csv", "isLocatedIn", "Comment", "Place"),
    ("dynamic", "forum_containerOf_post_0_0.csv", "containerOf", "Forum", "Post"),
    ("dynamic", "forum_hasMember_person_0_0.csv", "hasMember", "Forum", "Person"),
    ("dynamic", "forum_hasModerator_person_0_0.csv", "hasModerator", "Forum", "Person"),
    ("dynamic", "forum_hasTag_tag_0_0.csv", "hasTag", "Forum", "Tag"),
    ("dynamic", "comment_replyOf_post_0_0.csv", "replyOf", "Comment", "Post"),
    ("dynamic", "comment_replyOf_comment_0_0.csv", "replyOf", "Comment", "Comment"),
    ("static", "organisation_isLocatedIn_place_0_0.csv", "isLocatedIn", "Organisation", "Place"),
    ("static", "place_isPartOf_place_0_0.csv", "isPartOf", "Place", "Place"),
    ("static", "tag_hasType_tagclass_0_0.csv", "hasType", "Tag", "TagClass"),
    ("static", "tagclass_isSubclassOf_tagclass_0_0.csv", "isSubclassOf", "TagClass", "TagClass"),
]

# Person multi-valued [string] props kept in their own CSVs (one value per row). The prop NAMES
# (email/language) are the schema's multi=True props; the FILENAMES are loader knowledge.
PERSON_MULTI = [("person_email_emailaddress_0_0.csv", "email"),
                ("person_speaks_language_0_0.csv", "language")]

# Prop lists + types DERIVED from the schema (single source of truth). A schema type
# maps to the loader's s/i conversion flag (DATE stores as an epoch-ms int, so it is 'i'). Vertex
# multi props (email/language) ride in their own CSVs (PERSON_MULTI), so they are excluded here.
_KIND = {STR: "s", INT: "i", DATE: "i"}
_VERTEX_PROPS = {v.label: [(p.name, _KIND[p.type]) for p in v.props if not p.multi]
                 for v in VERTICES}
_EDGE_PROPS = {e.etype: [(p.name, _KIND[p.type]) for p in (e.props or ())] for e in EDGES}


def _root(sf, variant=None):
    """The SF's CSV tree; `variant` names a data variant beside it (sf<SF>-<variant>/, e.g. a fattened-content
    copy written by a content-fattening step)."""
    return os.path.join(ldbc_dir(sf, variant), "CsvBasic-LongDateFormatter")


def _conv(kind, raw):
    return int(raw) if kind == "i" else raw


def _person_multi(sf, variant=None):
    """{person raw id: {'email': [...], 'language': [...]}} from the two extra CSVs."""
    merged = {}
    for fname, prop in PERSON_MULTI:
        with open(os.path.join(_root(sf, variant), "dynamic", fname), newline="") as f:
            rd = csv.reader(f, delimiter="|")
            next(rd)                                     # header: Person.id|<value>
            for row in rd:
                merged.setdefault(row[0], {}).setdefault(prop, []).append(row[1])
    return merged


def stream(sf="0.1", schema=None, variant=None):
    """Yield ('v', vid, label, props) for every vertex, then ('e', etype, src, dst, props, src_label, dst_label)
    for every edge, lazily.

    The records come from the C++ reader (`kvevaluator records`, byte-identical to the frozen Python parser) when the
    binary exists; KVEVAL_PY_PARSER=1 forces the Python parser. `schema` (a compiled Schema) filters props to what
    that design declares, on either source. `variant`: a data variant's CSV tree (_root).
    """
    if not os.environ.get("KVEVAL_PY_PARSER"):
        cpp = _cpp_binary()
        if cpp:
            yield from _stream_cpp(cpp, sf, schema, variant)
            return
    yield from _stream_py(sf, schema, variant)


def _cpp_binary():
    """The binary that parses the record stream: the build's own, else KVEVAL_CPP_BIN's, else None (then the frozen
    Python parser runs). Not the binary under test first: a control is a dirty build, and its records are not reused."""
    for b in (BUILD_DIR / "cpp" / "kvevaluator", CPP_BIN):
        if b.exists():
            return str(b)
    return None


def _keep_filters(schema):
    if schema is None:
        return None, None
    v_keep = {label: {"id"} | {p for p, _t in cols}
              for label, cols in schema.vertex_prop_types.items()}
    e_keep = {et: {p for p, _t in cols} for et, cols in schema.edge_prop_types.items()}
    return v_keep, e_keep


def _schema_json_checked():
    """The committed schema.json the C++ reader parses with, asserted against the live LDBC_SCHEMA's hash: an edited
    schema without `python3 -m kvevaluator.datasets write-schema ldbc` fails loud.
    """
    import json as _json
    from kvevaluator.datasets import schema_path
    path = schema_path("ldbc")
    with open(path) as f:
        on_disk = _json.load(f)["hash"]
    from kvevaluator.schema.schema_json import to_json
    live = to_json(LDBC_SCHEMA)["hash"]
    if on_disk != live:
        raise RuntimeError(f"{path} is STALE (hash {on_disk[:12]} != live "
                           f"schema {live[:12]}) -- regenerate: python3 -m kvevaluator.datasets write-schema ldbc")
    return path


def _stream_cpp(cpp, sf, schema, variant=None):
    """Records from the C++ reader, with this side's schema filter applied on top. The C++
    reader always parses the CANONICAL structure (like _stream_py does); a reduced design
    is a FILTER here, never a different parse."""
    import json as _json
    import subprocess
    t0 = time.time()
    schema_json = _schema_json_checked()
    v_keep, e_keep = _keep_filters(schema)
    counts = {}
    proc = subprocess.Popen([cpp, "records", "--schema", schema_json, "--root", _root(sf, variant)],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1 << 20)
    done = False
    try:
        for line in proc.stdout:
            rec = _json.loads(line)
            if rec[0] == "v":
                _, v, label, props = rec
                counts[label] = counts.get(label, 0) + 1
                if v_keep is not None:
                    props = {p: x for p, x in props.items() if p in v_keep[label]}
                yield ("v", v, label, props)
            else:
                _, etype, src, dst, props, src_label, dst_label = rec
                counts[etype] = counts.get(etype, 0) + 1
                if e_keep is not None:
                    props = {p: x for p, x in props.items() if p in e_keep.get(etype, ())}
                yield ("e", etype, src, dst, props, src_label, dst_label)
        done = True
    finally:
        proc.stdout.close()
        if done:
            if proc.wait() != 0:
                raise RuntimeError(f"kvevaluator records exited {proc.returncode}")
        else:
            # the consumer abandoned the stream mid-way (islice, an exception upstream):
            # kill the child quietly -- a SIGPIPE death here is expected, not an error
            proc.kill()
            proc.wait()
    if done:
        summary = "  ".join(f"{k}={n}" for k, n in counts.items())
        print(f"[ldbc] sf{sf} streamed (cpp) in {time.time() - t0:.1f}s: {summary}", file=sys.stderr)


def _stream_py(sf, schema, variant=None):
    """The FROZEN Python CSV parser: the reference that re-validates the C++ reader after a schema or data change.
    Do not optimize; keep it byte-faithful.
    """
    t0 = time.time()
    counts = {}
    multi = _person_multi(sf, variant)
    v_keep, e_keep = _keep_filters(schema)
    for subdir, fname, label in VERTEX_FILES:
        fields = _VERTEX_PROPS[label]                    # (name, s/i) from the schema
        with open(os.path.join(_root(sf, variant), subdir, fname), newline="") as f:
            for r in csv.DictReader(f, delimiter="|"):
                props = {"id": int(r["id"])}
                for fld, kind in fields:
                    if r[fld] != "":                     # empty field -> absent prop
                        props[fld] = _conv(kind, r[fld])
                if label == "Person":
                    props.update(multi.get(r["id"], {}))
                counts[label] = counts.get(label, 0) + 1
                if v_keep is not None:
                    props = {p: v for p, v in props.items() if p in v_keep[label]}
                # vid from the GROUP's tag (Message for Post/Comment); label stays concrete.
                yield ("v", vid(_vid_label(label), r["id"]), label, props)
    for subdir, fname, etype, src_label, dst_label in EDGE_FILES:
        propfields = _EDGE_PROPS[etype]                  # (name, s/i) from the schema
        with open(os.path.join(_root(sf, variant), subdir, fname), newline="") as f:
            rd = csv.reader(f, delimiter="|")
            header = next(rd)                            # cols 0,1 = the src/dst id; 2.. carry the
                                                         # edge prop NAMES (the id names repeat, so
                                                         # DictReader can't be used, but props don't)
            # Read each edge prop from its NAMED column, so the schema's prop ORDER need not match
            # the CSV column order -- and a schema edge prop absent from the CSV fails LOUD here
            # (header.index raises), never silently mis-mapped to the wrong column.
            cols = [(p, k, header.index(p)) for p, k in propfields]
            for row in rd:
                props = {p: _conv(k, row[c]) for p, k, c in cols if row[c] != ""}
                if e_keep is not None:
                    props = {p: v for p, v in props.items() if p in e_keep.get(etype, ())}
                counts[etype] = counts.get(etype, 0) + 1
                yield ("e", etype, vid(_vid_label(src_label), row[0]),
                       vid(_vid_label(dst_label), row[1]),
                       props, src_label, dst_label)
    summary = "  ".join(f"{k}={n}" for k, n in counts.items())
    print(f"[ldbc] sf{sf} streamed in {time.time() - t0:.1f}s: {summary}", file=sys.stderr)


def load(sf="0.1", graph=None, schema=None):
    """An in-memory Graph of the stream; `schema` filters props as in stream() (None = unfiltered)."""
    g = graph or Graph()
    for rec in stream(sf, schema):
        if rec[0] == "v":
            _, v, label, props = rec
            g.add_vertex(v, label, props)
        else:
            _, etype, src, dst, props, src_label, dst_label = rec
            g.add_edge(etype, src, dst, props, src_label, dst_label)
    return g
