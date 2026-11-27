"""The stored size of a meta layout or mix (keys, key bytes, value bytes, per store part), read from a tally library.

A tally library (kvevaluator.predict.tally_library, a dir `stats/<dataset>/<scale>/tallies/`) holds one tally per
setting: keys and bytes per store part, counted from the record stream by the C++ tally, with no store built.
tally_library.tally_for_spec assembles any spec's tally from them, part by part (a mix too). `Frozen` serves the
library's setting files read-only, without its record stream (it never builds an entry), and tally_for_spec does the
assembly. The billed size of a store is cost_models.stored_bytes of the totals.

    python -m kvcost.layout_cost.stored_size --library <a tallies dir> --spec S.json [--spec ...]
        -> one JSON line per spec: its totals and the totals per part (vertex_props, adjacency, index)
"""
import argparse
import glob
import hashlib
import json
import os
import sys

from kvevaluator.predict import tally_library as TL

PARTS = ("vertex_props", "adjacency", "index")


class Frozen:
    """a tally library dir, read-only: the setting tallies it holds serve the specs that share their blocks"""

    def __init__(self, library):
        keys = sorted(glob.glob(os.path.join(library, "setting-*.key.json")))
        if not keys:
            raise SystemExit(f"stored_size: no setting tallies in {library}")
        self.path, self.seeds, self.files, self.loaded = library, [], {}, {}
        versions = set()
        for k in keys:
            with open(k) as f:
                have = json.load(f)
            try:
                TL.serves(have["spec"])
            except ValueError:
                continue          # a non-uniform entry serves no block alone
            self.seeds.append(have["spec"])
            self.files[TL.canonical(have["spec"])] = k[:-len(".key.json")] + ".json"
            versions.add(have.get("tally_version"))
        if len(versions) != 1:
            raise SystemExit(f"stored_size: {library}: setting tallies of tally versions {sorted(map(str, versions))}")
        self.version = versions.pop()

    def setting(self, side, blk, shared):
        """(path, tally) of the entry serving (side, blk) under `shared` (tally_library.Library.setting's lookup, but
        an entry the library lacks is refused, not built)"""
        for s in self.seeds:
            if TL.shared_of(s) == shared and (side, blk) in TL.serves(s):
                p = self.files[TL.canonical(s)]
                if p not in self.loaded:
                    with open(p) as f:
                        self.loaded[p] = json.load(f)
                return p, self.loaded[p]
        raise SystemExit(f"stored_size: {self.path} holds no setting tally for {side} {json.dumps(blk, sort_keys=True)}")


def fingerprint(library):
    """the sha256 of the library's setting files: of their sorted lines "<file name> <its sha256>" (one per
    setting-* file, each line ending in a newline)"""
    lines = []
    for path in sorted(glob.glob(os.path.join(library, "setting-*"))):
        with open(path, "rb") as f:
            lines.append(f"{os.path.basename(path)} {hashlib.sha256(f.read()).hexdigest()}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def size_of(spec, lib):
    """{keys, key_bytes, value_bytes, per part <part>_keys, <part>_key_bytes, <part>_value_bytes, and
    chunk_value_log2_max} of a spec. chunk_value_log2_max: the largest Chunked item's value, as floor(log2 bytes), over the
    spec's chunk families (None when no part is Chunked: the tally keeps no size for a Split or Condensed item)"""
    t = TL.tally_for_spec(spec, lib)
    out = dict(t["totals"])
    for part in PARTS:
        rows = [g for g in t["groups"] if g["part"] == part]
        for k in ("keys", "key_bytes", "value_bytes"):
            out[f"{part}_{k}"] = sum(g[k] for g in rows)
    logs = [int(b) for c in t["chunks"].values() for b in c["value_bytes_log2"]]
    out["chunk_value_log2_max"] = max(logs) if logs else None
    if any(sum(out[f"{p}_{k}"] for p in PARTS) != out[k] for k in ("keys", "key_bytes", "value_bytes")):
        raise SystemExit("stored_size: the parts do not add to the tally's totals")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--library", required=True, help="a tally library dir (stats/<dataset>/<scale>/tallies)")
    ap.add_argument("--spec", action="append", required=True, help="a meta spec file (repeatable)")
    a = ap.parse_args(argv)
    lib = Frozen(a.library)
    for path in a.spec:
        with open(path) as f:
            print(json.dumps({"spec": path, **size_of(json.load(f), lib)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
