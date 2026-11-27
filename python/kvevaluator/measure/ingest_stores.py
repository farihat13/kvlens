"""Build C++ stores for (format, schema, codec, chunk) combinations -- the batch face of
stores.ensure_store, so nobody hand-writes `kvevaluator ingest` commands.

    python3 -m kvevaluator.measure.ingest_stores --sf 0.1                       # nebula + dgraph, canonical
    python3 -m kvevaluator.measure.ingest_stores --sf 0.1 --schema label_only --formats dgraph,nebula,arangodb
    python3 -m kvevaluator.measure.ingest_stores --sf 0.1 --formats pertype_csr --chunk 16k --codec zstd
    python3 -m kvevaluator.measure.ingest_stores --sf 0.1 --formats nebula --rebuild
    python3 -m kvevaluator.measure.ingest_stores --sf 3 --formats dgraph,nebula,arangodb,pertype_csr --multi
    python3 -m kvevaluator.measure.ingest_stores --sf 3 --formats ... --multi --ram-budget-gb 64

--multi builds the MISSING (or, with --rebuild, all) requested stores in ONE pass over the
LDBC CSVs (`kvevaluator ingest --multi --csv-root`: the source is parsed once, one encoder
per format concurrently; --ram-budget-gb batches formats within a RAM bound, byte-identically).
The stores are byte-identical to the per-format path. Without --multi each format is its own ingest.
An existing store is reused (its manifest is printed); --rebuild removes it first. The store
dir is named <format>[-<schema>][-c<N>k][-<codec>] (measure/stores.py). reingest_stores.py rebuilds
existing stores from their own manifests; this one builds new combinations from flags.
"""
import argparse
import json
import os
import sys
import time


from kvevaluator.measure.stores import store_dir, cpp_store_name, ensure_store, ingest_multi
from kvevaluator.capture.store_identity import stale_key_layout, store_built
from kvevaluator.paths import ROOT

# the chunked per-type formats: the chunk tag is always in THEIR store names (stores.cpp_store_name's naming rule);
# dgraph's chunk knob is opt-in (--chunk on dgraph -> dgraph-c<N>k, the fairness arm; without
# it the bare `dgraph` store keeps unbounded postings)
from kvevaluator.formats.pertype_chunk import CHUNKED_FORMATS
from kvevaluator.measure.stores import CHUNK_KNOB_FORMATS, store_chunk

_EXP = str(ROOT)
DEFAULT_CHUNK = 65536


def parse_chunk(text):
    """'64k' / '65536' -> bytes; None when not given."""
    if text is None:
        return None
    t = text.strip().lower()
    return int(t[:-1]) * 1024 if t.endswith("k") else int(t)


def chunk_for(fmt, chunk):
    """The chunk a format's store carries: the flag, else the default for chunked formats;
    for dgraph's knob the flag, else KVEVAL_DGRAPH_CHUNK
    (the one knob every driver reads), else none; else none. A chunk on any other format is a naming mistake -- refuse."""
    if fmt in CHUNKED_FORMATS:
        return chunk or DEFAULT_CHUNK
    if fmt in CHUNK_KNOB_FORMATS:
        return chunk or store_chunk(fmt)
    if chunk:
        raise SystemExit(f"{fmt}: --chunk applies to {', '.join(CHUNKED_FORMATS + CHUNK_KNOB_FORMATS)} only")
    return None


def summary(path):
    """The manifest fields that identify what was built (the search stamps the same ones)."""
    with open(os.path.join(path, "manifest.json")) as f:
        m = json.load(f)
    return {"keys": m.get("keys"), "settled": m.get("settled"),
            "sst_bytes": m.get("sst_bytes"), "compression": m.get("compression"),
            "key_codec": m.get("key_codec"), "value_codec_hash": m.get("value_codec_hash"),
            "schema": (m.get("schema") or {}).get("name"),
            "ingest_wall_s": round(m.get("ingest_wall_s", 0), 1)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sf", default=os.environ.get("SF", "0.1"))
    ap.add_argument("--schema", default="canonical",
                    help="canonical | label_only | any <name> with a committed schema_<name>.json")
    ap.add_argument("--formats", default="nebula,dgraph", help="comma list (default nebula,dgraph)")
    ap.add_argument("--codec", default=None, help="block compression: none | zstd | zstd-dict16k | zstd-dict64k | lz4 | snappy (default: inputs/contract/store_config.json's rocksdb.compression, the codec of a store name with no codec suffix)")
    ap.add_argument("--chunk", default=None, help="chunk target for the per-type formats, e.g. 64k (default) or 16k; "
                    "on dgraph it is the opt-in chunk knob (dgraph-c64k, dgraph-c512k; unset = KVEVAL_DGRAPH_CHUNK)")
    ap.add_argument("--rebuild", action="store_true", help="remove an existing store first (destructive)")
    ap.add_argument("--dry-run", action="store_true", help="name the stores, build nothing")
    ap.add_argument("--multi", action="store_true",
                    help="one pass over the LDBC CSVs for every store to build (one encoder per format)")
    ap.add_argument("--ram-budget-gb", type=float, default=0,
                    help="--multi only: batch formats within this RAM bound (0 = all in one pass)")
    a = ap.parse_args(argv)
    chunk = parse_chunk(a.chunk)
    formats = [f.strip() for f in a.formats.split(",") if f.strip()]

    print(f"[ingest] sf{a.sf} schema={a.schema} codec={a.codec} formats={formats}"
          + (f" chunk={chunk}" if chunk else "") + (" REBUILD" if a.rebuild else "")
          + (" ONE-PASS" if a.multi else ""))
    built, reused = [], []
    plan = []                                    # (fmt, chunk, name, path, existed)
    for fmt in formats:
        c = chunk_for(fmt, chunk)
        name = cpp_store_name(fmt, a.schema, a.codec, c)
        path = store_dir("ldbc", a.sf, name)
        existed = store_built(path)[0] and not a.rebuild    # a half-built dir is built again (moved aside first)
        stale = stale_key_layout(path) if existed else None
        plan.append((fmt, c, name, path, existed))
        if a.dry_run:
            state = ("exists" if existed else "would build") + (f" (STALE key layout {stale[0]}, binary reads {stale[1]})" if stale else "")
            print(f"  {name}: {state} -> {os.path.relpath(path, _EXP)}")
        elif stale:
            # reused by name but the binary would refuse it at open: --rebuild is the way past
            raise SystemExit(f"{name}: exists but was ingested under key layout {stale[0]!r}; this binary "
                             f"reads {stale[1]!r} -- re-run with --rebuild")
    if a.dry_run:
        return 0
    t_multi = {}
    if a.multi:
        todo = [fmt for fmt, _c, _n, _p, existed in plan if not existed]
        chunks = {fmt: c for fmt, c, *_ in plan if fmt in todo}   # each format's own chunk rides with it
        if todo:
            t0 = time.time()
            ingest_multi(a.sf, todo, a.schema, a.codec, chunks,
                         ram_budget_gb=a.ram_budget_gb, rebuild=a.rebuild, quiet=False)
            t_multi = {fmt: time.time() - t0 for fmt in todo}
    for fmt, c, name, path, existed in plan:
        t0 = time.time()
        if not a.multi:
            ensure_store("ldbc", a.sf, fmt, schema_name=a.schema, codec=a.codec, chunk=c, rebuild=a.rebuild, quiet=False)
        s = summary(path)
        (reused if existed else built).append(name)
        state = "exists" if existed else (f"built in the one pass ({t_multi.get(fmt, 0):.0f}s)"
                                          if a.multi else f"built in {time.time() - t0:.0f}s")
        print(f"  {name}: {state} -> {os.path.relpath(path, _EXP)}")
        print(f"      keys={s['keys']} settled={s['settled']} sst_bytes={s['sst_bytes']} "
              f"compression={s['compression']} key_codec={s['key_codec']} "
              f"value_codec={s['value_codec_hash']} schema={s['schema']}")
        if s["settled"] is not True:
            # the ingest's own keys==entries settle check did not stamp the manifest
            raise SystemExit(f"{name}: manifest not settled -- the ingest did not finish cleanly")
    if not a.dry_run:
        print(f"[ingest] built {len(built)}, reused {len(reused)} at sf{a.sf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
