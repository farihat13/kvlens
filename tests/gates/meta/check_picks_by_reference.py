#!/usr/bin/env python3
"""The pick record's cells kept by reference are the previous record's rows, unchanged, at every scale (owner: experiments/meta).

A rewrite at the deciding scale (grid_pick_record.py --repick --keep-by-reference) re-decides the cells it captured and keeps
the rest as they were decided, on an earlier capture. This checks, for every cell of the previous record:
  1. it is in the new record: re-decided (captured by this rewrite) or kept by reference; none is lost;
  2. a kept cell's rows equal the previous record's: its picks, fingerprints, mix spec sha and assignment, and every
     smaller scale's own picks and held-out fingerprint (what picks.recorded_picks hands an sf0.1 / sf0.3 / sf30 reader);
  3. its by_reference field names the capture, the date, the binary and the held-out params it was decided on, and those
     params are the previous fingerprint's;
  4. every kept pick's body is that body at HEAD (grid_capture.drifted_record_bodies, empty: readers only warn).
--plant drops one kept cell's smaller-scale entry from the new record (in memory): the gate must fail.

    python3 tests/gates/meta/check_picks_by_reference.py --before <previous record> [--record inputs/picks/picks.csv] [--plant]
--before is a table's path, or <git ref>:inputs/picks/picks.csv (a ref with a one-file record: convert it first,
pick_record.OLD_NAMES). Exit 0 = all hold; prints how many entries it compared.
"""
import argparse
import copy
import sys

from kvevaluator.measure import grid_capture
from kvevaluator.plan_choice import pick_record
from kvevaluator.paths import ROOT

ROWS = ("cells", "fingerprints", "spec_sha", "assignment")


def load(spec):
    """a writer's record from a table's path or from <git ref>:<table path> (pick_record.load_full_at)"""
    return pick_record.load_full_at(spec)


def problems(before, rec):
    """(the problems, the entries compared)"""
    bad, n = [], 0
    kept = rec.get("by_reference", {})
    for cell in sorted(before.get("cells", {})):
        if cell not in rec.get("cells", {}):
            bad.append(f"{cell}: in the previous record, not in the new one (neither re-decided nor kept)")
            continue
        if cell not in kept:
            continue
        for k in ROWS:
            n += 1
            if before.get(k, {}).get(cell) != rec.get(k, {}).get(cell):
                bad.append(f"{cell}: {k} differs from the previous record")
        for part in ("scales", "scale_fingerprints"):
            for sf, cells in sorted(before.get(part, {}).items()):
                if cell in cells:
                    n += 1
                    if rec.get(part, {}).get(sf, {}).get(cell) != cells[cell]:
                        bad.append(f"{cell}: {part}[{sf}] differs from the previous record (a reader at sf{sf} gets another pick)")
        ref = kept[cell]
        missing = [f for f in ("capture", "decided", "held_out_params", "binary") if not ref.get(f)]
        if missing:
            bad.append(f"{cell}: by_reference lacks {missing}")
        if ref.get("held_out_params") != before["fingerprints"][cell]["held_out"].get("params"):
            bad.append(f"{cell}: by_reference held_out_params {ref.get('held_out_params')} are not the fingerprint's")
        try:   # strict here: a reader warns and marks a moved body (check_record_bodies), this check refuses it
            moved = grid_capture.drifted_record_bodies(rec["cells"][cell], "interactive")
        except (SystemExit, KeyError, ValueError) as e:
            moved = {"?": str(e)}
        if moved:
            bad.append(f"{cell}: a recorded body is not that body at HEAD: {moved}")
    for cell in sorted(set(kept) - set(before.get("cells", {}))):
        bad.append(f"{cell}: kept by reference but not in the previous record")
    return bad, n


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--before", required=True)
    ap.add_argument("--record", default=str(ROOT / "inputs" / "picks" / "picks.csv"))
    ap.add_argument("--plant", action="store_true", help="the control: drop one kept cell's smaller-scale entry")
    a = ap.parse_args()
    before, rec = load(a.before), load(a.record)
    kept = sorted(rec.get("by_reference", {}))
    if a.plant:
        rec = copy.deepcopy(rec)
        sf, cell = next((sf, c) for sf, cells in sorted(rec["scale_fingerprints"].items()) for c in sorted(cells) if c in kept)
        del rec["scale_fingerprints"][sf][cell]
        print(f"plant: dropped scale_fingerprints[{sf}][{cell}]")
    bad, n = problems(before, rec)
    print(f"{len(before.get('cells', {}))} cells in the previous record; {len(kept)} kept by reference; "
          f"{n} kept entries compared (rows and every scale's), {len(bad)} problems")
    for b in bad[:40]:
        print("  " + b)
    if not kept or not n:
        print("FAIL: no cell kept by reference, or 0 entries compared")
        return 1
    print("BY REFERENCE: OK" if not bad else f"FAIL: {len(bad)} problems")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
