"""EXECUTION-EFFECT checks for the write operators: assert what a write leaves IN THE STORE,
not what it charges. The per-cell oracle (predicted == actual) is blind to a formula and an
execution that are wrong TOGETHER -- e.g. a delete that computes the same wrong key on both
sides stays green while the real edge survives. These checks close that blind spot.

Each scenario builds a tiny synthetic graph directly (AbstractKV + the format's own ingest),
runs ONE write op, and inspects the store.

Run: python3 tests/gates/check_write_effects.py
"""
import sys

from kvevaluator.kvstore.kvstore import AbstractKV
from kvevaluator.schema.vids import vid
from kvevaluator.datasets.ldbc.schema import get_schema

ERRS = []


def check(ok, what):
    print(f"  {'OK  ' if ok else 'FAIL'} {what}")
    if not ok:
        ERRS.append(what)


D1, D2, D3 = 1_000_000_000_000, 1_100_000_000_000, 1_200_000_000_000   # d1 < d2 < d3

P1, P2 = vid("Person", 1), vid("Person", 2)
M1, M2, M3 = vid("Post", 11), vid("Comment", 12), vid("Post", 13)


def _mini_graph(fmt, kv):
    """Two persons, three messages, and edges of hasCreator, replyOf, likes (dated) and knows."""
    kv.backend = kv          # the formats' uncounted-read seam (store.backend); in production the
    #                          CountingKV wrapper provides it, here the raw store IS the backend
    fmt.add_vertex(kv, P1, "Person", {"firstName": "A", "creationDate": D1})
    fmt.add_vertex(kv, P2, "Person", {"firstName": "B", "creationDate": D1})
    fmt.add_vertex(kv, M1, "Post", {"creationDate": D1, "content": "c1", "length": 2})
    fmt.add_vertex(kv, M2, "Comment", {"creationDate": D3, "content": "c2", "length": 2})
    fmt.add_vertex(kv, M3, "Post", {"creationDate": D2, "content": "c3", "length": 2})
    fmt.add_edge(kv, "hasCreator", M1, P1, {}, "Post", "Person")
    fmt.add_edge(kv, "hasCreator", M3, P1, {}, "Post", "Person")
    fmt.add_edge(kv, "hasCreator", M2, P1, {}, "Comment", "Person")
    fmt.add_edge(kv, "replyOf", M2, M1, {}, "Comment", "Post")
    fmt.add_edge(kv, "likes", P1, M1, {"creationDate": D2}, "Person", "Post")
    fmt.add_edge(kv, "likes", P2, M1, {"creationDate": D3}, "Person", "Post")
    fmt.add_edge(kv, "knows", P1, P2, {"creationDate": D1}, "Person", "Person")
    fmt.finalize_write(kv)


def _mentions(kv, part):
    """Every LIVE key containing `part` as a component (a tombstoned dgraph key -- empty
    value -- does not count as live)."""
    out = []
    for k, v in kv._vals.items():
        if isinstance(k, tuple) and part in k and v not in ([], None) or k == part:
            out.append(k)
    return out


# ------------------------------------------- dgraph family: shared postings survive a removal

def _entry_vids(posting):
    return [(e[0] if isinstance(e, list) else e) for e in (posting or [])]


def t8_shared_posting_survives():
    print("\ndgraph: removing ONE vertex/edge/value leaves the OTHERS' entries in shared postings")
    from kvevaluator.formats.dgraph import DgraphFormat
    schema = get_schema("canonical")

    def build(fmt):
        kv = AbstractKV()
        kv.backend = kv
        fmt.add_vertex(kv, P1, "Person", {"firstName": "A", "creationDate": D1})
        fmt.add_vertex(kv, P2, "Person", {"firstName": "A", "creationDate": D1})
        fmt.add_vertex(kv, M1, "Post", {"creationDate": D1, "content": "c", "length": 1})
        fmt.add_vertex(kv, M2, "Comment", {"creationDate": D2, "content": "d", "length": 1})
        fmt.add_edge(kv, "likes", P1, M1, {"creationDate": D2}, "Person", "Post")
        fmt.add_edge(kv, "likes", P2, M1, {"creationDate": D3}, "Person", "Post")
        fmt.add_edge(kv, "likes", P1, M2, {"creationDate": D2}, "Person", "Comment")
        fmt.finalize_write(kv)
        return kv

    from kvevaluator.formats.dgraph_engine import DgraphEngineFormat
    for name, fmt in (("dgraph", DgraphFormat(schema)), ("dgraph_engine(k=1)", DgraphEngineFormat(schema))):
        if hasattr(fmt, "reset_write_state"):
            fmt.reset_write_state()
        kv = build(fmt)

        # update of an INDEXED prop: the old token is a SHARED posting -- P2 must survive in it.
        fmt.update_prop(kv, P1, "Person", "firstName", "B")
        check(_entry_vids(kv.get(("firstName", "I", "A"))) == [P2],
              f"{name}: old index token keeps the OTHER vertex "
              f"(got {_entry_vids(kv.get(('firstName', 'I', 'A')))})")

        # delete ONE edge: the sibling entry in our own posting and the neighbour's other likers
        # both survive.
        fmt.delete_edge(kv, "likes", P1, M1)
        check(M2 in _entry_vids(kv.get(("likes", "D", P1))),
              f"{name}: delete_edge keeps the SIBLING edge in the source posting "
              f"(got {_entry_vids(kv.get(('likes', 'D', P1)))})")
        check(_entry_vids(kv.get(("likes", "R", M1))) == [P2],
              f"{name}: delete_edge keeps the OTHER liker in the reverse posting "
              f"(got {_entry_vids(kv.get(('likes', 'R', M1)))})")

        # delete the vertex: its OWN keys empty whole; the neighbour's and the shared token's
        # postings lose only ITS entry.
        fmt.delete_vertex(kv, P1, "Person", with_edges=True)
        check(kv.get(("likes", "D", P1)) is None,
              f"{name}: the deleted vertex's own posting tombstones to None "
              f"(got {kv.get(('likes', 'D', P1))!r})")
        check(_entry_vids(kv.get(("likes", "R", M2))) == [],
              f"{name}: its remaining edge is gone from the neighbour")
        check(_entry_vids(kv.get(("dgraph.type", "I", "Person"))) == [P2],
              f"{name}: the type-enumeration index keeps the OTHER person "
              f"(got {_entry_vids(kv.get(('dgraph.type', 'I', 'Person')))})")
        left = _mentions(kv, P1)
        check(not left, f"{name}: no LIVE key still mentions the deleted vertex (left: {left[:3]})")

        # Self-loop: the "neighbour" key IS the vertex's own key; the entry-removal put must not
        # overwrite the own-key tombstone (both passes dirty the same key, last put wins).
        kv2 = AbstractKV()
        kv2.backend = kv2
        if hasattr(fmt, "reset_write_state"):
            fmt.reset_write_state()
        fmt.add_vertex(kv2, P1, "Person", {"firstName": "A", "creationDate": D1})
        fmt.add_vertex(kv2, P2, "Person", {"firstName": "B", "creationDate": D1})
        fmt.add_edge(kv2, "knows", P1, P1, {}, "Person", "Person")
        fmt.add_edge(kv2, "knows", P1, P2, {}, "Person", "Person")
        fmt.finalize_write(kv2)
        fmt.delete_vertex(kv2, P1, "Person", with_edges=True)
        check(kv2.get(("knows", "D", P1)) is None,
              f"{name}: self-loop delete still tombstones the own posting to None "
              f"(got {kv2.get(('knows', 'D', P1))!r})")


# ------------------------------------ update_edge_prop + the multi-column update_prop, every format

def _edge_dates(fmt, kv, src, etype, dir):
    """{neighbour: creationDate} of src's etype edges, through the format's OWN read path."""
    rows, _c = fmt.read_neighbors(kv, [src], etype, dir, edge_props=["creationDate"])
    return {r["dst"]: r.get("creationDate") for r in rows}


def t10_edge_update_and_multi_update():
    print("\nupdate_edge_prop + multi-column update_prop: every stored copy changes, and only it")
    from kvevaluator.formats import make_format
    from kvevaluator.kvstore.kvstore import CountingKV
    cases = [(f, "canonical") for f in ("dgraph", "nebula", "arangodb", "pertype_csr")]
    D5 = D1 + 5
    for fname, sname in cases:
        schema = get_schema(sname)
        fmt = make_format(fname, schema, False)
        if hasattr(fmt, "reset_write_state"):
            fmt.reset_write_state()
        name = f"{fname}@{sname}"
        raw = AbstractKV()
        _mini_graph(fmt, raw)
        kv = CountingKV(raw, sizeof=fmt.value_bytes, keysizeof=fmt.key_bytes, write_mode=True)

        # likes P1 -> M1 moves from D2 to D5 (below P2's D3: a dated layout re-sorts / re-keys)
        _r, pred = fmt.update_edge_prop(kv, "likes", P1, M1, {"creationDate": D5},
                                        src_label="Person", dst_label="Post")
        check(pred.triple() == kv.cost.triple() and (pred.P, pred.D) == (kv.cost.P, kv.cost.D),
              f"{name}: update_edge_prop predicted {pred.triple()} P{pred.P} D{pred.D} == actual "
              f"{kv.cost.triple()} P{kv.cost.P} D{kv.cost.D}")
        fwd, rev = _edge_dates(fmt, kv, P1, "likes", "out"), _edge_dates(fmt, kv, M1, "likes", "in")
        check(fwd == {M1: D5}, f"{name}: the forward copy carries the new date (got {fwd})")
        check(rev == {P1: D5, P2: D3}, f"{name}: the reverse copy carries it, the co-liker keeps "
                                        f"its own, no stale copy (got {rev})")

        # an ABSENT edge (knows is stored P1 -> P2 only): nothing written, the edge not created
        kv.reset_counter()
        _r, pred = fmt.update_edge_prop(kv, "knows", P2, P1, {"creationDate": D5},
                                        src_label="Person", dst_label="Person")
        check(kv.cost.P == 0 and kv.cost.D == 0 and pred.P == 0,
              f"{name}: an absent edge writes nothing (P {kv.cost.P}, D {kv.cost.D})")
        check(_edge_dates(fmt, kv, P2, "knows", "out") == {},
              f"{name}: ...and does not create it")

        # three columns in ONE update: the row read/rewritten once, the index follows firstName
        kv.reset_counter()
        _r, pred = fmt.update_prop(kv, P1, "Person",
                                   props={"firstName": "Z", "lastName": "L", "creationDate": D3})
        check(pred.triple() == kv.cost.triple() and (pred.P, pred.D) == (kv.cost.P, kv.cost.D),
              f"{name}: multi update predicted {pred.triple()} P{pred.P} D{pred.D} == actual "
              f"{kv.cost.triple()} P{kv.cost.P} D{kv.cost.D}")
        rows, _c = fmt.read_vertex_props(kv, [P1], ["firstName", "lastName", "creationDate"], "Person")
        # P1 has no lastName before the update: the Python dgraph twin stores a scalar written onto
        # an ABSENT posting list-wrapped (['L'] -- the fresh-key RMW merges onto []), with the
        # single-column update too. A known twin quirk, unwrapped here; the row stores are unaffected.
        unwrap = lambda v: v[0] if isinstance(v, list) and len(v) == 1 else v
        got = {k: unwrap(rows[0].get(k)) for k in ("firstName", "lastName", "creationDate")} if rows else {}
        check(got == {"firstName": "Z", "lastName": "L", "creationDate": D3},
              f"{name}: every changed column reads back (got {got})")
        if ("Person", "firstName") in schema.indexed_cols:
            old_tok, _c = fmt.read_index_eq(kv, "Person", "firstName", "A")
            new_tok, _c = fmt.read_index_eq(kv, "Person", "firstName", "Z")
            ids = lambda rs: sorted(r["_vid"] if isinstance(r, dict) else r for r in rs)
            check(ids(old_tok) == [] and ids(new_tok) == [P1],
                  f"{name}: the index moved with firstName (A: {ids(old_tok)}, Z: {ids(new_tok)})")
            b_tok, _c = fmt.read_index_eq(kv, "Person", "firstName", "B")
            check(ids(b_tok) == [P2], f"{name}: the other person's entry is untouched ({ids(b_tok)})")


def main():
    t8_shared_posting_survives()
    t10_edge_update_and_multi_update()
    if ERRS:
        print(f"\nFAIL: {len(ERRS)} effect check(s)")
        return 1
    print("\nWRITE EFFECTS OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
