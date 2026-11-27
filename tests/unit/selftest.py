"""Self-test: on a tiny directed graph (A knows B, C; D knows A), the operators
match the real op semantics with predicted == actual per step on all
three formats, and identical results across formats (plan-correctness).

Two direction cases:
  out  -- the qual headline: read_neighbors(A, knows, out) -> dgraph S1 (posting-list
          iterator), nebula S1 N2 (+prefix), arango S1 N2 (covering edge index).
  both -- the LDBC undirected friends pattern: every format pays both directions
          (dgraph fwd+rev blobs S2; nebula +/- prefixes S2 N3; arango from/to
          indexes S2 N3), matching real /kvstats (nebula prefix=2 verified).
Anchors: dgraph fqid-index SEEK, nebula free (vid is the key), arango pk GET.

Run from the experiment root:  python3 tests/unit/selftest.py
"""
import os
import sys

from kvevaluator.kvstore.kvstore import AbstractKV, CountingKV
from kvevaluator.plans.plan import Step, ref
from kvevaluator.oracle.runner import run_plan, print_report
from kvevaluator.datasets.demo import tiny as tiny_graph, DEMO_SCHEMA
from kvevaluator.formats.dgraph import DgraphFormat
from kvevaluator.formats.nebula import NebulaFormat
from kvevaluator.formats.arangodb import ArangodbFormat
from kvevaluator.formats.dgraph import DgraphSizing


def check_key_bytes():
    """key_bytes hand-computed for one known key of each shape per format, plus the
    tunability of a key width AND a value width. Guards the structural byte model (each
    format's in-file *Sizing dataclass) directly, independent of any plan -- a plan-level
    test only sees SUMS, which can hide an off-by-one that two shapes cancel out."""
    ok = True
    dg, nb, ar = DgraphFormat(DEMO_SCHEMA), NebulaFormat(DEMO_SCHEMA), ArangodbFormat(DEMO_SCHEMA)
    # (format, key, expected) -- expected hand-computed from the DEFAULT model (simplest config:
    # single-tenant, partition ignored, unversioned edges; see each format's *Sizing). Real-width
    # configs are reachable via overrides and exercised by the tunable check below.
    cases = [
        # dgraph: prefix0 + namespace0 (single-tenant) + typeByte1 + attr(real) + (uid8 | term real)
        (dg, ("firstName", "D", "person_933"), (0 + 0 + 1) + 9 + 8),   # scalar: attr 9 + uid 8
        (dg, ("dgraph.type", "I", "Person"),   (0 + 0 + 1) + 11 + 6),  # index: attr 11 + term "Person" 6
        # nebula: part_prefix1 (partition ignored) + ...
        (nb, ("person_933", "Person"),                 1 + 8 + 4),           # vertex: vid + tagId
        (nb, ("person_933", "+knows", "0", "person_1"), 1 + 8 + 4 + 0 + 8 + 0),  # edge (rank 0, placeholder 0)
        (nb, ("i", "Person", "firstName", "Chau", "person_933"), 1 + 4 + 4 + 8),  # index: colval "Chau" 4 + vid 8
        # arango: object_id8 + ... (unaffected by the simplest-config zeroing)
        (ar, ("Person", "d1"),                        8 + 8),               # document
        (ar, ("pk", "Person", "person_933"),          8 + 10),              # pk: _key "person_933" 10
        (ar, ("f", "knows", "person_933", "e1"),      8 + 10 + 8),          # edge index
        (ar, ("i", "Person", "firstName", "Chau", "d1"), 8 + 4 + 8),        # value index
    ]
    for fmt, key, want in cases:
        got = fmt.key_bytes(key)
        if got != want:
            print(f"  ! {fmt.name}.key_bytes({key!r}) = {got} != {want}")
            ok = False
    # tunable KEY width: modeling multi-tenant (namespace 0 -> 8) adds exactly 8 bytes.
    tuned = DgraphFormat(DEMO_SCHEMA, DgraphSizing(namespace=8))
    if tuned.key_bytes(("firstName", "D", "x")) != (0 + 8 + 1) + 9 + 8:
        print("  ! dgraph namespace override did not add 8 bytes")
        ok = False
    # tunable VALUE width: the value twin -- narrowing int 8 -> 4 makes an int value cost 4.
    # (default equals graph.value_bytes, so the seam is a no-op until tuned like this.)
    vt = DgraphFormat(DEMO_SCHEMA, DgraphSizing(int_bytes=4))
    if dg.value_bytes(1234) != 8 or vt.value_bytes(1234) != 4:
        print(f"  ! dgraph value_bytes int width: default {dg.value_bytes(1234)} (want 8), "
              f"tuned {vt.value_bytes(1234)} (want 4)")
        ok = False
    print(f"  byte model (key + value): {'OK' if ok else 'FAILURES'}")
    return ok


def check_packing():
    """The kind-1 value-packing size functions, hand-checked against worked
    examples, plus the on/off dispatch (default off = neutral). Guards the encoding math
    directly, the way check_key_bytes guards the key model."""
    from kvevaluator.formats.dgraph_uidpack import uidpack_bytes
    from kvevaluator.formats.arangodb import vpack_bytes
    from kvevaluator.formats.nebula import rowv2_bytes
    from kvevaluator.formats.arangodb import ArangoSizing
    ok = True

    def want(label, got, exp):
        nonlocal ok
        if got != exp:
            print(f"  ! {label} = {got} != {exp}")
            ok = False

    # dgraph UidPack: the worked example, the header-only singleton, and the per-block header.
    want("uidpack [17,23,31,44,300,305]", uidpack_bytes([17, 23, 31, 44, 300, 305]), 23)
    want("uidpack singleton [42]", uidpack_bytes([42]), 12)   # Base 8 + NumUids 4, no deltas
    want("uidpack empty", uidpack_bytes([]), 0)
    # crossing 256 UIDs opens a second block -> exactly one more 12-byte header.
    want("uidpack block boundary",
         uidpack_bytes(list(range(1, 258))), uidpack_bytes(list(range(1, 257))) + 12)
    # a top-32-bit change forces a NEW block too (dgraph match32MSB), even below 256 UIDs:
    # two singletons -> two 12-byte headers, not one block with a huge (uint32-overflowing) delta.
    want("uidpack MSB split [1, 2**32+5]", uidpack_bytes([1, 2**32 + 5]), 24)
    # arango VPack: the worked example (field names dominate a narrow doc).
    want("vpack {length:3,content:'yes'}", vpack_bytes({"length": 3, "content": "yes"}), 25)
    # nebula RowWriterV2: the fully-present worked example, and the tunable timestamp (0 drops 8).
    want("rowv2 {age:35,lastname:'Smith'}", rowv2_bytes({"age": 35, "lastname": "Smith"}), 31)
    want("rowv2 timestamp=0", rowv2_bytes({"age": 35, "lastname": "Smith"}, 0), 23)

    # on/off: default (pack off) is the neutral content size; pack on routes through VPack.
    doc = {"_key": "person_1", "age": 35}
    off = ArangodbFormat(DEMO_SCHEMA)
    on = ArangodbFormat(DEMO_SCHEMA, ArangoSizing(pack=True))
    from kvevaluator.schema.graph import content_bytes
    want("arango pack OFF = neutral", off.value_bytes(doc), content_bytes(doc))
    # pack on prices a document under the property codec: named = the
    # whole doc as one VPack object; schema (the default) = the system-member object + the props' RowV2 row
    # (header 1 + bitmap 1 + one 8-byte int slot = 10), the C++ store's form
    on.props_codec = "named"
    want("arango pack ON, named = vpack", on.value_bytes(doc), vpack_bytes(doc))
    on.props_codec = "schema"
    want("arango pack ON, schema = system object + row", on.value_bytes(doc), vpack_bytes({"_key": "person_1"}) + 10)
    print(f"  kind-1 packing model: {'OK' if ok else 'FAILURES'}")
    return ok


def check_content_bytes_tagged():
    """A bytes value in its JSON form ({"$bytes": base64}, what a C++ response carries) is sized at its raw length,
    the same as the bytes themselves and as the C++ neutral_bytes; every other value is sized as before."""
    import base64
    from kvevaluator.schema.graph import content_bytes, value_bytes
    ok = True

    def want(label, got, exp):
        nonlocal ok
        if got != exp:
            print(f"  ! {label} = {got} != {exp}")
            ok = False

    blob = bytes(range(256)) + bytes(29)                              # 285 B, non-ASCII included
    tagged = {"$bytes": base64.b64encode(blob).decode("ascii")}      # 380 chars of base64
    want("tagged 285 B blob", content_bytes(tagged), 285)
    want("raw 285 B blob", content_bytes(blob), 285)
    want("the getnode row, tagged", value_bytes([{"_vid": "x" * 16, "ntype": 1, "version": 2, "time": 3,
                                                  "data": tagged}]), 325)
    want("a plain str", content_bytes("héllo"), 5)
    want("a plain row", content_bytes({"a": "abc", "b": 7, "c": True}), 12)
    want("an empty tagged blob", content_bytes({"$bytes": ""}), 0)
    print(f"  tagged bytes sizing: {'OK' if ok else 'FAILURES'}")
    return ok


def check_rank_spaced():
    """LinkBench's params pick k rank-spaced items: k = 1 is the first (p1 at any k), never a division by zero."""
    from kvevaluator.datasets.linkbench.params import _rank_spaced
    items = list(range(10))
    cases = [(1, [0]), (2, [0, 9]), (4, [0, 3, 6, 9]), (10, items), (12, items)]
    bad = [(k, _rank_spaced(items, k), w) for k, w in cases if _rank_spaced(items, k) != w]
    for k, got, w in bad:
        print(f"  ! _rank_spaced(10 items, k={k}) = {got} != {w}")
    print(f"  rank-spaced params: {'OK' if not bad else 'FAILURES'} ({len(cases)} cases)")
    return not bad


def check_schema_compile():
    """The entity-oriented schema builder (schemadef.compile_schema) reproduces the LDBC
    physical design EXACTLY, and the flag validators reject bad declarations.

    GOLD is the byte-for-byte snapshot of LDBC_SCHEMA/LABEL_ONLY_SCHEMA/TYPE_INDEX_SCHEMA
    captured BEFORE the schema.py rewrite. It is an independent golden --
    comparing the compiled schemas to the module constants would be circular now that the
    constants ARE the compiled output. If a future edit changes an index/reverse/group, this
    fails loudly rather than silently moving every counts number."""
    from kvevaluator.datasets.ldbc.schema import LDBC_SCHEMA, LABEL_ONLY_SCHEMA, TYPE_INDEX_SCHEMA
    from kvevaluator.schema import schemadef as sd
    ok = True

    def want(label, got, exp):
        nonlocal ok
        if got != exp:
            print(f"  ! {label}: {got!r} != {exp!r}")
            ok = False

    reverse = {"containerOf", "hasCreator", "hasMember", "hasModerator", "hasTag", "hasType",
               "isLocatedIn", "isPartOf", "isSubclassOf", "knows", "likes", "replyOf"}
    idx = {("Person", "firstName"), ("Place", "name"), ("Tag", "name"), ("TagClass", "name")}
    dates = {("Comment", "creationDate"), ("Person", "creationDate"), ("Post", "creationDate")}
    multi = {("Person", "email"), ("Person", "language")}
    nulls = {("Post", "content"), ("Post", "imageFile"), ("Post", "language")}
    groups = {"Message": ("Post", "Comment")}
    all_labels = {"Person", "Post", "Comment", "Forum", "Organisation", "Place", "Tag", "TagClass"}
    GOLD = {
        "canonical":  (LDBC_SCHEMA, idx, reverse, dates, multi, groups, all_labels),
        "label_only": (LABEL_ONLY_SCHEMA, set(), reverse, set(), multi, groups, all_labels),
        "type_index": (TYPE_INDEX_SCHEMA, idx, reverse, dates, multi, groups, all_labels),
    }
    for name, (s, gi, gr, gd, gm, gg, gt) in GOLD.items():
        want(f"{name}.indexed_cols", set(s.indexed_cols), gi)
        want(f"{name}.reverse_etypes", set(s.reverse_etypes), gr)
        want(f"{name}.date_cols", set(s.date_cols), gd)
        want(f"{name}.multival_cols", set(s.multival_cols), gm)
        want(f"{name}.nullable_cols", set(s.nullable_cols), nulls)   # same across schemas
        want(f"{name}.type_groups", dict(s.type_groups), gg)
        want(f"{name}.type_indexed_labels", set(s.type_indexed_labels), gt)

    # builder validators reject bad declarations (fail loud at author time, not at query time).
    def rejects(label, thunk):
        nonlocal ok
        try:
            thunk()
            print(f"  ! {label}: expected ValueError, got none")
            ok = False
        except ValueError:
            pass
    _tv = [sd.vertex("T", "t", [sd.prop("s", sd.STR), sd.prop("n", sd.INT)])]   # a fixture
    rejects("unknown type", lambda: sd.prop("x", "float"))
    rejects("unknown index kind", lambda: sd.index("T", "s", "hash"))
    rejects("SORTED on a STR column",
            lambda: sd.compile_schema(_tv, [], [sd.index("T", "s", sd.SORTED)]))
    rejects("index on undeclared column",
            lambda: sd.compile_schema(_tv, [], [sd.index("T", "missing")]))
    rejects("duplicate prop", lambda: sd.compile_schema(
        [sd.vertex("T", "t", [sd.prop("a", sd.STR), sd.prop("a", sd.INT)])], []))
    # KIND INFERRED FROM TYPE: a STR column -> equality (indexed_cols), a numeric column ->
    # sorted (date_cols) -- no kind passed.
    _s = sd.compile_schema(_tv, [], [sd.index("T", "s")])
    want("STR index inferred VALUE", (set(_s.indexed_cols), set(_s.date_cols)),
         ({("T", "s")}, set()))
    _i = sd.compile_schema(_tv, [], [sd.index("T", "n")])
    want("INT index inferred SORTED", (set(_i.indexed_cols), set(_i.date_cols)),
         (set(), {("T", "n")}))
    # explicit override: an equality-only index on a numeric column.
    want("VALUE override on INT",
         set(sd.compile_schema(_tv, [], [sd.index("T", "n", sd.VALUE)]).indexed_cols),
         {("T", "n")})

    print(f"  schema compile gate: {'OK' if ok else 'FAILURES'}")
    return ok


def check_schema_parser():
    """The .schema text parser. The happy path over the REAL files is already covered by the
    compile gate (it parses all three ldbc*.schema into the golden Schemas); this pins a small
    round-trip and the ERROR paths (each malformed line must fail with a file:line message)."""
    import os
    import tempfile
    from kvevaluator.schema.schema_txt import parse_schema
    ok = True

    def parse(text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "t.schema")
        with open(p, "w") as f:
            f.write(text)
        try:
            return parse_schema(p)
        finally:
            os.remove(p)
            os.rmdir(d)

    def want(label, cond):
        nonlocal ok
        if not cond:
            print(f"  ! {label}")
            ok = False

    def rejects(label, text):
        nonlocal ok
        try:
            parse(text)
            print(f"  ! {label}: expected ValueError, got none")
            ok = False
        except ValueError:
            pass

    v, e, i, g, adj = parse("vertex Person prefix=person\n"
                       "    name str\n"
                       "    lastName str\n"
                       "    tags str multi\n"
                       "edge likes reverse\n"
                       "    Person->Post Person->Comment\n"
                       "    at date\n"
                       "index Person.name\n"
                       "index Person\n"
                       "group g1 Person.name Person.lastName\n"
                       "adj_sort_facet likes.at\n"
                       "adj_sort likes Person.name\n")
    want("vertex parsed", len(v) == 1 and v[0].label == "Person" and v[0].prefix == "person")
    want("multi prop parsed", any(p.name == "tags" and p.multi for p in v[0].props))
    want("edge reverse + 2 pairs", len(e) == 1 and e[0].reverse
         and set(e[0].between) == {("Person", "Post"), ("Person", "Comment")})
    want("edge prop parsed", bool(e[0].props) and e[0].props[0].name == "at")
    want("property + type index", len(i) == 2 and i[0].prop == "name" and i[1].prop is None)
    want("group parsed", len(g) == 1 and g[0].name == "g1"
         and set(g[0].cols) == {("Person", "name"), ("Person", "lastName")})
    want("adj_sort_facet parsed (dotted)", ("sort_facet", "likes", "at", None, "in") in adj)
    want("adj_sort parsed (neighbor-type qualified)", ("sort", "likes", "name", "Person", "in") in adj)
    rejects("adj_sort direction token must be in|out",
            "edge likes reverse\nadj_sort likes Person.name sideways\n")
    _, _, _, _, adj_out = parse("edge likes reverse\nadj_sort likes Person.name out\n")
    want("adj_sort direction parsed", ("sort", "likes", "name", "Person", "out") in adj_out)
    rejects("adj_sort_facet needs a dot",
            "edge likes reverse\n    at date\nadj_sort_facet likes at\n")
    rejects("adj_sort needs a dotted neighbor type",
            "edge likes reverse\nadj_sort likes name\n")

    # a group naming the SAME shared predicate per-label (Post.content Comment.content) dedups to ONE
    # predicate; must compile, not raise "two groups".
    from kvevaluator.schema.schemadef import compile_schema
    gv, ge, gi, gg, ga = parse("vertex Post prefix=post\n    content str\n"
                               "vertex Comment prefix=comment\n    content str\n"
                               "group g1 Post.content Comment.content\n")
    gsch = compile_schema(gv, ge, gi, gg, ga)
    want("shared-predicate group dedups to 1 predicate",
         gsch.prop_groups.get("g1") == frozenset({"content"}))

    rejects("unknown prop type", "vertex T prefix=t\n    x float\n")
    rejects("vertex needs prefix", "vertex T\n    x str\n")
    rejects("indented with no block", "    x str\n")
    rejects("unknown top keyword", "schema foo\n")
    rejects("index bad arity", "vertex T prefix=t\n    x str\nindex T.x extra\n")
    rejects("group too few columns", "vertex T prefix=t\n    x str\ngroup g1 T.x\n")
    rejects("group column not Label.prop", "vertex T prefix=t\n    x str\n    y str\ngroup g1 x y\n")

    # include: a feature schema pulls in a base (shared index set defined once) + adds its own.
    import shutil
    d = tempfile.mkdtemp()
    open(os.path.join(d, "base.schema"), "w").write(
        "vertex Person prefix=person\n    name str\n    lastName str\nindex Person\n")
    incp = os.path.join(d, "feat.schema")
    open(incp, "w").write("include base\ngroup g1 Person.name Person.lastName\n")
    iv, ie, ii, ig, ia = parse_schema(incp)   # default resolver: <name>.schema in the same dir
    want("include merges base + own decl", len(iv) == 1 and iv[0].label == "Person"
         and len(ig) == 1 and ig[0].name == "g1")
    shutil.rmtree(d)
    print(f"  schema text parser: {'OK' if ok else 'FAILURES'}")
    return ok


def check_schema_vs_loader():
    """DRIFT GUARD (Phase 2 slim): the loader now DERIVES its prop lists + types from the schema
    (_VERTEX_PROPS/_EDGE_PROPS off schema.VERTICES/EDGES), so those can no longer drift -- checking
    them would be schema-vs-schema. What still lives in BOTH the schema and loader.VERTEX_FILES/
    EDGE_FILES is the FILE MAPPING + edge ENDPOINTS (the loader needs file<->(etype,src,dst) for
    arango _from/_to), so this checks they cover the same types and the same endpoints. Person's
    multi props are checked against PERSON_MULTI (its filenames are loader-only; the prop names
    duplicate the schema's multi=True flag)."""
    from kvevaluator.datasets.ldbc import loader as L
    from kvevaluator.datasets.ldbc import schema as S
    ok = True

    def want(label, got, exp):
        nonlocal ok
        if got != exp:
            print(f"  ! {label}: {got!r} != {exp!r}")
            ok = False

    # coverage: every schema vertex label has exactly one CSV file mapping, and vice versa.
    want("vertex labels", {lb for _s, _f, lb in L.VERTEX_FILES}, {v.label for v in S.VERTICES})

    # edge types + endpoints: EDGE_FILES groups files by etype; schema.between is the same set of
    # (src, dst) pairs. These are the one thing Option-B keeps in two places, so guard it.
    file_pairs = {}
    for _s, _f, etype, src, dst in L.EDGE_FILES:
        file_pairs.setdefault(etype, set()).add((src, dst))
    schema_pairs = {e.etype: set(e.between or ()) for e in S.EDGES}
    want("edge types", set(file_pairs), set(schema_pairs))
    for etype in set(file_pairs) & set(schema_pairs):
        want(f"edge {etype} endpoints", file_pairs[etype], schema_pairs[etype])

    # Person multi props <-> PERSON_MULTI filenames; and multi props exist ONLY on Person (the
    # loader materializes multi values only there, so a multi prop elsewhere would be dropped).
    vmap = {v.label: v for v in S.VERTICES}
    want("Person multi props",
         {p.name for p in vmap["Person"].props if p.multi},
         {prop for _fname, prop in L.PERSON_MULTI})
    want("multi props exist only on Person",
         {v.label for v in S.VERTICES if any(p.multi for p in v.props)}, {"Person"})

    # Golden tripwire on the loader's DERIVED prop tables (name + s/i kind). Prop NAMES are already
    # guarded loud (a wrong name KeyErrors at stream time), but a prop TYPE typo on a NON-indexed
    # column (Person.birthday DATE->STR, Post.length INT->STR) is SILENT -- it only flips int<->str
    # loading, changes no Schema field, so the compile gate can't see it. This hash catches it. On a
    # legitimate prop name/type change, recompute: sha256 of repr(sorted(_VERTEX_PROPS.items())) +
    # repr(sorted(_EDGE_PROPS.items())).
    import hashlib
    blob = repr(sorted(L._VERTEX_PROPS.items())) + repr(sorted(L._EDGE_PROPS.items()))
    want("derived prop tables (name+kind) golden", hashlib.sha256(blob.encode()).hexdigest(),
         "685500048ba27833b8b86d09098fca1209c891ea5660d8bbcbcc322fd1bcfe27")

    print(f"  schema-vs-loader drift guard: {'OK' if ok else 'FAILURES'}")
    return ok


def check_arango_coll_map():
    """Regression: every arango document has a pk entry, edges too, and _ensure_coll rebuilds
    vid -> collection by scanning the whole ("pk",) family -- an edge's entry must not be read as a vertex.
    The map a FRESH format rebuilds from the store must equal the one the ingest filled."""
    g = tiny_graph()
    raw = AbstractKV()
    built = ArangodbFormat(DEMO_SCHEMA)
    built.materialize(g, raw)
    n_edge_pk, cur = 0, raw.seek(("pk",))
    while cur.valid() and cur.key()[0] == "pk":
        n_edge_pk += cur.key()[1] in DEMO_SCHEMA.etypes
        cur.next()
    fresh = ArangodbFormat(DEMO_SCHEMA)
    fresh._ensure_coll(raw)
    ok = fresh._coll == built._coll and n_edge_pk > 0
    print(f"  arango vid->collection rebuilt from the pk family ({len(fresh._coll)} vertices, {n_edge_pk} edge pk "
          f"entries skipped): {'OK' if ok else 'DIFFERS'}")
    return ok


def run(fmt, plan, g):
    raw = AbstractKV()
    fmt.materialize(g, raw)                  # setup (uncounted)
    store = CountingKV(raw, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes)
    return run_plan(plan, fmt, store)


def plan_for(direction):
    return [
        Step("a",       "read_vertex_by_id", vid="A", label="Person"),
        Step("friends", "read_neighbors", srcs=ref("a"), etype="knows", dir=direction),
        Step("names",   "read_vertex_props", vids=ref("friends"), props=["name"], label="Person"),
    ]


# expected actual (S, G, N) per step, per format and direction case.
# out: A has 2 out-read_neighbors. both: 2 out + 1 in = 3 (read_vertex_props scales with them).
# dgraph read_vertex_by_id is 0 SEEK since int vids landed: the vid IS the uid, so the
# SIM anchor is a direct address -- no fqid/id index lookup (_id_as_uid=True). The ENGINE twin still
# pays its 1 SEEK; that lives in DgraphEngineFormat, not here.
EXPECTED = {
    "out": {
        "dgraph":   {"read_vertex_by_id": (0, 0, 0), "read_neighbors": (1, 0, 0), "read_vertex_props": (0, 2, 0)},
        "nebula":   {"read_vertex_by_id": (0, 0, 0), "read_neighbors": (1, 0, 2), "read_vertex_props": (0, 2, 0)},
        "arangodb": {"read_vertex_by_id": (0, 1, 0), "read_neighbors": (1, 0, 2), "read_vertex_props": (0, 4, 0)},
    },
    "both": {
        "dgraph":   {"read_vertex_by_id": (0, 0, 0), "read_neighbors": (2, 0, 0), "read_vertex_props": (0, 3, 0)},
        "nebula":   {"read_vertex_by_id": (0, 0, 0), "read_neighbors": (2, 0, 3), "read_vertex_props": (0, 3, 0)},
        "arangodb": {"read_vertex_by_id": (0, 1, 0), "read_neighbors": (2, 0, 3), "read_vertex_props": (0, 6, 0)},
    },
}
EXPECTED_FRIENDS = {"out": ["B", "C"], "both": ["B", "C", "D"]}


def main():
    g = tiny_graph()
    ok = True
    for direction in ("out", "both"):
        print(f"\n########## read_neighbors dir={direction} ##########")
        plan = plan_for(direction)
        friends_by_format = {}
        for fmt in (DgraphFormat(DEMO_SCHEMA), NebulaFormat(DEMO_SCHEMA), ArangodbFormat(DEMO_SCHEMA)):
            outputs, kvcosts = run(fmt, plan, g)
            print_report(fmt.name, kvcosts)
            friends_by_format[fmt.name] = sorted(outputs["friends"])

            for step, pred, act in kvcosts:
                # oracle: predicted == actual (S/G/N) for every step
                if pred.triple() != act.triple():
                    print(f"  ! {fmt.name}.{step.op}: predicted {pred.triple()} "
                          f"!= actual {act.triple()}")
                    ok = False
                # the pinned op semantics per format
                want = EXPECTED[direction][fmt.name][step.op]
                if act.triple() != want:
                    print(f"  ! {fmt.name}.{step.op} {act.triple()} != expected {want}")
                    ok = False

        # plan-correctness: all formats must return the same friends
        print("\n  cross-format friends:", friends_by_format)
        distinct = {tuple(fr) for fr in friends_by_format.values()}
        if len(distinct) != 1 or friends_by_format["dgraph"] != EXPECTED_FRIENDS[direction]:
            print(f"  ! results differ across formats or != {EXPECTED_FRIENDS[direction]}")
            ok = False

    # Edge-prop reads + pairs + a free op: for dgraph/nebula edge props are
    # covered (facets in the blob / edge value) — shape and bytes change, ops
    # don't; arango pays 1 GET per edge (the edge doc; the covering index value
    # only holds the neighbor id). top_k is 0 KV.
    print("\n########## read_neighbors edge_props + pairs (covered read) ##########")
    eplan = [
        Step("a",   "read_vertex_by_id", vid="A", label="Person"),
        Step("fr",  "read_neighbors", srcs=ref("a"), etype="knows", dir="both",
             edge_props=["since"], pairs=True),
        Step("top", "top_k", rows=ref("fr"), keys=[("since", "desc")], k=2),
    ]
    want_top = [{"src": "A", "dst": "C", "since": 2025},
                {"src": "A", "dst": "B", "since": 2021}]
    EPROPS_EXPECTED = {"dgraph": (2, 0, 0), "nebula": (2, 0, 3), "arangodb": (2, 3, 3)}
    for fmt in (DgraphFormat(DEMO_SCHEMA), NebulaFormat(DEMO_SCHEMA), ArangodbFormat(DEMO_SCHEMA)):
        outputs, kvcosts = run(fmt, eplan, g)
        print_report(fmt.name, kvcosts)
        for step, pred, act in kvcosts:
            if pred.triple() != act.triple():
                print(f"  ! {fmt.name}.{step.op}: predicted {pred.triple()} "
                      f"!= actual {act.triple()}")
                ok = False
        nb = next(act for step, _p, act in kvcosts if step.op == "read_neighbors")
        if nb.triple() != EPROPS_EXPECTED[fmt.name]:
            print(f"  ! {fmt.name}: edge_props read_neighbors {nb.triple()} != "
                  f"{EPROPS_EXPECTED[fmt.name]}")
            ok = False
        if outputs["top"] != want_top:
            print(f"  ! {fmt.name} top_k rows wrong: {outputs['top']}")
            ok = False

    # read_index_eq (the formats' index layouts split) + read_filter_by_label_probe (dgraph pays
    # a type-list SEEK per vid, nebula a tag-probe GET per vid whose tag no earlier read named, arango nothing).
    # "only2" probes the same vids twice over: a repeated vid costs nothing more (one probe per distinct vid), so
    # it must cost what "only" costs, and keep every occurrence.
    print("\n########## read_index_eq + read_filter_by_label_probe ##########")
    iplan = [
        Step("tags",  "read_index_eq", label="Tag", prop="name", value="cool"),
        Step("mixed", "concat", a=ref("tags"), b=["tagclass_1"]),
        Step("only",  "read_filter_by_label_probe", vids=ref("mixed"), label="Tag"),
        Step("twice", "concat", a=ref("mixed"), b=ref("mixed")),
        Step("only2", "read_filter_by_label_probe", vids=ref("twice"), label="Tag"),
    ]
    IDX_EXPECTED = {
        "dgraph":   {"read_index_eq": (1, 0, 0), "read_filter_by_label_probe": (3, 0, 0)},
        # the two vids out of Tag's own index carry their tag (labels on rows): only the bare id is probed
        "nebula":   {"read_index_eq": (1, 0, 2), "read_filter_by_label_probe": (0, 1, 0)},
        "arangodb": {"read_index_eq": (1, 2, 2), "read_filter_by_label_probe": (0, 0, 0)},
    }
    for fmt in (DgraphFormat(DEMO_SCHEMA), NebulaFormat(DEMO_SCHEMA), ArangodbFormat(DEMO_SCHEMA)):
        outputs, kvcosts = run(fmt, iplan, g)
        print_report(fmt.name, kvcosts)
        for step, pred, act in kvcosts:
            if pred.triple() != act.triple():
                print(f"  ! {fmt.name}.{step.op}: predicted {pred.triple()} "
                      f"!= actual {act.triple()}")
                ok = False
            want = IDX_EXPECTED[fmt.name].get(step.op)
            if want and act.triple() != want:
                print(f"  ! {fmt.name}.{step.op} {act.triple()} != expected {want}")
                ok = False
        if sorted(outputs["tags"]) != ["tag_1", "tag_2"] or \
                sorted(outputs["only"]) != ["tag_1", "tag_2"] or \
                sorted(outputs["only2"]) != ["tag_1", "tag_1", "tag_2", "tag_2"]:
            print(f"  ! {fmt.name} index/type-filter results wrong: "
                  f"{outputs['tags']} / {outputs['only']} / {outputs['only2']}")
            ok = False

    # read_index_range: value-RANGE scan of the day-indexed creationDate (the IC3 access
    # path). Persons sit on days 100/102/104/106; [day101, day105) -> B(102), C(104).
    # dgraph walks day tokens (1 SEEK + 1 NEXT per token; the gaps keep it exact),
    # nebula reads the vid from the KEY (0 GETs), arango pays 1 doc GET per match
    # (docid -> _key). `labels` scans each tag/collection index (Post+Comment for
    # messages); the multi-label UNION path is exercised by the IC3 run.
    print("\n########## read_index_range (creationDate window) ##########")
    d = 86400000
    rplan = [Step("w", "read_index_range", labels=["Person"], prop="creationDate",
                  lo=101 * d, hi=105 * d)]
    RANGE_EXPECTED = {"dgraph": (1, 0, 2), "nebula": (1, 0, 2), "arangodb": (1, 2, 2)}
    for fmt in (DgraphFormat(DEMO_SCHEMA), NebulaFormat(DEMO_SCHEMA), ArangodbFormat(DEMO_SCHEMA)):
        outputs, kvcosts = run(fmt, rplan, g)
        print_report(fmt.name, kvcosts)
        for step, pred, act in kvcosts:
            if pred.triple() != act.triple():
                print(f"  ! {fmt.name}.{step.op}: predicted {pred.triple()} "
                      f"!= actual {act.triple()}")
                ok = False
            if act.triple() != RANGE_EXPECTED[fmt.name]:
                print(f"  ! {fmt.name}.{step.op} {act.triple()} != "
                      f"expected {RANGE_EXPECTED[fmt.name]}")
                ok = False
        if sorted(outputs["w"]) != ["B", "C"]:
            print(f"  ! {fmt.name} read_index_range result {sorted(outputs['w'])} != ['B', 'C']")
            ok = False

    print("\n########## key_bytes structural model ##########")
    ok = check_key_bytes() and ok

    print("\n########## kind-1 value packing ##########")
    ok = check_packing() and ok
    ok = check_content_bytes_tagged() and ok
    ok = check_rank_spaced() and ok

    print("\n########## schema text parser ##########")
    ok = check_schema_parser() and ok

    print("\n########## schema builder compiles to the LDBC design ##########")
    ok = check_schema_compile() and ok

    print("\n########## schema and loader describe the same dataset ##########")
    ok = check_schema_vs_loader() and ok
    ok = check_arango_coll_map() and ok

    print("\n" + ("ALL CHECKS PASSED" if ok else "FAILURES"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
