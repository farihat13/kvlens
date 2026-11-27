"""The counting versions a capture is keyed on, as the binary states them, and the one rule that compares them.

The C++ states four numbers per format (`kvevaluator --version` "counting"; every run response's provenance):
  seam          what a SEEK / GET / NEXT and each byte lane charge at the counting seam (store/counting.h)
  format        the format's own read rules (formats/registry.cc kFormatCounting, per format)
  write_seam    what a write's reads, PUTs and DELETEs charge at the seam
  write_format  the format's own write rules
A record stores the four it was counted under as `counting` (stamp). A read capture compares seam and format, a write
capture write_seam and write_format, the kvstats writes stage all four (PARTS).

An older record stores no `counting`. counting_fresh reads it by this rule, here and nowhere else:
  grid         a grid_bodies run record: seam 4, format 1
  grid_writes  a grid_writes record: write_seam = its write_measurement_version, write_format 1
  kvstats      a kvstats identity: seam = its measurement_version, format 1; write_seam = its write_measurement_version,
               write_format 1
A record of another kind, or one without the stored version its kind needs, is not fresh, and the reason says so.
"""
import functools

from kvevaluator.capture import binary

PARTS = {"read": ("seam", "format"), "write": ("write_seam", "write_format"),
         "read+write": ("seam", "format", "write_seam", "write_format")}
LEGACY_GRID_SEAM = 4     # cppbridge.MEASUREMENT_VERSION when the grid records were written without `counting`


@functools.lru_cache(maxsize=None)
def binary_counting():
    """the binary's "counting" block of `--version`: {"seam": {"read", "write"}, "formats": {name: {"read", "write"}}}"""
    got = binary.binary_provenance().get("counting")
    if not got:
        raise SystemExit(f"[counting] the binary {binary.binary_path()} states no counting versions (an older "
                         f"build): rebuild it (just build)")
    return got


def stamp(fmt):
    """the four versions a run on format `fmt` is counted under, as a record stores them"""
    return stamp_from(binary_counting(), fmt)


def stamp_from(c, fmt):
    """the four versions of format `fmt` in a "counting" block of `--version` (stamp's rule, for a given block)"""
    f = c["formats"].get(fmt)
    if f is None:
        raise SystemExit(f"[counting] the binary states no counting version for format {fmt!r} "
                         f"(it knows {sorted(c['formats'])})")
    return {"seam": c["seam"]["read"], "format": f["read"], "write_seam": c["seam"]["write"], "write_format": f["write"]}


def _stored(rec, kind):
    """(the versions `rec` was counted under, or None, why not): its `counting`, else the legacy rule (module doc)"""
    if rec.get("counting"):
        return rec["counting"], None
    if kind == "grid":
        return {"seam": LEGACY_GRID_SEAM, "format": 1}, None
    if kind == "grid_writes":
        w = rec.get("write_measurement_version")
        return ({"write_seam": w, "write_format": 1}, None) if w is not None else (
            None, "a write record with neither `counting` nor write_measurement_version")
    if kind == "kvstats":
        out = {"format": 1, "write_format": 1}
        if rec.get("measurement_version") is not None:
            out["seam"] = rec["measurement_version"]
        if rec.get("write_measurement_version") is not None:
            out["write_seam"] = rec["write_measurement_version"]
        return out, None
    raise ValueError(f"counting_fresh: unknown record kind {kind!r} (grid, grid_writes, kvstats)")


def counting_fresh(rec, fmt, parts, kind):
    """(fresh, why not) of record `rec` (a run record or a kvstats identity, of `kind`) against the binary's versions
    for format `fmt`, on `parts` (a PARTS key)"""
    stored, why = _stored(rec, kind)
    if stored is None:
        return False, why
    now = stamp(fmt)
    for k in PARTS[parts]:
        if k not in stored:
            return False, f"the record states no {k} counting version"
        if stored[k] != now[k]:
            return False, f"counted under {k} version {stored[k]}, the binary's is {now[k]}"
    return True, None


def format_of_slug(slug):
    """the format a store slug names: a named format's slug starts with its name (dgraph-c512k, pertype_csr-c64k, and a
    retired one such as dgneb); any other slug is a configurable-format cell or mix (meta)"""
    from kvevaluator.measure.kvstats_layout import FORMAT_ORDER
    if slug.endswith("_engine") or slug.startswith(("hybrid_", "pertype_csr_liquid")):
        return slug.split("-c")[0]   # an engine twin, or a retired named format: its own name
    for f in sorted(FORMAT_ORDER, key=len, reverse=True):
        if slug == f or slug.startswith(f + "-") or slug.startswith(f + "_c"):
            return f
    return "meta"


def cell_counting(rec):
    """one grid read record's (format, key_layout, counting, legacy): what a pick decided on it was counted under (the
    versions the record states, else the legacy rule's: _stored)"""
    spec = rec.get("spec") or {}
    stored, _why = _stored(rec, "grid")
    return {"format": format_of_slug(spec["format"] if spec.get("format") else rec["cell"]),
            "key_layout": (rec.get("store_manifest") or {}).get("key_codec"), "counting": stored,
            "legacy": not rec.get("counting")}
