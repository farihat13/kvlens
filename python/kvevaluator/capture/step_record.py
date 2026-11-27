"""The step record: the free fields of one response step, as ONE list the step writers share (the grid's
steps/<cell>.csv, the writes capture's steps, cpplatency's steps.csv), so one writer cannot drop a column another keeps."""
from kvevaluator import contract

# Every column name below is read from inputs/contract/capture_columns.json (kvevaluator.contract), the one list C++
# compiles in too: here they are only sliced and named, never retyped.
# the KV operation counts: SEEK / GET / NEXT, PUT / DEL, and the misses
BASE_OPS = tuple(contract.columns("op_counts_read"))    # S G N: every SEEK, GET and NEXT, the misses included
MISS_OPS = tuple(contract.columns("op_counts_miss"))    # the SEEKs, GETs and NEXTs that found nothing
WRITE_OPS = tuple(contract.columns("op_counts_write"))  # a write's PUTs and DELs
READ_OPS = (*BASE_OPS, *MISS_OPS)                       # the counts a read makes
OPS = (*BASE_OPS, *WRITE_OPS, *MISS_OPS)
# BYTE_LANES by what they are, for the code that prices them (one definition, sliced, never retyped):
HIT_BYTE_LANES = tuple(contract.columns("byte_lanes_hit"))      # the key and value bytes a GET or a scan returned
MISS_BYTE_LANES = tuple(contract.columns("byte_lanes_miss"))    # the keys a missed SEEK / NEXT / GET looked at
READ_BYTE_LANES = (*HIT_BYTE_LANES, *MISS_BYTE_LANES)  # every lane a read returns, the miss keys included (the reported "bytes read")
WRITE_BYTE_LANES = tuple(contract.columns("byte_lanes_write"))  # the bytes a PUT or DEL wrote
USED_BYTE_LANES = tuple(contract.columns("byte_lanes_used"))    # the bytes of what was read or written that the plan used
BYTE_LANES = (*READ_BYTE_LANES, *WRITE_BYTE_LANES, *USED_BYTE_LANES)
SHAPE = tuple(contract.columns("shape"))
INPUT = tuple(contract.columns("input"))      # the rows a step took in (every ref in its arguments), and how many carry a label
# KV-pair size histograms, one entry per request (cpp/kvevaluator/src/store/size_hist.h): column -> the response's key.
# Text: "bucket:count" pairs, bucket 0 = size 0, bucket i = [2^(i-1), 2^i) bytes (keys for keys_per_range_read),
# "u" = a deleted KV pair of unknown size; empty = no such request. Per step and per part.
HISTS = contract.response_keys("hists")
# exact Fetch per step and part, in two units (store/size_hist.h KvPairHists): *_fetch_4k = the sum over the requests of
# ceil(the request's kvbytes / 4096); *_store_blocks = the same requests in the store's unit (a KV pair of 4 KB or more
# is one block, the smaller KV pairs of a request fill 4 KB blocks together). A request that returned nothing is 0.
FETCH = tuple(contract.columns("fetch"))
# chunk statistics from the landing replay (exec/replay.h ChunkStats): column -> the response's key; the same at every P
CHUNKS = contract.response_keys("chunks")
# a write's touches of a family's LAST chunk (cpp/kvevaluator/src/store/counting.h Counts::LastChunk): column -> the
# response's key. reads / writes count the touches; *_bytes are the chunk's value bytes read, written, and before the
# write (base); *_target_bytes sum the family's configured chunk target per touch. The response carries the object only
# when a touch happened. A write capture's columns, never the step record's (layout_cost.compose's chunk view reads them)
LAST_CHUNK = contract.response_keys("last_chunk")
# exact write units per step and part (store/size_hist.h KvPairHists, the one rule): put_units_1k = the sum over PUTs of
# ceil(key + value bytes / 1024), at least 1 each; del_units_1k = the same over DELETEs of a known size + 1 per DELETE of
# unknown size. A write capture's columns, never the step record's (a read writes none); the bill reads them
# (kvcost layout_cost.compose bill_units)
WRITE_UNITS = tuple(contract.columns("write_units"))
# round trips against a remote store (cpp/kvevaluator/src/store/round_trips.h): requests sent together are one level;
# round_trips = per level 1 for its GETs + 1 per range read or page, + 1 per job's writes; round_trips_critical = the
# levels (the chain a client waits through); range_pages_extra = the pages after a range read's first, at the
# response's remote.page_bytes. The same at every P. rt_levels: the same requests level by level, as text (jobs by "|",
# a job's levels as "gets:ranges" with "xN" for N equal levels, "wN" its write keys, "*N" for N equal jobs; "" = not
# measured): a client with a batch size and an in-flight cap is costed from it (kvcost cost_models.remote_*)
ROUND_TRIPS = tuple(contract.columns("round_trips"))
CODEC = tuple(contract.columns("codec"))
# what the step decoded (cpp/kvevaluator/src/codec/decode_counts.h; deterministic): decode requests by kind as sparse text
# ("row:3 posting:1", kinds in DECODE_KINDS order, "" when nothing was decoded), the fields materialized, and the
# stored bytes of the values parsed (decoded_kvbytes: never the in-memory size)
DECODE_KINDS = ("row", "cell", "posting", "chunk", "doc")
# keys_crossed: cursor landings on a key whose value was never read (store/size_hist.h KvPairHists.keys_crossed)
DECODED = tuple(contract.columns("decoded"))


def kinds_text(kinds):
    """{"row": 3, "posting": 1} -> "row:3 posting:1" """
    return " ".join(f"{k}:{kinds[k]}" for k in DECODE_KINDS if kinds.get(k))


def decoded_fields(decoded, obj):
    """the DECODED columns of a response step (decoded = its codec block) or of one of its parts (decoded = the
    part's own "decoded" block, absent when the part's reads decoded nothing); obj holds the histograms"""
    if "hist" not in obj:                      # run.emit.kvpair_stats off (or an older binary): not measured, empty
        return dict.fromkeys(DECODED, "")
    decoded = decoded or {}
    return {"decode_requests": kinds_text(decoded.get("decode_requests") or {}),
            "fields_decoded": decoded.get("fields_decoded", 0), "decoded_kvbytes": decoded.get("decoded_kvbytes", 0),
            "keys_crossed": obj["hist"].get("keys_crossed", 0)}


def kinds_of(text):
    """the inverse of kinds_text: {kind: count} (absent kinds are 0 to a reader: use .get(kind, 0))"""
    return {k: int(n) for k, n in (x.split(":") for x in (text or "").split())}
# wall_ns in a COUNTS file is the counts run's own step clock (attribution on), never a latency of record; every
# clock column ends in _ns so the equality checks drop it by name
CLOCK = tuple(contract.columns("clock"))


def columns(byte_suffix="_bytes"):
    """the free columns in file order (the data file's step_record sequence); the grid's files name a byte lane without
    the suffix (byte_suffix='')"""
    return contract.columns("step_record", byte_suffix)


def columns_sha():
    """the identity of the step record's shape (its free columns, in order): a capture written under another column
    list is re-measured, not served as fresh (latency_fresh.identity). Not a counting version: what an operation is
    charged is the binary's counting versions' job (measure/counting.py)"""
    import hashlib
    from kvevaluator.capture.capture_columns import APPENDED_COLUMNS, RENAMED_COLUMNS   # (it imports this module)
    # a column renamed (RENAMED_COLUMNS) hashes under its old spelling: a rename alone is the same shape, so a capture
    # written before it stays fresh (its header is read through capture_layout.fix_header); an appended one
    # (APPENDED_COLUMNS) hashes as absent, for the same reason
    old = {new: old for old, new in RENAMED_COLUMNS.items()}
    return hashlib.sha256(",".join(old.get(c, c) for c in columns() if c not in APPENDED_COLUMNS).encode()).hexdigest()[:12]


def hist_text(h):
    """a response histogram ({bucket: count}) as "bucket:count" pairs in bucket order ("u" last); "0" when it holds
    no entry (measured, none). An EMPTY cell means not measured (run.emit.kvpair_stats off, or an older binary)"""
    return " ".join(f"{k}:{h[k]}" for k in sorted(h, key=lambda k: (k == "u", int(k) if k != "u" else 0))) or "0"


def _pairs(text):
    """the (bucket, count) pairs of a histogram cell ("0" and "" hold none)"""
    return [x.split(":") for x in (text or "").split() if ":" in x]


def hist_fields(obj):
    """the histogram columns of a response step or of one of its parts, and its exact Fetch columns beside them"""
    if "hist" not in obj:                      # not measured: empty cells, never a false "none"
        return dict.fromkeys([*HISTS, *FETCH], "")
    h = obj["hist"]
    return {**{col: hist_text(h.get(key) or {}) for col, key in HISTS.items()}, **{col: h.get(col, 0) for col in FETCH}}


def hist_sum(texts):
    """the histogram cells of several rows (a step's parts) added up, as one cell"""
    acc, measured = {}, False
    for t in texts:
        measured = measured or bool(t)
        for k, n in _pairs(t):
            acc[k] = acc.get(k, 0) + int(n)
    return hist_text(acc) if measured else ""


def plan_fields(step_rows, byte_suffix="_bytes"):
    """a plan row's free columns from its step rows (as fields() made them): counts, byte lanes, decode counts and
    chunk counts summed, the histograms added up, the two per-batch maxima as the largest step's; a column no step
    measured stays empty. rows / rows_in / kind / out_bytes are the caller's (they are not sums of the steps')."""
    out = {}
    maxima = ("chunks_distinct_max_batch", "held_chunk_bytes_peak")
    skip = set(SHAPE) | set(INPUT) | set(CLOCK)
    for col in columns(byte_suffix):
        vals = [r.get(col, "") for r in step_rows]
        have = [v for v in vals if v != ""]
        if col in skip:
            continue
        if col in HISTS:
            out[col] = hist_sum(vals)
        elif col == "decode_requests":
            acc = {}
            for v in vals:
                for k, n in kinds_of(v).items():
                    acc[k] = acc.get(k, 0) + n
            out[col] = kinds_text(acc)
        elif col == "rt_levels" or not have:   # rt_levels is per step, as text: the step rows carry it, the plan row not
            out[col] = ""
        else:
            out[col] = (max if col in maxima else sum)(int(v) for v in have)
    return out


def hist_count(text):
    """entries of a histogram cell (the invariant: equal to the operation count)"""
    return sum(int(n) for _, n in _pairs(text))


def hist_bounds(text):
    """(lowest, highest) total size a histogram cell allows: its exact lane must lie between them. None when it holds
    an entry of unknown size"""
    lo = hi = 0
    for k, n in _pairs(text):
        if k == "u":
            return None
        if int(k):
            lo += int(n) << (int(k) - 1)
            hi += int(n) * ((1 << int(k)) - 1)
    return lo, hi


def last_chunk_fields(obj):
    """the LAST_CHUNK columns of a write's response step or of one of its parts: 0 where it touched no last chunk, empty
    when not measured (run.emit.kvpair_stats off: the lane is counted with the statistics)"""
    if "hist" not in obj:
        return dict.fromkeys(LAST_CHUNK, "")
    lc = obj.get("last_chunk") or {}
    return {col: lc.get(key, 0) for col, key in LAST_CHUNK.items()}


def write_units_fields(obj):
    """the WRITE_UNITS columns of a write's response step or of one of its parts: 0 where it wrote nothing of that kind,
    empty when not measured (run.emit.kvpair_stats off: counted with the histograms)"""
    if "hist" not in obj:
        return dict.fromkeys(WRITE_UNITS, "")
    return {col: obj["hist"].get(col, 0) for col in WRITE_UNITS}


def fields(step, byte_suffix="_bytes"):
    """{column: value} of one response step (or of the response's totals: what it lacks is written empty)"""
    b, codec = step.get("bytes", {}), step.get("codec") or {}
    return {**{k: step.get(k, "") for k in SHAPE},
            **{o: step.get(o, 0) for o in OPS},
            **{lane + byte_suffix: b.get(lane, 0) for lane in BYTE_LANES},
            **{k: "" if codec.get(k, 0) is None else codec.get(k, 0) for k in CODEC},   # a clock not taken (null): empty
            **decoded_fields(codec, step),
            **{k: step.get(k, "") for k in INPUT},
            **hist_fields(step),
            **{col: (step.get("chunks") or {}).get(key, "") for col, key in CHUNKS.items()},
            **{k: step.get(k, "") for k in ROUND_TRIPS},
            **{k: step.get(k, "") for k in CLOCK}}


# The stored part each read operator reads (read_parts' per-part split; logical_census checks its kinds agree): the op
# table's `part` (inputs/contract/kv_ops.json). read_filter_by_label_probe reads vertex_props (a GET of the vertex to
# check its label); read_vertices_by_label reads index (the label's list of vertices, not their rows)
PART = {op["name"]: op["part"] for op in contract.kv_ops() if op["kind"] == "read"}
