#!/usr/bin/env python3
"""The remote-store model (cost_models, section "The remote store") on a hand-counted job, and its ends against the
step columns' meaning (cpp/kvevaluator/src/store/round_trips.h: round_trips = per level 1 for its GETs + 1 per range
read or page, + 1 for the job's writes; critical = the levels waited through, + 1 for the writes).

A job: level 1 has 250 GETs, level 2 three range reads, level 3 five GETs and one range read; then 7 write keys.

    python3 tests/unit/check_remote_store_model.py
"""
import math
import sys

from kvcost.layout_cost import cost_models as CM

LEVELS, WRITES = [(250, 0), (0, 3), (5, 1)], 7
S, G, PAGES = 4, 255, 0                 # the same job's step columns: 3 + 1 SEEKs, 250 + 5 GETs, no extra pages
ROUND_TRIPS, CRITICAL = 7, 4            # round_trips.h: 1 + 3 + (1 + 1) + 1 writes; 3 levels + 1 write wait


def main():
    want = {"naive": (266, 266),        # 250 + 3 + 6 requests + 7 single writes, each waited
            "dynamodb-like": (9, 4),    # 3 batches of 100 | 3 | 1 + 1 | 1 batch of writes; each level within 16
            "tikv-like": (7, 4),
            "ideal": (7, 4)}
    bad = 0
    for name, (B, P) in CM.REMOTE_CLIENTS.items():
        got = CM.remote_waits(LEVELS, WRITES, B, P)
        if got != want[name]:
            print(f"FAIL {name}: (requests, waits) {got}, want {want[name]}")
            bad += 1
    ends = CM.remote_ends(S, G, PAGES, ROUND_TRIPS, CRITICAL, WRITES)
    for name, (B, P) in (("ideal", (math.inf, math.inf)), ("naive", (1, 1))):   # the ends from the columns = the levels'
        if ends[name] != CM.remote_waits(LEVELS, WRITES, B, P):
            print(f"FAIL {name} end {ends[name]} != the levels' {CM.remote_waits(LEVELS, WRITES, B, P)}")
            bad += 1
    # a capped client: 250 GETs at B = 1 are 250 requests, 16 in flight: 16 waits on that level
    if CM.remote_waits([(250, 0)], 0, 1, 16) != (250, 16):
        print("FAIL capped level")
        bad += 1
    lat = CM.remote_latency(4, 1.25e9, 1e-3, 1.25e10)                 # 4 ms of waits + 0.1 s of bytes
    if not math.isclose(lat, 0.104):
        print(f"FAIL latency {lat}")
        bad += 1
    t = CM.remote_throughput(10, 50, 1e6, 0.01, 1.25e10, cores=2)
    if not (math.isclose(t["server"], 2 / (10 * CM.REMOTE_C_MSG_S + 50 * CM.REMOTE_C_WOPS_S + 1e6 * CM.REMOTE_C_BYTE_S))
            and math.isclose(t["link"], 12500) and math.isclose(t["client"], 100) and t["min"] == t["client"]):
        print(f"FAIL throughput {t}")
        bad += 1
    t = "250:0 0:3 5:1 w7|0:2x3*2"                                 # job 1 as above; job 2: 3 levels of 2 ranges, twice
    if CM.remote_waits_of_text(t, math.inf, math.inf, 19) != (19, 10) or CM.remote_waits_of_text(t, 1, 1, 19) != (278, 278):
        print(f"FAIL text cost {CM.remote_waits_of_text(t, math.inf, math.inf, 19)}")
        bad += 1
    # the clients between the ends, which the ends cannot see (ceil against floor, folds, writes alone, P not dividing)
    for text, B, P, want_rw in (("250:3x2 w150|5:0*3", 100, 2, (17, 10)),   # (3 + 3) x 2 + 2 writes + 3; 3 x 2 + 1 + 3
                                ("w250", 100, 16, (3, 1)),                    # a job of writes only
                                ("w250|w250*3", 100, 16, (12, 4)),            # writes-only jobs, folded
                                ("17:0", 1, 16, (17, 2)),                     # 17 requests, 16 in flight
                                ("0:33", 100, 16, (33, 3))):                  # range reads take no batch
        if CM.remote_waits_of_text(text, B, P, 1) != want_rw:
            print(f"FAIL {text!r} at B {B}, P {P}: {CM.remote_waits_of_text(text, B, P, 1)}, want {want_rw}")
            bad += 1
    refused = 0
    for text, rt in (("", 12), ("0:0", 1), ("1:2x", 1)):           # not measured beside round trips; malformed
        try:
            CM.remote_waits_of_text(text, 1, 1, rt)
        except ValueError:
            refused += 1
    if refused != 3 or CM.remote_waits_of_text("", 1, 1, 0) != (0, 0):
        print(f"FAIL refusals: {refused} of 3 refused, or an empty text on a step with no round trip not (0, 0)")
        bad += 1
    print(f"check_remote_store_model: {len(CM.REMOTE_CLIENTS)} clients + 12 checks, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
