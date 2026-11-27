#!/usr/bin/env python3
"""kvcost.layout_search.front.lower_left_front on synthetic points (no data): it keeps the points no earlier point
beats in y, in x order; of points equal in (x, y), the first given; a point equal in y to a kept one at larger x is
dropped; the payload is carried.

    python3 tests/unit/check_lower_left_front.py [--plant ties]   (the plant must exit 1)
"""
import argparse
import sys

from kvcost.layout_search.front import lower_left_front


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plant", choices=("ties",), help="a negative control: give the tied points in the other order (must exit 1)")
    a = ap.parse_args()
    pts = [(3, 5, "c"), (1, 9, "a"), (2, 9, "b"), (2, 7, "d1"), (2, 7, "d2"), (4, 5, "e"), (5, 1, "f")]
    if a.plant:
        pts = [p for p in pts if p[2] != "d1"] + [(2, 7, "d1")]
    got = [p[2] for p in lower_left_front(pts)]
    want = ["a", "d1", "c", "f"]      # b: beaten by a (same y); d2: d1's tie, given later; e: beaten by c (same y)
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} lower_left_front: {got} (want {want})")
    print(f"check_lower_left_front: 1 case, {0 if ok else 1} failed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
