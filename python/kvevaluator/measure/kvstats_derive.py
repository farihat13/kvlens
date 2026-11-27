"""The derived cells of the writes capture -- ONE place for the formulas every writes figure draws
from (experiments/core_suite/plot_kvstats.py calls these; neither carries its own copy).
Reads the accumulated capture (measure/kvstats_layout), measures nothing.

    writes_cells(rows)          {slug: {query: cell}} from the counts rows -- per-op counts, the
                                weighted ops, bytes read / written, the payload
    durable_cells(sf, slug)     {query: cell} from durable-p1/<slug>/steps.csv -- the write latency
                                (apply wall) split read-modify-write | WAL | memtable | other

The write latency = the APPLY wall, the whole operation into a checkpoint arena (the real write
path: WAL + memtable), decomposed into four segments that sum to it:
  read-modify-write  the paired SHADOW wall -- the same op applied to the in-memory overlay, i.e.
                     the read + decode + re-encode without the store write (measure/cppwlatency)
  WAL, memtable      the breakdown regime's direct RocksDB timers (write_wal_time,
                     write_memtable_time)
  other              apply - shadow - WAL - memtable, the residual (the executor's own work, the
                     apply glue), clamped at 0
Three of the four segments are direct measurements; only the residual is derived, so there is no
subtraction of two nearly-equal walls. The WAL write is buffered: no fsync on the timed path.
"""
import csv
import os
import statistics
from collections import defaultdict

from kvevaluator import weights
from kvevaluator.measure.kvstats_layout import latency_csv

# the read weights of the read grids (python/kvevaluator/weights.py DEFAULT) + PUT / DELETE as a coarse
# key-touch count: the real write cost is the bytes and the measured
# latency, not the op weight
WRITE_WEIGHTS = {**weights.DEFAULT, "P": weights.DEFAULT["G"], "D": weights.DEFAULT["G"]}   # a PUT or DEL = one GET
WRITE_PER_GET = {k: v / WRITE_WEIGHTS["G"] for k, v in WRITE_WEIGHTS.items()}   # the same vector in GET units: what is shown
OPS = ("P", "D", "S", "G", "N")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def writes_cells(rows):
    """{slug: {query: cell}} -- means over params. rows = kvstats_layout.load_counts(...) (each row
    carries `db` = the slug, the lane names). cell keys: S G N P D, wops (WRITE_WEIGHTS), read_bytes (the
    read-modify-write's every read lane), written_bytes (write key + value), payload_bytes, wa = (read + written) /
    payload (None when the payload is 0)."""
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[r["db"]][r["query"]].append(r)
    out = {}
    for slug, qs in by.items():
        out[slug] = {}
        for q, rs in qs.items():
            c = {k: statistics.mean(_f(r.get(k)) for r in rs)
                 for k in (*OPS, "write_key", "write_value", "read_bytes", "payload_bytes")}
            c["wops"] = sum(c[k] * WRITE_WEIGHTS[k] for k in OPS) / WRITE_WEIGHTS["G"]   # in GET units
            c["written_bytes"] = c["write_key"] + c["write_value"]
            c["wa"] = (c["read_bytes"] + c["written_bytes"]) / c["payload_bytes"] if c["payload_bytes"] else None
            out[slug][q] = c
    return out


def durable_cells(sf, slug, workload="writes"):
    """{query: cell} -- write_us (the apply wall = the whole op), rmw_us (the shadow wall), wal_us,
    memtable_us (the direct timers), other_us (the residual, >= 0), durable_us (WAL + memtable);
    MEDIANS over the recorded sequence (measured latency is heavy-tailed; a mean is the outlier
    write). {} when the slug has no durable capture."""
    p = latency_csv(sf, workload, "durable-p1", slug)
    if not os.path.exists(p):
        return {}
    by_q = defaultdict(lambda: defaultdict(list))
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            if r["step"] == "__plan__":
                by_q[r["query"]][r["regime"]].append(r)
    cells = {}
    for q, regs in by_q.items():
        head = regs.get("headline") or regs.get("breakdown")
        if not head:
            continue
        apply = statistics.median(_f(r["wall_ns"]) for r in head) / 1e3
        shadow = statistics.median(_f(r["shadow_wall_ns"]) for r in head) / 1e3
        brk = regs.get("breakdown") or []
        wal = statistics.median(_f(r.get("write_wal_time")) for r in brk) / 1e3 if brk else 0.0
        mem = statistics.median(_f(r.get("write_memtable_time")) for r in brk) / 1e3 if brk else 0.0
        # the segments must sum to the apply wall: the shadow is capped at what the timers leave
        rmw = min(shadow, max(apply - wal - mem, 0.0))
        cells[q] = {"write_us": apply, "rmw_us": rmw, "wal_us": wal, "memtable_us": mem,
                    "other_us": max(apply - rmw - wal - mem, 0.0), "durable_us": wal + mem,
                    "shadow_us": shadow, "apply_us": apply}
    return cells
