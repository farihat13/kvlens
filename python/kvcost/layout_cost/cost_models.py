"""The cost models, in one place.

  wops          weighted operations: G + 1.1 S + 0.01 N of the reads (a write's own reads included) + PUTs + DELs
  bytes         key and value bytes read over the 7 read lanes (missed keys included) + bytes written
  wops_bytes4k  wops + bytes / 4,096 (4 KB of bytes counts as one GET)
  the three reported ones; two latency proxies on the same vector (cost):
  ops_est_blocks     wops + est_blocks (reads only: an update adds no blocks)
  ops_est_blocks_rw  wops + est_blocks_rw (an update also pays the blocks of its own reads and of its PUTs)
and the bill's three forms (a billed key-value service's request units): bill_lower, bill_upper, bill_exact = read
units + WRITE_PRICE x write units. Dollars, the SLO view and the remote store (a client that reaches the store over a
network) are at the end.

These are the reported currencies. The plan pick rule (criteria's `wops_bytes4k`) scores plans on the same 7 read
lanes, so on a read query it is the same number; on a write it has no PUT / DEL term (a write's own bytes and ops are
the write's, not the plan's).
"""

import math

from kvevaluator.capture.step_record import READ_BYTE_LANES, WRITE_BYTE_LANES
from kvevaluator.plan_choice.optimizer.criteria import linear_score
from kvevaluator import weights
from kvevaluator.weights import DEFAULT as _W

MODELS = ("wops", "bytes", "wops_bytes4k")
# predict mode's answer: every layout predicted within this share of the predicted best, not the best alone (kvcost
# cost-layouts --count-source <set>; the layout report's tie_rule.csv scores each threshold against measured)
WITHIN = 0.05
# the bill (DynamoDB's rule; the units per step row: compose.bill_units): a
# write unit is priced 5 read units. Each form: (its read units, its write units), both channels of a priced vector
WRITE_PRICE = 5.0
BILL_UNITS = {"bill_lower": ("bill_read_lower", "bill_write_lower"),
              "bill_upper": ("bill_read_upper", "bill_write_upper"),
              "bill_exact": ("bill_read_exact", "bill_write_exact")}
BILL_MODELS = tuple(BILL_UNITS)


def _bytes_per_op():
    """the bytes that count as one GET in the pick rule wops_bytes4k (kvevaluator.plan_choice.optimizer.criteria.NAMED): 4,096"""
    w = linear_score("wops_bytes4k")
    n = w["G"] / w["total_bytes"]
    assert n == int(n), n
    return int(n)


BYTES_PER_OP = _bytes_per_op()
# the op weights with a GET = 1 (python/kvevaluator/weights.py DEFAULT, G:S:N = 100:110:1): 1.1 per SEEK, 0.01 per NEXT
SEEK_PER_GET = _W["S"] / _W["G"]
NEXT_PER_GET = _W["N"] / _W["G"]
# every lane the store returns on a read, the miss keys included (compose.REPORT_BYTE_LANES, the same 7)
READ_LANES = READ_BYTE_LANES
WRITE_LANES = WRITE_BYTE_LANES


def formulas(w=None):
    """each reported model's formula as text, under the vector `w` (None: weights.DEFAULT)"""
    g = weights.per_get(w or _W)
    return {"wops": f"G + {g['S']:g} S + {g['N']:g} N of the reads (a write's own reads included) + P + D",
            "bytes": "the 7 read lanes (get / scan key and value, seek / next / get miss keys) + bytes written",
            "wops_bytes4k": f"wops + bytes / {BYTES_PER_OP:,}"}


FORMULAS = formulas()


def currencies(t):
    """the two totals a workload is judged in, each a sum of LIKE units: ops = read weighted ops + the writes'
    own reads (weighted) + PUTs + DELs; bytes = bytes read + the bytes the writes read first + bytes written"""
    return {"ops": t["read_ops"] + t["write_rmw_ops"] + t["write_ops"],
            "bytes": t["read_bytes"] + t["write_rmw_bytes"] + t["write_bytes"]}


def cost(t, model):
    """a priced total vector's cost (kvcost.layout_cost.compose: query_cost, cost_mix) under one model"""
    c = currencies(t)
    if model == "wops":
        return c["ops"]
    if model == "bytes":
        return c["bytes"]
    if model == "wops_bytes4k":
        return wops_bytes4k(c["ops"], c["bytes"])
    if model == "ops_est_blocks":
        return c["ops"] + t["est_blocks"]
    if model == "ops_est_blocks_rw":
        return c["ops"] + t["est_blocks_rw"]
    if model in BILL_UNITS:
        return _bill(t, model)
    raise KeyError(model)


def _bill(t, form):
    r, w = BILL_UNITS[form]
    if r not in t or w not in t:
        raise SystemExit(f"{form}: this cost vector carries no {r if r not in t else w} (bill_exact needs the exact "
                         f"unit columns of a measured capture; a predicted capture gives bill_lower and bill_upper)")
    return bill_at(t[r], t[w])


def bill_at(read_units, write_units, write_price=WRITE_PRICE):
    """the bill of a read and a write total at a write price (a write unit = write_price read units): the one place the
    bill's sum is written (_bill at WRITE_PRICE; a read/write front's cheapest point at each price)"""
    return read_units + write_price * write_units


def bill_lower(t):
    """the bill's lower form: per step row and kind max(requests, bytes / unit)"""
    return _bill(t, "bill_lower")


def bill_upper(t):
    """the bill's upper form: per step row and kind requests + bytes / unit"""
    return _bill(t, "bill_upper")


def bill_exact(t):
    """the bill's exact form: each read and each write request rounded up alone (the capture's *_fetch_4k and *_units_1k)"""
    return _bill(t, "bill_exact")


def needs_bill(model):
    """which of the bill's units a cost under `model` needs in the vector (compose.Composer(bill=...)): "bounds" for
    bill_lower and bill_upper (requests and bytes per step row: any capture, predicted too), "sized" for bill_exact (also
    each request rounded up alone, from a measured capture's exact unit columns), "" for another model"""
    return "sized" if model == "bill_exact" else "bounds" if model in BILL_UNITS else ""


def op_weight(S, G, N, w=None):
    """the weighted ops of S / G / N under `w` scaled to G = 1 (None: python/kvevaluator/weights.py's DEFAULT,
    G:S:N = 100:110:1)"""
    if w is None or weights.is_default(w):
        return G + SEEK_PER_GET * S + NEXT_PER_GET * N
    g = weights.per_get(w)
    return G + g["S"] * S + g["N"] * N


def wops_bytes4k(wops, nbytes):
    return wops + nbytes / BYTES_PER_OP


def of_channels(ch, w=None):
    """{wops, bytes, wops_bytes4k} of one vector of raw channels (S, G, N, P, D, the 7 read lanes, write_key,
    write_value; a missing key counts 0): the three reported models, from the counts alone. A latency steps.csv row
    names its byte lanes <lane>_bytes; one with none of the lane names here is refused, not read as 0 bytes"""
    if not any(k in ch for k in READ_LANES) and any(f"{k}_bytes" in ch for k in READ_LANES):
        raise ValueError("of_channels: the row names its byte lanes <lane>_bytes (a latency steps.csv row) and has none "
                         f"of {', '.join(READ_LANES)}: its bytes would count 0; map them, or read the counts capture")
    g = lambda k: float(ch.get(k) or 0.0)
    ops = op_weight(g("S"), g("G"), g("N"), w) + g("P") + g("D")
    b = sum(g(k) for k in READ_LANES) + sum(g(k) for k in WRITE_LANES)
    return {"wops": ops, "bytes": b, "wops_bytes4k": wops_bytes4k(ops, b)}


def of_measures(m):
    """{wops, bytes, wops_bytes4k} of compose.py's measures (read_ops, read_bytes, write_ops, write_rmw_ops, write_bytes,
    write_rmw_bytes; a missing key counts 0): reads and a write's own reads, PUTs + DELs, bytes read and written"""
    g = lambda k: float(m.get(k) or 0.0)
    w = g("read_ops") + g("write_ops") + g("write_rmw_ops")
    b = g("read_bytes") + g("write_bytes") + g("write_rmw_bytes")
    return {"wops": w, "bytes": b, "wops_bytes4k": wops_bytes4k(w, b)}


def cost_of(t, cur):
    """a total vector's cost in one of CURRENCIES: ops, bytes, or their sum (wops_bytes4k)"""
    return cost(t, "wops_bytes4k") if cur == "sum" else currencies(t)[cur]


# -- Dollars: a billed key-value service's monthly cost (requests + storage) --------------------------------------------
# DynamoDB on-demand, Standard table class, US East (N. Virginia), as read from AWS's DynamoDB on-demand rate
# page: $ per read request unit, per write request unit, per GB-month stored. A stored item is billed on its key + value
# bytes + 100 B of overhead. GB = 2^30 bytes (AWS bills a GB-month as GiB); a month = 730 hours.
DOLLARS_PER = {"read_unit": 0.125e-6, "write_unit": 0.625e-6, "gb_month": 0.25}
ITEM_OVERHEAD_BYTES = 100
ITEM_LIMIT_BYTES = 400 * 1024      # the largest item DynamoDB stores (its documented limit: key + value, 400 KB)
GB = 2 ** 30
SECONDS_PER_MONTH = 730 * 3600
assert math.isclose(DOLLARS_PER["write_unit"] / DOLLARS_PER["read_unit"], WRITE_PRICE)    # a write unit costs WRITE_PRICE read units


def request_dollars(read_units, write_units):
    """the dollars of a read and a write unit total (the bill in dollars)"""
    return DOLLARS_PER["read_unit"] * bill_at(read_units, write_units)


def stored_bytes(items, key_bytes, value_bytes):
    """the bytes a service bills for storing `items` items: key + value bytes + ITEM_OVERHEAD_BYTES per item"""
    return key_bytes + value_bytes + ITEM_OVERHEAD_BYTES * items


def storage_dollars(nbytes):
    """the dollars per month of storing `nbytes` billed bytes (stored_bytes)"""
    return DOLLARS_PER["gb_month"] * nbytes / GB


def monthly_dollars(read_units_per_run, write_units_per_run, nbytes, runs_per_second):
    """{requests, storage, total} dollars per month: a workload run `runs_per_second` times a second (a run = one query
    of the workload's mix, drawn by its frequencies), over a store of `nbytes` billed bytes"""
    req = request_dollars(read_units_per_run, write_units_per_run) * runs_per_second * SECONDS_PER_MONTH
    sto = storage_dollars(nbytes)
    return {"requests": req, "storage": sto, "total": req + sto}


def crossover_rate(read_units_per_run, write_units_per_run, nbytes):
    """the runs per second at which requests cost as much as storage (below it, storage is the larger part)"""
    return storage_dollars(nbytes) / (request_dollars(read_units_per_run, write_units_per_run) * SECONDS_PER_MONTH)


# -- The SLO view: the cheapest layout that keeps every query within k x its target --------------------------------------
def slo_ratios(costs, targets):
    """{layout: {query: cost / target}} of per-query costs {layout: {query: cost}} against {query: target cost}"""
    return {lay: {q: c / targets[q] for q, c in sorted(per.items())} for lay, per in sorted(costs.items())}


def worst(per):
    """(the largest value, its key) of {key: value}; ties go to the first key in sorted order"""
    k = min(sorted(per), key=lambda q: -per[q])
    return per[k], k


def slo_choice(ratios, totals, k):
    """(layout, its total) with the lowest workload total among the layouts whose every query's ratio is <= k, ties by
    name; None when none is"""
    ok = [lay for lay in sorted(ratios) if worst(ratios[lay])[0] <= k * (1 + 1e-12)]
    if not ok:
        return None
    best = min(ok, key=lambda lay: (totals[lay], lay))
    return best, totals[best]


def balance(costs):
    """{layout: (the worst ratio of a query's cost to its cost on the query's own cheapest layout, that query)}"""
    queries = sorted({q for per in costs.values() for q in per})
    low = {q: min(per[q] for per in costs.values()) for q in queries}
    return {lay: worst({q: per[q] / low[q] for q in per}) for lay, per in sorted(costs.items())}


# --- The remote store: a client one network hop from the key-value store -----------------------------------------
# A request: one batch of up to B GETs of one level, one range read or one further page of it (each its own request),
# or one batch of up to B writes. A level: the requests whose keys are known before any is answered
# (cpp/kvevaluator/src/store/round_trips.h); a client keeps at most P requests in flight, so a level of k requests is
# ceil(k / P) waits. Latency = RTT x waits + bytes / bandwidth; the local work is not in it (it is the store's).
REMOTE_CLIENTS = {                      # name -> (B keys per GET batch, P requests in flight)
    "naive": (1, 1),                    # every key-value operation is a request, each waited for
    "dynamodb-like": (100, 16),         # BatchGetItem's 100 keys; a client's 16 in flight
    "tikv-like": (math.inf, 15),        # a batch of any size; 15 in flight
    "ideal": (math.inf, math.inf),      # a level is one wait (round_trips_critical)
}
REMOTE_RTT_S = {"0.28ms": 0.28e-3,      # measured: two machines on one 100 Gb/s private link (20 pings)
                "0.5ms": 0.5e-3, "1ms": 1e-3,
                "30ms": 30e-3}          # S3's median base latency (Durner, Leis, Neumann, PVLDB 16(11) 2023, Sec. 2.3)
REMOTE_BANDWIDTH_BPS = {"100Gb": 100e9 / 8, "10Gb": 10e9 / 8}     # bytes per second
# assumed, not measured here (a x0.5-x2 band is shown beside them): server CPU per request, an optimized RPC stack's
# ~10 million small RPCs per second per core (eRPC: Kalia, Kaminsky, Andersen, NSDI 2019); server CPU per wops unit
# (one GET), RocksDB's ~20 us per point query reading ~4 KB from storage (M. Callaghan, smalldatum blog, 2023-02)
REMOTE_C_MSG_S = 0.1e-6
REMOTE_C_WOPS_S = 20e-6
# assumed, with no source and not yet measured: server CPU per byte returned, the copy, checksum and send of a
# value beyond the block a wops unit already pays for, at ~2 GB/s per core. Without it a 256 KB chunk costs one GET
REMOTE_C_BYTE_S = 0.5e-9
REMOTE_SERVER_CORES = 32                # one server: 32 cores (the size of the machines the counts ran on)


def remote_level_requests(gets, ranges, B):
    """the requests of one level: its GETs in batches of B, each range read or page its own"""
    return (max(1, math.ceil(gets / B)) if gets else 0) + ranges     # B = inf: one batch


def remote_waits(levels, writes, B, P):
    """(requests, waits) of one job: levels = [(gets, ranges)] in order, writes = the job's write keys (one batch of up
    to B per request, sent after its reads)"""
    req = waits = 0
    for gets, ranges in levels:
        k = remote_level_requests(gets, ranges, B)
        req += k
        waits += max(1, math.ceil(k / P)) if k else 0                # P = inf: one wait
    if writes:
        k = max(1, math.ceil(writes / B))
        req += k
        waits += max(1, math.ceil(k / P))
    return req, waits


def remote_ends(S, G, pages, round_trips, critical, writes=0):
    """{client: (requests, waits)} of the two ends a step's three round-trip columns give without its levels: naive
    (B = P = 1: every SEEK, GET, page and written key (PUT + DEL) a request, each waited) and ideal (B = P = inf:
    round_trips requests, one wait per level, round_trips_critical)"""
    n = S + G + pages + writes
    return {"naive": (n, n), "ideal": (round_trips, critical)}


def remote_latency(waits, nbytes, rtt_s, bandwidth_bps):
    """seconds: RTT x waits + bytes / bandwidth (the network part only)"""
    return rtt_s * waits + nbytes / bandwidth_bps


def remote_throughput(requests, wops, nbytes, latency_s, bandwidth_bps, c_msg_s=REMOTE_C_MSG_S, c_wops_s=REMOTE_C_WOPS_S,
                      c_byte_s=REMOTE_C_BYTE_S, cores=REMOTE_SERVER_CORES):
    """{bound: queries per second} of one query run (or a workload's mean run): server (`cores` cores, each spending
    requests x c_msg + wops x c_wops + bytes x c_byte of CPU per run), link (bandwidth / bytes), client (per query in
    flight: 1 / latency); and their min"""
    inf = math.inf
    cpu = requests * c_msg_s + wops * c_wops_s + nbytes * c_byte_s
    out = {"server": cores / cpu if cpu else inf,
           "link": bandwidth_bps / nbytes if nbytes else inf,
           "client": 1 / latency_s if latency_s else inf}
    out["min"] = min(out.values())
    return out


def remote_jobs(text):
    """an rt_levels text (a step row's; cpp/kvevaluator/src/store/round_trips.h RtText) as [(levels, writes, n)]: a job's
    levels [(gets, ranges, repeat)] in order, its write keys, and how many equal jobs in a row. "" -> [] (no request);
    None (an old capture's empty cell read as missing) is refused: not measured is not zero"""
    if text is None:
        raise ValueError("rt_levels: not measured (a capture without the column): re-count, never read as 0")
    jobs = []
    for part in text.split("|") if text else []:
        body, _, n = part.partition("*")
        levels, writes = [], 0
        for tok in body.split():
            if tok[0] == "w":
                writes += int(tok[1:])
                continue
            lvl, x, rep = tok.partition("x")
            gets, ranges = (int(v) for v in lvl.split(":"))
            if (x and not rep) or not (gets or ranges):
                raise ValueError(f"rt_levels: a malformed level {tok!r} in {text!r}")
            levels.append((gets, ranges, int(rep or 1)))
        jobs.append((levels, writes, int(n or 1)))
    return jobs


def remote_waits_of_text(text, B, P, round_trips):
    """(requests, waits) of one step row's rt_levels text for a client (B, P): each job by remote_waits, times its
    repeat; B = P = inf gives (round_trips, round_trips_critical). `round_trips` is the row's own column: an empty text
    beside a nonzero one is a capture from before the column (not measured), refused, never costed as 0"""
    if text in ("", None) and float(round_trips or 0):
        raise ValueError(f"rt_levels: not measured on a step with {round_trips} round trips (a capture without "
                         "the column): re-count, never read as 0")
    req = waits = 0
    for levels, writes, n in remote_jobs(text):
        r = w = 0
        for gets, ranges, rep in levels:
            r1, w1 = remote_waits([(gets, ranges)], 0, B, P)
            r, w = r + rep * r1, w + rep * w1
        if writes:
            r1, w1 = remote_waits([], writes, B, P)
            r, w = r + r1, w + w1
        req, waits = req + n * r, waits + n * w
    return req, waits
