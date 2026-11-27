#!/usr/bin/env python3
"""The legality rules that follow a step's possible labels (kvevaluator.plan_choice.generate.possible_labels): no store, no binary.

  1. bi2's whole-row body (hand~8d4e82) reads creationDate under [Post, Comment] on hasTag in-neighbours, which hold
     Forums that reach the output: it needs label_scoped_props; bi16_index intersects a shared creationDate index with
     hasTag in-neighbours (Forums too): it needs per_type_index;
  2. controls: bi16's and bi8's props reads on the same neighbours drop the Forums before the output (no hasCreator
     edge; an inner join): no new cap;
  3. every reviewed exception (LABEL_LEAK_REVIEWED) is live: without it, its body gains label_scoped_props;
  4. a ratchet: the props reads under a label whose vids' labels the inference cannot follow (an op possible_labels
     does not model) are taken as legal; their count may only fall (a new one is a body to review by hand).

    python3 tests/unit/check_label_inference.py
"""
import sys

from kvevaluator.plan_choice import generate as G


def body(wl, q, name):
    for b in G.generate(wl, q).bodies.values():
        if b.name == name:
            return b
    raise SystemExit(f"{wl}/{q}: no body {name!r}")


def needs(wl, q, name, reviewed=True):
    b = body(wl, q, name)
    keep = dict(G.LABEL_LEAK_REVIEWED)
    try:
        if not reviewed:
            G.LABEL_LEAK_REVIEWED.clear()
        return set(G.op_requires(b.steps, G._schema(wl), b.output, (wl, q)))
    finally:
        G.LABEL_LEAK_REVIEWED.clear()
        G.LABEL_LEAK_REVIEWED.update(keep)


UNFOLLOWED_MAX = 155       # the count (ldbc, bi, microbench generated bodies)


def unfollowed_reads():
    """the (body, step) props reads under a label whose vids possible_labels cannot follow, over every generated body"""
    from kvevaluator.plan_choice.candidates import workload_queries
    n = 0
    for wl in ("interactive", "bi", "microbench"):
        schema = G._schema(wl)
        for q in workload_queries(wl)[0]:
            for b in G.generate(wl, q).bodies.values():
                lab = G.possible_labels(b.steps, schema)
                for s in b.steps:
                    r = G._refs(s.get("vids"))
                    if s.get("op") == "read_vertex_props" and G._labels(s) and len(r) == 1 and next(iter(r)) not in lab:
                        n += 1
    return n


def main():
    bad = 0
    cases = [("plant", "bi", "bi2", "hand~8d4e82", "label_scoped_props", True),
             ("plant", "bi", "bi16", "bi16_index(hand)", "per_type_index", True),
             ("control", "bi", "bi16", "hand", "label_scoped_props", False),
             ("control", "bi", "bi8", "hand~828f07", "label_scoped_props", False)]
    for kind, wl, q, name, cap, want in cases:
        got = cap in needs(wl, q, name)
        ok = got == want
        bad += not ok
        print(f"{'PASS' if ok else 'FAIL'} {kind:7s} {wl}/{q} {name}: needs {cap} = {got} (want {want})")
    live = 0
    for (wl, q, step, _vids), _why in sorted(G.LABEL_LEAK_REVIEWED.items()):
        names = [b.name for b in G.generate(wl, q).bodies.values() if any(s.get("name") == step for s in b.steps)]
        hit = [n for n in names if "label_scoped_props" in needs(wl, q, n, reviewed=False)
               and "label_scoped_props" not in needs(wl, q, n)]
        live += bool(hit)
        print(f"{'PASS' if hit else 'FAIL'} reviewed {wl}/{q} step {step}: without the entry {len(hit)} of "
              f"{len(names)} bodies would need label_scoped_props")
        bad += not hit
    unknown = unfollowed_reads()
    ok = unknown <= UNFOLLOWED_MAX
    bad += not ok
    print(f"{'PASS' if ok else 'FAIL'} ratchet: {unknown} props reads under a label on vids of unfollowed labels "
          f"(at most {UNFOLLOWED_MAX}; lower UNFOLLOWED_MAX when it falls, review a new one by hand)")
    print(f"check_label_inference: {len(cases)} cases, {len(G.LABEL_LEAK_REVIEWED)} reviewed exceptions "
          f"({live} live), {bad} failed")
    return 1 if bad or not G.LABEL_LEAK_REVIEWED else 0


if __name__ == "__main__":
    sys.exit(main())
