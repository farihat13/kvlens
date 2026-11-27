#!/usr/bin/env python3
"""BI18's answer size per tag (genparams.interested_pairs: each person's friends walked once) equals the per-tag 2-hop
walk it replaced, and hand-counted answers: on hand-made graphs (a path, a triangle, a square with a diagonal, a pair
listed in both directions) and on a seeded random graph. A self-loop is refused. No data, under a second.

Run:  python3 tests/unit/check_bi_interested_pairs.py
"""
import random
import sys

from kvevaluator.workloads.bi.genparams import interested_pairs


def knows(pairs):
    """_knows' sets from a list of (x, y) pairs, as genparams builds them from the CSV (both directions)"""
    adj = {}
    for x, y in pairs:
        adj.setdefault(x, set()).add(y)
        adj.setdefault(y, set()).add(x)
    return adj


def walk(adj, P):
    """the replaced count: per tag, every interested p1's friends of friends p2 (p2 interested, not p1, not a friend)"""
    out = {}
    for t, ps in P.items():
        n = 0
        for p1 in ps:
            for mf in adj.get(p1, ()):
                for p2 in adj.get(mf, ()):
                    if p2 in ps and p2 != p1 and p2 not in adj[p1]:
                        n += 1
        out[t] = n
    return out


def main():
    fails, cases = [], 0

    def case(what, got, want):
        nonlocal cases
        cases += 1
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {what}: {got}" + ("" if ok else f" (want {want})"))
        if not ok:
            fails.append(what)

    def both(what, pairs, P, want):
        adj = knows(pairs)
        got = {t: interested_pairs(adj, P).get(t, 0) for t in P}
        case(f"{what}: hand count", got, want)
        case(f"{what}: the replaced walk", got, walk(adj, P))

    # a path 1-2-3, tag A on 1 and 3: 1-2-3 and 3-2-1; tag B on 1 and 2 (friends, no middle): 0
    both("path", [(1, 2), (2, 3)], {"A": {1, 3}, "B": {1, 2}}, {"A": 2, "B": 0})
    # the triangle 1-2-3: 1 and 3 now know each other, so A has none
    both("triangle", [(1, 2), (2, 3), (1, 3)], {"A": {1, 3}}, {"A": 0})
    # a square 1-2-3-4-1 with the diagonal 2-4; A on 1 and 3 (two middles, 2 and 4: 4 paths); C on 2 and 4 (friends): 0
    both("square + diagonal", [(1, 2), (2, 3), (3, 4), (4, 1), (2, 4)], {"A": {1, 3}, "C": {2, 4}}, {"A": 4, "C": 0})
    # the path again with each pair listed in both directions (the CSV may hold x,y and y,x): the same sets, the same count
    both("pairs listed twice", [(1, 2), (2, 1), (2, 3), (3, 2)], {"A": {1, 3}}, {"A": 2})
    # a star: centre 0 and leaves 1-5, tag A on every leaf: 5 * 4 ordered pairs through the centre
    both("star", [(0, i) for i in range(1, 6)], {"A": set(range(1, 6))}, {"A": 20})

    rng = random.Random(7)   # a random graph with many tags: equal to the replaced walk
    pairs = [(rng.randrange(60), rng.randrange(60)) for _ in range(400)]
    pairs = [(x, y) for x, y in pairs if x != y]
    P = {t: {p for p in range(60) if rng.random() < 0.15} for t in range(25)}
    adj = knows(pairs)
    got = {t: interested_pairs(adj, P).get(t, 0) for t in P}
    case(f"random graph ({len(pairs)} pairs, 25 tags, {sum(1 for v in got.values() if v)} with an answer): the walk",
         got, walk(adj, P))

    try:
        interested_pairs(knows([(1, 1), (1, 2)]), {"A": {1, 2}})
        case("a self-loop is refused", "not refused", "refused")
    except SystemExit:
        case("a self-loop is refused", "refused", "refused")

    print(f"bi interested pairs {'PASS' if not fails else 'FAIL'}: {cases} cases, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
