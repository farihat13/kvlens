"""Shared result/output helpers: where output lands, and how a run is summarized.

Everything here is used by more than one driver (the measure drivers, the checks, the experiments), which is why it is
its own module: nothing imports another script's private helpers."""
import os
import subprocess

from kvevaluator.cost import KVCost
from kvevaluator.output_tree import Tree


def output_root():
    return str(Tree().root)


def output_dir(sf, *parts):
    """an LDBC study's output: summary/ldbc/sf<SF>/<parts...> (sf=None: output/tiny/<parts...>, the demo graph)"""
    if not sf:
        return os.path.join(output_root(), "tiny", *parts)
    return str(Tree().summary("ldbc", sf, *parts))


def git_provenance():
    """('abc1234', dirty?) for the repo: the code state a write capture's record names (grid_writes). With no .git:
    a release archive's (its commit, "release"), else ("unknown", "unknown"), never a clean False (binary.binary_state)."""
    from kvevaluator.paths import ROOT, checkout_of, release_commit
    if checkout_of(ROOT) is None:
        release = release_commit()
        return (release[:7], "release") if release else ("unknown", "unknown")
    try:
        sha = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      stderr=subprocess.DEVNULL, text=True).strip()
        # -uno: TRACKED changes only. Untracked files (scratch) can't
        # change what a sweep measures; counting them made every sweep read as dirty.
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain", "-uno"],
                                             stderr=subprocess.DEVNULL, text=True).strip())
        return sha, dirty
    except Exception:
        return "unknown", False


def totals(kvcosts):
    """Whole-query sum over the per-step ACTUALS, as a KVCost (S/G/N + value_bytes +
    scan_key_bytes + get_key_bytes, plus the write columns P/D + write bytes). Use .total_bytes
    (bytes.read-validated) or .total_bytes (adds point-GET key bytes) for the read
    scalars, .write_bytes for the write one. Read-only plans leave the write columns at 0."""
    t = KVCost()
    for _, _, x in kvcosts:
        t.S += x.S
        t.G += x.G
        t.N += x.N
        t.get_value_bytes += x.get_value_bytes
        t.scan_value_bytes += x.scan_value_bytes
        t.scan_key_bytes += x.scan_key_bytes
        t.get_key_bytes += x.get_key_bytes
        t.P += x.P
        t.D += x.D
        t.write_key_bytes += x.write_key_bytes
        t.write_value_bytes += x.write_value_bytes
        t.S_miss += x.S_miss
        t.G_miss += x.G_miss
        t.N_miss += x.N_miss
        t.seek_miss_key_bytes += x.seek_miss_key_bytes
        t.next_miss_key_bytes += x.next_miss_key_bytes
        t.get_miss_key_bytes += x.get_miss_key_bytes
    return t


def normalize(result):
    """Sort a step output for cross-format comparison (lists of vids or dict rows).
    repr as the sort key keeps rows with None/mixed values comparable — only
    EQUALITY of the normalized lists matters, not the order itself."""
    if result and isinstance(result[0], dict):
        return sorted((tuple(sorted(d.items())) for d in result), key=repr)
    try:
        return sorted(result)
    except TypeError:
        return sorted(result, key=repr)


def canon_rows(result):
    """Canonicalise each ROW but PRESERVE the list's order — the sequence-equality
    counterpart to normalize().

    normalize() sorts, so it answers "did the formats return the same SET of rows?".
    LDBC answers are ranked lists (ORDER BY ... LIMIT 20), so that question is weaker
    than the one the spec asks. Comparing raw repr() instead would be wrong in the
    other direction: two formats can build the same row with different dict INSERTION
    order, which repr() shows as a difference that no reader would call one. Sorting
    each row's items removes that, and only that.

    Used by the C++-against-Python gate's order check; normalize() backs the set comparison.
    """
    if result and isinstance(result[0], dict):
        return [tuple(sorted(d.items())) for d in result]
    return list(result)
