#!/usr/bin/env python3
"""The opaque-vid rule, guarded: no read or write of a guarded format takes a type, a candidate label set or a "this
probe is ruled out" decision from a vid's bits, or from a table keyed by vid. python/kvevaluator/schema/vids.py: the 4-bit
tag "is used ONLY here at construction: a format NEVER parses a vid for its type".
- formats/meta: every call of tag_of_uid / labels_of_tag / group_owns must be one of the named allowances below
  (ingest, or own placement's uses: kept as an option); anything else fails.
- every guarded format (meta, arangodb, pertype_csr, dgraph, nebula): no raw shift of a vid to its tag bits
  (`>> kVidShift`, `>> 60`) and no kVidIdMask -- the shortcut a uint64 vid invites (util/vid.h holds the only ones).
- formats/dgraph, nebula (their engine twins included): no tag_of_uid / labels_of_tag / group_owns at all.
- formats/arangodb: the vid -> collection map (coll_, coll_of, ensure_coll, warm) may appear ONLY in the engine twin
  (engine.h / engine.cc: a real ArangoDB id is "collection/key", the engine's own storage fact).
  The fair format's files must not name it, nor read a type from a vid.

  python3 cpp/kvevaluator/tests/check_vid_opaque.py cpp/kvevaluator/src     (a ctest: check_vid_opaque)
"""
import glob
import os
import re
import sys

CALL = re.compile(r"\b(tag_of_uid|labels_of_tag|group_owns)\s*\(")
# (why, a substring of the allowed line)
ALLOWED = [
    ("ingest: the chunker closes a chunk at a vid-group boundary", "static bool other_group(uint64_t first_owner"),
    ("ingest: own-placement ordinals per (etype, src group)", "gs.emplace(tag_of_uid(v[i].first), i)"),
    ("ingest: own-placement ordinals per (etype, src group)", "group_start.at(etype).at(tag_of_uid(src))"),
    ("ingest: the own edge-prop chunk key by group", "uint64_t tag = tag_of_uid(eb.src[e]);"),
    ("held T3 (own placement): the helper", "bool Meta::group_owns(uint64_t owner"),
    ("held T3 (own placement): the helper", "for (auto& lab : schema_.labels_of_tag(tag_of_uid(owner)))"),
    ("held T3 (own placement): the helper's declaration", "bool group_owns(uint64_t owner, const std::string& etype, bool out) const;"),
    ("held T3 (own placement): the edge-prop read", "if (!group_owns(su, etype, true)) return Value::row({});"),
    ("held T3 (own placement): the edge-prop read", "pep_prefix(eid, uint8_t(tag_of_uid(su)))"),
    ("held T4 (own placement): an edge write's ep row", "uint64_t tag = tag_of_uid(su);"),
    ("held T4 (own placement): an edge write's ep row", "ep_rmw(tag_of_uid(su), etype"),
    ("held T5 (own placement): a vertex delete's in-edge ep rows", "uint64_t ot = tag_of_uid(out ? u : nbrs[i]);"),
    # the predictor's write truth card (formats/meta/predict.cc) prices the same writes, so it names the same ep chunk
    ("held T4 (own placement): the write truth card's ep row", "uint64_t tag = tag_of_uid(su), ordinal = kNoOrdinal;"),
    ("held T4 (own placement): the write truth card's ep row", "pep_prefix(e.eid, uint8_t(tag_of_uid(su)))"),
]


# formats/pertype_csr: no read, write or ingest step names the id's label group (empty since the stored far-end label,
# a write takes a label from the step, the schema's endpoints or an entry's stored code; ingest from the
# record). A name that returns here fails
PERTYPE_CALL = re.compile(r"\b(tag_of_uid|labels_of_tag|labels_for|group_of)\s*\(")
PERTYPE_ALLOWED = []


# a raw shift or mask of a vid to its tag bits, in every guarded format; and the baseline formats, which name no tag call
SHIFT = re.compile(r">>\s*(kVidShift|60)\b|\bkVidIdMask\b")
BASELINES = ("dgraph", "nebula")

# the map from vid to collection: the engine twin's only (its files are skipped below)
ARANGO_MAP = re.compile(r"\b(coll_|coll_of|ensure_coll|coll_loaded_|warm)\b")
ARANGO_TWIN = ("engine.h", "engine.cc")


def sources(src, fmt):
    return sorted(glob.glob(os.path.join(src, "formats", fmt, "*.cc")) + glob.glob(os.path.join(src, "formats", fmt, "*.h")))


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
    bad, ok = [], 0
    for path in sources(src, "meta"):
        for n, line in enumerate(open(path), 1):
            code = line.split("//", 1)[0]
            if not CALL.search(code):
                continue
            if any(sub in code for _, sub in ALLOWED):
                ok += 1
            else:
                bad.append(f"{os.path.relpath(path, src)}:{n}: {line.strip()}")
    twin, fair_lines = 0, 0
    for path in sources(src, "arangodb"):
        is_twin = os.path.basename(path) in ARANGO_TWIN
        for n, line in enumerate(open(path), 1):
            code = line.split("//", 1)[0]
            fair_lines += not is_twin
            if CALL.search(code) or (ARANGO_MAP.search(code) and not is_twin):
                bad.append(f"{os.path.relpath(path, src)}:{n}: {line.strip()}")
            elif ARANGO_MAP.search(code):
                twin += 1
    pt_ok = pt_lines = 0
    for path in sources(src, "pertype_csr"):
        for n, line in enumerate(open(path), 1):
            code = line.split("//", 1)[0]
            pt_lines += 1
            if not PERTYPE_CALL.search(code):
                continue
            if any(sub in code for _, sub in PERTYPE_ALLOWED):
                pt_ok += 1
            else:
                bad.append(f"{os.path.relpath(path, src)}:{n}: {line.strip()}")
    shift_lines = {}
    for fmt in ("meta", "arangodb", "pertype_csr") + BASELINES:
        shift_lines[fmt] = 0
        for path in sources(src, fmt):
            for n, line in enumerate(open(path), 1):
                code = line.split("//", 1)[0]
                shift_lines[fmt] += 1
                if SHIFT.search(code) or (fmt in BASELINES and CALL.search(code)):
                    bad.append(f"{os.path.relpath(path, src)}:{n}: {line.strip()}")
    for b in bad:
        print("  NOT ALLOWED (a query or write takes a decision from a vid's bits, or from a table keyed by vid):", b)
    good = (not bad and ok and twin and fair_lines and pt_ok == len(PERTYPE_ALLOWED) and pt_lines > 0
            and all(shift_lines.values()))
    print(f"[vid-opaque] meta: {ok} allowed uses (ingest; own placement, held); arangodb: {fair_lines} lines of the fair "
          f"format scanned, the map named on {twin} lines of the engine twin only; pertype_csr: {pt_lines} lines "
          f"scanned, {pt_ok} of {len(PERTYPE_ALLOWED)} named uses; no raw tag shift in "
          f"{', '.join(f'{k} ({v} lines)' for k, v in shift_lines.items())}; {len(bad)} not allowed -> "
          f"{'OK' if good else 'FAIL'}")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
