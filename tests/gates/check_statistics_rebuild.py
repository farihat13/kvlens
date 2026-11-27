#!/usr/bin/env python3
"""LDBC's committed statistics (python/kvevaluator/datasets/ldbc/statistics/sf<SF>.json) are what build_stats makes
from that scale's record stream today: rebuilt into a temp dir with the binary under test and compared field by field,
the file's identity aside (provenance, wall_s, records.path and records.key_sha256: the machine and the run). Prints the fields and entries
compared; 0 compared fails. --write regenerates the committed file instead (after a statistics change, with the gate
re-run on it). Numbers that moved under the committed file's graph_stats_version fail both ways: a saved file is
reused while that version matches (build_stats.stale), so the change must bump kGraphStatsVersion.

    python3 tests/gates/check_statistics_rebuild.py [--sf 0.1] [--records R] [--write]
"""
import argparse
import json
import os
import sys
import tempfile

from kvevaluator.paths import ROOT
from kvevaluator.datasets import schema_path
from kvevaluator.predict import build_stats

COMMITTED = ROOT / "python" / "kvevaluator" / "datasets" / "ldbc" / "statistics"
IDENTITY = ("provenance", "wall_s")   # the run's and the machine's, not the graph's


def committed_path(sf):
    return COMMITTED / f"sf{sf}.json"


def normalized(g, sf):
    """the file as committed: the record stream named relative to the output dir (no machine path), without its
    freshness key's hash (the key holds the CSV files' mtimes and the binary's commit: the machine's, not the graph's)"""
    g = json.loads(json.dumps(g))
    g["records"]["path"] = f"stats/ldbc/sf{sf}/records.jsonl"
    g["records"].pop("key_sha256", None)
    g["records"].pop("sources_sha256", None)   # the key without the commit: still the CSV files' mtimes
    g["records"].pop("content_sha256", None)   # the stream's bytes: named by the key file, not by the statistics
    return g


def leaves(x, path=""):
    """{path: value} of every leaf (a number, a string, a list of numbers kept whole)"""
    if isinstance(x, dict):
        out = {}
        for k in sorted(x):
            out.update(leaves(x[k], f"{path}.{k}" if path else k))
        return out
    return {path: x}


def compare(want, got):
    """(entries compared, [differences]): every leaf but the identity fields"""
    a = {k: v for k, v in leaves(want).items() if k.split(".")[0] not in IDENTITY}
    b = {k: v for k, v in leaves(got).items() if k.split(".")[0] not in IDENTITY}
    diff = [f"{k}: committed {a.get(k)!r}, rebuilt {b.get(k)!r}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    return len(set(a) | set(b)), diff


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sf", default="0.1")
    ap.add_argument("--records", help="the record stream (default: this output dir's, records_file)")
    ap.add_argument("--write", action="store_true", help="write the committed file instead of checking it")
    a = ap.parse_args(argv)
    from kvevaluator.capture.binary import binary_path
    from kvevaluator.measure.records import records_file
    records = a.records or records_file("ldbc", a.sf)
    binary = binary_path()
    print(f"check_statistics_rebuild: binary {binary}; records {records}")
    with tempfile.TemporaryDirectory() as t:
        g, _ = build_stats.build(records, schema_path("ldbc"), "ldbc", a.sf,
                                 os.path.join(t, "stats.json"))
    print(f"check_statistics_rebuild: built by {g['provenance'].get('binary_git_sha', '?')[:10]} "
          f"dirty={g['provenance'].get('binary_dirty')}")
    got = normalized(g, a.sf)
    path = committed_path(a.sf)
    want = json.load(open(path)) if path.exists() else None
    # a saved statistics file is reused while its graph_stats_version matches (build_stats.stale): numbers that move
    # under the same version would be served stale, so the version must be bumped with them
    version = got["provenance"].get("graph_stats_version")
    if want is not None and want.get("provenance", {}).get("graph_stats_version") == version and compare(want, got)[1]:
        print(f"check_statistics_rebuild: FAIL: graph-stats' numbers moved under the same graph_stats_version "
              f"{version}: bump kGraphStatsVersion (cpp/kvevaluator/src/cli/graph_stats.cc), then --write")
        return 1
    if a.write:
        os.makedirs(path.parent, exist_ok=True)
        with open(path, "w") as f:
            json.dump(got, f, indent=1, sort_keys=True)
            f.write("\n")
        print(f"check_statistics_rebuild: wrote {os.path.relpath(path, ROOT)}")
        return 0
    if want is None:
        print(f"check_statistics_rebuild: FAIL: no committed file {os.path.relpath(path, ROOT)}")
        return 1
    n, diff = compare(want, got)
    for d in diff[:20]:
        print(f"   DIFFERS {d}")
    ok = n > 0 and not diff
    print(f"check_statistics_rebuild: sf{a.sf}: {n} entries compared, {len(diff)} differ: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
