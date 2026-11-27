#!/usr/bin/env python3
"""grid_pins.record_scale (`--small-scale --record [--replace-scale]`) on a synthetic record: no store, no capture.

  1. per cell (the default): this capture's cells take its entries and fingerprints; another scale's entries, and this
     scale's entries of a cell the capture did not decide, stay;
  2. --replace-scale: each store the call decides (a key of its fingerprints) has its entries and fingerprint replaced
     whole: its stale entry (an exception that no longer holds) is dropped and returned; a store the call does not
     decide keeps its entries and fingerprint (returned as kept); another scale is untouched. At SF30 the composed 64 KB
     grid makes all nine cells units of the call, so their stale entries go;
  3. the control: the same inputs without --replace-scale keep the stale entry of a decided store with no exception left
     (ch_ch: the per-cell merge only replaces the cells the capture gives entries for), so check 2 tests the flag;
  4. a scale left with no entry has no key (the record carries no empty scale).

    python3 tests/unit/check_grid_pins_replace_scale.py
"""
import copy
import importlib.util
import os
import sys

from kvevaluator.paths import ROOT

_spec = importlib.util.spec_from_file_location("grid_pins", os.path.join(str(ROOT), "python", "kvcost", "layout_cost", "grid_pick_record.py"))
grid_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grid_pins)


def entry(body):
    return {"body": body, "body_sha": "x", "recorded_body_ratio": 1.5, "why": "test"}


BASE = {"scales": {"30": {"sp_sp": {"ic10": entry("old_ic10"), "is7": entry("old_is7")},
                          "sp_co": {"ic10": entry("old_ic10")},
                          "ch_ch": {"is2": entry("old_is2")}},
                   "0.1": {"sp_sp": {"is7": entry("small_is7")}}},
        "scale_fingerprints": {"30": {"sp_sp": "fp-old", "sp_co": "fp-old", "ch_ch": "fp-old"}, "0.1": {"sp_sp": "fp-small"}}}
# this capture decides sp_sp (ic10 still an exception, is7 no longer), co_co (new) and ch_ch (no exception left: its
# is2 entry is stale); sp_co is not a unit of it
OWN = {"sp_sp": {"ic10": entry("new_ic10")}, "co_co": {"ic3": entry("new_ic3")}}
FPS = {"sp_sp": "fp-new", "co_co": "fp-new", "ch_ch": "fp-new"}


def main():
    bad = 0

    def check(n, ok, what):
        nonlocal bad
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {n} {what}")

    rec = copy.deepcopy(BASE)
    dropped, kept = grid_pins.record_scale(rec, "30", copy.deepcopy(OWN), dict(FPS), replace=False)
    s30 = rec["scales"]["30"]
    check(1, dropped == [] and kept == [] and s30["sp_sp"] == OWN["sp_sp"] and s30["co_co"] == OWN["co_co"]
          and s30["sp_co"] == BASE["scales"]["30"]["sp_co"] and rec["scale_fingerprints"]["30"]["sp_co"] == "fp-old"
          and rec["scales"]["0.1"] == BASE["scales"]["0.1"],
          f"per cell: the cells with entries replaced, the others kept ({sorted(s30)})")

    rec = copy.deepcopy(BASE)
    dropped, kept = grid_pins.record_scale(rec, "30", copy.deepcopy(OWN), dict(FPS), replace=True)
    s30, f30 = rec["scales"]["30"], rec["scale_fingerprints"]["30"]
    check(2, dropped == [("ch_ch", "is2", "old_is2"), ("sp_sp", "is7", "old_is7")] and kept == ["sp_co"]
          and s30 == {**OWN, "sp_co": BASE["scales"]["30"]["sp_co"]} and f30 == {**FPS, "sp_co": "fp-old"}
          and rec["scales"]["0.1"] == BASE["scales"]["0.1"] and rec["scale_fingerprints"]["0.1"] == {"sp_sp": "fp-small"},
          f"--replace-scale: a decided store's stale entry dropped {dropped}; an undecided store kept {kept}; sf0.1 untouched")

    rec = copy.deepcopy(BASE)
    grid_pins.record_scale(rec, "30", copy.deepcopy(OWN), dict(FPS), replace=False)
    check(3, rec["scales"]["30"].get("ch_ch") == BASE["scales"]["30"]["ch_ch"],
          "control: without the flag the decided ch_ch's stale is2 entry stays")

    rec = copy.deepcopy(BASE)
    rec["scales"]["30"].pop("sp_co")
    grid_pins.record_scale(rec, "30", {}, {"sp_sp": "fp-new", "ch_ch": "fp-new"}, replace=True)
    check(4, "30" not in rec["scales"]
          and rec["scale_fingerprints"]["30"] == {"sp_sp": "fp-new", "ch_ch": "fp-new", "sp_co": "fp-old"},
          "no exception left: no empty scale key; the fingerprints say what was checked")
    print(f"check_grid_pins_replace_scale: 4 checks, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
