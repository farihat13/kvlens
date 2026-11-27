"""KV operation profiling for Dgraph, NebulaGraph, ArangoDB and TuGraph: reset -> execute -> snapshot of the engine's
kvstats, per-op key tracing (Dgraph's from alpha.log), and key decoding.

Usage:
    from kvengines.kv import KVProfiler

    profiler = KVProfiler(
        dgraph_url="http://localhost:8080",
        nebula_kvstats_url="http://127.0.0.1:19779/kvstats",
        alpha_log_path="/path/to/dgraph/runs/alpha.log",
    )

    # Simple kvstats measurement
    stats = profiler.dgraph_measure(lambda: client.execute(q, params))

    # With key-level tracing
    stats, trace = profiler.dgraph_trace(lambda: client.execute(q, params))
    profiler.print_breakdown(stats, trace)
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

import requests


# ── Badger key decoding ──────────────────────────────────────────────

# TypeByte values — located at b[11 + attr_len], NOT b[0].
# b[0] is always 0x00 (DefaultPrefix) for data/index/reverse/count keys.
# Values from x/keys.go ByteXxx constants in dgraph-25.0.0.
_KEY_TYPES = {
    0x00: "data",
    # 0x01 (ByteSchema) and 0x02 (ByteType) are b[0] markers, not TypeByte values.
    # Schema/type keys have a different layout and are handled by the prefix check.
    0x02: "index",
    0x04: "reverse",
    0x08: "count",
    0x0C: "count_rev",  # ByteCount | ByteReverse
}

# Dgraph tokenizer identifiers (prepended to index tokens).
# Values from tok/tok.go IdentXxx constants in dgraph-25.0.0.
_TOKENIZERS = {
    0x01: "term",      # IdentTerm     — token is a term string
    0x02: "exact",     # IdentExact    — token is the raw string
    0x03: "exact_lang",# IdentExactLang— token is lang-tagged string
    0x04: "year",      # IdentYear     — token is year(2B)
    0x05: "geo",       # IdentGeo      — token is geo cell
    0x06: "int",       # IdentInt      — token is sign(1B)+value(8B)
    0x07: "float",     # IdentFloat    — token is sign(1B)+value(8B)
    0x08: "fulltext",  # IdentFullText — token is a term string
    0x09: "bool",      # IdentBool     — token is 9 bytes
    0x0A: "trigram",   # IdentTrigram  — token is a 3-char string
    0x0B: "hash",      # IdentHash     — token is a hash string
    0x0C: "sha256",    # IdentSha      — token is a SHA256 hash
    0x0D: "bigfloat",  # IdentBigFloat
    0x0E: "vfloat",    # IdentVFloat
    0x0F: "ngram",     # IdentNGram
    0x41: "month",     # IdentMonth    — token is year(2B)+month(2B)
    0x42: "day",       # IdentDay      — token is year(2B)+month(2B)+day(2B)
    0x43: "hour",      # IdentHour     — token is year(2B)+month(2B)+day(2B)+hour(2B)
}


def _decode_token(tokenizer_id: int, token_bytes: bytes) -> str | None:
    """Decode an index token into a human-readable string.
    Returns None if the tokenizer is unknown or token is empty.
    """
    if not token_bytes:
        return None

    tid = _TOKENIZERS.get(tokenizer_id)

    if tid == "day" and len(token_bytes) == 6:
        y = int.from_bytes(token_bytes[0:2], "big")
        m = int.from_bytes(token_bytes[2:4], "big")
        d = int.from_bytes(token_bytes[4:6], "big")
        return f"{y:04d}-{m:02d}-{d:02d}"

    if tid == "year" and len(token_bytes) == 2:
        y = int.from_bytes(token_bytes[0:2], "big")
        return f"{y:04d}"

    if tid == "month" and len(token_bytes) == 4:
        y = int.from_bytes(token_bytes[0:2], "big")
        m = int.from_bytes(token_bytes[2:4], "big")
        return f"{y:04d}-{m:02d}"

    if tid == "hour" and len(token_bytes) == 8:
        y = int.from_bytes(token_bytes[0:2], "big")
        m = int.from_bytes(token_bytes[2:4], "big")
        d = int.from_bytes(token_bytes[4:6], "big")
        h = int.from_bytes(token_bytes[6:8], "big")
        return f"{y:04d}-{m:02d}-{d:02d}T{h:02d}"

    if tid in ("exact", "exact_lang", "term", "fulltext", "sha256"):
        return token_bytes.decode("utf-8", errors="replace")

    if tid == "hash":
        # Raw binary blake2b hash — display as hex, not UTF-8
        return token_bytes.hex()

    if tid in ("int", "float", "bool") and len(token_bytes) == 9:
        # Sign byte (0=negative, 1=positive, for sort order) + 8-byte two's complement int64.
        # Read as signed int64 — correct for both positive and negative values.
        val = int.from_bytes(token_bytes[1:9], "big", signed=True)
        if tid == "bool":
            return str(bool(val))
        return str(val)

    return token_bytes.hex()


def decode_badger_key(hex_str: str) -> dict:
    """Decode a Dgraph Badger key from hex string.

    Key format:
      [0x00 basePrefix][namespace:8B][attr_len:2B][attr:varB][TypeByte:1B][tail:varB]
       b[0]             b[1:9]       b[9:11]      b[11:11+N] b[11+N]     b[12+N:]

    TypeByte determines what the tail contains:
      ByteData (0x00)    → tail = uid (8 bytes)
      ByteIndex (0x02)   → tail = tokenizer_id (1 byte) + token (variable)
      ByteReverse (0x04) → tail = uid (8 bytes)
      ByteCount (0x08)   → tail = uid (8 bytes)

    Returns dict with: key_type, namespace, attr, uid (data/reverse/count),
    token (index), tokenizer (index), raw_hex
    """
    try:
        b = bytes.fromhex(hex_str)
    except ValueError:
        return {"raw_hex": hex_str, "attr": "?", "error": "invalid hex"}

    if len(b) < 11:
        return {"raw_hex": hex_str, "attr": "?", "error": "too short"}

    namespace = int.from_bytes(b[1:9], "big")
    attr_len = int.from_bytes(b[9:11], "big")
    attr = b[11:11 + attr_len].decode("ascii", errors="replace")

    type_pos = 11 + attr_len
    if len(b) <= type_pos:
        # Key is just the prefix (no TypeByte or tail) — used as iterator prefix
        return {
            "key_type": "prefix",
            "namespace": namespace,
            "attr": attr,
            "uid": None,
            "token": None,
            "tokenizer": None,
            "raw_hex": hex_str,
        }

    key_type_byte = b[type_pos]
    key_type = _KEY_TYPES.get(key_type_byte, f"0x{key_type_byte:02x}")
    tail = b[type_pos + 1:]

    uid = None
    token = None
    tokenizer = None

    if key_type_byte == 0x02:
        # Index key: tail = tokenizer_id(1B) + token(varB)
        if len(tail) >= 1:
            tok_id = tail[0]
            tokenizer = _TOKENIZERS.get(tok_id, f"0x{tok_id:02x}")
            token_bytes = tail[1:]
            token = _decode_token(tok_id, token_bytes)
            if token is None:
                token = token_bytes.hex() if token_bytes else ""
    elif key_type_byte in (0x08, 0x0C):
        # Count / count_rev: tail = count (4 bytes, big-endian uint32)
        if len(tail) >= 4:
            uid = int.from_bytes(tail[:4], "big")  # stored in 'uid' field for simplicity
    else:
        # Data, reverse: tail = uid (up to 8 bytes, big-endian).
        # Trace logging can occasionally drop a leading zero byte, so
        # accept 1-8 bytes and interpret as big-endian uint64.
        if len(tail) >= 1:
            uid = int.from_bytes(tail[:8], "big")

    return {
        "key_type": key_type,
        "namespace": namespace,
        "attr": attr,
        "uid": uid,
        "token": token,
        "tokenizer": tokenizer,
        "raw_hex": hex_str,
    }


def decode_nebula_key(hex_str: str, vid_len: int = 30) -> dict:
    """Decode a NebulaGraph RocksDB key from hex string.

    Key formats (FIXED_STRING vid, vid_len=30 for LDBC):
      Tag key:  type(1) + partId(3) + vertexId(vid_len) + tagId(4)
      Edge key: type(1) + partId(3) + srcId(vid_len) + edgeType(4) + rank(8) + dstId(vid_len) + placeholder(1)

    Type bytes: 0x01 = vertex/tag, 0x02 = edge, 0x03 = index, 0x04 = system

    Special cases (from expanded trace ops):
      - Empty key: 'scan' op (full table scan)
      - "<hex> - <hex>": 'range_with_prefix' or 'remove_range' (contains two keys joined by ' - ')
    """
    # Handle empty key (scan op)
    if not hex_str:
        return {"raw_hex": "", "attr": "(full scan)", "key_type": "scan"}

    # Handle composite keys from range_with_prefix and remove_range: "hex1 - hex2"
    if " - " in hex_str:
        parts = hex_str.split(" - ")
        if len(parts) == 2:
            d1 = decode_nebula_key(parts[0], vid_len)
            d2 = decode_nebula_key(parts[1], vid_len)
            return {
                "raw_hex": hex_str,
                "attr": f"{d1.get('attr', '?')} → {d2.get('attr', '?')}",
                "key_type": "composite",
                "start": d1,
                "end": d2,
            }

    try:
        b = bytes.fromhex(hex_str)
    except ValueError:
        return {"raw_hex": hex_str, "attr": "?", "error": "invalid hex"}

    if len(b) < 4:
        return {"raw_hex": hex_str, "attr": "?", "error": "too short"}

    key_type_byte = b[0]
    part_id = int.from_bytes(b[1:4], "big")

    # Tag key: type=0x01, length = 1 + 3 + vid_len + 4 = 38 for vid_len=30
    tag_key_len = 1 + 3 + vid_len + 4
    # Edge key: type=0x02, length = 1 + 3 + vid_len + 4 + 8 + vid_len + 1 = 77 for vid_len=30
    edge_key_len = 1 + 3 + vid_len + 4 + 8 + vid_len + 1

    if key_type_byte == 0x01 and len(b) >= tag_key_len:
        # Tag key
        vid_bytes = b[4:4 + vid_len]
        vid = vid_bytes.rstrip(b'\x00').decode("utf-8", errors="replace")
        tag_id = int.from_bytes(b[4 + vid_len:4 + vid_len + 4], "big")
        return {
            "key_type": "tag",
            "part_id": part_id,
            "vid": vid,
            "tag_id": tag_id,
            "attr": f"tag:{tag_id}",
            "raw_hex": hex_str,
        }
    elif key_type_byte == 0x02 and len(b) >= edge_key_len:
        # Edge key
        src_start = 4
        src_bytes = b[src_start:src_start + vid_len]
        src_vid = src_bytes.rstrip(b'\x00').decode("utf-8", errors="replace")
        edge_type = int.from_bytes(b[src_start + vid_len:src_start + vid_len + 4], "big")
        rank_bytes = b[src_start + vid_len + 4:src_start + vid_len + 12]
        rank = int.from_bytes(rank_bytes, "big")
        dst_start = src_start + vid_len + 12
        dst_bytes = b[dst_start:dst_start + vid_len]
        dst_vid = dst_bytes.rstrip(b'\x00').decode("utf-8", errors="replace")
        return {
            "key_type": "edge",
            "part_id": part_id,
            "src_vid": src_vid,
            "edge_type": edge_type,
            "rank": rank,
            "dst_vid": dst_vid,
            "vid": src_vid,
            "attr": f"edge:{edge_type}",
            "raw_hex": hex_str,
        }
    else:
        type_name = {0x01: "tag", 0x02: "edge", 0x03: "index", 0x04: "system"}.get(
            key_type_byte, f"0x{key_type_byte:02x}")
        return {
            "key_type": type_name,
            "part_id": part_id,
            "attr": type_name,
            "raw_hex": hex_str,
        }


def decode_arango_key(hex_str: str, cf: str = "") -> dict:
    """Decode an ArangoDB RocksDB key from hex.

    Every ArangoDB key begins with an 8-byte big-endian objectId naming the collection (documents family) or the index
    (primary / edge / vpack / ...). The column family, passed through from the /_admin/kvtrace entry, tells how to read
    the tail:
      documents : objectId(8) + localDocumentId(8)
      primary   : objectId(8) + _key   (ascii)
      edge      : objectId(8) + vertex id "coll/key" (ascii) + 0x00 + localId(8)
      other     : objectId(8) + raw tail (hex)
    Falls back to raw hex when the key is too short to carry an objectId.
    """
    if not hex_str:
        return {"raw_hex": "", "attr": cf or "(empty)", "cf": cf, "key_type": "empty"}
    try:
        b = bytes.fromhex(hex_str)
    except ValueError:
        return {"raw_hex": hex_str, "attr": cf or "?", "cf": cf, "error": "invalid hex"}
    if len(b) < 8:
        return {"raw_hex": hex_str, "attr": cf or "?", "cf": cf, "error": "too short"}

    object_id = int.from_bytes(b[0:8], "big")
    tail = b[8:]
    out = {
        "raw_hex": hex_str,
        "cf": cf,
        "key_type": cf or "?",
        "object_id": object_id,
        "attr": f"{cf}:{object_id}" if cf else f"obj:{object_id}",
    }

    if cf == "documents" and len(tail) >= 8:
        out["local_id"] = int.from_bytes(tail[:8], "big")
    elif cf == "primary":
        out["doc_key"] = tail.rstrip(b"\x00").decode("utf-8", errors="replace")
    elif cf == "edge":
        # vertex id ("coll/key") runs up to the 0x00 separator
        nul = tail.find(b"\x00")
        vtx = tail[:nul] if nul >= 0 else tail
        out["vertex"] = vtx.decode("utf-8", errors="replace")
    elif tail:
        out["tail_hex"] = tail.hex()
    return out


# ── Log parsing ──────────────────────────────────────────────────────

def _parse_trace_lines(lines: list[str]) -> list[dict]:
    """Parse T/BG trace lines into structured entries.

    Each line: T/BG: {op} key={hex} prefix={hex}
    Returns list of dicts: {op, key_hex, prefix_hex, decoded}
    """
    entries = []
    for line in lines:
        if "T/BG:" not in line:
            continue
        # Extract op and key from: T/BG: Seek key=abcd prefix=efgh
        try:
            parts = line.split("T/BG:")[1].strip().split()
            op = parts[0]
            key_hex = ""
            prefix_hex = ""
            for p in parts[1:]:
                if p.startswith("key="):
                    key_hex = p[4:]
                elif p.startswith("prefix="):
                    prefix_hex = p[7:]
            decoded = decode_badger_key(key_hex) if key_hex else {}
            entries.append({
                "key_hex": key_hex,
                "prefix_hex": prefix_hex,
                "op": op,
                "attr": decoded.get("attr", "?"),
                "uid": decoded.get("uid"),
                "key_type": decoded.get("key_type", "?"),
                "token": decoded.get("token"),
                "tokenizer": decoded.get("tokenizer"),
            })
        except (IndexError, ValueError):
            continue
    return entries


# ── KV Profiler ──────────────────────────────────────────────────────

class KVProfiler:
    """KV operation profiler for Dgraph, NebulaGraph, and ArangoDB.

    Handles kvstats reset/snapshot, kvtrace enable/disable (Dgraph's parsed from alpha.log), and rollup pause/resume.

    ArangoDB's `/_admin/kvstats` and `/_admin/kvtrace` are process-global (one RocksDBEngine, not per query), so the
    caller must keep the server quiescent during a measurement. `arango_trace()` records one entry per GET and per
    iterator SEEK (NEXT/PREV stay counter-only), each tagged with its column family. `_normalize_kvstats` maps the
    counters to GET/SEEK/NEXT; Arango's absolute counts are not directly comparable to Dgraph's or Nebula's.

    TuGraph's `/kvstats` (its own port, engines/tugraph/) counts its LMDB calls in Dgraph's field names (`get_count`,
    `seek_count`, `next_count`, `prev_count`, their `_bytes` and `_latency_ns`), so the generic branch reads it;
    `get_current_*` (a read of the entry a cursor stands on, no move) is kept in the raw dump and in no total.
    """

    def __init__(
        self,
        dgraph_url: str = "http://localhost:8080",
        nebula_kvstats_url: str = "http://127.0.0.1:19779/kvstats",
        arango_kvstats_url: str = "http://127.0.0.1:8529/_admin/kvstats",
        tugraph_kvstats_url: str = "http://127.0.0.1:7072/kvstats",
        alpha_log_path: str | Path | None = None,
        timeout: int = 10,
    ):
        self.dgraph_url = dgraph_url
        self.nebula_kvstats_url = nebula_kvstats_url
        self.arango_kvstats_url = arango_kvstats_url
        self.tugraph_kvstats_url = tugraph_kvstats_url
        self.alpha_log = Path(alpha_log_path) if alpha_log_path else None
        self.timeout = timeout
        self._log_offset = 0

        # Sync log offset to end of file
        if self.alpha_log and self.alpha_log.exists():
            self._log_offset = self.alpha_log.stat().st_size

    # ── Dgraph kvstats ───────────────────────────────────────────

    def dgraph_kvstats_reset(self):
        requests.put(f"{self.dgraph_url}/kvstats/reset", timeout=self.timeout).raise_for_status()

    def dgraph_kvstats_snapshot(self) -> dict:
        resp = requests.get(f"{self.dgraph_url}/kvstats", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def dgraph_measure(self, execute_fn: Callable, repeat: int = 1) -> dict:
        """Reset → execute → snapshot, repeat N times and return snapshot with fewest ops."""
        best = None
        for _ in range(repeat):
            self.dgraph_kvstats_reset()
            execute_fn()
            stats = self.dgraph_kvstats_snapshot()
            n = KVProfiler._normalize_kvstats(stats)
            w = KVProfiler._normalize_writes(stats)
            total = (n["get_count"] + n["seek_count"] + n["next_count"]
                     + w["put"] + w["delete"])
            if best is None or total < best[1]:
                best = (stats, total)
        return best[0]

    def dgraph_measure_write(self, execute_fn: Callable) -> dict:
        """Measure a Dgraph mutation. Dgraph does not write the posting store at commit: the mutation appends an in-memory
        delta and the badger SET happens at rollup, so a write measurement forces the delta to materialize as counted SETs.
        Both modes keep the mutation's own get/seek/next (its read-modify-write reads) from the pre-roll snapshot and take
        the write counters (set_count/bytes) from the post-roll one, so the rollup's own scan reads are not counted here.

        Mode via env DGRAPH_WRITE_MEASURE (default 'drain'):
          - 'drain':    roll up only this mutation's dirtied keys via the apply-path recorder: no full-store scan, no
                        cross-talk between params. Checked equal to forceall. Needs the addMutationInternal
                        recorder patch.
          - 'forceall': roll up the whole store: slower (a full scan per param), but assumes nothing about the store
                        being settled.
        Not repeated: a mutation cannot be replayed (a fresh id per row), and its counts are deterministic.
        """
        mode = os.environ.get("DGRAPH_WRITE_MEASURE", "drain")
        if mode == "forceall":
            return self._dgraph_measure_write_forceall(execute_fn)
        return self._dgraph_measure_write_drain(execute_fn)

    def _merge_write(self, reads: dict, full: dict) -> dict:
        """Write counters (set_*) from the post-roll snapshot `full`; read counters
        (get/seek/next) from the pre-roll snapshot `reads`, so the roll-up's own scan
        reads are not counted as the mutation's reads."""
        merged = dict(full)
        for k in ("get_count", "seek_count", "next_count",
                  "get_bytes", "seek_bytes", "next_bytes", "get_found_count"):
            if k in reads:
                merged[k] = reads[k]
        return merged

    def _dgraph_measure_write_forceall(self, execute_fn: Callable) -> dict:
        """forceall rolls up ALL keys, so clear any pre-existing pending deltas FIRST
        (pre-forceall) — otherwise leftover deltas (e.g. from the load) would be counted
        as this mutation's SETs."""
        self.dgraph_rollup_forceall()            # clear any pre-existing deltas
        self.dgraph_kvstats_reset()
        execute_fn()
        reads = self.dgraph_kvstats_snapshot()   # mutation's RMW reads; set_count ~0
        self.dgraph_rollup_forceall()            # materialize this mutation's SETs
        full = self.dgraph_kvstats_snapshot()
        return self._merge_write(reads, full)

    def _dgraph_measure_write_drain(self, execute_fn: Callable) -> dict:
        """The apply-path recorder captures this mutation's dirtied posting-list keys, and
        /rollup/drain rolls up ONLY those — no full-store scan, no cross-talk from other
        keys' pending deltas. Rollup is paused so only our manual drain rolls up. Assumes a
        settled store at entry (start-server.sh forceall's after restore); per-mutation
        drain keeps it settled."""
        self.dgraph_rollup_pause()
        try:
            self.dgraph_rollup_drain_enable()        # start recording (clears prior set)
            self.dgraph_kvstats_reset()
            execute_fn()
            reads = self.dgraph_kvstats_snapshot()   # mutation's RMW reads; set_count ~0
            self.dgraph_rollup_drain()               # roll up ONLY this mutation's keys
            full = self.dgraph_kvstats_snapshot()
        finally:
            self.dgraph_rollup_drain_disable()
            self.dgraph_rollup_resume()
        return self._merge_write(reads, full)

    # ── Nebula kvstats ───────────────────────────────────────────

    def nebula_kvstats_reset(self):
        requests.put(f"{self.nebula_kvstats_url}/reset", timeout=self.timeout).raise_for_status()

    def nebula_kvstats_snapshot(self) -> dict:
        resp = requests.get(self.nebula_kvstats_url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def nebula_measure(self, execute_fn: Callable, repeat: int = 1) -> dict:
        """Reset → execute → snapshot, repeat N times and return snapshot with fewest ops."""
        best = None
        for _ in range(repeat):
            self.nebula_kvstats_reset()
            execute_fn()
            stats = self.nebula_kvstats_snapshot()
            n = KVProfiler._normalize_kvstats(stats)
            w = KVProfiler._normalize_writes(stats)
            total = n["get_count"] + n["seek_count"] + n["next_count"] + w["put"] + w["delete"]
            if best is None or total < best[1]:
                best = (stats, total)
        return best[0]

    # ── ArangoDB kvstats ──────────────────────────────────────────
    # /_admin/kvstats response depends on the compiled flags (see
    # engines/arangodb/patches/provenance.txt):
    #   ARANGO_KV_TICKERS    -> "tickers" + "native_histograms" (RocksDB native,
    #                           process-global; need --rocksdb.enable-statistics)
    #   ARANGO_KV_STATS      -> flat get_/seek_/next_/prev_ count+found+time_ns+bytes
    #                           (engine-wrapper hand counters; query-scoped, the
    #                           analog of Nebula's KV_STATS — preferred for counts)
    #   ARANGO_KV_HISTOGRAMS -> "histograms" (engine get/seek/next/prev latency)
    # _normalize_kvstats prefers the engine-wrapper counts when present and only
    # falls back to the process-global tickers otherwise. The ARANGO_KV_STATS build
    # also exposes /_admin/kvtrace (see the arango kvtrace section below).

    def arango_kvstats_reset(self):
        requests.put(f"{self.arango_kvstats_url}/reset", timeout=self.timeout).raise_for_status()

    def arango_kvstats_snapshot(self) -> dict:
        resp = requests.get(self.arango_kvstats_url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def arango_measure(self, execute_fn: Callable, repeat: int = 1) -> dict:
        """Reset → execute → snapshot, repeat N times and return snapshot with fewest ops.

        Keeping the fewest-ops run mirrors dgraph_measure/nebula_measure: it
        suppresses the occasional background-ticker spike (compaction, stats
        flush) that would otherwise inflate a single sample.
        """
        best = None
        for _ in range(repeat):
            self.arango_kvstats_reset()
            execute_fn()
            stats = self.arango_kvstats_snapshot()
            n = KVProfiler._normalize_kvstats(stats)
            w = KVProfiler._normalize_writes(stats)
            total = n["get_count"] + n["seek_count"] + n["next_count"] + w["put"] + w["delete"]
            if best is None or total < best[1]:
                best = (stats, total)
        return best[0]

    # ── TuGraph kvstats ──────────────────────────────────────────
    # process-global like ArangoDB's: the caller keeps the server quiescent during a measurement

    def tugraph_kvstats_reset(self):
        requests.put(f"{self.tugraph_kvstats_url}/reset", timeout=self.timeout).raise_for_status()

    def tugraph_kvstats_snapshot(self) -> dict:
        resp = requests.get(self.tugraph_kvstats_url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ── ArangoDB kvtrace ──────────────────────────────────────────
    # The ArangoDB analog of Nebula's /kvtrace. Records one entry per engine-
    # wrapper GET and per iterator SEEK; NEXT/PREV stay counter-only. Each entry
    # carries the RocksDB column family, so decode_arango_key can label the key
    # (Documents / PrimaryIndex / EdgeIndex / ...) without full byte decoding.
    # Process-global like the counters → keep the server quiescent during a trace.

    @property
    def _arango_kvtrace_url(self):
        base = self.arango_kvstats_url.rsplit("/_admin/kvstats", 1)[0]
        return f"{base}/_admin/kvtrace"

    def arango_kvtrace_enable(self, max_entries: int | None = None):
        """Enable the trace (clears the buffer). max_entries → ?max= raises the
        server-side cap (default 100000)."""
        url = f"{self._arango_kvtrace_url}/enable"
        if max_entries is not None:
            url = f"{url}?max={int(max_entries)}"
        requests.put(url, timeout=self.timeout).raise_for_status()

    def arango_kvtrace_disable(self):
        requests.put(f"{self._arango_kvtrace_url}/disable", timeout=self.timeout).raise_for_status()

    def arango_kvtrace_fetch(self) -> list[dict]:
        """Fetch trace entries from the server and decode each key."""
        resp = requests.get(self._arango_kvtrace_url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        # The buffer is capped server-side; if it filled, entries were dropped and
        # the trace no longer matches the kvstats counts. Warn loudly — silent
        # truncation would look like a parity bug. Raise --max-trace to fix.
        count, cap = data.get("count", 0), data.get("max", 0)
        if cap and count >= cap:
            print(f"  WARNING: arango kvtrace hit the {cap}-entry cap — trace is "
                  f"TRUNCATED and will undercount vs kvstats. Raise --max-trace.")
        entries = []
        for e in data.get("entries", []):
            cf = e.get("cf", "")
            decoded = decode_arango_key(e.get("key", ""), cf)
            # The server resolves objectId -> collection/index name in-engine
            # ("Person", "Person.creationDate"); the local decode adds the row
            # identity (doc_key / vertex / local_id), the analog of Dgraph's uid
            # or Nebula's vid. Together: name + row, like the other two DBs.
            name = e.get("name", "")
            entries.append({
                "op": e.get("op", "?"),
                "key_hex": e.get("key", ""),
                "key_size": e.get("key_size", 0),
                "value_size": e.get("value_size", 0),
                "cf": cf,
                "name": name,
                # Group the trace summary by the resolved name (falls back to cf).
                "attr": name or cf or "?",
                "object_id": e.get("object_id", decoded.get("object_id")),
                "doc_key": decoded.get("doc_key"),
                "vertex": decoded.get("vertex"),
                "local_id": decoded.get("local_id"),
            })
        return entries

    def arango_trace(self, execute_fn: Callable,
                     max_entries: int | None = None) -> tuple[dict, list[dict]]:
        """Measure kvstats AND collect the key trace in one shot, mirroring
        nebula_trace: enable → reset → execute → snapshot → fetch → disable.

        Returns (kvstats_dict, trace_entries). Unlike the *_measure paths there is
        no fewest-of-N repeat — a trace is a single faithful capture, so the caller
        must keep the server quiescent (single-threaded, --server.statistics false).
        """
        self.arango_kvtrace_enable(max_entries=max_entries)
        try:
            self.arango_kvstats_reset()
            execute_fn()
            stats = self.arango_kvstats_snapshot()
            trace = self.arango_kvtrace_fetch()
        finally:
            # The buffer is process-global; a raising query must not leave tracing
            # on (it would pollute every later query and pin the buffer at the cap).
            self.arango_kvtrace_disable()
        return stats, trace

    # ── Nebula kvtrace ────────────────────────────────────────────

    @property
    def _nebula_kvtrace_url(self):
        # kvtrace is on the same storaged HTTP port as kvstats (19779)
        base = self.nebula_kvstats_url.rsplit("/kvstats", 1)[0]
        return f"{base}/kvtrace"

    def nebula_kvtrace_enable(self, max_entries: int | None = None):
        """Enable storaged kvtrace. If max_entries is set, pass it as ?max= so
        the server allocates a larger buffer (default on server side: 100000)."""
        url = f"{self._nebula_kvtrace_url}/enable"
        if max_entries is not None:
            url = f"{url}?max={int(max_entries)}"
        requests.put(url, timeout=self.timeout).raise_for_status()

    def nebula_kvtrace_disable(self):
        requests.put(f"{self._nebula_kvtrace_url}/disable", timeout=self.timeout).raise_for_status()

    def nebula_kvtrace_fetch(self) -> list[dict]:
        """Fetch trace entries from storaged and decode keys."""
        resp = requests.get(self._nebula_kvtrace_url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        entries = []
        for e in data.get("entries", []):
            decoded = decode_nebula_key(e.get("key", ""))
            entries.append({
                "op": e.get("op", "?"),
                "key_hex": e.get("key", ""),
                "key_size": e.get("key_size", 0),
                "value_size": e.get("value_size", 0),
                "key_type": decoded.get("key_type", "?"),
                "attr": decoded.get("attr", "?"),
                "vid": decoded.get("vid"),
                "tag_id": decoded.get("tag_id"),
                "edge_type": decoded.get("edge_type"),
            })
        return entries

    def nebula_trace(self, execute_fn: Callable,
                     max_entries: int | None = None) -> tuple[dict, list[dict]]:
        """Measure kvstats AND collect key trace in one shot.

        Returns (kvstats_dict, trace_entries). Set max_entries to raise the
        storaged-side trace buffer cap (default 100000 entries).
        """
        self.nebula_kvtrace_enable(max_entries=max_entries)
        self.nebula_kvstats_reset()
        execute_fn()
        stats = self.nebula_kvstats_snapshot()
        trace = self.nebula_kvtrace_fetch()
        self.nebula_kvtrace_disable()
        return stats, trace

    # ── Dgraph kvtrace ───────────────────────────────────────────

    def dgraph_kvtrace_enable(self, ops: list[str] | None = None, level: str = "keys"):
        """Enable KV tracing for specified ops at given level.

        Args:
            ops: List of ops to trace. Default: ["seek", "get"].
            level: "keys" (one line per op) or "stack" (+ goroutine stack trace).
        """
        if ops is None:
            ops = ["seek", "get"]
        # Record log position before enabling
        if self.alpha_log and self.alpha_log.exists():
            self._log_offset = self.alpha_log.stat().st_size
        for op in ops:
            requests.put(
                f"{self.dgraph_url}/kvtrace/{op}/{level}",
                timeout=self.timeout,
            ).raise_for_status()

    def dgraph_kvtrace_disable(self):
        requests.put(
            f"{self.dgraph_url}/kvtrace/all/disable",
            timeout=self.timeout,
        ).raise_for_status()

    def _read_new_log_lines(self) -> list[str]:
        """Read new lines from alpha.log since last offset."""
        if not self.alpha_log or not self.alpha_log.exists():
            return []
        with open(self.alpha_log, "rb") as f:
            f.seek(self._log_offset)
            new_data = f.read()
            self._log_offset = f.tell()
        return new_data.decode("utf-8", errors="replace").splitlines()

    def dgraph_trace(
        self,
        execute_fn: Callable,
        ops: list[str] | None = None,
        level: str = "keys",
    ) -> tuple[dict, list[dict]]:
        """Measure kvstats AND collect key trace in one shot.

        Returns (kvstats_dict, trace_entries).
        """
        self.dgraph_kvtrace_enable(ops=ops, level=level)
        self.dgraph_kvstats_reset()
        execute_fn()
        stats = self.dgraph_kvstats_snapshot()
        self.dgraph_kvtrace_disable()
        lines = self._read_new_log_lines()
        trace = _parse_trace_lines(lines)
        return stats, trace

    # ── Dgraph rollup control ────────────────────────────────────

    def dgraph_rollup_pause(self):
        resp = requests.put(f"{self.dgraph_url}/rollup/pause", timeout=self.timeout)
        resp.raise_for_status()

    def dgraph_rollup_resume(self):
        resp = requests.put(f"{self.dgraph_url}/rollup/resume", timeout=self.timeout)
        resp.raise_for_status()

    def dgraph_rollup_forceall(self):
        """Force an immediate rollup of all pending deltas. Dgraph writes the
        posting-store SET at rollup, not at mutation-commit, so a write
        measurement forces a rollup to materialize the delta as counted SETs."""
        resp = requests.post(f"{self.dgraph_url}/rollup/forceall", timeout=self.timeout)
        resp.raise_for_status()

    def dgraph_rollup_drain_enable(self):
        """Start recording dirtied posting-list keys (clears any prior set). Pairs with
        dgraph_rollup_drain / _disable. Needs the addMutationInternal recorder patch:
        the recorder captures the true dirtied set on the apply path."""
        resp = requests.post(f"{self.dgraph_url}/rollup/drain/enable", timeout=self.timeout)
        resp.raise_for_status()

    def dgraph_rollup_drain(self) -> int:
        """Roll up ONLY the keys dirtied since recording started (fast — no full-store
        scan — and per-op isolated), then clear the recorded set. Returns the number of
        keys rolled up (the `keys` field of the response)."""
        resp = requests.post(f"{self.dgraph_url}/rollup/drain", timeout=self.timeout)
        resp.raise_for_status()
        try:
            return int(resp.json().get("data", {}).get("keys", 0))
        except Exception:
            return 0

    def dgraph_rollup_drain_disable(self):
        """Stop recording and clear the recorded set."""
        resp = requests.post(f"{self.dgraph_url}/rollup/drain/disable", timeout=self.timeout)
        resp.raise_for_status()

    def dgraph_rollup_drain_peek(self) -> list:
        """Return the currently recorded dirtied keys (parsed 'attr|type|uid', sorted)
        WITHOUT draining. Call it after a mutation to verify the recorder captured exactly
        the expected posting-list keys before rolling them up."""
        resp = requests.post(f"{self.dgraph_url}/rollup/drain/peek", timeout=self.timeout)
        resp.raise_for_status()
        return sorted(resp.json().get("data", {}).get("keys", []))

    def dgraph_rollup_drain_verbose(self, on: bool):
        """Toggle per-key recorder logging: each dirtied key + op (SET/DEL) is logged to
        alpha.log as it is recorded (a live trace of what drain will roll up)."""
        sub = "enable" if on else "disable"
        resp = requests.post(f"{self.dgraph_url}/rollup/drain/verbose/{sub}", timeout=self.timeout)
        resp.raise_for_status()

    # ── Dgraph localcache control ────────────────────────────────

    def dgraph_localcache_set(self, mode: str):
        """Set LocalCache mode: 'disable', 'lock', or 'enable'."""
        resp = requests.put(
            f"{self.dgraph_url}/localcache/{mode}",
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def dgraph_knob_set(self, knob: str, mode: str):
        """Set a filter access-path knob ('typefilterpath' | 'eqfilterpath' | 'ineqfilterpath') to
        'index' | 'per_vertex' | 'default'. Pins the plan Dgraph picks for a @filter. Mirrors
        dgraph_localcache_set; requires a binary built with the filter-path patch (else 404)."""
        resp = requests.put(
            f"{self.dgraph_url}/{knob}/{mode}",
            timeout=self.timeout,
        )
        resp.raise_for_status()

    # ── Output helpers ───────────────────────────────────────────

    @staticmethod
    def save(data: dict | list, path: Path | str):
        """Save kvstats or trace JSON to file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def save_results(
        stats: dict,
        trace: list[dict],
        output_dir: Path | str,
        name: str,
        db: str = "dgraph",
    ):
        """Save kvstats + trace to output directory.

        Writes:
          output_dir/{db}/{name}.kvstats.json   — raw kvstats counters
          output_dir/{db}/{name}.trace.json     — per-op trace entries
          output_dir/{db}/{name}.summary.json   — grouped counts by (op, attr)
        """
        out = Path(output_dir) / db
        out.mkdir(parents=True, exist_ok=True)
        safe = name.replace(" ", "_").replace("(", "").replace(")", "")

        # # Raw kvstats
        # (out / f"{safe}.kvstats.json").write_text(json.dumps(stats, indent=2))

        # Trace entries
        (out / f"{safe}.trace.json").write_text(json.dumps(trace, indent=2))

        # Summary: grouped counts by (op, attr)
        counts: dict[str, int] = {}
        for e in trace:
            key = f"{e['op']}.{e['attr']}"
            counts[key] = counts.get(key, 0) + 1
        summary = {
            "kvstats": stats,
            "trace_summary": counts,
            "trace_total": len(trace),
        }
        (out / f"{safe}.summary.json").write_text(json.dumps(summary, indent=2))

    @staticmethod
    def _normalize_kvstats(stats: dict) -> dict:
        """Normalize kvstats across Dgraph, Nebula, and ArangoDB into a uniform
        {get,seek,next}_{count,bytes} view.

        Dgraph exposes `next_count` / `seek_count` directly. Nebula exposes
        `iterator_next_count` and splits "seek" across four counters
        (`prefix_count + range_count + range_with_prefix_count + scan_count`).
        Presence of `iterator_next_count` is the Nebula signal.

        ArangoDB (signal: `tickers` key present) exposes two counter sources:
          * engine-wrapper hand counters (ARANGO_KV_STATS): flat get_/seek_/next_/
            prev_ fields incremented at RocksDBMethods + TimedRocksDBIterator.
            Query-scoped by construction — the analog of Nebula's KV_STATS. PREFERRED.
          * RocksDB native tickers (ARANGO_KV_TICKERS): process-global; one logical
            Arango read fans out to several RocksDB ops. Fallback only.
        Either way we fold forward+reverse iteration (next + prev) into one "rows
        walked" figure, exactly like the Nebula branch folds iterator_next + prev.
        NOTE: even the engine counts are not 1:1 with Dgraph/Nebula (a doc fetch is
        primary-index + document = 2 GETs); compare Arango against other Arango.
        """
        if "tickers" in stats:
            if "get_count" in stats:  # engine-wrapper counters present (ARANGO_KV_STATS)
                return {
                    "get_count":  stats.get("get_count", 0),
                    "seek_count": stats.get("seek_count", 0),
                    "next_count": stats.get("next_count", 0) + stats.get("prev_count", 0),
                    "get_bytes":  stats.get("get_bytes", 0),
                    "seek_bytes": stats.get("seek_bytes", 0),
                    "next_bytes": stats.get("next_bytes", 0) + stats.get("prev_bytes", 0),
                }
            t = stats["tickers"]
            return {
                "get_count":  t.get("rocksdb.number.keys.read", 0),
                "seek_count": t.get("rocksdb.number.db.seek", 0),
                # fold forward (db.next) + reverse (db.prev) iteration
                "next_count": t.get("rocksdb.number.db.next", 0) + t.get("rocksdb.number.db.prev", 0),
                "get_bytes":  t.get("rocksdb.bytes.read", 0),
                "seek_bytes": 0,  # RocksDB has no per-seek byte ticker
                "next_bytes": t.get("rocksdb.db.iter.bytes.read", 0),
            }
        if "iterator_next_count" in stats:
            # range_reverse_count and prefix_reverse_count are populated only when
            # the PLANNER_FIXES build is loaded with --fix_n4_storage_reverse_scan
            # on. They count seeks in the reverse-direction code path. Folding
            # them into seek_count keeps the harness's apples-to-apples view
            # (total seeks, any direction). Same for iterator_prev_count vs
            # iterator_next_count: reverse-walking still scans rows; we count
            # all row-reads together so V4 deltas reflect rows-scanned, not
            # direction-of-walk.
            return {
                "get_count":  stats.get("get_count", 0) + stats.get("multi_get_count", 0),
                "seek_count": (stats.get("prefix_count", 0) + stats.get("range_count", 0)
                               + stats.get("range_with_prefix_count", 0) + stats.get("scan_count", 0)
                               + stats.get("range_reverse_count", 0)
                               + stats.get("prefix_reverse_count", 0)),
                "next_count": stats.get("iterator_next_count", 0) + stats.get("iterator_prev_count", 0),
                "get_bytes":  stats.get("get_bytes", 0) + stats.get("multi_get_bytes", 0),
                "seek_bytes": 0,  # Nebula doesn't expose a seek-bytes counter
                "next_bytes": stats.get("iterator_next_bytes", 0) + stats.get("iterator_prev_bytes", 0),
            }
        # Generic fallback (Dgraph and TuGraph: next_count/seek_count directly;
        # also an engine-only Arango build with no tickers). prev_* is 0 where
        # absent; TuGraph's get_current_* is in no total.
        return {
            "get_count":  stats.get("get_count", 0),
            "seek_count": stats.get("seek_count", 0),
            "next_count": stats.get("next_count", 0) + stats.get("prev_count", 0),
            "get_bytes":  stats.get("get_bytes", 0),
            "seek_bytes": stats.get("seek_bytes", 0),
            "next_bytes": stats.get("next_bytes", 0) + stats.get("prev_bytes", 0),
        }

    # The read timers each engine's patch keeps, in nanoseconds (one definition: the sum of per-call wall time of the
    # KV read calls, monotonic clock, around the call only; iterator creation is inside the seek time).
    STORAGE_READ_NS = {
        "dgraph": ("get_latency_ns", "seek_latency_ns", "next_latency_ns"),
        "nebula": ("get_latency_ns", "multi_get_latency_ns", "range_latency_ns", "prefix_latency_ns",
                   "range_with_prefix_latency_ns", "scan_latency_ns", "iterator_next_latency_ns",
                   "iterator_prev_latency_ns"),
        "arangodb": ("get_time_ns", "seek_time_ns", "next_time_ns", "prev_time_ns"),
        "tugraph": ("get_latency_ns", "seek_latency_ns", "next_latency_ns", "prev_latency_ns"),
    }

    @staticmethod
    def storage_read_ns(db: str, stats: dict):
        """The time one query spent in the KV store's read calls (ns), from a kvstats dump; None when the binary
        predates the nanosecond timers (no field of db's set present)."""
        keys = KVProfiler.STORAGE_READ_NS[db]
        if not any(k in stats for k in keys):
            return None
        return sum(int(stats.get(k, 0)) for k in keys)

    @staticmethod
    def _normalize_writes(stats: dict) -> dict:
        """Normalize write counters across the three DBs into a uniform {put, delete, commit, write_bytes} view, apart from
        _normalize_kvstats (reads).

        PUT and DELETE stay separate on purpose: Dgraph has no real KV delete (a logical delete is a tombstone SET, so it
        lands in `put`), while Nebula and Arango issue real RocksDB deletes.

        Sources per DB:
          * Arango: engine-wrapper write counters `put_count`/`delete_count`/`commit_count`. The kvstats build has no
            `tickers` key, so it is detected by `put_count` (checked after Nebula, which also has `put_count`). A
            tickers-only fallback uses the process-global `rocksdb.number.keys.written` (insert/update only: delete and
            commit read 0).
          * Nebula (signal: `iterator_next_count`): writes reach RocksDB either directly (`put_count`/`multi_put_keys`) or
            via the Raft WriteBatch (`wb_put_count` + `commit_batch_write_count`). The two paths are exclusive per write, so
            summing is safe.
          * Dgraph (fallback): every write goes through `txn.SetEntry` -> `set_count` (mutation deltas and delete
            tombstones). `delete_count`/`commit_count` read 0 unless the build exposes them.
        """
        if "tickers" in stats:
            if "put_count" in stats:  # engine-wrapper write counters (parity build)
                return {
                    "put":    stats.get("put_count", 0),
                    "delete": stats.get("delete_count", 0) + stats.get("singledelete_count", 0),
                    "commit": stats.get("commit_count", 0),
                    "write_bytes": stats.get("put_bytes", 0),
                }
            t = stats["tickers"]
            return {
                "put":    t.get("rocksdb.number.keys.written", 0),
                "delete": 0,  # no process-global "keys deleted" ticker
                "commit": 0,
                "write_bytes": 0,
            }
        if "iterator_next_count" in stats:  # Nebula
            return {
                "put":    (stats.get("put_count", 0) + stats.get("multi_put_keys", 0)
                           + stats.get("wb_put_count", 0)),
                "delete": (stats.get("remove_count", 0) + stats.get("multi_remove_keys", 0)
                           + stats.get("wb_remove_count", 0)),
                "commit": stats.get("commit_batch_write_count", 0),
                "write_bytes": stats.get("put_bytes", 0) + stats.get("multi_put_bytes", 0),
            }
        if "put_count" in stats:  # Arango kvstats parity build (engine counters, no `tickers`); TuGraph's
            # Reached only after Nebula (which also has put_count) is excluded above.
            # `delete_count` already folds Delete + SingleDelete (one engine counter); TuGraph names it `del_count`.
            return {
                "put":    stats.get("put_count", 0),
                "delete": stats.get("delete_count", 0) + stats.get("del_count", 0),
                "commit": stats.get("commit_count", 0),
                "write_bytes": stats.get("put_bytes", 0),
            }
        # Dgraph fallback: SET captures all writes incl. delete-tombstones.
        return {
            "put":    stats.get("set_count", 0),
            "delete": stats.get("delete_count", 0),
            "commit": stats.get("commit_count", 0),
            "write_bytes": stats.get("set_bytes", 0),
        }

    @staticmethod
    def print_kvstats(stats: dict, label: str = ""):
        """Print one-line kvstats summary (plus a write line if any writes)."""
        if not stats:
            return
        tag = f" [{label}]" if label else ""
        n = KVProfiler._normalize_kvstats(stats)
        print(f"  KV ops{tag}: GET={n['get_count']} ({n['get_bytes']}B)"
              f"  SEEK={n['seek_count']} ({n['seek_bytes']}B)"
              f"  NEXT={n['next_count']} ({n['next_bytes']}B)")
        w = KVProfiler._normalize_writes(stats)
        if w["put"] or w["delete"] or w["commit"]:
            # GET above is the read-modify-write read (0 for Dgraph, >0 for Nebula/Arango).
            print(f"  KV writes{tag}: PUT={w['put']} DEL={w['delete']} COMMIT={w['commit']}"
                  f"  ({w['write_bytes']}B written)")

    @staticmethod
    def print_trace_summary(trace: list[dict]):
        """Print grouped trace summary: count per (op, attr) pair."""
        if not trace:
            print("  (no trace entries)")
            return

        # Group by (op, attr)
        counts: Counter = Counter()
        for e in trace:
            counts[(e["op"], e["attr"])] += 1

        print(f"  {'Op':<6} {'Attr':<25} {'Count':>5}")
        print(f"  {'─'*6} {'─'*25} {'─'*5}")
        for (op, attr), count in sorted(counts.items()):
            print(f"  {op:<6} {attr:<25} {count:>5}")
        print(f"  {'':>32} {'─'*5}")
        print(f"  {'Total':<32} {len(trace):>5}")

    @staticmethod
    def print_breakdown(stats: dict, trace: list[dict]):
        """Print kvstats + trace breakdown together."""
        # KVProfiler.print_kvstats(stats)
        # print()
        KVProfiler.print_trace_summary(trace)
