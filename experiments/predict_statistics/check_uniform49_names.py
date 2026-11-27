#!/usr/bin/env python3
"""uniform49's 49 layout names round-trip: every CELLS name reads back (settings_of, by kvevaluator.layouts.cell_parts)
to the two settings that wrote it (cell_id), spec_of builds its spec, and a name outside the 49 or in an old spelling is
refused. No data, no binary; reads the tracked specs only. Under a second.

Run:  python3 experiments/predict_statistics/check_uniform49_names.py
"""
import sys

from kvcost.predict_accuracy import uniform_49_tallies as U


def main():
    fails, cases = [], 0

    def case(what, ok, got=""):
        nonlocal cases
        cases += 1
        if not ok:
            print(f"  FAIL {what}: {got}")
            fails.append(what)

    def refused(name):
        try:
            U.settings_of(name)
        except SystemExit:
            return True
        return False

    case("49 distinct names", len(set(U.CELLS)) == len(U.CELLS) == 49, len(set(U.CELLS)))
    for p in U.SETTINGS:
        for a in U.SETTINGS:
            name = U.cell(p, a)
            try:
                back = U.settings_of(name)
            except SystemExit as e:
                back = str(e)
            case(f"{name} -> settings -> name", back == (p, a), back)
            try:
                U.spec_of(name)
                U.short(name)
                ok, got = True, ""
            except (SystemExit, KeyError, ValueError) as e:
                ok, got = False, e
            case(f"{name}: spec_of and short", ok, got)
    for bad in ("props_chunked_c2k_adj_split",              # a size outside the 49
                "props_chunked_adj_chunked_c4k",            # the one-size ChK-ChK spelling
                "props_chunked_c4k_adj_chunked",            # likewise
                "props_split_adj_split_own",                # not a cell id
                "mix_w16"):
        case(f"{bad} refused", refused(bad), "accepted")
    print(f"{cases} cases, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
