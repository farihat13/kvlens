#!/usr/bin/env python3
"""cost_models.of_channels refuses a row whose byte lanes are named <lane>_bytes (a latency steps.csv row) and has
none of the lane names it reads: such a row's bytes would count 0, and wops_bytes4k would be wops alone. Three cases:
  1. a steps-style row (S, G, N and get_key_bytes, get_value_bytes, ...): refused (ValueError);
  2. a capture-style row (the same channels named get_key, get_value, ...): its bytes counted;
  3. a row with no byte lanes at all (S, G, N only): 0 bytes, as before.
On the code before the guard, case 1 fails (the row is read with 0 bytes).

    python3 tests/unit/check_of_channels_byte_lanes.py
"""
import sys

from kvcost.layout_cost import cost_models as C

OPS = {"S": "10", "G": "5", "N": "100"}
LANES = {"get_key": "4096", "get_value": "40960"}


def main():
    bad = 0
    try:
        got = C.of_channels({**OPS, **{f"{k}_bytes": v for k, v in LANES.items()}})
        ok, note = False, f"read as {got}"
    except ValueError as e:
        ok, note = "_bytes" in str(e), str(e)[:80]
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} 1 a steps-style row is refused: {note}")
    got = C.of_channels({**OPS, **LANES})
    ok = got["bytes"] == 45056.0 and got["wops_bytes4k"] == got["wops"] + 45056.0 / 4096
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} 2 a capture-style row counts its bytes: {got}")
    got = C.of_channels(dict(OPS))
    ok = got["bytes"] == 0.0 and got["wops_bytes4k"] == got["wops"]
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} 3 a row with no byte lanes: 0 bytes, as before: {got}")
    print(f"3 cases, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
