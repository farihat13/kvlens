"""KVCounts — a (SEEK, GET, NEXT) triple, the unit the cost doc uses.

S / G / N map to the normalized `/kvstats` counters `seek_count` / `get_count` /
`next_count` (KVProfiler._normalize_kvstats handles the Dgraph vs Nebula field differences).

The `S()`, `G()`, `N()` constructors let a formula model read like the doc, e.g.
    return S(2) + S(2*d1) + G(11*v_result)
mirrors "2 S + 2 d1 S + 11 v_result G".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class KVCounts:
    seek: int = 0
    get: int = 0
    next: int = 0

    def __add__(self, other: "KVCounts") -> "KVCounts":
        return KVCounts(self.seek + other.seek, self.get + other.get, self.next + other.next)

    def __mul__(self, k: int) -> "KVCounts":
        return KVCounts(self.seek * k, self.get * k, self.next * k)

    __rmul__ = __mul__

    def as_dict(self) -> dict[str, int]:
        return {"S": self.seek, "G": self.get, "N": self.next}


def S(n: int = 1) -> KVCounts:
    return KVCounts(seek=n)


def G(n: int = 1) -> KVCounts:
    return KVCounts(get=n)


def N(n: int = 1) -> KVCounts:
    return KVCounts(next=n)
