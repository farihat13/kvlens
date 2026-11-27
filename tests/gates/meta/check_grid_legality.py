#!/usr/bin/env python3
"""The grid measures a body on a named store exactly where the generator calls it legal: grid_capture.invalid_on_cell
(validity caps + the ops the store's C++ format runs) equals plan_choice/generate.legal on every named store, query and body
of interactive, bi and microbench (the agreement gate runs generate.legal's set). Opens no store; asks the binary its ops.

    python3 tests/gates/meta/check_grid_legality.py
"""
import sys

from kvevaluator.capture.spec_sets import format_spec
from kvevaluator.measure.grid_capture import invalid_on_cell
from kvevaluator.plan_choice import generate as G
from kvevaluator.plan_choice.candidates import candidates, workload_queries


def main():
    n = d = 0
    for wl in ("interactive", "bi", "microbench"):
        schema = G._schema(wl)
        labels = sorted(set(schema.vertex_props) | set(schema.etypes))
        for st in [s for s in G.stores(wl) if s.spec is None]:
            sc = G.store_caps(st, schema, labels)
            inv = invalid_on_cell(wl, format_spec(st.name))
            for q in workload_queries(wl)[0]:
                gen = {b.name: G.legal(b, sc) for b in G.generate(wl, q).bodies.values()}
                for name, _ir in candidates(wl, q)[1]:
                    if name not in gen:
                        continue
                    n += 1
                    if bool(gen[name]) != (name in inv.get(q, {})):
                        d += 1
                        print(f"  DIFF {wl} {st.name} {q} {name}: generator lacks {gen[name]}, grid {inv.get(q, {}).get(name)}")
    ok = n > 0 and d == 0
    print(f"check_grid_legality: {n} (store, query, body) compared, {d} differ -- {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
