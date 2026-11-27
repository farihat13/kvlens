"""kvsize -- stored-size breakdown per format, on the C++ evaluator's stores.

How many bytes each format's ingested store spends, split by the ROLE of every key
(vertex / edge / index-vertex / index-edge), each as (key bytes, value bytes): the C++ LOGICAL size (the encoded
bytes the ingest wrote, before SST framing and codec; `kvevaluator size --roles`, cached by
store_identity.store_roles) beside the PHYSICAL on-disk SST bytes.

  python3 -m kvevaluator.measure.kvsize --sf 0.1
  python3 -m kvevaluator.measure.kvsize --sf 0.1 --detail          # per sub-category too
  python3 -m kvevaluator.measure.kvsize --sf 0.1 --schema label_only --rebuild
  python3 -m kvevaluator.measure.kvsize --sf 0.1 --store meta-props_split_adj_chunked   # + an existing store, as it is

No query runs -- each store is scanned once (and cached). `index-edge` is the tell for the edge-storage asymmetry:
non-zero for arango (its f/t covering indexes), zero for dgraph/nebula (both edge directions are stored as edge
DATA, not a separate index).
"""
import argparse
import json
import os
import subprocess
import sys


from kvevaluator.capture.binary import binary_path
from kvevaluator.measure.stores import store_dir, ensure_store, store_chunk
from kvevaluator.capture.store_identity import store_roles
from kvevaluator.datasets.ldbc.schema import SCHEMAS

HEADLINES = ("vertex", "edge", "index-vertex", "index-edge", "other")


def _pct(part, whole):
    return 100.0 * part / whole if whole else 0.0


# Structural count-equalities that hold for ANY dataset (each format writes these
# categories at a fixed ratio: one per vertex, or one per edge in each direction). They
# are an INDEPENDENT guard against a misroute -- the byte cross-checks (sum-to-grand, empty
# "other") are blind to a key sent to the WRONG non-"other" bucket, but a misroute changes
# a sub-category's COUNT and breaks one of these. NOT exhaustive (a vertex-prop misrouted to
# an unrelated bucket with no partner isn't covered) -- a cheap sanity net, not a proof.
_COUNT_EQUALITIES = {
    "nebula":   [("edge-fwd", "edge-rev")],                              # both directions / edge
    "arangodb": [("idx-edge-fwd", "idx-edge-rev", "edge-doc"),           # one of each / edge
                 ("idx-pk", "vertex-doc")],                             # one pk / vertex
    # dgraph SIM stores no fqid/id records since `_id_as_uid` (the vid IS the uid; the
    # engine variant still builds them but is not swept here), so the old one-per-vertex
    # triple (vertex-type-record / vertex-id-record / idx-id-lookup) no longer holds and
    # no cheap per-vertex count-equality remains for dgraph.
    "dgraph":   [],
    # pertype_csr stores CHUNKS, not per-entity keys, so no key-count equality exists; the
    # entity/edge cross-arm check uses the format's own ingest_counts (the R1 driver).
    "pertype_csr":  [],
}


def _structural_mismatches(db, roles):
    """[(subs, counts)] for each declared count-equality that FAILS (empty = all hold)."""
    counts = {sub: v[0] for subs in roles.values() for sub, v in subs.items()}
    bad = []
    for subs in _COUNT_EQUALITIES.get(db, ()):
        vals = [counts.get(s, 0) for s in subs]
        if len(set(vals)) != 1:
            bad.append((subs, vals))
    return bad


def _fmt_int(n):
    return f"{n:,}"


def report(title, roles, manifest, sst, detail, db):
    """Print one store's breakdown (headline table, optional detail, edge roll-up) and the classifier cross-checks;
    db = its format (picks the count equalities). Returns the number of failed checks."""
    heads = {h: [sum(v[j] for v in roles.get(h, {}).values()) for j in range(3)] for h in HEADLINES}
    gn, gkb, gvb = (sum(heads[h][j] for h in HEADLINES) for j in range(3))
    gtot = gkb + gvb
    print(f"\n=== {title}  ({_fmt_int(gn)} keys, {_fmt_int(gtot)} logical bytes = {gkb / 1e6:.2f} MB key + "
          f"{gvb / 1e6:.2f} MB value; on disk: {sst / 1e6:.2f} MB SST) ===")
    print(f"  {'category':14s} {'keys':>11} {'key_bytes':>13} {'value_bytes':>13} {'%tot':>7}")
    for h in HEADLINES:
        n, kb, vb = heads[h]
        if h == "other" and n == 0:
            continue                                   # hide the empty residual (the good case)
        print(f"  {h:14s} {_fmt_int(n):>11} {_fmt_int(kb):>13} {_fmt_int(vb):>13} {_pct(kb + vb, gtot):>6.1f}%")
    print(f"  {'TOTAL':14s} {_fmt_int(gn):>11} {_fmt_int(gkb):>13} {_fmt_int(gvb):>13} {100.0:>6.1f}%")

    # edge-total: what it costs to store the EDGES, folding arango's covering edge indexes
    # back in with the edge data so the per-format number is honest (dgraph/nebula have no
    # index-edge, so for them edge-total == edge).
    etot = heads["edge"][1] + heads["edge"][2] + heads["index-edge"][1] + heads["index-edge"][2]
    print(f"  edge-total (edge + index-edge): {_fmt_int(etot)} bytes  "
          f"({_pct(etot, gtot):.1f}% of store)")

    if detail:
        print(f"  -- sub-categories --")
        for h in HEADLINES:
            for sub, (n, kb, vb) in sorted(roles.get(h, {}).items()):
                print(f"    {h:13s} {sub:22s} {_fmt_int(kb):>12} {_fmt_int(vb):>12}  ({_fmt_int(n)} keys)")

    # Cross-checks. (1) the role table sums to the manifest's totals (the ingest's own count). (2) nothing landed
    # in "other" (classifier COVERAGE). (3) the structural count-equalities (a misroute between buckets).
    bad = 0
    if gn != manifest["keys"] or gtot != manifest["logical_bytes"]:
        print(f"  !! WARNING: the role table ({gn} keys, {gtot} B) != the manifest ({manifest['keys']}, "
              f"{manifest['logical_bytes']})")
        bad += 1
    other_n = heads["other"][0]
    if other_n:
        print(f"  !! WARNING: {other_n} keys fell in 'other' -- classifier missed a key shape")
    for subs, vals in _structural_mismatches(db, roles):
        bad += 1
        pairs = ", ".join(f"{s}={v}" for s, v in zip(subs, vals))
        print(f"  !! WARNING: count mismatch ({pairs}) -- a misroute between buckets")
    return other_n + bad


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kvsize", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"), help="LDBC scale factor")
    ap.add_argument("--schema", default="canonical", choices=list(SCHEMAS))
    ap.add_argument("--db", action="append",
                    help="repeatable; default: dgraph, nebula, arangodb")
    ap.add_argument("--rebuild", action="store_true",
                    help="re-ingest the C++ store(s) before scanning (needed after a layout change)")
    ap.add_argument("--detail", action="store_true", help="also print the per sub-category rows")
    ap.add_argument("--store", action="append", default=[],
                    help="repeatable: also size this EXISTING store under output/stores/ldbc/sf<SF>/ as it is (e.g. "
                         "a meta cell's store); never built here")
    a = ap.parse_args(argv)

    dbs = a.db or ["dgraph", "nebula", "arangodb"]
    print(f"[kvsize] sf{a.sf}  schema={a.schema}  (logical = the C++ ingest's encoded bytes; SST = on disk)")
    totals = {}
    missed = 0
    paths = {db: ensure_store("ldbc", a.sf, db, schema_name=a.schema, chunk=store_chunk(db), rebuild=a.rebuild) for db in dbs}
    for name in a.store:
        paths[name] = store_dir("ldbc", a.sf, name)
        if not os.path.isfile(os.path.join(paths[name], "manifest.json")):
            raise SystemExit(f"--store {name}: no store at {paths[name]} (its own driver builds it)")
    for name, path in paths.items():
        with open(os.path.join(path, "manifest.json")) as f:
            manifest = json.load(f)
        db = manifest.get("format", name) if name in a.store else name
        phys = json.loads(subprocess.run([binary_path(), "size", path, "--repeats", "0"], capture_output=True, text=True,
                                         check=True).stdout.strip().splitlines()[-1])
        missed += report(db if name == db else f"{name} ({db})", store_roles(path)["roles"], manifest,
                         phys["sst_bytes"], a.detail, db)
        totals[name] = manifest["logical_bytes"]

    # cross-format summary: total store size and relative size (smallest = 1.00x)
    if len(totals) > 1:
        base = min(totals.values())
        print(f"\n=== store size across formats (sf{a.sf}, {a.schema}; logical bytes) ===")
        for name, t in totals.items():
            print(f"  {name:13s} {_fmt_int(t):>14} bytes  ({t / base:.2f}x)")

    if missed:
        raise SystemExit(f"classifier check failed ({missed} issue(s): unclassified keys, a role table off the "
                         "manifest, and/or count mismatches -- see WARNINGs above)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
