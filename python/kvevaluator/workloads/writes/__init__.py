"""Writes workload: the write primitives (writes/microbench, w_*) and the LDBC insert operations (writes/ldbc, iu*).

A primitive isolates one write -- update a property, insert or delete a vertex or an edge -- so a format's write
cost is quoted one operation at a time. Most mirror an mb2 row (MB81-88), so the counts can be checked against a
real engine's. w_del_v has no mb2 twin on purpose: the engines' default deletes differ (nebula leaves the edges,
dgraph's reaches the reverse postings), so w_del_v_record is the mb2 comparison and w_del_v (the vertex and its
edges) the fair cross-format one. An IU operation is a sum of primitives; both share one registry and one CSV, so
the composition can be checked.

    python3 -m kvevaluator.measure.grid_writes --sf 0.1 --format dgraph,nebula   # the named formats' stores
    python3 -m kvevaluator.measure.grid_writes --sf 0.1                          # every grid cell
    python3 tests/unit/check_write_model.py --sf 0.1                             # vs mb2's measured numbers
"""
