"""Measure a query execution's actual KV operations via KVProfiler: one plain run.

The 5 Dgraph knobs (typefilterpath, eqfilterpath, ineqfilterpath, localcache, detsort) are declared in the DGRAPH_*
env and applied once at server start (engines/start-server.sh reads the same vars); measure_kv does not re-pin them.
DGRAPH_KNOBS records the declared point. Nebula has no such knobs. The counters are reset right before execution, so
the result is per query. Dgraph's background rollup adds about 0 after a reset for these queries; if that changes,
pause rollup around the run (KVProfiler.dgraph_rollup_pause / resume).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


from kvengines.kv import KVProfiler

from ..kv_counts import KVCounts

# The 5 dgraph knobs (dgraph-profile.patch), each a mode applied via PUT /{knob}/{mode}:
#   typefilterpath / eqfilterpath / ineqfilterpath  -> default|index|per_vertex
#   localcache                                       -> disable|lock|enable
#   detsort                                          -> enable|disable  (deterministic sort)
# Each is read from DGRAPH_<KNOB> -- the SAME variable the start-server scripts read -- with a hard default,
# so one .env declares the operating point for both the server start and this record. Set a filter knob to
# "default" for the stock uid-count heuristic. These are DECLARATIVE here: they are applied to the server
# at start (start-server.sh) and left stable; measure_kv does not re-pin them (see _set_dgraph_knobs).
def _knob(name: str, default: str) -> str:
    return os.environ.get(f"DGRAPH_{name}") or default


DGRAPH_KNOBS = {
    "typefilterpath": _knob("TYPEFILTERPATH", "index"),
    "eqfilterpath": _knob("EQFILTERPATH", "index"),
    "ineqfilterpath": _knob("INEQFILTERPATH", "per_vertex"),
    "localcache": _knob("LOCALCACHE", "disable"),
    "detsort": _knob("DETSORT", "enable"),
}
_knob_warned = False


def _set_dgraph_knobs(profiler: KVProfiler, knobs: dict | None = None) -> None:
    """Re-pin the dgraph knobs (filter paths + localcache) on the live server, for a caller that wants another operating
    point: mutate DGRAPH_KNOBS (or pass `knobs`), then call this. A binary without the knob patch (404) gets a one-time
    warning."""
    global _knob_warned
    for knob, mode in (knobs or DGRAPH_KNOBS).items():
        try:
            profiler.dgraph_knob_set(knob, mode)
        except Exception as e:
            if not _knob_warned:
                print(f"  [warn] could not set dgraph knob {knob}={mode} "
                      f"({e}); binary may predate the knob patch — measuring with stock state.")
                _knob_warned = True


def pin_dgraph_config(profiler: KVProfiler, cache: str, ineqpath: str) -> dict:
    """Re-pin the dgraph operating point on the live server (a sweep, a plan capture), and pause rollup. `cache` is off|on
    (localcache disable|enable) or a raw localcache mode; `ineqpath` is per_vertex|index. Returns all 5 knobs as pinned."""
    DGRAPH_KNOBS["ineqfilterpath"] = ineqpath
    DGRAPH_KNOBS["localcache"] = {"off": "disable", "on": "enable"}.get(cache, cache)
    _set_dgraph_knobs(profiler)
    profiler.dgraph_rollup_pause()
    return {"cache_mode": cache,
            "ineqfilterpath": DGRAPH_KNOBS["ineqfilterpath"],
            "eqfilterpath": DGRAPH_KNOBS["eqfilterpath"],
            "typefilterpath": DGRAPH_KNOBS["typefilterpath"],
            "detsort": DGRAPH_KNOBS["detsort"]}


def measure_kv_normalized(profiler: KVProfiler, client, db: str, query: str, params: dict) -> dict:
    """Reset counters, run `query`, snapshot: the full normalized kvstats dict, {get,seek,next}_{count,bytes}. Sets no knob.
    seek_bytes is 0 for Nebula and Arango (RocksDB has no per-seek byte ticker), so their bytes read are get + next bytes."""
    if db == "dgraph":
        profiler.dgraph_kvstats_reset()
        client.execute(query, params)
        return KVProfiler._normalize_kvstats(profiler.dgraph_kvstats_snapshot())
    elif db == "nebula":
        profiler.nebula_kvstats_reset()
        client.execute(query, params)
        return KVProfiler._normalize_kvstats(profiler.nebula_kvstats_snapshot())
    elif db == "arangodb":
        # Arango /_admin/kvstats = process-global RocksDB tickers, so the server MUST run with
        # --server.statistics false (cost_eval/setup/arangodb/start-server.sh) to keep idle drift ~0.
        # No LocalCache / filter-path knobs (arango has none).
        profiler.arango_kvstats_reset()
        client.execute(query, params)
        return KVProfiler._normalize_kvstats(profiler.arango_kvstats_snapshot())
    raise ValueError(f"unknown db: {db}")


def measure_kv(profiler: KVProfiler, client, db: str, query: str, params: dict) -> KVCounts:
    """Reset counters, run `query`, snapshot: a KVCounts (S, G, N). `db` is "dgraph" | "nebula" | "arangodb". Sets no knob."""
    n = measure_kv_normalized(profiler, client, db, query, params)
    return KVCounts(seek=n["seek_count"], get=n["get_count"], next=n["next_count"])
