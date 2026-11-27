"""I01: LDBC SNB Interactive's own operation mix (third-party frequencies), and I02: its reads only; in the ldbc suite.
Their files (inputs/workloads/ldbc/i01.json, i02.json) are written by make_workloads.py, whose recipe reads inputs/frequencies/ldbc/sf3/mix.json (python/kvevaluator/workloads/interactive/gen_ldbc_mix.py, from LDBC's driver config and update stream):
complex reads IC1-IC12 at the driver's prescribed sf3 frequencies (one run per freq updates), short reads IS1-IS7
at the driver's random-walk rate (~1.255 runs of each per complex read, IC13/IC14's walks included), updates IU1-IU8 at their share of the sf3
update stream. Counts are per 100,000 updates. The sf3 prescription is used at any analysis SF (sf0.1's file sets
every frequency to 1 -- a test-coverage config, not a mix); the scale is the recipe's argument (make_workloads.i01). IC13/IC14 are out of scope (not in our plans).
"""
LDBC, W = "interactive", "writes"
PER = 100_000          # counts per this many updates
# The driver paces reads by time: interleave = freq x update_interleave (1542 ms in sf3.properties), while the shipped
# sf3 update stream's mean gap is ~954 ms (9,469,437,602 ms / 9,925,582 events), so a real run issues ~954 / 1542 =
# 0.62x the complex reads per update that the frequencies state.
DRIVER_READ_FACTOR = 954 / 1542

QUERIES = {
    **{f"ic{i}": f"LDBC complex read IC{i}" for i in range(1, 13)},
    **{f"is{i}": f"LDBC short read IS{i}" for i in range(1, 8)},
    "iu1": "add a Person", "iu3": "add a like of a Comment", "iu4": "add a Forum", "iu5": "add a Forum membership",
    "iu8": "add a friendship",   # iu2, iu6, iu7: described in designed.py (W01-W15 use them)
}
PARAMS = {LDBC: "the grid's 5 params per query (LDBC's own substitution parameters)"}

